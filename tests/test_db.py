"""Schema migration + query helpers."""
import db
import planner as P


def test_migration_adds_columns():
    # init_db ran via fixture; new columns must be queryable.
    iid = db.add_item(type="task", title="x", status="open",
                      created_at=P.now().isoformat(), effort_min=120)
    row = db.get_item(iid)
    assert "remaining_effort_min" in row.keys()
    assert "actual_effort_min" in row.keys()
    assert "last_checkin_at" in row.keys()
    assert "waiting_since" in row.keys()


def test_remaining_defaults_to_effort():
    iid = db.add_item(type="task", title="x", status="open",
                      created_at=P.now().isoformat(), effort_min=90)
    assert db.get_item(iid)["remaining_effort_min"] == 90


def test_schedulable_open_filters():
    now = P.now().isoformat()
    db.add_item(type="task", title="has due", status="open", created_at=now,
                due_at=now, effort_min=60)
    db.add_item(type="idea", title="no due", status="open", created_at=now)
    db.add_item(type="task", title="no due task", status="open", created_at=now)
    rows = db.schedulable_open()
    titles = {r["title"] for r in rows}
    assert titles == {"has due"}


def test_list_waiting():
    now = P.now().isoformat()
    db.add_item(type="waiting", title="api key", status="open", created_at=now,
                person="Sneha", waiting_since=now)
    db.add_item(type="task", title="other", status="open", created_at=now)
    rows = db.list_waiting()
    assert len(rows) == 1 and rows[0]["title"] == "api key"


def test_missed_before():
    past = (P.now().replace(year=2020)).isoformat()
    future = (P.now().replace(year=2030)).isoformat()
    db.add_item(type="commitment", title="late", status="open", created_at=past, due_at=past)
    db.add_item(type="task", title="future", status="open", created_at=past, due_at=future)
    rows = db.missed_before(P.now().isoformat())
    assert [r["title"] for r in rows] == ["late"]


def test_checkin_record():
    now = P.now().isoformat()
    iid = db.add_item(type="task", title="x", status="open", created_at=now, effort_min=120)
    db.add_checkin(iid, now, 120, 60)
    # no helper to read back, but the insert must not raise and item still queryable
    assert db.get_item(iid) is not None
