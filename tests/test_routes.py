"""HTTP route smoke tests via FastAPI TestClient (scheduler stubbed)."""
import pytest
from fastapi.testclient import TestClient

import app
import db
import planner as P
from conftest import fake_capture


class _FakeScheduler:
    def __init__(self, *a, **k): pass
    def add_job(self, *a, **k): pass
    def start(self): pass
    def shutdown(self, *a, **k): pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, "AsyncIOScheduler", _FakeScheduler)
    sent = []

    async def fake_send(chat_id, text, buttons=None):
        sent.append({"chat_id": chat_id, "text": text})
        return {"ok": True}

    monkeypatch.setattr(app, "send_message", fake_send)
    with TestClient(app.app) as c:
        c._sent = sent
        yield c


def test_health(client):
    r = client.get("/")
    assert r.status_code == 200 and "running" in r.text.lower()


def test_webhook_bad_secret(client):
    r = client.post("/tg/wrongsecret", json={})
    assert r.status_code == 403


def test_webhook_capture_roundtrip(client, monkeypatch):
    monkeypatch.setattr(app.llm, "parse_capture",
                        fake_capture(type="task", title="webhook task"))
    payload = {"message": {"chat": {"id": 99}, "text": "do a webhook task"}}
    r = client.post("/tg/testsecret", json={**payload})
    assert r.status_code == 200
    assert any(i["title"] == "webhook task" for i in db.list_open())
    assert any("Captured" in m["text"] for m in client._sent)


def test_api_items_requires_token(client):
    assert client.get("/api/items").status_code == 403
    assert client.get("/api/items?token=testtoken").status_code == 200


def test_api_items_shape(client):
    now = P.now().isoformat()
    db.add_item(type="task", title="x", status="open", created_at=now,
                due_at=now, effort_min=120)
    data = client.get("/api/items?token=testtoken").json()
    assert "buckets" in data and "capacity" in data and "timelog" in data
    cap = data["capacity"]
    assert {"today_min", "week_min", "cap_day_min", "cap_week_min"} <= set(cap)
    assert "waiting" in data["buckets"]


def test_dashboard_page_served(client):
    r = client.get("/dashboard")
    assert r.status_code == 200 and "TaskPilot" in r.text
