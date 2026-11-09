"""Coordinator: poll, reserve, generate, revalidate and publish without automatic retries."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .content import ContentError, generate_content
from .images import ImageError, ImageGenerator, validate_image
from .life import Activity, ActivityWatch, LIFE_API, LifeError, interpret
from .publisher import PublishError, Publisher, PublishUncertain
from .store import ClaimError, Store


class ServiceError(Exception):
    """Operator-facing, redacted service error."""


CLAIM_MESSAGES = {
    "duplicate": "当前活动已处理或已有草稿，不会重复生成",
    "busy": "其他任务正在生成或提交，本次不排队",
    "cooldown": "仍在生成冷却期，本次不补发",
    "daily_limit": "今日生成尝试额度已用完，本次不补发",
    "not_found": "任务不存在，请先执行 /daily status 查看任务ID",
    "activity_changed": "任务不属于当前活动或活动已结束，不能重试",
    "not_retryable": "该任务不是可重试的生成失败，或已经提交过空间；不会重新生成或重发",
}


class DailyService:
    def __init__(self, ctx: Any, settings: Settings, data_dir: Path, *,
                 images: Any = None, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.ctx, self.settings = ctx, settings
        self.clock, self.monotonic = clock, monotonic
        self.data_dir = Path(data_dir)
        self.cache_dir = self.data_dir / "images"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir / "daily_life.sqlite3")
        self.images = images if images is not None else ImageGenerator(settings.image)
        self.publisher = Publisher(ctx, settings.publish)
        self.watch = ActivityWatch()
        self.tasks: set[asyncio.Task] = set()
        self.busy_tasks: set[asyncio.Task] = set()
        self.last_error = ""
        self.last_delivery_error = ""
        self.current_title = "尚未读取"
        self.closed = False
        self.last_cleanup = 0.0
        self.lease_seconds = settings.text.timeout_seconds + settings.image.timeout_seconds + settings.publish.timeout_seconds + 120

    def day(self) -> str:
        return datetime.fromtimestamp(self.clock(), ZoneInfo(self.settings.trigger.timezone)).date().isoformat()

    def problems(self) -> list[str]:
        errors = []
        if not self.settings.plugin.enabled:
            errors.append("插件主开关已关闭")
        if not self.settings.plugin.admin_qqs:
            errors.append("未配置管理员 QQ 白名单")
        if not self.settings.character.appearance.strip():
            errors.append("未配置固定角色外貌")
        errors.extend(self.images.problems())
        return errors

    async def read_activity(self) -> Activity | None:
        try:
            async with asyncio.timeout(15):
                result = await self.ctx.api.call(LIFE_API, version="1")
        except Exception as exc:
            raise LifeError("MaiWeave 状态 API 不可用或超时") from exc
        return interpret(result, self.clock())

    def spawn(self, coroutine: Any, *, busy: bool = True, job_id: str | None = None, job_attempt: int = 0) -> asyncio.Task:
        task = asyncio.create_task(coroutine)

        def completed(done: asyncio.Task) -> None:
            if done.cancelled() and job_id:
                # A Task cancelled before its first step cannot run generate()'s finally/error path.
                with contextlib.suppress(Exception):
                    self.store.update(job_id, "generating", self.clock(), expected_retry_count=job_attempt,
                                      state="failed", error="cancelled_no_retry")
            if not done.cancelled() and done.exception() is not None:
                self.report("后台任务发生内部错误；请检查最近任务状态，不会自动重试")

        task.add_done_callback(completed)
        self.tasks.add(task)
        if busy:
            self.busy_tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(self.busy_tasks.discard)
        return task

    async def run(self) -> None:
        while not self.closed:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.report("后台检查发生内部错误；本轮已跳过")
            await asyncio.sleep(self.settings.trigger.poll_seconds)

    def report(self, message: str) -> None:
        # No exception strings/tracebacks, raw state, prompts or base64 in logs.
        if message != self.last_error:
            self.ctx.logger.warning("Daily Life: %s", message)
        self.last_error = message

    def maintain(self) -> None:
        now = self.clock()
        self.store.recover(now, self.lease_seconds)
        self.store.expire_drafts(now, self.settings.publish.draft_ttl_seconds)
        if now - self.last_cleanup < 3600:
            return
        self.last_cleanup = now
        self.store.prune(now, self.settings.storage.log_retention_days * 86400)
        cutoff = now - self.settings.storage.image_retention_days * 86400
        for path in self.cache_dir.glob("*.img"):
            # Only our UUID-named files, never paths supplied by state or a model.
            if len(path.stem) != 32 or any(c not in "0123456789abcdef" for c in path.stem):
                continue
            job = self.store.get(path.stem)
            if job and job["state"] in {"generating", "submitting", "draft", "uncertain"}:
                continue
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()

    async def tick(self) -> None:
        self.maintain()
        if self.closed or not self.settings.plugin.enabled or not self.settings.trigger.auto_enabled:
            self.watch.reset()
            return
        try:
            activity = await self.read_activity()
        except LifeError as exc:
            self.watch.gap()
            self.report(str(exc))
            return
        self.current_title = activity.snapshot["title"] if activity else "空闲、睡眠或引擎关闭"
        first_observation = not self.watch.initialized
        new_activity = bool(activity and self.watch.initialized and activity.key != self.watch.key)
        due = self.watch.observe(activity, self.monotonic(), self.settings.trigger.stable_seconds)
        if activity and first_observation:
            self.store.remember_auto(activity.key)
        elif new_activity and self.store.auto_seen(activity.key):
            self.watch.consume()
            return
        if new_activity:
            reason = self.store.block_reason(self.clock(), self.day(), self.settings.trigger.daily_limit,
                                             self.settings.trigger.cooldown_seconds)
            if reason:
                self.watch.consume()
                self.store.skip(activity.key, activity.snapshot, self.clock(),
                                "busy" if reason == "busy" else CLAIM_MESSAGES[reason])
                return
        # A change observed while busy is consumed immediately, not queued until stability/cooldown clears.
        if activity and self.busy_tasks:
            self.watch.consume()
            self.store.skip(activity.key, activity.snapshot, self.clock(), "busy")
            return
        if not due or activity is None:
            return
        self.watch.consume()
        self.store.remember_auto(activity.key)
        try:
            job = await self.reserve(activity, "auto")
        except ServiceError as exc:
            self.store.skip(activity.key, activity.snapshot, self.clock(), str(exc))
            self.report(str(exc))
            return
        self.spawn(self.generate(job, auto=True), job_id=job["id"])

    async def reserve(self, activity: Activity, mode: str) -> dict:
        if self.closed:
            raise ServiceError("插件正在停止")
        problems = self.problems()
        if problems:
            raise ServiceError("；".join(problems))
        try:
            await self.publisher.check()
            return self.store.reserve(
                activity.key, activity.snapshot, self.clock(), self.day(),
                self.settings.trigger.daily_limit, self.settings.trigger.cooldown_seconds,
                mode=mode, lease_seconds=self.lease_seconds, ignore_limits=mode in {"preview", "post"},
            )
        except ClaimError as exc:
            if exc.reason == "duplicate":
                existing = self.store.for_activity(activity.key)
                if existing:
                    job_id, state = existing["id"], existing["state"]
                    if state == "failed" and not existing.get("submission_started") and not existing.get("image_path"):
                        raise ServiceError(f"本活动上次生成失败（任务 {job_id}）；修正错误后用 /daily retry {job_id} 显式重试；手动命令不受本插件冷却和每日次数限制") from exc
                    if state == "draft":
                        raise ServiceError(f"本活动已有草稿 {job_id}，请用 /daily publish {job_id}，不会重新生成") from exc
                    if state in {"submitting", "uncertain"}:
                        raise ServiceError(f"任务 {job_id} 正在提交或发布结果不明，请先检查真实空间，不会重发") from exc
            raise ServiceError(CLAIM_MESSAGES.get(exc.reason, "任务未取得执行资格")) from exc
        except PublishError as exc:
            raise ServiceError(str(exc)) from exc

    async def preview(self, stream_id: str) -> None:
        await self.create_current(stream_id, publish=False)

    async def post(self, stream_id: str) -> None:
        """Explicit administrator request: create a current-activity draft and publish it once."""
        await self.create_current(stream_id, publish=True)

    async def create_current(self, stream_id: str, *, publish: bool) -> None:
        try:
            activity = await self.read_activity()
            if activity is None:
                raise ServiceError("当前为空闲、睡眠或引擎关闭，不能生成草稿")
            job = await self.reserve(activity, "post" if publish else "preview")
            await self.generate(job, auto=publish, stream_id=stream_id)
        except (LifeError, ServiceError) as exc:
            self.report(str(exc))
            await self.notify(stream_id, str(exc))

    async def retry(self, job_id: str, stream_id: str) -> None:
        """Retry a failed, never-submitted generation only on an explicit admin command."""
        try:
            if self.closed:
                raise ServiceError("插件正在停止")
            problems = self.problems()
            if problems:
                raise ServiceError("；".join(problems))
            activity = await self.read_activity()
            if activity is None:
                raise ServiceError("当前为空闲、睡眠或引擎关闭，不能重试生成")
            await self.publisher.check()
            job = self.store.retry(job_id, activity.key, activity.snapshot, self.clock(), self.day(),
                                   self.settings.trigger.daily_limit, self.settings.trigger.cooldown_seconds,
                                   lease_seconds=self.lease_seconds, ignore_limits=True)
            await self.generate(job, auto=True, stream_id=stream_id)
        except (LifeError, ServiceError, PublishError, ClaimError) as exc:
            message = CLAIM_MESSAGES.get(exc.reason, "任务未取得重试资格") if isinstance(exc, ClaimError) else str(exc)
            self.report(message)
            await self.notify(stream_id, message)

    async def notify(self, stream_id: str, text: str) -> None:
        if not stream_id:
            return
        try:
            async with asyncio.timeout(15):
                result = await self.ctx.send.text(text, stream_id, return_details=True,
                                                  storage_message=False, sync_to_maisaka_history=False)
            if result is False or (isinstance(result, dict) and result.get("sent") is False):
                self.delivery_failure()
            elif result is True or (isinstance(result, dict) and result.get("sent") is True):
                self.last_delivery_error = ""
        except Exception:
            self.delivery_failure()

    def delivery_failure(self) -> None:
        message = "管理员通知未送达（可能被防循环/冷却 Hook 拦截）；不会自动补发，请私聊或等待冷却后执行 /daily status"
        if message != self.last_delivery_error:
            self.ctx.logger.warning("Daily Life: %s", message)
        self.last_delivery_error = message

    def image_path(self, job_id: str) -> Path:
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ServiceError("草稿 ID 无效")
        return self.cache_dir / (job_id + ".img")

    async def still_current(self, job: dict) -> bool:
        if self.closed or not self.settings.plugin.enabled:
            return False
        if self.clock() - job["created_at"] >= self.settings.publish.draft_ttl_seconds:
            return False
        activity = await self.read_activity()
        return activity is not None and activity.key == job["activity_key"]

    async def generate(self, job: dict, *, auto: bool, stream_id: str = "") -> None:
        job_id = job["id"]
        try:
            caption, prompt = await generate_content(self.ctx, self.settings, job["snapshot"])
            if not self.store.renew(job_id, self.clock(), self.lease_seconds, expected_retry_count=job.get("retry_count", 0)):
                return
            if not await self.still_current(job):
                self.store.update(job_id, "generating", self.clock(), expected_retry_count=job.get("retry_count", 0), state="skipped", error="activity_changed_or_expired")
                await self.notify(stream_id, "活动已变化或草稿过期，已取消绘图")
                return
            if not self.store.renew(job_id, self.clock(), self.lease_seconds, expected_retry_count=job.get("retry_count", 0)):
                return
            image = await self.images.generate(prompt)
            image = validate_image(image, self.settings.image.max_image_bytes, self.settings.image.max_pixels)
            if not self.store.renew(job_id, self.clock(), self.lease_seconds, expected_retry_count=job.get("retry_count", 0)):
                return
            if not await self.still_current(job):
                self.store.update(job_id, "generating", self.clock(), expected_retry_count=job.get("retry_count", 0), state="skipped", error="activity_changed_or_expired")
                await self.notify(stream_id, "活动已变化或结束，已丢弃生成结果")
                return
            if not self.store.renew(job_id, self.clock(), self.lease_seconds, expected_retry_count=job.get("retry_count", 0)):
                return
            path = self.image_path(job_id)
            path.write_bytes(image)
            if not self.store.update(job_id, "generating", self.clock(), expected_retry_count=job.get("retry_count", 0), state="draft", caption=caption, image_path=path.name):
                return
            if auto:
                await self.publish_draft(job_id, stream_id)
            else:
                await self.notify(stream_id, f"草稿 {job_id}（尚未发布）\n{caption}\n确认发布：/daily publish {job_id}")
                try:
                    async with asyncio.timeout(30):
                        await self.ctx.send.image(base64.b64encode(image).decode("ascii"), stream_id,
                                                  storage_message=False, sync_to_maisaka_history=False)
                except Exception:
                    await self.notify(stream_id, "草稿已保存，但预览图片发送失败；不会自动发布")
        except asyncio.CancelledError:
            self.store.update(job_id, "generating", self.clock(), expected_retry_count=job.get("retry_count", 0),
                              state="failed", error="cancelled_no_retry")
            raise
        except (ContentError, ImageError, LifeError, ServiceError) as exc:
            if not self.store.update(job_id, "generating", self.clock(), expected_retry_count=job.get("retry_count", 0),
                                     state="failed", error=str(exc)):
                return
            self.report(str(exc))
            await self.notify(stream_id, f"任务 {job_id} 生成失败：{exc}；修正后可用 /daily retry {job_id}（手动命令不限冷却和次数，每次尝试可能收费）")
        except Exception:
            message = "生成任务内部错误；不会自动重试"
            if not self.store.update(job_id, "generating", self.clock(), expected_retry_count=job.get("retry_count", 0),
                                     state="failed", error=message):
                return
            self.report(message)
            await self.notify(stream_id, message)

    async def publish_draft(self, job_id: str, stream_id: str = "") -> None:
        job = self.store.get(job_id)
        if not job or job["state"] != "draft":
            await self.notify(stream_id, "草稿不存在、已处理或发布结果不明；不会重复发布")
            return
        claimed = False
        try:
            if self.closed or not self.settings.plugin.enabled:
                raise ServiceError("插件已关闭，不会发布")
            if not await self.still_current(job):
                self.store.update(job_id, "draft", self.clock(), state="skipped", error="activity_changed_or_expired")
                raise ServiceError("草稿已过期或活动已变化，拒绝发布")
            await self.publisher.check()
            path = self.image_path(job_id)
            image = validate_image(path.read_bytes(), self.settings.image.max_image_bytes, self.settings.image.max_pixels)
            # Recheck after preflight network I/O, immediately before atomic submission claim.
            if not await self.still_current(job):
                self.store.update(job_id, "draft", self.clock(), state="skipped", error="activity_changed_or_expired")
                raise ServiceError("活动已变化或结束，拒绝发布")
            if not self.store.begin_submission(job_id, self.clock(), self.lease_seconds):
                raise ServiceError("草稿正在提交或其他任务执行中，本次不排队")
            claimed = True
            tid = await self.publisher.publish(job["caption"], image)
            try:
                saved = self.store.confirm_submission(job_id, tid, self.clock())
            except Exception:
                saved = False
            if saved:
                await self.notify(stream_id, f"已发布到 QQ 空间，tid={tid}")
            else:
                message = f"空间返回成功 tid={tid}，但本地记录保存失败；请人工核对，不要重发"
                self.report(message)
                await self.notify(stream_id, message)
        except asyncio.CancelledError:
            if claimed:
                self.store.update(job_id, "submitting", self.clock(), state="uncertain", error="cancelled_during_submission")
            raise
        except PublishUncertain as exc:
            self.store.update(job_id, "submitting", self.clock(), state="uncertain", error=str(exc))
            self.report(str(exc))
            await self.notify(stream_id, str(exc))
        except (PublishError, ServiceError, LifeError, ImageError, OSError) as exc:
            message = str(exc) if isinstance(exc, (PublishError, ServiceError, LifeError, ImageError)) else "草稿图片读取失败"
            if claimed:
                self.store.update(job_id, "submitting", self.clock(), state="failed", error=message)
            self.report(message)
            await self.notify(stream_id, message)
        except Exception:
            message = "发布内部错误，结果不明；请检查真实空间" if claimed else "草稿发布前检查失败，未提交"
            if claimed:
                self.store.update(job_id, "submitting", self.clock(), state="uncertain", error=message)
            self.report(message)
            await self.notify(stream_id, message)

    async def status(self) -> str:
        issues = self.problems()
        try:
            activity = await self.read_activity()
            self.current_title = activity.snapshot["title"] if activity else "空闲、睡眠或引擎关闭"
        except LifeError as exc:
            issues.append(str(exc))
        try:
            await self.publisher.check()
        except PublishError as exc:
            issues.append(str(exc))
        recent = self.store.latest(5)
        lines = [
            "Daily Life 状态", "自动发布：" + ("开启" if self.settings.trigger.auto_enabled else "关闭"),
            "可见性：" + {1: "公开", 4: "好友", 64: "仅自己"}[self.settings.publish.visibility],
            "当前：" + self.current_title,
            f"今日生成尝试记录：{self.store.attempts(self.day())}；自动任务上限：{self.settings.trigger.daily_limit}（手动命令不限）",
            "检查：" + ("；".join(issues) if issues else "已通过只读依赖检查，尚不代表真实发帖成功"),
        ]
        if self.last_error:
            lines.append("最近提示：" + self.last_error)
        if self.last_delivery_error:
            lines.append("通知发送：" + self.last_delivery_error)
        for job in recent:
            lines.append(f"{job['id']} {job['state']}" + (f" tid={job['tid']}" if job.get("tid") else "")
                         + (f" 提示={job['error']}" if job.get("error") else "")
                         + (f" 手动重试次数={job['retry_count']}" if job.get("retry_count") else ""))
        return "\n".join(lines)

    async def close(self) -> None:
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.images.close()
        self.store.close()
