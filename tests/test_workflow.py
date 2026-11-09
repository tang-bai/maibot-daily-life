"""Offline workflow regressions using real SQLite and real decoded PNG data.

No SDK runtime, model provider, HTTP request, account, or wall-clock wait is used.
Run: .venv/Scripts/python.exe -m unittest tests.test_workflow -v
"""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from daily_life.config import Settings
from daily_life.images import ImageError, ImageGenerator, validate_image
from daily_life.life import LIFE_API
from daily_life.publisher import QZONE_API, VERSION_API
from daily_life.service import DailyService


CAPTION = "我正捧着温热的茶，慢慢翻看手边的书页，窗边柔和的光落在桌面上，这样安静的小片刻让心情也轻松起来。"
SCENE = "窗边的虚拟角色坐在木桌旁翻书，桌面放着茶杯，暖色自然光，无文字与水印。"
SECRET = "workflow-secret-token-must-never-escape"


class ControlledClock:
    def __init__(self):
        self.wall = datetime(2026, 11, 8, 4, tzinfo=timezone.utc).timestamp()
        self.mono = 1000.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds, *, wall=True):
        self.mono += seconds
        if wall:
            self.wall += seconds


def life_status(clock, activity_id="baseline", **overrides):
    now = datetime.fromtimestamp(clock.wall, timezone.utc)
    status = {
        "version": 1, "ready": True, "enabled": True, "sleeping": False,
        "mood": 0, "energy": 50, "status": "正在活动",
        "location": "PRIVATE_LOCATION", "next_plan": "PRIVATE_NEXT_PLAN",
        "current": {
            "id": activity_id, "kind": "study", "title": "窗边读书",
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(days=7)).isoformat(),
        },
    }
    status.update(overrides)
    return status


class Gate:
    """Deterministic suspension point; never uses a real time delay."""
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def pause(self):
        self.entered.set()
        await self.release.wait()

    async def wait(self):
        await asyncio.wait_for(self.entered.wait(), timeout=3)


