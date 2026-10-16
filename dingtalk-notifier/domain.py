"""Shared bridge values; no network calls or credential access."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
MINIMUM_INTERVALS = {'rollcall_watch': 30, 'todo_watch': 60}


@dataclass(frozen=True)
class Task:
    id: str
    course: str
    title: str
    deadline: datetime | None
    source: str = 'lazy'
    submitted: bool | None = None

    @property
    def reminder_key(self):
        stamp = self.deadline.isoformat() if self.deadline else 'none'
        return f'{self.source}:{self.id}:{stamp}'


def parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
