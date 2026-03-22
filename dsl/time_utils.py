from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_day_start(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    return datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=timezone.utc)


def last_n_days_spans_utc(n: int) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    end_day = utc_day_start(now)
    start_day = end_day - timedelta(days=n - 1)
    return int(start_day.timestamp()), int(end_day.timestamp())
