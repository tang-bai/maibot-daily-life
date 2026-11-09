"""MaiBot SDK entry point. All slow work runs in tracked, cancellable tasks."""
from __future__ import annotations

import asyncio
import contextlib
import re

from maibot_sdk import Command, MaiBotPlugin

if __package__:
    from .daily_life.config import Settings
    from .daily_life.migrate import migrate_legacy_data
    from .daily_life.service import DailyService
else:  # Direct imports used by local tests; Runner loads a synthetic package.
    from daily_life.config import Settings
    from daily_life.migrate import migrate_legacy_data
    from daily_life.service import DailyService


class DailyLifePlugin(MaiBotPlugin):
    config_model = Settings

    def __init__(self) -> None:
        super().__init__()
        self.service: DailyService | None = None
        self.poll_task: asyncio.Task | None = None
        self.lifecycle_lock = asyncio.Lock()

    async def on_load(self) -> None:
        async with self.lifecycle_lock:
            await self._start()

    async def _start(self) -> None:
        settings = Settings.model_validate(self.config.model_dump())
        data_dir = self.ctx.paths.data_dir
        if migrate_legacy_data(data_dir):
            self.ctx.logger.info("Daily Life 已从旧插件 ID 数据目录复制任务记录与草稿图片")
        self.service = DailyService(self.ctx, settings, data_dir)
        self.poll_task = asyncio.create_task(self.service.run())
        self.ctx.logger.info("Daily Life 已加载；自动发布：%s", settings.trigger.auto_enabled)

    async def _stop(self) -> None:
        service, task = self.service, self.poll_task
        self.service, self.poll_task = None, None
        if service:
            service.closed = True
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if service:
            await service.close()

    async def on_unload(self) -> None:
        async with self.lifecycle_lock:
            await self._stop()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        # Runner applies the strongly typed plugin configuration before this callback.
        # Restart the coordinator, never mutate settings of an in-flight paid request.
        del config_data, version
        if scope not in {"self", "plugin"}:
            return
        async with self.lifecycle_lock:
            await self._stop()
            await self._start()

    @Command("daily_life", description="管理员：日常空间状态、预览、一键生成发布及发布已有草稿",
             pattern=r"^/daily(?:\s+(?P<action>status|preview|post|publish|retry)(?:\s+(?P<draft>[a-f0-9]{32}))?)?\s*$",
             intercept_message_level=1)
    async def handle_daily(self, text: str = "", stream_id: str = "", platform: str = "",
                           user_id: str = "", **kwargs):
        result = await self._handle_daily(text, stream_id, platform, user_id, **kwargs)
        # Accepted asynchronous work sends only its final result. A preliminary
        # acknowledgement followed immediately by an error trips group loop guards.
        if not result[0] and self.service is not None and stream_id and result[1]:
            await self.service.notify(stream_id, result[1])
        return result

    async def _handle_daily(self, text: str, stream_id: str, platform: str, user_id: str, **kwargs):
        del kwargs
        service = self.service
        if service is None:
            return False, "插件正在加载或停止", True
        # Host provides these fields from trusted message metadata, not command captures.
        if platform.casefold() != "qq" or str(user_id) not in service.settings.plugin.admin_qqs:
            return False, "仅管理员 QQ 白名单可执行此命令", True
        match = re.fullmatch(r"/daily(?:\s+(status|preview|post|publish|retry)(?:\s+([a-f0-9]{32}))?)?\s*", text)
        if not match or not stream_id:
            return False, "用法：/daily status | /daily preview | /daily post | /daily publish <草稿ID> | /daily retry <任务ID>", True
        action, draft_id = match.groups()
        if action in (None, "status"):
            if draft_id:
                return False, "status 不接受草稿ID", True
            service.spawn(self._send_status(service, stream_id), busy=False)
            return True, "正在检查，只读检查不会生成图片或发空间", True
        if not service.settings.plugin.enabled:
            return False, "插件主开关已关闭", True
        if action == "preview" and draft_id is None:
            service.spawn(service.preview(stream_id))
            return True, "预览请求已收到；手动命令不受本插件冷却和每日次数限制，不会发空间，每次生成可能收费", True
        if action == "post" and draft_id is None:
            service.spawn(service.post(stream_id))
            return True, "已收到生成并发布请求；手动命令不限本插件冷却和每日次数，仍检查活动、配置与防重状态，最终以任务结果为准", True
        if action == "retry" and draft_id:
            service.spawn(service.retry(draft_id, stream_id))
            return True, "已收到重试请求；仅重试尚未提交空间的生成失败任务，不限本插件冷却和每日次数，可能再次计费", True
        if action == "publish" and draft_id:
            service.spawn(service.publish_draft(draft_id, stream_id))
            return True, "草稿发布请求已受理；最终结果以回执为准", True
        return False, "用法：/daily status | /daily preview | /daily post | /daily publish <草稿ID> | /daily retry <任务ID>", True

    async def _send_status(self, service: DailyService, stream_id: str) -> None:
        try:
            await service.notify(stream_id, await service.status())
        except asyncio.CancelledError:
            raise
        except Exception:
            await service.notify(stream_id, "状态检查失败；请检查插件依赖和本地数据目录")


def create_plugin() -> DailyLifePlugin:
    return DailyLifePlugin()
