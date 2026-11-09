"""Boundary regressions for the no-backfill/no-queue publication contract."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import inspect
import unittest

from daily_life.config import Settings
from daily_life.service import DailyService
from tests.test_workflow import ControlledClock, FakeAPI, FakeLLM, FakeImages, FakeSend, FakeLogger, life_status


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = TemporaryDirectory()
        self.clock = ControlledClock()
        settings = Settings()
        settings.plugin.admin_qqs = ["12345"]
        settings.character.appearance = "成年角色，黑色短发，浅色休闲服"
        settings.trigger.auto_enabled = True
        self.ctx = SimpleNamespace(api=FakeAPI(life_status(self.clock)), llm=FakeLLM(),
                                   send=FakeSend(), logger=FakeLogger())
        self.service = DailyService(self.ctx, settings, Path(self.folder.name), images=FakeImages(),
                                    clock=self.clock.time, monotonic=self.clock.monotonic)

    async def asyncTearDown(self):
        await self.service.close()
        self.folder.cleanup()

    async def drain(self):
        while self.service.tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(self.service.tasks)), timeout=3)

    def seed_attempt(self, *, active=False):
        job = self.service.store.reserve("previous", {}, self.clock.wall, self.service.day(), 3, 0,
                                         lease_seconds=180)
        if not active:
            self.service.store.update(job["id"], "generating", self.clock.wall, state="failed")
        return job

    async def test_change_during_last_minute_of_cooldown_is_not_queued(self):
        self.seed_attempt()
        await self.service.tick()
        self.clock.advance(7100)
        self.ctx.api.status["current"]["id"] = "during-cooldown"
        await self.service.tick()
        self.clock.advance(120)  # Now beyond 7200s, but this activity is already skipped.
        await self.service.tick()
        await self.drain()
        self.assertEqual(self.ctx.llm.calls, [])
        self.assertEqual(self.ctx.api.submissions, [])
        self.assertEqual(self.service.store.attempts(self.service.day()), 1)
        self.assertTrue(any(job["mode"] == "skip" for job in self.service.store.latest()))

    async def test_change_at_day_limit_before_midnight_is_not_queued_next_day(self):
        self.clock.wall = datetime(2026, 11, 8, 15, 59, 30, tzinfo=timezone.utc).timestamp()
        self.ctx.api.status = life_status(self.clock)
        self.service.settings.trigger.daily_limit = 1
        self.service.settings.trigger.cooldown_seconds = 0
        self.seed_attempt()
        await self.service.tick()
        self.ctx.api.status["current"]["id"] = "at-daily-limit"
        await self.service.tick()
        self.clock.advance(120)
        await self.service.tick()
        await self.drain()
        self.assertEqual(self.service.store.attempts(self.service.day()), 0)
        self.assertEqual(self.ctx.llm.calls, [])
        self.assertEqual(self.ctx.api.submissions, [])

    async def test_change_while_another_instance_busy_is_not_queued_after_lease_expiry(self):
        self.service.settings.trigger.cooldown_seconds = 0
        self.seed_attempt(active=True)
        await self.service.tick()
        self.ctx.api.status["current"]["id"] = "external-busy"
        await self.service.tick()
        self.clock.advance(200)
        await self.service.tick()
        await self.drain()
        self.assertEqual(self.ctx.llm.calls, [])
        self.assertEqual(self.ctx.api.submissions, [])
        self.assertEqual(self.service.store.attempts(self.service.day()), 1)

    async def test_startup_baseline_cannot_reappear_after_idle_and_trigger(self):
        baseline = deepcopy(self.ctx.api.status)
        await self.service.tick()
        self.ctx.api.status["current"] = None
        await self.service.tick()
        self.ctx.api.status = baseline
        await self.service.tick()
        self.clock.advance(180)
        await self.service.tick()
        await self.drain()
        self.assertEqual(self.ctx.llm.calls, [])
        self.assertEqual(self.service.store.attempts(self.service.day()), 0)

    async def test_startup_baseline_survives_restart_with_initial_idle(self):
        baseline = deepcopy(self.ctx.api.status)
        await self.service.tick()
        data_dir, settings = self.service.data_dir, self.service.settings
        await self.service.close()
        self.service = DailyService(self.ctx, settings, data_dir, images=FakeImages(),
                                    clock=self.clock.time, monotonic=self.clock.monotonic)
        self.ctx.api.status["current"] = None
        await self.service.tick()
        self.ctx.api.status = baseline
        await self.service.tick()
        self.clock.advance(180)
        await self.service.tick()
        await self.drain()
        self.assertEqual(self.ctx.llm.calls, [])
        self.assertEqual(self.service.store.attempts(self.service.day()), 0)
        # Automatic-only baseline dedupe must not prevent an explicit manual preview.
        await self.service.preview("admin")
        self.assertEqual(self.service.store.latest()[0]["state"], "draft")
        self.assertEqual(len(self.ctx.llm.calls), 1)

    async def test_immediate_task_cancellation_closes_coroutine_and_releases_claim(self):
        job = self.seed_attempt(active=True)

        async def not_started():
            raise AssertionError("cancelled coroutine must never run")

        coroutine = not_started()
        task = self.service.spawn(coroutine, job_id=job["id"])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(inspect.getcoroutinestate(coroutine), inspect.CORO_CLOSED)
        self.assertEqual(self.service.store.get(job["id"])["state"], "failed")
        self.assertNotIn(task, self.service.tasks)
        self.assertNotIn(task, self.service.busy_tasks)

    async def test_late_success_receipt_resolves_expired_submission_lease(self):
        await self.service.preview("admin")
        job = self.service.store.latest()[0]

        async def late_publish(caption, image):
            self.clock.advance(self.service.lease_seconds + 1)
            self.service.store.recover(self.clock.wall, self.service.lease_seconds)
            self.assertEqual(self.service.store.get(job["id"])["state"], "uncertain")
            return "late-genuine-tid"

        with patch.object(self.service.publisher, "publish", side_effect=late_publish):
            await self.service.publish_draft(job["id"], "admin")
        result = self.service.store.get(job["id"])
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["tid"], "late-genuine-tid")
        self.assertEqual(self.service.store.attempts(self.service.day()), 1)
        self.assertTrue(any("已发布到 QQ 空间" in text for text, _, _ in self.ctx.send.texts))

    async def test_receipt_storage_failure_warns_without_resubmission(self):
        await self.service.preview("admin")
        job = self.service.store.latest()[0]
        with patch.object(self.service.store, "confirm_submission", side_effect=OSError("DO-NOT-LEAK")):
            await self.service.publish_draft(job["id"], "admin")
        self.assertEqual(len(self.ctx.api.submissions), 1)
        notices = "\n".join(text for text, _, _ in self.ctx.send.texts)
        self.assertIn("本地记录保存失败", notices)
        self.assertIn("offline-tid", notices)
        self.assertNotIn("DO-NOT-LEAK", notices)
        await self.service.publish_draft(job["id"], "admin")
        self.assertEqual(len(self.ctx.api.submissions), 1)


if __name__ == "__main__":
    unittest.main()
