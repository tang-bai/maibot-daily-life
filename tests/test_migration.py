"""Legacy data-directory migration must copy once, never move or overwrite."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from daily_life.migrate import DB_NAME, LEGACY_PLUGIN_IDS, migrate_legacy_data
from daily_life.store import Store


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.plugins = Path(self.folder.name) / "plugins"
        self.legacy = self.plugins / LEGACY_PLUGIN_IDS[0]
        self.target = self.plugins / "github.username.maibot-daily-life"
        self.legacy.mkdir(parents=True)
        store = Store(self.legacy / DB_NAME)
        store.reserve("activity-key", {"title": "读书"}, 100.0, "2026-11-09", 3, 0)
        store.close()
        (self.legacy / "images").mkdir()
        (self.legacy / "images" / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.img").write_bytes(b"png-bytes")

    def rows(self, path):
        with closing(sqlite3.connect(str(path))) as connection:
            return connection.execute("SELECT activity_key FROM tasks").fetchall()

    def test_copies_database_and_draft_images_once(self):
        self.assertTrue(migrate_legacy_data(self.target))
        self.assertEqual(self.rows(self.target / DB_NAME), [("activity-key",)])
        image = self.target / "images" / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.img"
        self.assertEqual(image.read_bytes(), b"png-bytes")
        self.assertTrue((self.legacy / DB_NAME).is_file())
        self.assertFalse(migrate_legacy_data(self.target))
        self.assertEqual(self.rows(self.target / DB_NAME), [("activity-key",)])

    def test_does_not_overwrite_existing_target_data(self):
        self.target.mkdir(parents=True)
        store = Store(self.target / DB_NAME)
        store.reserve("current-key", {"title": "散步"}, 200.0, "2026-11-09", 3, 0)
        store.close()
        self.assertFalse(migrate_legacy_data(self.target))
        self.assertEqual(self.rows(self.target / DB_NAME), [("current-key",)])

    def test_missing_or_identical_legacy_directory_is_a_no_op(self):
        # A directory with no legacy sibling is never migrated.
        unrelated = Path(self.folder.name) / "unrelated" / "fresh"
        self.assertFalse(migrate_legacy_data(unrelated))
        self.assertFalse((unrelated / DB_NAME).exists())
        self.assertFalse(migrate_legacy_data(self.legacy))

    def test_picks_the_newest_previous_identity_after_a_second_rename(self):
        import os
        second = self.plugins / LEGACY_PLUGIN_IDS[1]
        second.mkdir(parents=True)
        store = Store(second / DB_NAME)
        store.reserve("newer-key", {"title": "散步"}, 300.0, "2026-11-09", 3, 0)
        store.close()
        os.utime(second / DB_NAME, (4_000_000_000, 4_000_000_000))
        target = self.plugins / "github.realuser.maibot-daily-life"
        self.assertTrue(migrate_legacy_data(target))
        self.assertEqual(self.rows(target / DB_NAME), [("newer-key",)])

    def test_interrupted_copy_is_retried_without_partial_database(self):
        self.target.mkdir(parents=True)
        (self.target / (DB_NAME + ".migrating")).write_bytes(b"partial")
        self.assertTrue(migrate_legacy_data(self.target))
        self.assertFalse((self.target / (DB_NAME + ".migrating")).exists())
        self.assertEqual(self.rows(self.target / DB_NAME), [("activity-key",)])


if __name__ == "__main__":
    unittest.main()
