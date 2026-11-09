"""Persistence invariants tested against real, independent SQLite connections."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import sqlite3
import unittest
from uuid import UUID

from daily_life.store import ClaimError, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "daily.sqlite3"
        self.store = Store(self.path)
        self.day = "2026-11-08"

    def reserve(self, key="activity", now=100.0, day=None, **kwargs):
        options = dict(daily_limit=10, cooldown_seconds=0)
        options.update(kwargs)
        return self.store.reserve(key, {"title": "散步", "nested": {"items": [1, 2]}},
                                  now, day or self.day, **options)

    def assert_rejected(self, reason, callback):
        with self.assertRaises(ClaimError) as caught:
            callback()
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), reason)

    def retry(self, task, now=102.0, day=None, **kwargs):
        options = dict(daily_limit=10, cooldown_seconds=0)
        options.update(kwargs)
        return self.store.retry(task["id"], task["activity_key"],
                                {"title": "重试散步", "nested": {"items": [3]}},
                                now, day or self.day, **options)

    def test_manual_ignore_limits_records_attempts_but_automatic_claim_still_limits(self):
        first = self.reserve()
        self.store.update(first["id"], "generating", 100, state="failed")
        manual = self.reserve("manual", now=101, daily_limit=1, cooldown_seconds=7200,
                              mode="post", ignore_limits=True)
        self.assertEqual(manual["state"], "generating")
        self.assertEqual(self.store.attempts(self.day), 2)
        self.store.update(manual["id"], "generating", 101, state="failed")
        self.assert_rejected("daily_limit", lambda: self.reserve("auto", now=102, daily_limit=1))
        self.assert_rejected("cooldown", lambda: self.reserve("auto", now=102, daily_limit=10, cooldown_seconds=7200))

    def test_manual_ignore_limits_never_ignores_busy_or_duplicate(self):
        first = self.reserve(ignore_limits=True, daily_limit=0)
        self.assert_rejected("duplicate", lambda: self.reserve(ignore_limits=True, daily_limit=0))
        self.assert_rejected("busy", lambda: self.reserve("other", ignore_limits=True, daily_limit=0))
        self.assertEqual(self.store.attempts(self.day), 1)
        self.store.update(first["id"], "generating", 101, state="uncertain")
        self.assert_rejected("not_retryable", lambda: self.retry(first, ignore_limits=True, daily_limit=0))

    def test_manual_retry_bypasses_both_limits_without_erasing_history(self):
        task = self.reserve()
        self.store.update(task["id"], "generating", 100, state="failed")
        retried = self.retry(task, now=101, daily_limit=1, cooldown_seconds=7200, ignore_limits=True)
        self.assertEqual(retried["retry_count"], 1)
        self.assertEqual(self.store.attempts(self.day), 2)
        self.assert_rejected("duplicate", lambda: self.reserve(ignore_limits=True, daily_limit=0))

    def test_manual_command_can_claim_pristine_automatic_skip_only(self):
        self.store.skip("auto-skipped", {}, 100, "cooldown")
        skipped = self.store.for_activity("auto-skipped")
        self.assert_rejected("duplicate", lambda: self.reserve("auto-skipped"))
        manual = self.reserve("auto-skipped", now=101, mode="preview", daily_limit=0,
                              cooldown_seconds=9999, ignore_limits=True)
        self.assertEqual(manual["id"], skipped["id"])
        self.assertEqual(manual["mode"], "preview")
        self.assertEqual(manual["state"], "generating")
        self.assertEqual(self.store.attempts(self.day), 1)
        self.assert_rejected("duplicate", lambda: self.reserve("auto-skipped", ignore_limits=True))

    def test_record_fields_json_and_reopen(self):
        task = self.reserve(mode="manual", lease_seconds=40)
        self.assertEqual(UUID(hex=task["id"]).hex, task["id"])
        self.assertEqual(set(task), {"id", "activity_key", "snapshot", "mode", "state",
                                     "created_at", "updated_at", "day", "caption",
                                     "image_path", "tid", "error", "lease_until",
                                     "submission_started", "retry_count"})
        self.assertEqual(task["submission_started"], 0)
        self.assertEqual(task["retry_count"], 0)
        self.assertEqual(task["mode"], "manual")
        self.assertEqual(task["state"], "generating")
        self.assertEqual(task["lease_until"], 140)
        self.assertEqual(task["snapshot"]["title"], "散步")
        self.assertIsNone(task["caption"])
        self.store.close()
        reopened = Store(self.path)
        self.assertEqual(reopened.get(task["id"]), task)
        task["snapshot"]["nested"]["items"].append(99)
        self.assertEqual(reopened.get(task["id"])["snapshot"]["nested"]["items"], [1, 2])
        self.assertIsNone(reopened.get("missing"))
        self.assertEqual(reopened.attempts(self.day), 1)

    def test_atomic_same_activity_across_instances(self):
        stores = [Store(self.path), Store(self.path)]
        barrier = Barrier(2)

        def claim(store):
            barrier.wait(timeout=5)
            try:
                return store.reserve("same", {}, 100, self.day, 10, 0)
            except ClaimError as error:
                return error.reason

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, stores))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn("duplicate", results)
        self.assertEqual(self.store.attempts(self.day), 1)
        self.assertEqual(len(self.store.latest()), 1)

    def test_atomic_global_busy_across_instances(self):
        stores = [Store(self.path), Store(self.path)]
        barrier = Barrier(2)

        def claim(index):
            barrier.wait(timeout=5)
            try:
                return stores[index].reserve(f"task-{index}", {}, 100, self.day, 10, 0)
            except ClaimError as error:
                return error.reason

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, range(2)))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn("busy", results)
        self.assertEqual(self.store.attempts(self.day), 1)

    def test_failed_attempt_counts_toward_daily_limit(self):
        task = self.reserve(daily_limit=1)
        self.assertTrue(self.store.update(task["id"], "generating", 101, state="failed", error="generation_failed"))
        self.assert_rejected("daily_limit", lambda: self.reserve("second", now=102, daily_limit=1))
        self.assertEqual(self.store.attempts(self.day), 1)
        next_task = self.reserve("second", now=103, day="2026-11-09", daily_limit=1)
        self.assertEqual(next_task["day"], "2026-11-09")
        self.assertEqual(self.store.attempts("2026-11-09"), 1)

    def test_zero_daily_limit_rejects_without_consuming(self):
        self.assert_rejected("daily_limit", lambda: self.reserve(daily_limit=0))
        self.assertEqual(self.store.latest(), [])
        self.assertEqual(self.store.attempts(self.day), 0)

    def test_cooldown_is_global_across_days_and_boundary_is_allowed(self):
        task = self.reserve(now=100)
        self.store.update(task["id"], "generating", 101, state="failed")
        self.assert_rejected("cooldown", lambda: self.reserve("next", now=129,
                             day="2026-11-09", cooldown_seconds=30))
        self.assertEqual(self.store.attempts("2026-11-09"), 0)
        self.assertEqual(self.reserve("next", now=130, day="2026-11-09",
                                     cooldown_seconds=30)["state"], "generating")

    def test_compare_and_swap_and_field_whitelist(self):
        task = self.reserve()
        second = Store(self.path)
        self.assertFalse(second.update(task["id"], "draft", 101, state="failed"))
        self.assertFalse(second.update("missing", "generating", 101, state="draft"))
        for key in ("snapshot", "activity_key", "mode", "day", "lease_until", "updated_at",
                    "submission_started", "retry_count"):
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, "invalid_update_field"):
                second.update(task["id"], "generating", 101, **{key: "changed"})
        with self.assertRaisesRegex(ValueError, "invalid_state"):
            second.update(task["id"], "generating", 101, state="nonsense")
        self.assertTrue(second.update(task["id"], "generating", 102, state="draft",
                                      caption="你好", image_path="local/image.png"))
        self.assertFalse(self.store.update(task["id"], "generating", 103, state="failed"))
        saved = self.store.get(task["id"])
        self.assertEqual(saved["caption"], "你好")
        self.assertEqual(saved["image_path"], "local/image.png")
        self.assertEqual(saved["updated_at"], 102)
        self.assertIsNone(saved["lease_until"])
        self.assertTrue(second.update(task["id"], "draft", 103, state="submitting"))
        self.assertTrue(second.update(task["id"], "submitting", 104, state="success", tid="123"))
        self.assertEqual(self.store.get(task["id"])["tid"], "123")

    def test_atomic_compare_and_swap_has_one_winner(self):
        task = self.reserve()
        barrier = Barrier(2)
        stores = [Store(self.path), Store(self.path)]

        def change(index):
            barrier.wait(timeout=5)
            return stores[index].update(task["id"], "generating", 101,
                                        state=("draft", "failed")[index])

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(change, range(2))), [False, True])

    def test_draft_does_not_block_new_generation_but_cannot_take_busy_lock(self):
        draft = self.reserve()
        self.store.update(draft["id"], "generating", 101, state="draft")
        active = self.reserve("new", now=102)
        self.assertFalse(self.store.update(draft["id"], "draft", 103, state="submitting"))
        self.assertEqual(self.store.get(draft["id"])["state"], "draft")
        self.store.update(active["id"], "generating", 104, state="failed")
        self.assertTrue(self.store.update(draft["id"], "draft", 105, state="submitting"))
        self.assertEqual(self.store.get(draft["id"])["lease_until"], 1005)
        self.assert_rejected("busy", lambda: self.reserve("third", now=106))

    def test_begin_submission_does_not_charge_quota_or_restart_cooldown(self):
        task = self.reserve(daily_limit=1)
        self.store.update(task["id"], "generating", 101, state="draft", caption="ready")
        self.assertTrue(Store(self.path).begin_submission(task["id"], 120, lease_seconds=17))
        saved = self.store.get(task["id"])
        self.assertEqual(saved["state"], "submitting")
        self.assertEqual(saved["updated_at"], 120)
        self.assertEqual(saved["lease_until"], 137)
        self.assertEqual(saved["caption"], "ready")
        self.assertEqual(self.store.attempts(self.day), 1)
        self.assertFalse(self.store.begin_submission(task["id"], 121))
        self.store.update(task["id"], "submitting", 122, state="success")
        # Original generation was at 100, not the publishing time of 120.
        self.assertEqual(self.reserve("next", now=130, day="2026-11-09",
                                     cooldown_seconds=30)["state"], "generating")

    def test_begin_submission_requires_draft_and_does_not_steal_live_task(self):
        draft = self.reserve()
        self.assertFalse(self.store.begin_submission(draft["id"], 101))
        self.assertFalse(self.store.begin_submission("missing", 101))
        self.store.update(draft["id"], "generating", 102, state="draft")
        active = self.reserve("new", now=103, lease_seconds=10)
        self.assertFalse(Store(self.path).begin_submission(draft["id"], 112))
        self.assertEqual(self.store.get(draft["id"])["state"], "draft")
        self.assertEqual(self.store.get(draft["id"])["updated_at"], 102)
        self.assertEqual(self.store.get(active["id"])["state"], "generating")
        with self.assertRaisesRegex(ValueError, "invalid_lease"):
            self.store.begin_submission(draft["id"], 112, lease_seconds=0)

    def test_begin_submission_recovers_expired_generating_and_submitting(self):
        for index, state in enumerate(("generating", "submitting")):
            with self.subTest(state=state):
                now = 100 + index * 100
                draft = self.reserve(f"draft-{index}", now=now)
                self.store.update(draft["id"], "generating", now + 1, state="draft")
                old = self.reserve(f"active-{index}", now=now + 2, lease_seconds=10)
                if state == "submitting":
                    self.store.update(old["id"], "generating", now + 3, state="submitting")
                    self.store.renew(old["id"], now + 3, lease_seconds=9)
                self.assertTrue(Store(self.path).begin_submission(draft["id"], now + 12))
                self.assertEqual(self.store.get(old["id"])["state"],
                                 "failed" if state == "generating" else "uncertain")
                self.assertEqual(self.store.get(draft["id"])["state"], "submitting")
                self.store.update(draft["id"], "submitting", now + 13, state="success")
        self.assertEqual(self.store.attempts(self.day), 4)

    def test_rejected_begin_submission_still_commits_recovery(self):
        old = self.reserve(lease_seconds=10)
        self.assertFalse(self.store.begin_submission("missing", 110))
        self.assertEqual(self.store.get(old["id"])["state"], "failed")

    def test_begin_submission_races_with_automatic_reserve_atomically(self):
        draft = self.reserve()
        self.store.update(draft["id"], "generating", 101, state="draft")
        stores = [Store(self.path), Store(self.path)]
        barrier = Barrier(2)

        def claim(index):
            barrier.wait(timeout=5)
            if index == 0:
                return stores[index].begin_submission(draft["id"], 102)
            try:
                stores[index].reserve("new", {}, 102, self.day, 10, 0)
                return True
            except ClaimError as error:
                self.assertEqual(error.reason, "busy")
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(claim, range(2))), [False, True])
        active = [row for row in self.store.latest() if row["state"] in ("generating", "submitting")]
        self.assertEqual(len(active), 1)
        expected_attempts = 1 if active[0]["id"] == draft["id"] else 2
        self.assertEqual(self.store.attempts(self.day), expected_attempts)
        if active[0]["id"] != draft["id"]:
            self.assertEqual(self.store.get(draft["id"])["state"], "draft")

    def test_two_draft_submissions_have_one_atomic_winner(self):
        drafts = []
        for index in range(2):
            task = self.reserve(str(index), now=100 + index * 2)
            self.store.update(task["id"], "generating", 101 + index * 2, state="draft")
            drafts.append(task)
        stores = [Store(self.path), Store(self.path)]
        barrier = Barrier(2)

        def claim(index):
            barrier.wait(timeout=5)
            return stores[index].begin_submission(drafts[index]["id"], 110)

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(claim, range(2))), [False, True])
        self.assertEqual(sorted(self.store.get(task["id"])["state"] for task in drafts),
                         ["draft", "submitting"])
        self.assertEqual(self.store.attempts(self.day), 2)

    def test_recover_uses_lease_not_instance_age_or_stale_threshold(self):
        task = self.reserve(lease_seconds=100)
        second = Store(self.path)
        second.recover(199, stale_seconds=1)
        self.assertEqual(self.store.get(task["id"])["state"], "generating")
        second.recover(200, stale_seconds=10000)
        saved = self.store.get(task["id"])
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["error"], "lease_expired")
        self.assertIsNone(saved["lease_until"])
        self.assertEqual(self.store.attempts(self.day), 1)
        second.recover(300, stale_seconds=0)
        self.assertEqual(self.store.get(task["id"])["updated_at"], 200)

    def test_expired_submitting_becomes_uncertain_not_retryable(self):
        task = self.reserve()
        self.store.update(task["id"], "generating", 101, state="submitting")
        self.assertTrue(self.store.renew(task["id"], 102, lease_seconds=10))
        Store(self.path).recover(111, stale_seconds=0)
        self.assertEqual(self.store.get(task["id"])["state"], "submitting")
        self.store.recover(112, stale_seconds=900)
        saved = self.store.get(task["id"])
        self.assertEqual(saved["state"], "uncertain")
        self.assertEqual(saved["error"], "submission_uncertain")
        self.assert_rejected("duplicate", lambda: self.reserve(now=113))
        self.assertFalse(self.store.update(task["id"], "submitting", 113, state="success"))

    def test_reserve_recovers_expired_tasks_automatically(self):
        task = self.reserve(lease_seconds=10)
        self.assert_rejected("busy", lambda: self.reserve("next", now=109))
        next_task = Store(self.path).reserve("next", {}, 110, self.day, 10, 0)
        self.assertEqual(self.store.get(task["id"])["state"], "failed")
        self.assertEqual(next_task["state"], "generating")
        self.assertEqual(self.store.attempts(self.day), 2)

    def test_denied_reserve_still_commits_lease_recovery(self):
        task = self.reserve(daily_limit=1, lease_seconds=10)
        self.assert_rejected("daily_limit", lambda: self.reserve("next", now=110, daily_limit=1))
        self.assertEqual(self.store.get(task["id"])["state"], "failed")

    def test_renew_is_active_only_and_expired_leases_cannot_be_resurrected(self):
        task = self.reserve(lease_seconds=10)
        self.assertTrue(Store(self.path).renew(task["id"], 109, lease_seconds=100))
        self.assertEqual(self.store.get(task["id"])["lease_until"], 209)
        self.store.recover(120, stale_seconds=0)
        self.assertEqual(self.store.get(task["id"])["state"], "generating")
        self.assertFalse(self.store.renew("missing", 120))
        self.assertFalse(self.store.renew(task["id"], 209))
        self.assertFalse(self.store.update(task["id"], "generating", 209, state="success"))
        self.store.recover(209, stale_seconds=0)
        self.assertFalse(self.store.renew(task["id"], 210))
        draft = self.reserve("draft", now=211)
        self.store.update(draft["id"], "generating", 212, state="draft")
        self.assertFalse(self.store.renew(draft["id"], 213))

    def test_retry_count_fence_blocks_stale_runner_renew_and_update(self):
        old = self.reserve(lease_seconds=10)
        self.assertEqual(old["retry_count"], 0)
        self.assertTrue(self.store.renew(old["id"], 101, 9, expected_retry_count=0))
        self.assertTrue(self.store.update(old["id"], "generating", 102,
                                          expected_retry_count=0, error="in_progress"))
        self.store.recover(110, stale_seconds=0)
        self.assertEqual(self.store.get(old["id"])["state"], "failed")
        new = self.retry(old, now=110, lease_seconds=20)
        self.assertEqual(new["retry_count"], 1)
        other = Store(self.path)
        self.assertFalse(other.renew(old["id"], 111, 900, expected_retry_count=old["retry_count"]))
        self.assertFalse(other.update(old["id"], "generating", 111,
                                      expected_retry_count=old["retry_count"], state="failed", error="stale_failure"))
        self.assertFalse(other.update(old["id"], "generating", 111,
                                      expected_retry_count=old["retry_count"], state="draft", caption="stale_caption"))
        self.assertEqual(self.store.get(new["id"]), new)
        self.assertTrue(other.renew(new["id"], 112, 30, expected_retry_count=new["retry_count"]))
        self.assertEqual(self.store.get(new["id"])["lease_until"], 142)
        self.assertTrue(other.update(new["id"], "generating", 113,
                                     expected_retry_count=new["retry_count"], state="draft", caption="new_caption"))
        draft = self.store.get(new["id"])
        self.assertFalse(other.update(old["id"], "draft", 114,
                                      expected_retry_count=0, state="submitting"))
        self.assertEqual(self.store.get(new["id"]), draft)
        self.assertEqual(draft["submission_started"], 0)
        self.assertTrue(other.update(new["id"], "draft", 114,
                                     expected_retry_count=1, state="submitting"))
        self.assertEqual(self.store.get(new["id"])["submission_started"], 1)
        self.assertFalse(other.renew(old["id"], 115, expected_retry_count=0))
        self.assertTrue(other.renew(new["id"], 115, expected_retry_count=1))
        self.assertEqual(self.store.attempts(self.day), 2)

    def test_retry_count_fence_none_preserves_unfenced_update_and_renew(self):
        old = self.reserve(lease_seconds=10)
        self.store.recover(110, stale_seconds=0)
        new = self.retry(old, now=110)
        self.assertFalse(self.store.renew(new["id"], 111, expected_retry_count=2))
        self.assertFalse(self.store.update(new["id"], "generating", 111,
                                           expected_retry_count=2, state="failed"))
        self.assertEqual(self.store.get(new["id"]), new)
        self.assertTrue(self.store.renew(new["id"], 111, 20, expected_retry_count=None))
        self.assertTrue(self.store.update(new["id"], "generating", 112,
                                          expected_retry_count=None, error="compatible"))
        self.assertTrue(self.store.renew(new["id"], 113, 20))
        self.assertTrue(self.store.update(new["id"], "generating", 114, state="failed"))
        self.assertEqual(self.store.get(new["id"])["retry_count"], 1)
        self.assertEqual(self.store.get(new["id"])["error"], "compatible")

    def test_skip_deduplicates_without_attempt_or_cooldown_charge(self):
        self.store.skip("skip", {"x": 1}, 100, "not_selected")
        task = self.store.latest()[0]
        self.assertEqual(task["state"], "skipped")
        self.assertEqual(task["mode"], "skip")
        self.assertEqual(task["error"], "not_selected")
        self.assertEqual(self.store.attempts(task["day"]), 0)
        self.assertEqual(self.store.attempts(self.day), 0)
        self.store.skip("skip", {"x": 2}, 101, "another_reason")
        self.assertEqual(self.store.get(task["id"])["snapshot"], {"x": 1})
        self.assert_rejected("duplicate", lambda: self.reserve("skip"))
        active = self.reserve("new", now=101, cooldown_seconds=9000)
        self.store.skip("new", {}, 102, "not_selected")
        self.assertEqual(self.store.get(active["id"])["state"], "generating")

    def test_expire_drafts_returns_changed_records_only(self):
        old = self.reserve()
        self.store.update(old["id"], "generating", 101, state="draft")
        recent = self.reserve("recent", now=150)
        self.store.update(recent["id"], "generating", 151, state="draft")
        active = self.reserve("active", now=170)
        expired = self.store.expire_drafts(now=201, ttl_seconds=100)
        self.assertEqual([row["id"] for row in expired], [old["id"]])
        self.assertEqual(expired[0]["state"], "skipped")
        self.assertEqual(expired[0]["error"], "draft_expired")
        self.assertEqual(expired[0]["updated_at"], 201)
        self.assertEqual(self.store.get(recent["id"])["state"], "draft")
        self.assertEqual(self.store.get(active["id"])["state"], "generating")
        self.assertEqual(self.store.expire_drafts(201, 100), [])
        self.assertEqual(self.store.attempts(self.day), 3)
        self.assert_rejected("duplicate", lambda: self.reserve(now=202))

    def test_prune_preserves_seen_ledger_and_cooldown(self):
        task = self.reserve(daily_limit=1)
        self.store.update(task["id"], "generating", 101, state="failed")
        self.store.prune(200, retention_seconds=10)
        reopened = Store(self.path)
        self.assertIsNone(reopened.get(task["id"]))
        self.assertEqual(reopened.attempts(self.day), 1)
        self.assert_rejected("duplicate", lambda: reopened.reserve("activity", {}, 201, self.day, 10, 0))
        self.assert_rejected("daily_limit", lambda: reopened.reserve("next", {}, 201, self.day, 1, 0))
        self.assert_rejected("cooldown", lambda: reopened.reserve("next", {}, 201, "2026-11-09", 10, 1000))
        reopened.skip("activity", {}, 202, "not_selected")
        self.assertEqual(reopened.latest(), [])

    def test_prune_only_old_safe_terminal_states(self):
        retained = []
        deleted = []
        for index, state in enumerate(("success", "failed", "skipped", "uncertain", "draft")):
            task = self.reserve(str(index), now=100 + index * 2)
            self.store.update(task["id"], "generating", 101 + index * 2, state=state)
            (retained if state in ("uncertain", "draft") else deleted).append(task["id"])
        active = self.reserve("active", now=120, lease_seconds=10000)
        retained.append(active["id"])
        self.store.skip("recent", {}, 999, "not_selected")
        recent = self.store.latest()[0]["id"]
        retained.append(recent)
        self.store.prune(1000, retention_seconds=100)
        for id in deleted:
            self.assertIsNone(self.store.get(id))
        for id in retained:
            self.assertIsNotNone(self.store.get(id))

    def test_for_activity_decodes_snapshot_and_missing_or_pruned_returns_none(self):
        self.assertIsNone(self.store.for_activity("missing"))
        task = self.reserve()
        self.assertEqual(Store(self.path).for_activity("activity"), task)
        found = self.store.for_activity("activity")
        found["snapshot"]["nested"]["items"].append(99)
        self.assertEqual(self.store.for_activity("activity")["snapshot"]["nested"]["items"], [1, 2])
        self.store.update(task["id"], "generating", 101, state="failed")
        self.store.prune(200, 10)
        self.assertIsNone(self.store.for_activity("activity"))
        self.assert_rejected("not_found", lambda: self.retry(task, now=201))
        self.assert_rejected("duplicate", lambda: self.reserve(now=201))

    def test_retry_reuses_task_and_preserves_all_attempts_and_seen_markers(self):
        task = self.reserve()
        self.store.remember_auto("activity")
        self.store.update(task["id"], "generating", 101, state="failed", error="generation_failed",
                          caption="", image_path="", tid="")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            original = connection.execute("SELECT * FROM attempt_ledger").fetchall()
        retried = self.retry(task, now=200, day="2026-11-09", lease_seconds=17)
        self.assertEqual(retried["id"], task["id"])
        self.assertEqual(retried["activity_key"], "activity")
        self.assertEqual(retried["mode"], "retry")
        self.assertEqual(retried["state"], "generating")
        self.assertEqual(retried["retry_count"], 1)
        self.assertEqual(retried["submission_started"], 0)
        self.assertEqual(retried["created_at"], 200)
        self.assertEqual(retried["updated_at"], 200)
        self.assertEqual(retried["day"], "2026-11-09")
        self.assertEqual(retried["lease_until"], 217)
        self.assertEqual(retried["snapshot"], {"title": "重试散步", "nested": {"items": [3]}})
        for field in ("error", "caption", "image_path", "tid"):
            self.assertIsNone(retried[field])
        self.assertEqual(Store(self.path).get(task["id"]), retried)
        self.assertEqual(self.store.for_activity("activity"), retried)
        self.assertEqual(len(self.store.latest()), 1)
        self.assertEqual(self.store.attempts(self.day), 1)
        self.assertEqual(self.store.attempts("2026-11-09"), 1)
        self.assertTrue(self.store.auto_seen("activity"))
        self.assert_rejected("duplicate", lambda: self.reserve(now=201))
        self.assert_rejected("duplicate", lambda: self.reserve(now=201, mode="manual"))
        self.store.update(task["id"], "generating", 201, state="failed", error="again")
        self.assert_rejected("duplicate", lambda: self.reserve(now=202))
        second = self.retry(task, now=202, day="2026-11-09")
        self.assertEqual(second["retry_count"], 2)
        self.assertEqual(second["lease_until"], 1102)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            ledger = connection.execute("SELECT * FROM attempt_ledger ORDER BY attempted_at").fetchall()
            self.assertEqual(connection.execute("SELECT * FROM seen").fetchall(), [("activity", task["id"])])
        self.assertEqual(ledger[:1], original)
        self.assertEqual(len(ledger), 3)
        self.assertEqual(len({row[0] for row in ledger}), 3)
        for entry_id, _, _ in ledger:
            self.assertEqual(UUID(hex=entry_id).hex, entry_id)
        self.store.update(task["id"], "generating", 203, state="failed")
        self.store.prune(300, 10)
        self.assertEqual(self.store.attempts(self.day), 1)
        self.assertEqual(self.store.attempts("2026-11-09"), 2)
        self.assertTrue(self.store.auto_seen("activity"))
        self.assert_rejected("duplicate", lambda: self.reserve(now=301))

    def test_retry_obeys_quota_and_global_cooldown_and_charges_each_failure(self):
        task = self.reserve(daily_limit=1)
        self.store.update(task["id"], "generating", 101, state="failed")
        self.assert_rejected("daily_limit", lambda: self.retry(task, daily_limit=1))
        self.assert_rejected("cooldown", lambda: self.retry(task, now=129, day="2026-11-09",
                                                          daily_limit=1, cooldown_seconds=30))
        self.assertEqual(self.store.attempts(self.day), 1)
        self.assertEqual(self.store.attempts("2026-11-09"), 0)
        self.retry(task, now=130, day="2026-11-09", daily_limit=1, cooldown_seconds=30)
        self.store.update(task["id"], "generating", 131, state="failed")
        self.assert_rejected("daily_limit", lambda: self.retry(task, now=132, day="2026-11-09", daily_limit=1))
        self.assert_rejected("cooldown", lambda: self.retry(task, now=159, day="2026-11-09",
                                                          daily_limit=2, cooldown_seconds=30))
        self.assert_rejected("cooldown", lambda: self.reserve("next", now=159, cooldown_seconds=30))
        self.retry(task, now=160, day="2026-11-09", daily_limit=2, cooldown_seconds=30)
        self.store.update(task["id"], "generating", 161, state="failed")
        self.assert_rejected("daily_limit", lambda: self.retry(task, now=200, day="2026-11-09", daily_limit=2))
        self.assertEqual(self.store.attempts("2026-11-09"), 2)
        self.assertEqual(self.store.get(task["id"])["retry_count"], 2)

    def test_retry_rejects_missing_or_changed_activity_without_charging(self):
        task = self.reserve()
        self.store.update(task["id"], "generating", 101, state="failed")
        saved = self.store.get(task["id"])
        self.assert_rejected("not_found", lambda: self.store.retry("missing", "activity", {}, 102, self.day, 10, 0))
        self.assert_rejected("activity_changed", lambda: self.store.retry(task["id"], "changed", {}, 102, self.day, 10, 0))
        self.assertEqual(self.store.get(task["id"]), saved)
        self.assertEqual(self.store.attempts(self.day), 1)
        self.assertIsNone(self.store.for_activity("changed"))

    def test_retry_rejects_every_nonfailed_state(self):
        for index, state in enumerate(("success", "draft", "skipped", "generating", "submitting", "uncertain")):
            with self.subTest(state=state):
                now = 100 + index * 10
                task = self.reserve(state, now=now)
                if state != "generating":
                    self.store.update(task["id"], "generating", now + 1, state=state)
                self.assert_rejected("not_retryable", lambda: self.retry(task, now=now + 2))
                saved = self.store.get(task["id"])
                self.assertEqual(saved["state"], state)
                self.assertEqual(saved["retry_count"], 0)
                if state in ("generating", "submitting"):
                    self.store.update(task["id"], state, now + 3, state="failed")
        self.assertEqual(self.store.attempts(self.day), 6)

    def test_retry_rejects_any_saved_posting_artifact_even_without_flag(self):
        for index, field in enumerate(("caption", "image_path", "tid")):
            with self.subTest(field=field):
                task = self.reserve(field, now=100 + index * 10)
                self.store.update(task["id"], "generating", 101 + index * 10, state="failed", **{field: " "})
                self.assertEqual(self.store.get(task["id"])["submission_started"], 0)
                self.assert_rejected("not_retryable", lambda: self.retry(task, now=102 + index * 10))
        self.assertEqual(self.store.attempts(self.day), 3)

    def test_submission_started_is_persistent_for_both_submission_paths(self):
        for index, use_begin in enumerate((False, True)):
            with self.subTest(begin_submission=use_begin):
                now = 100 + index * 10
                task = self.reserve(str(index), now=now)
                self.store.update(task["id"], "generating", now + 1, state="draft")
                if use_begin:
                    self.assertTrue(self.store.begin_submission(task["id"], now + 2))
                else:
                    self.assertTrue(self.store.update(task["id"], "draft", now + 2, state="submitting"))
                self.assertEqual(Store(self.path).get(task["id"])["submission_started"], 1)
                self.store.update(task["id"], "submitting", now + 3, state="failed",
                                  caption="", image_path="", tid="", error="post_failed")
                self.assertEqual(Store(self.path).get(task["id"])["submission_started"], 1)
                self.assert_rejected("not_retryable", lambda: self.retry(task, now=now + 4))
        self.assertEqual(self.store.attempts(self.day), 2)

    def test_busy_submission_failure_does_not_set_submission_flag(self):
        draft = self.reserve("draft")
        self.store.update(draft["id"], "generating", 101, state="draft")
        active = self.reserve("active", now=102)
        self.assertFalse(self.store.begin_submission(draft["id"], 103))
        self.assertFalse(self.store.update(draft["id"], "draft", 103, state="submitting"))
        self.assertEqual(self.store.get(draft["id"])["submission_started"], 0)
        self.assertEqual(self.store.get(active["id"])["submission_started"], 0)

    def test_retry_respects_global_busy_generation_and_submission(self):
        task = self.reserve()
        self.store.update(task["id"], "generating", 101, state="failed")
        active = self.reserve("other", now=102)
        self.assert_rejected("busy", lambda: self.retry(task, now=103))
        self.store.update(active["id"], "generating", 104, state="submitting")
        self.assert_rejected("busy", lambda: self.retry(task, now=105))
        self.assertEqual(self.store.get(task["id"])["retry_count"], 0)
        self.assertEqual(self.store.attempts(self.day), 2)
        self.store.update(active["id"], "submitting", 106, state="failed")
        self.assertEqual(self.retry(task, now=107)["retry_count"], 1)

    def test_retry_recovers_own_expired_generation_but_never_uncertain_submission(self):
        task = self.reserve(lease_seconds=10)
        self.assert_rejected("not_retryable", lambda: self.retry(task, now=109))
        self.assertEqual(self.retry(task, now=110)["state"], "generating")
        self.store.update(task["id"], "generating", 111, state="submitting")
        self.store.renew(task["id"], 112, lease_seconds=10)
        self.assert_rejected("not_retryable", lambda: self.retry(task, now=122))
        saved = self.store.get(task["id"])
        self.assertEqual(saved["state"], "uncertain")
        self.assertEqual(saved["error"], "submission_uncertain")
        self.assertEqual(saved["submission_started"], 1)
        self.assertEqual(saved["retry_count"], 1)
        self.assertEqual(self.store.attempts(self.day), 2)

    def test_denied_retry_still_commits_recovery_for_each_rejection(self):
        for index, reason in enumerate(("not_found", "activity_changed", "not_retryable", "daily_limit", "cooldown")):
            with self.subTest(reason=reason):
                now = 100 + index * 100
                task = self.reserve(str(index), now=now, lease_seconds=10)
                if reason == "not_retryable":
                    self.store.update(task["id"], "generating", now + 1, image_path="old/post.png")
                job_id = "missing" if reason == "not_found" else task["id"]
                key = "changed" if reason == "activity_changed" else task["activity_key"]
                self.assert_rejected(reason, lambda: self.store.retry(
                    job_id, key, {}, now + 10, self.day, 0 if reason == "daily_limit" else 10,
                    30 if reason == "cooldown" else 0))
                saved = self.store.get(task["id"])
                self.assertEqual(saved["state"], "failed")
                self.assertEqual(saved["error"], "lease_expired")
                self.assertEqual(saved["retry_count"], 0)
        self.assertEqual(self.store.attempts(self.day), 5)

    def test_concurrent_retry_same_task_has_one_winner_and_one_new_attempt(self):
        task = self.reserve()
        self.store.update(task["id"], "generating", 101, state="failed")
        stores = [Store(self.path), Store(self.path)]
        barrier = Barrier(2)

        def claim(store):
            barrier.wait(timeout=5)
            try:
                return store.retry(task["id"], "activity", {}, 102, self.day, 10, 0)
            except ClaimError as error:
                return error.reason

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, stores))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual([result for result in results if isinstance(result, str)], ["not_retryable"])
        self.assertEqual(self.store.attempts(self.day), 2)
        self.assertEqual(self.store.get(task["id"])["retry_count"], 1)
        self.assertEqual(len(self.store.latest()), 1)

    def test_retry_races_with_reserve_or_submission_for_one_global_lease(self):
        for index, submission in enumerate((False, True)):
            with self.subTest(submission=submission):
                now = 100 + index * 100
                task = self.reserve(f"failed-{index}", now=now)
                self.store.update(task["id"], "generating", now + 1, state="failed")
                draft = self.reserve(f"draft-{index}", now=now + 2)
                self.store.update(draft["id"], "generating", now + 3, state="draft")
                before = self.store.attempts(self.day)
                stores = [Store(self.path), Store(self.path)]
                barrier = Barrier(2)

                def claim(worker):
                    barrier.wait(timeout=5)
                    try:
                        if worker == 0:
                            stores[worker].retry(task["id"], task["activity_key"], {}, now + 4, self.day, 10, 0)
                            return True
                        if submission:
                            return stores[worker].begin_submission(draft["id"], now + 4)
                        stores[worker].reserve(f"other-{index}", {}, now + 4, self.day, 10, 0)
                        return True
                    except ClaimError as error:
                        self.assertEqual(error.reason, "busy")
                        return False

                with ThreadPoolExecutor(max_workers=2) as executor:
                    self.assertEqual(sorted(executor.map(claim, range(2))), [False, True])
                active = [row for row in self.store.latest(20) if row["state"] in ("generating", "submitting")]
                self.assertEqual(len(active), 1)
                extra = 0 if active[0]["id"] == draft["id"] else 1
                self.assertEqual(self.store.attempts(self.day), before + extra)
                self.store.update(active[0]["id"], active[0]["state"], now + 5, state="failed")

    def test_retry_rejects_invalid_limits_without_mutating_task(self):
        task = self.reserve()
        self.store.update(task["id"], "generating", 101, state="failed")
        saved = self.store.get(task["id"])
        for options in ({"lease_seconds": 0}, {"lease_seconds": -1},
                        {"cooldown_seconds": -1}, {"daily_limit": -1}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "invalid_reservation"):
                self.retry(task, **options)
        self.assertEqual(self.store.get(task["id"]), saved)
        self.assertEqual(self.store.attempts(self.day), 1)

    def create_legacy_database(self, path, existing_column=None):
        # Deliberately use pre-migration DDL, rather than Store to build a new DB.
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, activity_key TEXT NOT NULL UNIQUE,
                    snapshot TEXT NOT NULL, mode TEXT NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, day TEXT NOT NULL,
                    caption TEXT, image_path TEXT, tid TEXT, error TEXT, lease_until REAL
                )
            """)
            if existing_column is not None:
                connection.execute(f"ALTER TABLE tasks ADD COLUMN {existing_column} INTEGER NOT NULL DEFAULT 0")
            connection.execute("CREATE TABLE seen (activity_key TEXT PRIMARY KEY, task_id TEXT NOT NULL)")
            connection.execute("CREATE TABLE auto_seen (activity_key TEXT PRIMARY KEY)")
            connection.execute("CREATE TABLE attempt_ledger (task_id TEXT PRIMARY KEY, day TEXT NOT NULL, attempted_at REAL NOT NULL)")
            for job_id, state, image_path, lease in (
                    ("old-generating", "generating", None, 110),
                    ("old-post-failed", "failed", "old-post.png", None),
                    ("old-uncertain", "uncertain", None, None),
                    ("old-generation-failed", "failed", None, None)):
                connection.execute("""
                    INSERT INTO tasks (id, activity_key, snapshot, mode, state, created_at,
                                       updated_at, day, image_path, error, lease_until)
                    VALUES (?, ?, '{}', 'auto', ?, 100, 101, ?, ?, 'old_failure', ?)
                """, (job_id, job_id, state, self.day, image_path, lease))
                connection.execute("INSERT INTO seen VALUES (?, ?)", (job_id, job_id))
                connection.execute("INSERT INTO auto_seen VALUES (?)", (job_id,))
                connection.execute("INSERT INTO attempt_ledger VALUES (?, ?, 100)", (job_id, self.day))

    def test_old_database_migration_allows_generation_retry_but_not_old_post_failure(self):
        path = Path(self.directory.name) / "legacy.sqlite3"
        self.create_legacy_database(path)
        store = Store(path)
        with closing(sqlite3.connect(path)) as connection, connection:
            columns = {row[1]: row for row in connection.execute("PRAGMA table_info(tasks)")}
        for name in ("submission_started", "retry_count"):
            self.assertEqual(columns[name][2], "INTEGER")
            self.assertEqual(columns[name][3], 1)
            self.assertEqual(columns[name][4], "0")
        for row in store.latest(10):
            self.assertEqual(row["submission_started"], 0)
            self.assertEqual(row["retry_count"], 0)
        retried = store.retry("old-generating", "old-generating", {"new": True}, 110, self.day, 10, 0)
        self.assertEqual(retried["state"], "generating")
        self.assertEqual(retried["retry_count"], 1)
        self.assertEqual(retried["id"], "old-generating")
        for job_id in ("old-post-failed", "old-uncertain"):
            self.assert_rejected("not_retryable", lambda: store.retry(job_id, job_id, {}, 111, self.day, 10, 0))
            self.assertEqual(store.get(job_id)["submission_started"], 0)
        self.assertEqual(store.get("old-post-failed")["image_path"], "old-post.png")
        self.assertEqual(store.get("old-uncertain")["state"], "uncertain")
        store.update("old-generating", "generating", 112, state="failed")
        retried = store.retry("old-generation-failed", "old-generation-failed", {}, 113, self.day, 10, 0)
        self.assertEqual(retried["retry_count"], 1)
        self.assertEqual(store.attempts(self.day), 6)
        self.assertEqual(Store(path).get("old-generation-failed"), retried)
        for job_id in ("old-generating", "old-generation-failed", "old-post-failed", "old-uncertain"):
            self.assertTrue(store.auto_seen(job_id))
            self.assert_rejected("duplicate", lambda: store.reserve(job_id, {}, 114, self.day, 10, 0))
        with closing(sqlite3.connect(path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM seen").fetchone()[0], 4)
            old_entries = connection.execute("SELECT COUNT(*) FROM attempt_ledger WHERE attempted_at = 100").fetchone()[0]
            self.assertEqual(old_entries, 4)

    def test_partial_migrations_are_idempotent_and_preserve_existing_values(self):
        for name, value in (("submission_started", 1), ("retry_count", 3)):
            with self.subTest(existing_column=name):
                path = Path(self.directory.name) / f"partial-{name}.sqlite3"
                self.create_legacy_database(path, existing_column=name)
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute(f"UPDATE tasks SET {name} = ? WHERE id = 'old-generation-failed'", (value,))
                first = Store(path)
                reopened = Store(path)
                saved = reopened.get("old-generation-failed")
                self.assertEqual(first.get("old-generation-failed"), saved)
                self.assertEqual(saved[name], value)
                if name == "submission_started":
                    self.assert_rejected("not_retryable", lambda: reopened.retry(
                        "old-generation-failed", "old-generation-failed", {}, 110, self.day, 10, 0))
                else:
                    retried = reopened.retry("old-generation-failed", "old-generation-failed", {}, 110, self.day, 10, 0)
                    self.assertEqual(retried["retry_count"], 4)

    def test_latest_limit_and_ordering(self):
        for index in range(7):
            self.store.skip(str(index), {}, 100 + index, "not_selected")
        self.assertEqual([task["activity_key"] for task in self.store.latest()], ["6", "5", "4", "3", "2"])
        self.assertEqual(len(self.store.latest(2)), 2)
        self.assertEqual(self.store.latest(0), [])


if __name__ == "__main__":
    unittest.main()
