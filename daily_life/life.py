"""Strict read-only status interpretation and activity debounce."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

LIFE_API = "ilworkcn.maiweave.get_life_status"
KINDS = {
    "meal": "用餐", "work": "工作", "study": "学习", "leisure": "休闲",
    "travel": "出行", "rest": "休息", "social": "社交", "diary": "写日记", "sleep": "睡眠",
}


class LifeError(Exception):
    """Sanitized invalid/unavailable state error."""


def public_title(value: Any, kind: str) -> str:
    """Conservative privacy filter: omit titles resembling contact details or addresses.

    Raw location, next_plan and chat history are never sent to either provider.
    Operators still need to keep their activity names free of personal information.
    """
    if not isinstance(value, str):
        return KINDS[kind]
    value = " ".join(value.split())[:120]
    if re.search(r"https?://|@|[0-9０-９]{5,}|(?:路|街|巷|弄|小区|大厦|住址|地址|栋|单元|号楼)", value):
        return KINDS[kind]
    return value or KINDS[kind]


@dataclass(frozen=True)
class Activity:
    key: str
    snapshot: dict[str, Any]


def interpret(status: Any, now: float) -> Activity | None:
    if not isinstance(status, dict) or type(status.get("version")) is not int or status["version"] != 1:
        raise LifeError("MaiWeave 状态格式或版本不支持")
    if status.get("ready") is not True:
        raise LifeError("MaiWeave 尚未准备好")
    if status.get("enabled") is not True or status.get("sleeping") is not False:
        return None
    current = status.get("current")
    if current is None:
        return None
    if not isinstance(current, dict):
        raise LifeError("MaiWeave 当前活动格式错误")
    kind = current.get("kind")
    if kind == "sleep":
        return None
    if not isinstance(kind, str) or kind not in KINDS:
        raise LifeError("MaiWeave 活动类型不支持")
    aid = current.get("id")
    if not isinstance(aid, str) or not aid or len(aid) > 500:
        raise LifeError("MaiWeave 活动标识无效")
    try:
        start = datetime.fromisoformat(current["start"])
        end = datetime.fromisoformat(current["end"])
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError
        if not start.timestamp() <= now < end.timestamp():
            return None
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise LifeError("MaiWeave 活动时间无效") from exc
    key = hashlib.sha256(json.dumps([aid, start.isoformat()], ensure_ascii=False).encode()).hexdigest()
    mood = status.get("mood")
    if not isinstance(mood, (int, float)) or isinstance(mood, bool) or not math.isfinite(mood):
        mood = 0
    snapshot = {
        "kind": kind, "title": public_title(current.get("title"), kind),
        "mood": "愉快" if mood > 5 else "低落" if mood < -5 else "平静",
        "start": start.isoformat(), "end": end.isoformat(),
    }
    return Activity(key, snapshot)


class ActivityWatch:
    """First observation is a baseline; an activity is eligible at most once."""

    def __init__(self) -> None:
        self.initialized = False
        self.key: str | None = None
        self.since = 0.0
        self.consumed = True
        self.consumed_keys: set[str] = set()

    def reset(self) -> None:
        self.__init__()

    def gap(self) -> None:
        # A failed read breaks continuous stability, but does not establish a new baseline.
        self.since = float("inf")

    def observe(self, activity: Activity | None, mono: float, stable: float) -> bool:
        key = activity.key if activity else None
        if not self.initialized:
            self.initialized = True
            self.key = key
            self.consumed = True
            self.since = mono
            if key:
                self.consumed_keys.add(key)
            return False
        if key != self.key:
            self.key, self.since, self.consumed = key, mono, key in self.consumed_keys
        elif self.since == float("inf"):
            self.since = mono
        return bool(key and not self.consumed and mono - self.since >= stable)

    def consume(self) -> None:
        self.consumed = True
        if self.key:
            self.consumed_keys.add(self.key)
