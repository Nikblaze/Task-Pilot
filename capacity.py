"""Capacity engine — Approach B (spread effort across the start→due window).

Pure functions over item dicts/rows. No DB, no network. An "item" only needs the
keys: effort_min, remaining_effort_min, start_by_at, due_at. Dates are parsed via
planner so the configured timezone is respected.

Load model
----------
Each open task/commitment with a deadline contributes its *remaining* effort, spread
evenly across the working days in [start_by .. due]. Past days are clipped to today,
so the load you still have to do shows up on the days you can still do it. With no
start_by (no window), the whole effort lands on the due date.

Budgets (minutes): cap_day_min, cap_week_min. Working days: work_days ("0,1,2,3,4").
"""
from datetime import timedelta

import planner as P

DEFAULT_CAP_DAY_MIN = 360      # 6h focus/day
DEFAULT_CAP_WEEK_MIN = 1800    # 30h focus/week
DEFAULT_WORK_DAYS = "0,1,2,3,4"  # Mon..Fri (Python weekday(): Mon=0)


def parse_work_days(s):
    out = set()
    for part in (s or DEFAULT_WORK_DAYS).split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part) % 7)
    return out or {0, 1, 2, 3, 4}


def _get(item, key):
    try:
        return item[key]
    except (KeyError, IndexError, TypeError):
        return item.get(key) if hasattr(item, "get") else None


def _effort_of(item):
    """Effort still to spend: remaining if known, else original estimate, else default."""
    for key in ("remaining_effort_min", "effort_min"):
        v = _get(item, key)
        if v:
            return int(v)
    return P.DEFAULT_EFFORT_MIN


def _working_days(start_date, end_date, work_days):
    """List of dates in [start_date, end_date] whose weekday is a working day."""
    if end_date < start_date:
        start_date = end_date
    days, d = [], start_date
    while d <= end_date:
        if d.weekday() in work_days:
            days.append(d)
        d += timedelta(days=1)
    return days


def item_daily_share(item, today, work_days):
    """Map {date: minutes} of this item's effort spread over its remaining window."""
    due = P.parse_iso(_get(item, "due_at"))
    if not due:
        return {}
    effort = _effort_of(item)
    due_date = due.date()
    start = P.parse_iso(_get(item, "start_by_at"))
    start_date = start.date() if start else due_date
    # Only count days you can still work: clamp window start to today, end to due.
    win_start = max(start_date, today)
    win_start = min(win_start, due_date)
    window = _working_days(win_start, due_date, work_days)
    if not window:
        # due date itself isn't a working day (or window empty) -> dump on due date
        window = [due_date]
    share = effort / len(window)
    return {d: share for d in window}


def spread_load(items, today, work_days):
    """Aggregate {date: total_minutes} across all items."""
    load = {}
    for it in items:
        for d, mins in item_daily_share(it, today, work_days).items():
            load[d] = load.get(d, 0) + mins
    return load


def week_key(d):
    iso = d.isocalendar()
    return (iso[0], iso[1])  # (ISO year, ISO week)


def week_load(load, d):
    wk = week_key(d)
    return sum(m for dd, m in load.items() if week_key(dd) == wk)


def _settings(s):
    s = s or {}
    return (
        int(s.get("cap_day_min", DEFAULT_CAP_DAY_MIN)),
        int(s.get("cap_week_min", DEFAULT_CAP_WEEK_MIN)),
        parse_work_days(s.get("work_days")),
    )


def check_overflow(items, today, settings, focus_item=None):
    """Return overflow info for the current item set.

    If focus_item is given (the just-captured one), only flag overflow on days/weeks
    that item actually contributes to — so we warn about *this* commitment, not a
    pre-existing overload.
    """
    cap_day, cap_week, wd = _settings(settings)
    load = spread_load(items, today, wd)

    focus_days = set()
    focus_weeks = set()
    if focus_item is not None:
        for d in item_daily_share(focus_item, today, wd):
            focus_days.add(d)
            focus_weeks.add(week_key(d))

    worst_day = worst_week = None
    for d, mins in load.items():
        if focus_item is not None and d not in focus_days:
            continue
        if mins > cap_day and (worst_day is None or mins > worst_day[1]):
            worst_day = (d, mins)
    seen_weeks = {}
    for d in load:
        wk = week_key(d)
        if wk in seen_weeks:
            continue
        seen_weeks[wk] = True
        if focus_item is not None and wk not in focus_weeks:
            continue
        wl = week_load(load, d)
        if wl > cap_week and (worst_week is None or wl > worst_week[1]):
            worst_week = (d, wl)

    return {
        "overflow": bool(worst_day or worst_week),
        "worst_day": worst_day,    # (date, minutes) or None
        "worst_week": worst_week,  # (date_in_week, minutes) or None
        "cap_day": cap_day,
        "cap_week": cap_week,
    }


def _hm(mins):
    mins = int(round(mins))
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h}h{m:02d}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def overflow_warning(over):
    """One-line warning string for a capture confirmation, or '' if no overflow."""
    if not over["overflow"]:
        return ""
    bits = []
    if over["worst_day"]:
        d, mins = over["worst_day"]
        bits.append(f"{d.strftime('%a %d %b')} now {_hm(mins)} / {_hm(over['cap_day'])} day budget")
    if over["worst_week"]:
        _, mins = over["worst_week"]
        bits.append(f"week now {_hm(mins)} / {_hm(over['cap_week'])} budget")
    return "⚠️ Capacity: " + "; ".join(bits) + ". Consider moving the deadline or dropping something."


def summary_text(items, today, settings):
    """Capacity line for the daily brief: today + this week's load vs budget."""
    cap_day, cap_week, wd = _settings(settings)
    load = spread_load(items, today, wd)
    today_load = load.get(today, 0)
    wk_load = week_load(load, today)
    flag_day = " ⚠️" if today_load > cap_day else ""
    flag_week = " ⚠️" if wk_load > cap_week else ""
    return (
        f"📊 Capacity: today {_hm(today_load)} / {_hm(cap_day)}{flag_day} · "
        f"week {_hm(wk_load)} / {_hm(cap_week)}{flag_week}"
    )
