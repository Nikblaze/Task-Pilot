"""Date math: timezone, working-hour snapping, and backward planning.

The core idea you asked for lives in compute_start_by(): given a deadline and an
effort estimate, work backwards to the latest safe day to *start*.
"""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil import parser as dateparser

TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
WORK_START = int(os.getenv("WORK_START_HOUR", "9"))    # earliest a reminder fires
WORK_END = int(os.getenv("WORK_END_HOUR", "21"))       # latest a reminder fires
BUFFER_DAYS = float(os.getenv("BUFFER_DAYS", "1"))     # safety runway beyond effort
DEFAULT_EFFORT_MIN = int(os.getenv("DEFAULT_EFFORT_MIN", "60"))


def now():
    return datetime.now(TZ)


def parse_iso(s):
    if not s:
        return None
    try:
        dt = dateparser.isoparse(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def to_iso(dt):
    return dt.astimezone(TZ).isoformat() if dt else None


def snap_to_work_hours(dt):
    """Don't fire reminders at 3am. Pull them into WORK_START..WORK_END."""
    if dt.hour < WORK_START:
        return dt.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
    if dt.hour >= WORK_END:
        nxt = dt + timedelta(days=1)
        return nxt.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
    return dt.replace(second=0, microsecond=0)


def compute_start_by(due_at, effort_min):
    """start_by = due - effort - safety buffer, snapped to working hours.

    If that lands in the past, start as soon as reasonable (10 min from now).
    """
    if not due_at:
        return None
    effort = effort_min or DEFAULT_EFFORT_MIN
    start = due_at - timedelta(minutes=effort) - timedelta(days=BUFFER_DAYS)
    start = snap_to_work_hours(start)
    if start <= now():
        start = now() + timedelta(minutes=10)
    return start


def fmt(dt):
    if not dt:
        return ""
    return dt.strftime("%a %d %b · %I:%M %p").replace(" 0", " ")
