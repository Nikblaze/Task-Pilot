"""Test fixtures. Env is set BEFORE importing app modules (they read env at import).

No real Telegram token or LLM key is set, so the network clients stay inert; tests
patch the LLM and the Telegram send functions explicitly.
"""
import os
import tempfile

# Deterministic config, before any project import.
os.environ.setdefault("TIMEZONE", "Asia/Kolkata")
os.environ.setdefault("WORK_START_HOUR", "9")
os.environ.setdefault("WORK_END_HOUR", "21")
os.environ.setdefault("BUFFER_DAYS", "1")
os.environ.setdefault("DEFAULT_EFFORT_MIN", "60")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("DASHBOARD_TOKEN", "testtoken")
_TMP = tempfile.mkdtemp(prefix="taskpilot-test-")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")

import pytest  # noqa: E402

import db  # noqa: E402
import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Each test gets an empty database."""
    path = os.environ["DB_PATH"]
    if os.path.exists(path):
        os.remove(path)
    db.init_db()
    yield
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def sent(monkeypatch):
    """Capture outgoing Telegram messages instead of hitting the network."""
    msgs = []

    async def fake_send(chat_id, text, buttons=None):
        msgs.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        return {"ok": True}

    async def fake_cb(callback_id, text=None):
        return {"ok": True}

    monkeypatch.setattr(app, "send_message", fake_send)
    monkeypatch.setattr(app, "answer_callback", fake_cb)
    return msgs


@pytest.fixture
def owner():
    """Register an owner chat id (most flows no-op without one)."""
    db.set_setting("owner_chat_id", 42)
    return 42


def fake_capture(**overrides):
    """Build a parse_capture replacement returning fixed structured output."""
    base = {
        "type": "task", "title": "do the thing", "person": None,
        "due": None, "effort_minutes": None, "explicit_reminder": None,
    }
    base.update(overrides)

    def _fn(note, now_iso, tz_name):
        return dict(base)

    return _fn