class FakeAPI:
    def __init__(self, status):
        self.status = status
        self.calls = []
        self.gets = []
        self.errors = {}
        self.registered = True
        self.version_response = {
            "status": "ok", "retcode": 0, "data": {"app_name": "SnowLuma"},
        }
        self.publish_response = {"status": "ok", "retcode": 0, "data": {"tid": "offline-tid"}}
        self.publish_gate = None
        self.version_hook = None

    async def get(self, name, *, version):
        self.gets.append((name, version))
        if "get" in self.errors:
            raise self.errors["get"]
        return {"name": name} if self.registered else None

    async def call(self, name, *, version, params=None):
        self.calls.append((name, version, deepcopy(params)))
        if name in self.errors:
            raise self.errors[name]
        if name == LIFE_API:
            return deepcopy(self.status)
        if name == VERSION_API:
            if self.version_hook:
                self.version_hook()
            return deepcopy(self.version_response)
        if name == QZONE_API:
            if self.publish_gate:
                await self.publish_gate.pause()
            return deepcopy(self.publish_response)
        raise AssertionError("Unexpected API call: " + name)

    @property
    def submissions(self):
        return [params for name, _, params in self.calls if name == QZONE_API]


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.gate = None
        self.error = None
        self.result = {"success": True, "response": json.dumps(
            {"caption": CAPTION, "image_prompt": SCENE}, ensure_ascii=False)}

    async def generate(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if self.gate:
            await self.gate.pause()
        if self.error:
            raise self.error
        return deepcopy(self.result)


class FakeImages:
    def __init__(self):
        output = io.BytesIO()
        Image.new("RGB", (8, 8), (120, 180, 220)).save(output, format="PNG")
        self.png = output.getvalue()
        self.calls = []
        self.gate = None
        self.closed = False

    def problems(self):
        return []

    async def generate(self, prompt):
        self.calls.append(prompt)
        if self.gate:
            await self.gate.pause()
        return self.png

    async def close(self):
        self.closed = True


class FakeSend:
    def __init__(self):
        self.texts = []
        self.images = []

    async def text(self, text, stream_id, **kwargs):
        self.texts.append((text, stream_id, kwargs))

    async def image(self, image, stream_id, **kwargs):
        self.images.append((image, stream_id, kwargs))


class FakeLogger:
    def __init__(self):
        self.messages = []

    def warning(self, template, *args):
        self.messages.append(template % args)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.services = []
        self.tasks = []
        self.clock = ControlledClock()
        # A stray production transport must fail immediately instead of reaching a provider.
        self.http_guard = patch("daily_life.images.aiohttp.ClientSession",
                                side_effect=AssertionError("Network is forbidden in workflow tests"))
        self.http_guard.start()
        self.addCleanup(self.http_guard.stop)
        self.addCleanup(self.directory.cleanup)
        self.service = self.make_service()
        self.ctx = self.service.ctx
        self.images = self.service.images

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for service in reversed(self.services):
            await service.close()

    def make_service(self, *, data_dir=None, real_images=False):
        settings = Settings()
        settings.plugin.admin_qqs = ["12345"]
        settings.character.appearance = "银色短发、蓝色外套的虚拟角色"
        settings.trigger.auto_enabled = True
        settings.trigger.stable_seconds = 120
        settings.trigger.cooldown_seconds = 0
        settings.trigger.daily_limit = 24
        settings.image.base_url = "https://image.invalid/v1"
        settings.image.model = "offline-image-model"
        ctx = SimpleNamespace(api=FakeAPI(life_status(self.clock)), llm=FakeLLM(),
                              send=FakeSend(), logger=FakeLogger())
        images = ImageGenerator(settings.image) if real_images else FakeImages()
        path = data_dir or Path(self.directory.name) / str(len(self.services))
        service = DailyService(ctx, settings, path, images=images,
                               clock=self.clock.time, monotonic=self.clock.monotonic)
        self.services.append(service)
        return service

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    def change(self, activity_id="new", *, service=None):
        service = service or self.service
        service.ctx.api.status["current"]["id"] = activity_id

    async def drain(self, service=None):
        service = service or self.service
        while service.tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(service.tasks)), timeout=3)

    async def trigger(self, service=None):
        service = service or self.service
        await service.tick()  # Existing activity is a baseline, not a backlog.
        self.change(service=service)
        await service.tick()
        self.clock.advance(120)
        await service.tick()
        await self.drain(service)

    def job(self, service=None):
        jobs = (service or self.service).store.latest(100)
        self.assertEqual(len(jobs), 1)
        return jobs[0]

    def assert_free(self, service=None):
        service = service or self.service
        self.assertEqual(service.store.attempts(service.day()), 0)
        self.assertEqual(service.ctx.llm.calls, [])
        self.assertEqual(service.ctx.api.submissions, [])

    async def preview(self, service=None):
        service = service or self.service
        await service.preview("admin-stream")
        job = self.job(service)
        self.assertEqual(job["state"], "draft")
        return job

    async def assert_redacted(self, service=None):
        service = service or self.service
        public = [service.last_error, await service.status()]
        public.extend(service.ctx.logger.messages)
        public.extend(text for text, _, _ in service.ctx.send.texts)
        public.extend(job.get("error") or "" for job in service.store.latest(100))
        for text in public:
            self.assertNotIn(SECRET, text)

    async def test_existing_baseline_is_never_backfilled(self):
        await self.service.tick()
        self.clock.advance(600)
        await self.service.tick()
        await self.drain()
        self.assert_free()
        self.assertEqual(self.service.store.latest(), [])
        restarted = self.make_service(data_dir=self.service.data_dir)
        restarted.ctx.api.status = deepcopy(self.ctx.api.status)
        await restarted.tick()
        self.clock.advance(600)
        await restarted.tick()
        self.assert_free(restarted)

    async def test_activity_requires_full_120_seconds_on_monotonic_clock(self):
        await self.service.tick()
        self.change()
        await self.service.tick()
        # Wall clock jumps do not satisfy the stability interval.
        self.clock.wall += 300
        await self.service.tick()
        self.assert_free()
        self.clock.advance(119, wall=False)
        await self.service.tick()
        self.assert_free()
        self.clock.advance(1, wall=False)
        await self.service.tick()
        await self.drain()
        self.assertEqual(self.job()["state"], "success")
        self.assertEqual(len(self.ctx.api.submissions), 1)

    async def test_numeric_status_and_title_changes_do_not_trigger(self):
        await self.service.tick()
        for mood in (-10, 10, 0):
            self.ctx.api.status.update(mood=mood, energy=mood + 50, status="状态变化")
            self.ctx.api.status["current"]["title"] = "同一个活动的更新描述"
            self.clock.advance(180)
            await self.service.tick()
        await self.drain()
        self.assert_free()
        self.assertEqual(self.service.store.latest(), [])

    async def test_inactive_sleep_idle_not_ready_and_engine_disabled_never_generate(self):
        cases = [
            ("sleeping", {"sleeping": True}),
            ("sleep-kind", {}),
            ("idle", {"current": None}),
            ("ready-false", {"ready": False}),
            ("enabled-false", {"enabled": False}),
        ]
        for name, changes in cases:
            with self.subTest(name=name):
                service = self.make_service()
                await service.tick()
                self.change(service=service)
                service.ctx.api.status.update(changes)
                if name == "sleep-kind":
                    service.ctx.api.status["current"]["kind"] = "sleep"
                await service.tick()
                self.clock.advance(120)
                await service.tick()
                await self.drain(service)
                self.assert_free(service)

    async def test_plugin_or_auto_disabled_resets_baseline_without_backfill(self):
        for option in ("plugin", "auto"):
            with self.subTest(option=option):
                service = self.make_service()
                await service.tick()
                self.change(service=service)
                await service.tick()
                if option == "plugin":
                    service.settings.plugin.enabled = False
                else:
                    service.settings.trigger.auto_enabled = False
                self.clock.advance(120)
                await service.tick()
                if option == "plugin":
                    service.settings.plugin.enabled = True
                else:
                    service.settings.trigger.auto_enabled = True
                await service.tick()
                self.clock.advance(120)
                await service.tick()
                self.assert_free(service)

    async def test_api_gap_restarts_entire_stability_interval(self):
        await self.service.tick()
        self.change()
        await self.service.tick()
        self.clock.advance(100)
        self.ctx.api.errors[LIFE_API] = RuntimeError(SECRET)
        await self.service.tick()
        del self.ctx.api.errors[LIFE_API]
        self.clock.advance(100)
        await self.service.tick()  # Restarts stability, not immediately due.
        self.assert_free()
        self.clock.advance(119)
        await self.service.tick()
        self.assert_free()
        self.clock.advance(1)
        await self.service.tick()
        await self.drain()
        self.assertEqual(self.job()["state"], "success")
        await self.assert_redacted()

    async def test_ready_false_gap_also_restarts_stability(self):
        await self.service.tick()
        self.change()
        await self.service.tick()
        self.clock.advance(119)
        self.ctx.api.status["ready"] = False
        await self.service.tick()
        self.clock.advance(10)
        self.ctx.api.status["ready"] = True
        await self.service.tick()
        self.clock.advance(119)
        await self.service.tick()
        self.assert_free()
        self.clock.advance(1)
        await self.service.tick()
        await self.drain()
        self.assertEqual(self.job()["state"], "success")

    async def test_auto_generates_paired_caption_png_friends_only_and_success_tid(self):
        await self.trigger()
        job = self.job()
        self.assertEqual((job["mode"], job["state"], job["tid"]),
                         ("auto", "success", "offline-tid"))
        self.assertEqual(self.service.store.attempts(self.service.day()), 1)
        self.assertEqual(len(self.ctx.llm.calls), 1)
        self.assertEqual(len(self.images.calls), 1)
        self.assertIn(self.service.settings.character.appearance, self.images.calls[0])
        self.assertIn(SCENE, self.images.calls[0])
        self.assertEqual(len(self.ctx.api.submissions), 1)
        params = self.ctx.api.submissions[0]
        self.assertEqual(params["content"], CAPTION)
        self.assertEqual(params["ugc_right"], 4)
        self.assertEqual(len(params["images"]), 1)
        self.assertTrue(params["images"][0].startswith("base64://"))
        png = base64.b64decode(params["images"][0][len("base64://"):], validate=True)
        self.assertEqual(validate_image(png), self.images.png)
        self.assertEqual(self.service.image_path(job["id"]).read_bytes(), png)
        self.assertEqual(self.ctx.send.images, [])
        self.assertIn((QZONE_API, "1"), self.ctx.api.gets)
        self.assertTrue(any(name == VERSION_API and version == "1" and args == {}
                            for name, version, args in self.ctx.api.calls))
        for _ in range(3):
            self.clock.advance(120)
            await self.service.tick()
        self.assertEqual(len(self.ctx.api.submissions), 1)

    async def test_generation_material_excludes_private_state_and_contacts(self):
        self.ctx.api.status["current"]["title"] = "地址测试路123号楼"
        await self.service.preview("admin-stream")
        self.assertEqual(self.job()["state"], "draft")
        prompt = self.ctx.llm.calls[0]["prompt"]
        for forbidden in ("PRIVATE_LOCATION", "PRIVATE_NEXT_PLAN", "地址测试路", "baseline"):
            self.assertNotIn(forbidden, prompt)
        material = json.loads(prompt.split("配置与素材：\n", 1)[1])
        self.assertEqual(set(material["current_activity"]), {"kind", "title", "mood"})
        self.assertEqual(material["current_activity"]["title"], "学习")

    async def test_activity_change_during_text_generation_skips_image_and_publish(self):
        gate = self.ctx.llm.gate = Gate()
        task = self.start(self.service.preview("admin-stream"))
        await gate.wait()
        self.change()
        gate.release.set()
        await task
        self.assertEqual(self.job()["state"], "skipped")
        self.assertEqual(self.images.calls, [])
        self.assertEqual(self.ctx.api.submissions, [])

    async def test_activity_change_during_image_generation_discards_result(self):
        gate = self.images.gate = Gate()
        task = self.start(self.service.preview("admin-stream"))
        await gate.wait()
        self.change()
        gate.release.set()
        await task
        self.assertEqual(self.job()["state"], "skipped")
        self.assertEqual(self.ctx.api.submissions, [])
        self.assertEqual(list(self.service.cache_dir.glob("*.img")), [])

    async def test_disabling_plugin_during_image_generation_cannot_publish(self):
        gate = self.images.gate = Gate()
        task = self.start(self.service.preview("admin-stream"))
        await gate.wait()
        self.service.settings.plugin.enabled = False
        gate.release.set()
        await task
        self.assertEqual(self.job()["state"], "skipped")
        self.assertEqual(self.ctx.api.submissions, [])

    async def test_cooldown_and_day_limit_skip_without_later_queue(self):
        for reason in ("cooldown", "daily_limit"):
            with self.subTest(reason=reason):
                service = self.make_service()
                seed = service.store.reserve("previous", {}, self.clock.wall, service.day(), 24, 0)
                service.store.update(seed["id"], "generating", self.clock.wall, state="failed")
                service.settings.trigger.cooldown_seconds = 300 if reason == "cooldown" else 0
                service.settings.trigger.daily_limit = 1 if reason == "daily_limit" else 24
                await self.trigger(service)
                skips = [job for job in service.store.latest() if job["mode"] == "skip"]
                self.assertEqual(len(skips), 1)
                self.assertEqual(skips[0]["state"], "skipped")
                self.assertIn("冷却" if reason == "cooldown" else "额度", skips[0]["error"])
                self.assertEqual(service.ctx.llm.calls, [])
                self.clock.advance(86400 if reason == "daily_limit" else 500)
                await service.tick()
                await self.drain(service)
                self.assertEqual(service.ctx.llm.calls, [])
                self.assertEqual(service.ctx.api.submissions, [])
                self.assertEqual(len(service.store.latest()), 2)

    async def test_busy_observation_is_consumed_before_stability_not_queued(self):
        gate = self.images.gate = Gate()
        await self.service.tick()
        self.change("generating")
        await self.service.tick()
        self.clock.advance(120)
        await self.service.tick()
        await gate.wait()
        self.change("arrived-while-busy")
        await self.service.tick()  # Not stable yet, but must already be consumed.
        gate.release.set()
        await self.drain()
        self.clock.advance(120)
        await self.service.tick()
        await self.drain()
        self.assertEqual(len(self.ctx.llm.calls), 1)
        self.assertEqual(self.ctx.api.submissions, [])
        jobs = self.service.store.latest()
        self.assertEqual(len(jobs), 2)
        self.assertTrue(any(job["mode"] == "skip" and job["error"] == "busy" for job in jobs))

    async def test_persisted_global_busy_claim_is_not_queued(self):
        seed = self.service.store.reserve("other-worker", {}, self.clock.wall,
                                          self.service.day(), 24, 0, lease_seconds=1000)
        await self.trigger()
        self.assertEqual(self.ctx.llm.calls, [])
        self.service.store.update(seed["id"], "generating", self.clock.wall, state="failed")
        self.clock.advance(120)
        await self.service.tick()
        await self.drain()
        self.assertEqual(self.ctx.llm.calls, [])
        self.assertEqual(self.ctx.api.submissions, [])

    async def test_preview_does_not_publish_then_manual_publish_is_once(self):
        job = await self.preview()
        self.assertEqual(self.ctx.api.submissions, [])
        self.assertEqual(len(self.ctx.send.images), 1)
        image, stream, options = self.ctx.send.images[0]
        self.assertEqual(base64.b64decode(image), self.images.png)
        self.assertEqual(stream, "admin-stream")
        self.assertEqual(options, {"storage_message": False, "sync_to_maisaka_history": False})
        self.assertTrue(any("尚未发布" in text for text, _, _ in self.ctx.send.texts))
        await self.service.publish_draft(job["id"], "admin-stream")
        await self.service.publish_draft(job["id"], "admin-stream")
        self.assertEqual(len(self.ctx.api.submissions), 1)
        self.assertEqual(self.job()["state"], "success")
        self.assertEqual(self.service.store.attempts(self.service.day()), 1)
        self.assertEqual(len(self.ctx.llm.calls), 1)
        self.assertEqual(len(self.images.calls), 1)

    async def test_concurrent_manual_publish_claim_submits_only_once(self):
        job = await self.preview()
        gate = self.ctx.api.publish_gate = Gate()
        first = self.start(self.service.publish_draft(job["id"], "admin-stream"))
        await gate.wait()
        self.assertEqual(self.job()["state"], "submitting")
        await self.service.publish_draft(job["id"], "admin-stream")
        gate.release.set()
        await first
        self.assertEqual(len(self.ctx.api.submissions), 1)
        self.assertEqual(self.job()["state"], "success")

    async def test_expired_or_changed_draft_is_not_published(self):
        for reason in ("expired", "changed", "disabled"):
            with self.subTest(reason=reason):
                service = self.make_service()
                job = await self.preview(service)
                if reason == "expired":
                    self.clock.advance(service.settings.publish.draft_ttl_seconds)
                elif reason == "changed":
                    self.change(service=service)
                else:
                    service.settings.plugin.enabled = False
                await service.publish_draft(job["id"], "admin-stream")
                self.assertEqual(service.ctx.api.submissions, [])
                self.assertNotEqual(service.store.get(job["id"])["state"], "success")
                self.assertEqual(service.store.attempts(service.day()), 1)

    async def test_activity_change_during_publish_preflight_is_rechecked(self):
        job = await self.preview()
        self.ctx.api.version_hook = lambda: self.change()
        await self.service.publish_draft(job["id"], "admin-stream")
        self.assertEqual(self.job()["state"], "skipped")
        self.assertEqual(self.ctx.api.submissions, [])

    async def test_uncertain_submission_is_durable_and_never_resent(self):
        job = await self.preview()
        self.ctx.api.errors[QZONE_API] = TimeoutError(SECRET)
        await self.service.publish_draft(job["id"], "admin-stream")
        self.assertEqual(self.job()["state"], "uncertain")
        self.assertIsNone(self.job()["tid"])
        del self.ctx.api.errors[QZONE_API]
        await self.service.publish_draft(job["id"], "admin-stream")
        restarted = self.make_service(data_dir=self.service.data_dir)
        restarted.ctx.api.status = deepcopy(self.ctx.api.status)
        await restarted.publish_draft(job["id"], "admin-stream")
        await restarted.preview("admin-stream")
        self.assertEqual(restarted.ctx.api.submissions, [])
        self.assertEqual(restarted.ctx.llm.calls, [])
        self.assertEqual(len(self.ctx.api.submissions), 1)
        self.assertEqual(restarted.store.get(job["id"])["state"], "uncertain")
        await self.assert_redacted()

    async def test_missing_invalid_tid_or_ambiguous_receipt_is_not_success(self):
        responses = [
            {"status": "ok", "retcode": 0, "data": {}},
            {"status": "ok", "retcode": 0, "data": {"tid": "  "}},
            {"status": "ok", "retcode": 0, "data": {"tid": 123}},
            {"status": "ok", "retcode": False, "data": {"tid": "not-proof"}},
            {"success": True, "tid": "not-onebot"},
            None,
        ]
        for response in responses:
            with self.subTest(response=response):
                service = self.make_service()
                job = await self.preview(service)
                service.ctx.api.publish_response = response
                await service.publish_draft(job["id"], "admin-stream")
                self.assertEqual(service.store.get(job["id"])["state"], "uncertain")
                self.assertIsNone(service.store.get(job["id"])["tid"])
                await service.publish_draft(job["id"], "admin-stream")
                self.assertEqual(len(service.ctx.api.submissions), 1)

    async def test_definite_api_rejection_is_failed_redacted_and_not_resent(self):
        job = await self.preview()
        self.ctx.api.publish_response = {"status": "failed", "retcode": 100,
                                         "message": SECRET, "wording": SECRET}
        await self.service.publish_draft(job["id"], "admin-stream")
        self.assertEqual(self.job()["state"], "failed")
        await self.service.publish_draft(job["id"], "admin-stream")
        self.assertEqual(len(self.ctx.api.submissions), 1)
        await self.assert_redacted()

    async def test_cancel_during_submission_marks_uncertain_and_never_resends(self):
        job = await self.preview()
        gate = self.ctx.api.publish_gate = Gate()
        task = self.start(self.service.publish_draft(job["id"], "admin-stream"))
        await gate.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.job()["state"], "uncertain")
        self.assertEqual(self.job()["error"], "cancelled_during_submission")
        self.ctx.api.publish_gate = None
        await self.service.publish_draft(job["id"], "admin-stream")
        self.assertEqual(len(self.ctx.api.submissions), 1)

    async def test_close_cancels_auto_submission_as_uncertain(self):
        gate = self.ctx.api.publish_gate = Gate()
        await self.service.tick()
        self.change()
        await self.service.tick()
        self.clock.advance(120)
        await self.service.tick()
        await gate.wait()
        await self.service.close()
        self.assertEqual(self.job()["state"], "uncertain")
        self.assertEqual(self.job()["error"], "cancelled_during_submission")
        self.assertEqual(len(self.ctx.api.submissions), 1)
        self.assertTrue(self.images.closed)

    async def test_cancel_during_generation_is_failed_and_does_not_retry(self):
        gate = self.images.gate = Gate()
        task = self.start(self.service.preview("admin-stream"))
        await gate.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.job()["state"], "failed")
        self.assertEqual(self.job()["error"], "cancelled_no_retry")
        await self.service.preview("admin-stream")
        self.assertEqual(len(self.images.calls), 1)
        self.assertEqual(self.ctx.api.submissions, [])

    async def test_missing_configuration_never_charges_generation_attempt(self):
        for missing in ("admin", "appearance", "plugin", "image-key", "image-url", "image-model"):
            with self.subTest(missing=missing):
                service = self.make_service(real_images=missing.startswith("image-"))
                if missing == "admin":
                    service.settings.plugin.admin_qqs = []
                elif missing == "appearance":
                    service.settings.character.appearance = " "
                elif missing == "plugin":
                    service.settings.plugin.enabled = False
                else:
                    service.settings.image.api_key = "" if missing == "image-key" else "offline-key"
                    if missing == "image-url":
                        service.settings.image.base_url = ""
                    if missing == "image-model":
                        service.settings.image.model = ""
                await service.preview("admin-stream")
                self.assert_free(service)
                self.assertEqual(service.store.latest(), [])
                self.assertEqual(service.ctx.api.gets, [])
                self.assertTrue(service.ctx.send.texts)

    async def test_publisher_preflight_errors_are_free_and_hide_upstream_token(self):
        for failing in ("registration", "get", "version-error", "wrong-client", "wrong-envelope"):
            with self.subTest(failing=failing):
                service = self.make_service()
                if failing == "registration":
                    service.ctx.api.registered = False
                elif failing == "get":
                    service.ctx.api.errors["get"] = RuntimeError(SECRET)
                elif failing == "version-error":
                    service.ctx.api.errors[VERSION_API] = RuntimeError(SECRET)
                elif failing == "wrong-client":
                    service.ctx.api.version_response["data"]["app_name"] = "NapCat"
                else:
                    service.ctx.api.version_response = {"success": True, "data": {"app_name": "SnowLuma"}}
                await service.preview("admin-stream")
                self.assert_free(service)
                self.assertEqual(service.store.latest(), [])
                await self.assert_redacted(service)

    async def test_life_and_llm_errors_are_redacted_and_not_automatically_retried(self):
        self.ctx.api.errors[LIFE_API] = RuntimeError(SECRET)
        await self.service.preview("admin-stream")
        self.assert_free()
        await self.assert_redacted()
        del self.ctx.api.errors[LIFE_API]
        self.ctx.llm.error = RuntimeError(SECRET)
        await self.service.preview("admin-stream")
        self.assertEqual(self.job()["state"], "failed")
        self.assertEqual(self.service.store.attempts(self.service.day()), 1)
        await self.service.preview("admin-stream")
        self.assertEqual(len(self.ctx.llm.calls), 1)
        self.assertEqual(self.images.calls, [])
        await self.assert_redacted()

    async def test_invalid_generated_content_never_reaches_image_or_publish(self):
        bad = [
            {"success": False, "response": SECRET},
            {"success": True, "response": "not json " + SECRET},
            {"success": True, "response": json.dumps({"caption": "太短", "image_prompt": SCENE})},
            {"success": True, "response": json.dumps({"caption": CAPTION + " https://private.invalid",
                                                       "image_prompt": SCENE})},
        ]
        for result in bad:
            with self.subTest(result=result):
                service = self.make_service()
                service.ctx.llm.result = result
                await service.preview("admin-stream")
                self.assertEqual(self.job(service)["state"], "failed")
                self.assertEqual(service.images.calls, [])
                self.assertEqual(service.ctx.api.submissions, [])
                self.assertEqual(service.store.attempts(service.day()), 1)
                await self.assert_redacted(service)

    async def test_invalid_generated_image_is_rejected_by_real_validator(self):
        self.images.png = b"not-an-image"
        await self.service.preview("admin-stream")
        self.assertEqual(self.job()["state"], "failed")
        self.assertEqual(self.ctx.api.submissions, [])
        self.assertEqual(self.ctx.send.images, [])
        self.assertEqual(list(self.service.cache_dir.glob("*.img")), [])

    async def test_receipt_tid_is_trimmed_after_proven_success(self):
        job = await self.preview()
        self.ctx.api.publish_response["data"]["tid"] = "  proven-tid  "
        await self.service.publish_draft(job["id"], "admin-stream")
        self.assertEqual(self.job()["state"], "success")
        self.assertEqual(self.job()["tid"], "proven-tid")


    async def test_readonly_background_task_does_not_consume_activity_as_busy(self):
        gate = Gate()
        readonly = self.service.spawn(gate.pause(), busy=False)
        await gate.wait()
        self.assertIn(readonly, self.service.tasks)
        self.assertNotIn(readonly, self.service.busy_tasks)
        await self.service.tick()
        self.change()
        await self.service.tick()
        self.clock.advance(120)
        await self.service.tick()
        self.assertEqual(self.service.store.attempts(self.service.day()), 1)
        gate.release.set()
        await self.drain()
        self.assertEqual(self.job()["state"], "success")
        self.assertEqual(len(self.ctx.api.submissions), 1)

    async def test_guarded_background_error_is_redacted_and_releases_busy(self):
        async def broken():
            raise RuntimeError(SECRET)

        task = self.service.spawn(broken())
        # spawn returns the actual coroutine Task; its callback retrieves errors
        # for fire-and-forget use, while explicit awaiters retain normal semantics.
        with self.assertRaises(RuntimeError):
            await task
        self.assertNotIn(task, self.service.tasks)
        self.assertNotIn(task, self.service.busy_tasks)
        await self.assert_redacted()
        await self.trigger()
        self.assertEqual(self.job()["state"], "success")

    async def test_post_generates_and_publishes_once_with_automatic_mode_disabled(self):
        self.service.settings.trigger.auto_enabled = False
        await self.service.post("admin-stream")
        job = self.job()
        self.assertEqual((job["mode"], job["state"], job["tid"]), ("post", "success", "offline-tid"))
        self.assertEqual(len(self.ctx.llm.calls), 1)
        self.assertEqual(len(self.images.calls), 1)
        self.assertEqual(len(self.ctx.api.submissions), 1)
        self.assertEqual(self.ctx.api.submissions[0]["content"], CAPTION)
        self.assertEqual(self.ctx.api.submissions[0]["ugc_right"], 4)
        self.assertEqual(self.ctx.send.images, [])  # Post is not a chat preview.
        self.assertTrue(any("已发布到 QQ 空间" in text and stream == "admin-stream"
                            for text, stream, _ in self.ctx.send.texts))
        await self.service.post("admin-stream")
        self.assertEqual(len(self.ctx.llm.calls), 1)
        self.assertEqual(len(self.ctx.api.submissions), 1)
        self.assertEqual(self.service.store.attempts(self.service.day()), 1)

    async def test_post_does_not_regenerate_or_implicitly_publish_an_existing_preview(self):
        job = await self.preview()
        await self.service.post("admin-stream")
        self.assertEqual(self.service.store.get(job["id"])["state"], "draft")
        self.assertEqual(len(self.ctx.llm.calls), 1)
        self.assertEqual(self.ctx.api.submissions, [])
        await self.service.publish_draft(job["id"], "admin-stream")
        self.assertEqual(len(self.ctx.api.submissions), 1)
        self.assertEqual(len(self.ctx.llm.calls), 1)

    async def test_manual_post_ignores_cooldown_and_daily_limits(self):
        for reason in ("cooldown", "daily_limit"):
            with self.subTest(reason=reason):
                service = self.make_service()
                seed = service.store.reserve("previous", {}, self.clock.wall, service.day(), 24, 0)
                service.store.update(seed["id"], "generating", self.clock.wall, state="failed")
                service.settings.trigger.cooldown_seconds = 7200 if reason == "cooldown" else 0
                service.settings.trigger.daily_limit = 1 if reason == "daily_limit" else 24
                await service.post("admin-stream")
                self.assertEqual(len(service.ctx.llm.calls), 1)
                self.assertEqual(len(service.ctx.api.submissions), 1)
                self.assertEqual(service.store.attempts(service.day()), 2)

    async def test_manual_preview_ignores_limits_and_keeps_existing_draft_publishable(self):
        seed = self.service.store.reserve("previous", {}, self.clock.wall, self.service.day(), 24, 0)
        self.service.store.update(seed["id"], "generating", self.clock.wall, state="failed")
        self.service.settings.trigger.daily_limit = 1
        self.service.settings.trigger.cooldown_seconds = 7200
        await self.service.preview("admin-stream")
        drafts = [job for job in self.service.store.latest() if job["state"] == "draft"]
        self.assertEqual(len(drafts), 1)
        self.assertEqual(self.service.store.attempts(self.service.day()), 2)
        self.assertEqual(self.ctx.api.submissions, [])
        await self.service.publish_draft(drafts[0]["id"], "admin-stream")
        self.assertEqual(len(self.ctx.api.submissions), 1)

    async def test_automatic_skip_during_limits_does_not_block_explicit_manual_post(self):
        self.service.settings.trigger.daily_limit = 1
        self.service.settings.trigger.cooldown_seconds = 7200
        await self.service.post("admin-stream")
        await self.service.tick()
        self.change("second-activity")
        await self.service.tick()
        self.clock.advance(120)
        await self.service.tick()
        await self.drain()
        self.assertEqual(len(self.ctx.llm.calls), 1)  # Automatic path remains limited.
        await self.service.post("admin-stream")
        self.assertEqual(len(self.ctx.llm.calls), 2)
        self.assertEqual(len(self.ctx.api.submissions), 2)
        self.assertEqual(self.service.store.attempts(self.service.day()), 2)
        self.assertIn("手动命令不限", await self.service.status())

    async def test_post_revalidates_activity_changed_during_generation(self):
        gate = self.images.gate = Gate()
        task = self.start(self.service.post("admin-stream"))
        await gate.wait()
        self.change()
        gate.release.set()
        await task
        self.assertEqual(self.job()["state"], "skipped")
        self.assertEqual(self.ctx.api.submissions, [])
        self.assertEqual(self.ctx.send.images, [])
        self.assertTrue(any("丢弃" in text for text, _, _ in self.ctx.send.texts))

    async def test_post_uncertain_receipt_never_resubmits(self):
        self.ctx.api.errors[QZONE_API] = RuntimeError(SECRET)
        await self.service.post("admin-stream")
        self.assertEqual(self.job()["state"], "uncertain")
        await self.service.post("admin-stream")
        self.assertEqual(len(self.ctx.api.submissions), 1)
        self.assertEqual(len(self.ctx.llm.calls), 1)
        self.assertTrue(any("结果不明" in text for text, _, _ in self.ctx.send.texts))
        await self.assert_redacted()

    async def test_post_rejects_idle_sleep_and_disabled_plugin_without_cost(self):
        for reason in ("idle", "sleep", "disabled"):
            with self.subTest(reason=reason):
                service = self.make_service()
                if reason == "idle":
                    service.ctx.api.status["current"] = None
                elif reason == "sleep":
                    service.ctx.api.status["sleeping"] = True
                else:
                    service.settings.plugin.enabled = False
                await service.post("admin-stream")
                self.assert_free(service)

    async def test_explicit_retry_regenerates_failed_drawing_and_posts_once(self):
        self.images.png = b"not-an-image"
        await self.service.post("admin-stream")
        job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(len(self.ctx.api.submissions), 0)
        self.assertTrue(any("/daily retry " + job["id"] in text for text, _, _ in self.ctx.send.texts))
        await self.service.post("admin-stream")  # Repeating post is still not an implicit retry.
        self.assertEqual(len(self.ctx.llm.calls), 1)
        self.images.png = FakeImages().png
        await self.service.retry(job["id"], "admin-stream")
        saved = self.job()
        self.assertEqual(saved["state"], "success")
        self.assertEqual(saved["retry_count"], 1)
        self.assertEqual(saved["submission_started"], 1)
        self.assertEqual(saved["tid"], "offline-tid")
        self.assertEqual(len(self.ctx.llm.calls), 2)
        self.assertEqual(len(self.ctx.api.submissions), 1)
        self.assertEqual(self.service.store.attempts(self.service.day()), 2)
        await self.service.retry(job["id"], "admin-stream")
        self.assertEqual(len(self.ctx.llm.calls), 2)
        self.assertEqual(len(self.ctx.api.submissions), 1)

    async def test_explicit_retry_ignores_cooldown_and_daily_budget(self):
        self.images.png = b"not-an-image"
        await self.service.post("admin-stream")
        job = self.job()
        self.images.png = FakeImages().png
        self.service.settings.trigger.cooldown_seconds = 7200
        self.service.settings.trigger.daily_limit = 1
        await self.service.retry(job["id"], "admin-stream")
        self.assertEqual(len(self.ctx.llm.calls), 2)
        self.assertEqual(len(self.ctx.api.submissions), 1)
        self.assertEqual(self.job()["state"], "success")
        self.assertEqual(self.service.store.attempts(self.service.day()), 2)

    async def test_retry_cannot_resend_uncertain_or_rejected_submissions(self):
        for uncertain in (True, False):
            with self.subTest(uncertain=uncertain):
                service = self.make_service()
                if uncertain:
                    service.ctx.api.errors[QZONE_API] = RuntimeError(SECRET)
                else:
                    service.ctx.api.publish_response = {"status": "failed", "retcode": 100, "data": {}}
                await service.post("admin-stream")
                job = self.job(service)
                self.assertEqual(job["state"], "uncertain" if uncertain else "failed")
                await service.retry(job["id"], "admin-stream")
                self.assertEqual(len(service.ctx.llm.calls), 1)
                self.assertEqual(len(service.ctx.api.submissions), 1)
                self.assertEqual(service.store.attempts(service.day()), 1)
                await self.assert_redacted(service)

    async def test_retry_rejects_activity_changes_without_spending_quota(self):
        self.images.png = b"not-an-image"
        await self.service.post("admin-stream")
        job = self.job()
        self.change()
        await self.service.retry(job["id"], "admin-stream")
        self.assertEqual(len(self.ctx.llm.calls), 1)
        self.assertEqual(self.service.store.attempts(self.service.day()), 1)
        self.assertTrue(any("当前活动" in text for text, _, _ in self.ctx.send.texts))

    async def test_blocked_notification_preserves_real_drawing_failure_and_never_resends(self):
        from unittest.mock import AsyncMock
        self.service.report("绘图 API HTTP 401：认证失败")
        for result in (False, {"sent": False, "message_id": None}):
            with self.subTest(result=result):
                self.ctx.send.text = AsyncMock(return_value=result)
                await self.service.notify("admin-stream", "safe notice")
                self.ctx.send.text.assert_awaited_once()
                self.assertIn("HTTP 401", self.service.last_error)
                self.assertIn("Hook", self.service.last_delivery_error)
                self.assertIn("HTTP 401", await self.service.status())
                self.assertIn("私聊", await self.service.status())
        self.assertEqual(self.ctx.api.submissions, [])

    async def test_accepted_post_emits_only_final_result_not_preliminary_ack(self):
        import plugin
        instance = plugin.create_plugin()
        instance.service = self.service
        result = await instance.handle_daily(text="/daily post", stream_id="admin-stream", platform="qq", user_id="12345")
        self.assertTrue(result[0])
        await self.drain()
        self.assertEqual(len(self.ctx.send.texts), 1)
        self.assertIn("已发布到 QQ 空间", self.ctx.send.texts[0][0])
        self.assertEqual(len(self.ctx.api.submissions), 1)

    async def test_stale_generation_cannot_mutate_or_publish_a_new_manual_retry(self):
        old_gate = self.ctx.llm.gate = Gate()
        old_task = self.start(self.service.preview("admin-stream"))
        await old_gate.wait()
        job = self.job()
        self.clock.advance(self.service.lease_seconds + 1)
        self.service.store.recover(self.clock.wall, self.service.lease_seconds)
        self.assertEqual(self.job()["state"], "failed")
        self.ctx.llm.gate = None
        new_gate = self.images.gate = Gate()
        retry_task = self.start(self.service.retry(job["id"], "admin-stream"))
        await new_gate.wait()
        self.assertEqual(self.job()["retry_count"], 1)
        old_gate.release.set()
        await asyncio.wait_for(old_task, timeout=3)
        self.assertEqual(self.job()["state"], "generating")
        self.assertEqual(len(self.images.calls), 1)
        self.assertEqual(self.ctx.api.submissions, [])
        new_gate.release.set()
        await retry_task
        self.assertEqual(self.job()["state"], "success")
        self.assertEqual(len(self.ctx.api.submissions), 1)

    async def test_failed_post_command_emits_one_final_diagnostic_with_retry_id(self):
        import plugin
        instance = plugin.create_plugin()
        instance.service = self.service
        self.images.png = b"bad-image"
        await instance.handle_daily(text="/daily post", stream_id="admin-stream", platform="qq", user_id="12345")
        await self.drain()
        self.assertEqual(len(self.ctx.send.texts), 1)
        self.assertIn("生成失败", self.ctx.send.texts[0][0])
        self.assertIn("/daily retry " + self.job()["id"], self.ctx.send.texts[0][0])
        self.assertEqual(self.ctx.api.submissions, [])

    async def test_two_services_share_durable_single_submission_claim(self):
        job = await self.preview()
        second = self.make_service(data_dir=self.service.data_dir)
        second.ctx.api.status = deepcopy(self.ctx.api.status)
        gate = self.ctx.api.publish_gate = Gate()
        first = self.start(self.service.publish_draft(job["id"], "admin-stream"))
        await gate.wait()
        await second.publish_draft(job["id"], "admin-stream")
        self.assertEqual(second.ctx.api.submissions, [])
        gate.release.set()
        await first
        self.assertEqual(self.job()["state"], "success")
        self.assertEqual(len(self.ctx.api.submissions), 1)
        self.assertEqual(second.store.attempts(second.day()), 1)

    async def test_publish_while_other_generation_busy_remains_draft_without_queue(self):
        job = await self.preview()
        other = self.service.store.reserve("other-generation", {}, self.clock.wall,
                                           self.service.day(), 24, 0)
        await self.service.publish_draft(job["id"], "admin-stream")
        self.assertEqual(self.service.store.get(job["id"])["state"], "draft")
        self.assertEqual(self.ctx.api.submissions, [])
        self.service.store.update(other["id"], "generating", self.clock.wall, state="failed")
        # Finishing the other task does not schedule a previously blocked publication.
        self.assertEqual(self.ctx.api.submissions, [])
        await self.service.publish_draft(job["id"], "admin-stream")
        self.assertEqual(self.service.store.get(job["id"])["state"], "success")
        self.assertEqual(len(self.ctx.api.submissions), 1)
        self.assertEqual(self.service.store.attempts(self.service.day()), 2)


if __name__ == "__main__":
    unittest.main()
