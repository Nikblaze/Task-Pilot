"""End-to-end-ish flow tests calling app coroutines with Telegram + LLM mocked."""
from datetime import timedelta

import pytest

import app
import db
import planner as P
from conftest import fake_capture


async def test_capture_task_stores_and_confirms(sent, owner, monkeypatch):
    monkeypatch.setattr(app.llm, "parse_capture",
                        fake_capture(type="task", title="ship report"))
    await app.handle_capture(owner, "ship the report")
    rows = db.list_open()
    assert len(rows) == 1 and rows[0]["title"] == "ship report"
    assert any("Captured" in m["text"] for m in sent)


async def test_capture_waiting_tracked(sent, owner, monkeypatch):
    monkeypatch.setattr(app.llm, "parse_capture",
                        fake_capture(type="waiting", title="api key", person="Sneha"))
    await app.handle_capture(owner, "waiting on Sneha for the api key")
    waits = db.list_waiting()
    assert len(waits) == 1
    assert waits[0]["waiting_since"] is not None
    assert any("waiting" in m["text"].lower() for m in sent)


async def test_capture_overflow_warns_but_saves(sent, owner, monkeypatch):
    # 8h commitment due today -> lands fully on one day, over the 6h day budget.
    due = P.now().replace(hour=23, minute=0, second=0, microsecond=0).isoformat()
    monkeypatch.setattr(app.llm, "parse_capture",
                        fake_capture(type="commitment", title="big launch",
                                     person="CEO", due=due, effort_minutes=480))
    await app.handle_capture(owner, "promise the big launch to CEO today, 8h")
    assert len(db.list_open()) == 1                       # still saved
    assert any("Capacity" in m["text"] for m in sent)     # and warned


async def test_briefing_has_capacity_and_risk(owner):
    now = P.now()
    past = (now - timedelta(hours=2)).isoformat()
    soon = (now + timedelta(hours=6)).isoformat()
    db.add_item(type="task", title="risky", status="open", created_at=past,
                due_at=soon, effort_min=120, start_by_at=past)
    text = app.build_briefing_text()
    assert "Capacity" in text
    assert "At risk" in text and "risky" in text


async def test_checkin_updates_remaining(sent, owner, monkeypatch):
    now = P.now()
    past = (now - timedelta(hours=1)).isoformat()
    soon = (now + timedelta(days=1)).isoformat()
    iid = db.add_item(type="task", title="in flight", status="open", created_at=past,
                      due_at=soon, effort_min=240, start_by_at=past)
    items = app.active_checkin_items()
    assert any(i["id"] == iid for i in items)
    # simulate "½ left" button tap
    await app.handle_callback(owner, "cb1", f"ck:50:{iid}")
    assert db.get_item(iid)["remaining_effort_min"] == 120


async def test_checkin_custom_sets_pending(sent, owner):
    now = P.now()
    iid = db.add_item(type="task", title="t", status="open",
                      created_at=now.isoformat(), due_at=now.isoformat(), effort_min=120)
    await app.handle_callback(owner, "cb2", f"ck:custom:{iid}")
    pend = db.get_pending(owner)
    assert pend and pend["action"] == "checkin_remaining" and pend["item_id"] == iid


async def test_done_logs_actual_effort(sent, owner):
    now = P.now()
    iid = db.add_item(type="task", title="t", status="open",
                      created_at=now.isoformat(), effort_min=120)
    await app.mark_done(owner, iid)
    row = db.get_item(iid)
    assert row["status"] == "done"
    assert row["actual_effort_min"] == 120
    assert row["remaining_effort_min"] == 0


async def test_weekly_review_done_vs_missed(owner):
    now = P.now()
    # a completed item this week
    iid = db.add_item(type="task", title="finished", status="open",
                      created_at=now.isoformat(), effort_min=60)
    db.update_item(iid, status="done", done_at=now.isoformat(), actual_effort_min=60)
    # a missed item (past due, still open)
    past = now.replace(year=2020).isoformat()
    db.add_item(type="commitment", title="late one", status="open",
                created_at=past, due_at=past)
    text = app.build_timelog_text()
    assert "completed" in text and "missed" in text
    assert "late one" in text


async def test_stale_waiting_nudges(sent, owner):
    old = (P.now() - timedelta(days=10)).isoformat()
    db.add_item(type="waiting", title="old wait", status="open", created_at=old,
                person="Raj", waiting_since=old)
    await app.reminder_tick()
    assert any("Still waiting" in m["text"] for m in sent)
