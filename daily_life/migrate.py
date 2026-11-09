"""One-time copy of task data from the pre-rename plugin directory.

The marketplace convention renames the plugin ID to github.<user>.<repo>, which
moves the Host data directory. Copying (never moving or deleting) preserves
deduplication, quotas and valid drafts so a rename cannot cause a repost.
"""
from __future__ import annotations

import contextlib
import shutil
from pathlib import Path

# Every ID this plugin shipped under before it was renamed again.
LEGACY_PLUGIN_IDS = (
    "local.maibot-daily-life",
    "github.username.maibot-daily-life",
    "github.tang-bai.maibot-daily-life",
)
DB_NAME = "daily_life.sqlite3"


def migrate_legacy_data(data_dir: Path) -> bool:
    """Copy legacy data once; returns True only when a migration happened."""
    target_dir = Path(data_dir)
    if (target_dir / DB_NAME).exists():
        return False
    candidates = []
    for plugin_id in LEGACY_PLUGIN_IDS:
        legacy = target_dir.parent / plugin_id
        if legacy != target_dir and (legacy / DB_NAME).is_file():
            candidates.append(legacy)
    if not candidates:
        return False
    # Prefer the most recently written legacy database if several rename steps exist.
    legacy_dir = max(candidates, key=lambda path: (path / DB_NAME).stat().st_mtime)
    sources = sorted(legacy_dir.glob(DB_NAME + "*"))
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        # Images first, database last: the database is the completion marker, so
        # an interrupted copy is retried on the next start instead of half-used.
        images = legacy_dir / "images"
        if images.is_dir():
            destination = target_dir / "images"
            destination.mkdir(parents=True, exist_ok=True)
            for item in images.glob("*.img"):
                if item.is_file() and not (destination / item.name).exists():
                    shutil.copy2(item, destination / item.name)
        for source in sources:
            temporary = target_dir / (source.name + ".migrating")
            with contextlib.suppress(OSError):
                temporary.unlink()
            shutil.copy2(source, temporary)
            temporary.replace(target_dir / source.name)
    except OSError:
        return False
    return True
