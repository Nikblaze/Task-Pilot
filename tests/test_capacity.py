"""Pure capacity math + effort parsing — no DB, no network."""
from datetime import date, timedelta

import capacity as C
import planner as P


def _item(due, start_by=None, effort=None, remaining=None):
    return {
        "due_at": due, "start_by_at": start_by,
        "effort_min": effort, "remaining_effort_min": remaining,
    }


def iso(d, h=18):
    return f"{d.isoformat()}T{h:02d}:00:00"


WD = "0,1,2,3,4"  # Mon-Fri


def test_parse_work_days():
    assert C.parse_work_days("0,1,2,3,4") == {0, 1, 2, 3, 4}
    assert C.parse_work_days("") == {0, 1, 2, 3, 4}
    assert C.parse_work_days("5,6") == {5, 6}


def test_single_item_no_window_lands_on_due():
    # No start_by -> whole effort on the due date.
    d = date(2026, 6, 15)  # Monday
    load = C.spread_load([_item(iso(d), effort=120)], today=d, work_days=C.parse_work_days(WD))
    assert round(load[d]) == 120


def test_spread_across_working_days():
    # 6h effort, window Mon..Wed (3 working days) -> 2h each.
    start = date(2026, 6, 15)  # Mon
    due = date(2026, 6, 17)    # Wed
    it = _item(iso(due), start_by=iso(start, 9), effort=360)
    load = C.spread_load([it], today=start, work_days=C.parse_work_days(WD))
    assert round(load[date(2026, 6, 15)]) == 120
    assert round(load[date(2026, 6, 16)]) == 120
    assert round(load[date(2026, 6, 17)]) == 120


def test_weekend_skipped_in_spread():
    # Window Fri..Mon: Sat+Sun skipped -> only Fri & Mon get load.
    start = date(2026, 6, 12)  # Friday
    due = date(2026, 6, 15)    # Monday
    it = _item(iso(due), start_by=iso(start, 9), effort=240)
    load = C.spread_load([it], today=start, work_days=C.parse_work_days(WD))
    assert date(2026, 6, 13) not in load  # Sat
    assert date(2026, 6, 14) not in load  # Sun
    assert round(load[date(2026, 6, 12)]) == 120
    assert round(load[date(2026, 6, 15)]) == 120


def test_remaining_overrides_estimate():
    d = date(2026, 6, 15)
    it = _item(iso(d), effort=480, remaining=60)
    load = C.spread_load([it], today=d, work_days=C.parse_work_days(WD))
    assert round(load[d]) == 60


def test_past_window_clamped_to_today():
    # start_by in the past; today is mid-window -> load only from today..due.
    today = date(2026, 6, 16)  # Tue
    start = date(2026, 6, 15)  # Mon (past)
    due = date(2026, 6, 17)    # Wed
    it = _item(iso(due), start_by=iso(start, 9), effort=240)
    load = C.spread_load([it], today=today, work_days=C.parse_work_days(WD))
    assert date(2026, 6, 15) not in load          # past day dropped
    assert round(load[date(2026, 6, 16)]) == 120  # 240 over Tue+Wed
    assert round(load[date(2026, 6, 17)]) == 120


def test_overflow_day_detected():
    d = date(2026, 6, 15)  # Monday
    items = [_item(iso(d), effort=480)]  # 8h on one day
    settings = {"cap_day_min": 360, "cap_week_min": 1800, "work_days": WD}
    over = C.check_overflow(items, today=d, settings=settings)
    assert over["overflow"] is True
    assert over["worst_day"][0] == d
    assert round(over["worst_day"][1]) == 480


def test_no_overflow_under_budget():
    d = date(2026, 6, 15)
    items = [_item(iso(d), effort=120)]
    settings = {"cap_day_min": 360, "cap_week_min": 1800, "work_days": WD}
    over = C.check_overflow(items, today=d, settings=settings)
    assert over["overflow"] is False
    assert C.overflow_warning(over) == ""


def test_overflow_week_detected():
    # Five items of 6h each in one week = 30h day-spread but week budget 20h.
    mon = date(2026, 6, 15)
    items = [_item(iso(mon + timedelta(days=i)), effort=360) for i in range(5)]
    settings = {"cap_day_min": 600, "cap_week_min": 1200, "work_days": WD}
    over = C.check_overflow(items, today=mon, settings=settings)
    assert over["overflow"] is True
    assert over["worst_week"] is not None


def test_focus_item_limits_warning_to_its_days():
    # Pre-existing overloaded Monday; new item only on Friday and under budget.
    mon = date(2026, 6, 15)
    fri = date(2026, 6, 19)
    pre = _item(iso(mon), effort=480)
    new = _item(iso(fri), effort=60)
    settings = {"cap_day_min": 360, "cap_week_min": 100000, "work_days": WD}
    over = C.check_overflow([pre, new], today=mon, settings=settings, focus_item=new)
    assert over["overflow"] is False  # Friday isn't overloaded; Monday ignored


def test_summary_text_shape():
    d = date(2026, 6, 15)
    items = [_item(iso(d), effort=120)]
    s = C.summary_text(items, today=d, settings={"work_days": WD})
    assert "Capacity" in s and "today" in s and "week" in s


def test_parse_effort_min():
    assert P.parse_effort_min("2h") == 120
    assert P.parse_effort_min("30m") == 30
    assert P.parse_effort_min("1h30") == 90
    assert P.parse_effort_min("90") == 90
    assert P.parse_effort_min("half day") == 240
    assert P.parse_effort_min("full day") == 480
    assert P.parse_effort_min("done") == 0
    assert P.parse_effort_min("none") == 0
    assert P.parse_effort_min("") is None
    assert P.parse_effort_min("gibberish") is None
