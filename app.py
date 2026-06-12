"""TaskPilot — personal capture + reminder bot.

One process serves three things:
  1. POST /tg/{secret}      Telegram webhook (capture, commands, button taps)
  2. background scheduler    reminder tick (every 60s) + daily briefing (08:00)
  3. GET  /dashboard         a web page you open in any browser (nothing to install)
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from contextlib import asynccontextmanager
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

import db
import llm
import planner as P
import capacity as C
from telegram_api import send_message, answer_callback, set_webhook, button

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me").strip()
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "change-me").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

if DASHBOARD_TOKEN == "change-me":
    print("WARNING: DASHBOARD_TOKEN is unset — using default 'change-me'. Set DASHBOARD_TOKEN in Railway.")
if WEBHOOK_SECRET == "change-me":
    print("WARNING: WEBHOOK_SECRET is unset — using default 'change-me'. Set WEBHOOK_SECRET in Railway.")
if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
    print("WARNING: TELEGRAM_BOT_TOKEN is unset — Telegram bot will not work.")
if not PUBLIC_URL:
    print("WARNING: PUBLIC_URL is unset — webhook will not be registered.")

TYPE_EMOJI = {"task": "✅", "commitment": "🤝", "waiting": "⏳", "idea": "💡", "note": "📝"}

STALE_WAITING_DAYS = float(os.getenv("STALE_WAITING_DAYS", "3"))


def capacity_settings():
    """Capacity config from settings, with safe defaults (see DECISIONS.md)."""
    return {
        "cap_day_min": db.get_setting("cap_day_min", C.DEFAULT_CAP_DAY_MIN),
        "cap_week_min": db.get_setting("cap_week_min", C.DEFAULT_CAP_WEEK_MIN),
        "work_days": db.get_setting("work_days", C.DEFAULT_WORK_DAYS),
    }


# ----------------------------------------------------------------------------
# lifespan: init db + start scheduler + register webhook
# ----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler = AsyncIOScheduler(timezone=str(P.TZ))
    scheduler.add_job(reminder_tick, "interval", seconds=60, id="tick")
    scheduler.add_job(send_briefing, "cron", hour=P.WORK_START, minute=0, id="briefing")
    # evening check-in one hour before the workday ends (see DECISIONS.md #12)
    checkin_hour = max(P.WORK_START, P.WORK_END - 1)
    scheduler.add_job(send_checkin, "cron", hour=checkin_hour, minute=0, id="checkin")
    scheduler.start()
    if PUBLIC_URL:
        try:
            await set_webhook(f"{PUBLIC_URL}/tg/{WEBHOOK_SECRET}", WEBHOOK_SECRET)
        except Exception as e:  # noqa
            print("webhook registration failed:", e)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def owner_chat():
    v = db.get_setting("owner_chat_id")
    return int(v) if v else None


def is_owner(chat_id):
    oc = owner_chat()
    return oc is None or chat_id == oc


def reminder_buttons(item_id):
    return [
        [button("✅ Done", f"done:{item_id}"), button("▶️ Starting now", f"start:{item_id}")],
        [button("Remind tonight", f"tonight:{item_id}"), button("Tomorrow 9am", f"tomorrow:{item_id}")],
        [button("📅 Pick date & time", f"pick:{item_id}")],
    ]


def line_for(item):
    em = TYPE_EMOJI.get(item["type"], "•")
    s = f"{em} <b>#{item['id']}</b> {item['title']}"
    if item["person"]:
        s += f" · for {item['person']}"
    due = P.parse_iso(item["due_at"])
    if due:
        s += f"\n   ⏰ due {P.fmt(due)}"
    return s


# ----------------------------------------------------------------------------
# capture
# ----------------------------------------------------------------------------
async def handle_capture(chat_id, text):
    now = P.now()
    parsed = llm.parse_capture(text, now.isoformat(), str(P.TZ))

    due = P.parse_iso(parsed["due"])
    effort = parsed["effort_minutes"]
    explicit = P.parse_iso(parsed["explicit_reminder"])
    start_by = P.compute_start_by(due, effort) if due else None
    remind_at = explicit  # an explicit "remind me at X" wins as the next ping
    is_waiting = parsed["type"] == "waiting"

    item_id = db.add_item(
        type=parsed["type"], title=parsed["title"], raw=text,
        person=parsed["person"], due_at=P.to_iso(due), effort_min=effort,
        start_by_at=P.to_iso(start_by), remind_at=P.to_iso(remind_at),
        status="open", created_at=now.isoformat(),
        waiting_since=now.isoformat() if is_waiting else None,
    )
    db.add_event(item_id, "created", now.isoformat())

    # confirmation
    em = TYPE_EMOJI.get(parsed["type"], "•")
    msg = f"{em} Captured <b>#{item_id}</b> — {parsed['type']}\n<b>{parsed['title']}</b>"
    if parsed["person"]:
        label = "⏳ waiting on" if is_waiting else "👤"
        msg += f"\n{label} {parsed['person']}"
    if due:
        msg += f"\n⏰ due {P.fmt(due)}"
    if effort:
        msg += f"  ·  ~{effort // 60}h {effort % 60}m".replace(" 0m", "")
    if is_waiting:
        msg += "\n⏳ tracked — I'll nudge you if it goes stale (/waiting to review)"
    elif start_by:
        msg += f"\n🟠 I'll nudge you to start by {P.fmt(start_by)}"
    elif explicit:
        msg += f"\n🔔 reminder set for {P.fmt(explicit)}"
    elif parsed["type"] in ("idea", "note"):
        msg += "\n💡 parked — no deadline, won't nag you"

    # capacity warning (warn + still save) — only for schedulable items that just landed
    if parsed["type"] in ("task", "commitment") and due:
        items = [dict(r) for r in db.schedulable_open()]
        focus = next((i for i in items if i["id"] == item_id), None)
        over = C.check_overflow(items, now.date(), capacity_settings(), focus_item=focus)
        warn = C.overflow_warning(over)
        if warn:
            msg += f"\n\n{warn}"
    await send_message(chat_id, msg)


# ----------------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------------
async def cmd_start(chat_id):
    if owner_chat() is None:
        db.set_setting("owner_chat_id", chat_id)
    await send_message(
        chat_id,
        "👋 <b>TaskPilot ready.</b>\n\n"
        "Just type or speak anything — a task, a promise you made, an idea — "
        "and I'll file it and remind you in time.\n\n"
        "Examples:\n"
        "• <i>commit to investigate caching for Sneha, by Mon, 3h</i>\n"
        "• <i>idea: batch the webhook calls to cut latency</i>\n"
        "• <i>deploy payments API friday eod</i>\n\n"
        "Commands: /today  /checkin  /week  /list  /waiting  /ideas  /dashboard  /help",
    )


async def cmd_today(chat_id):
    await send_message(chat_id, build_briefing_text() or "Nothing scheduled. 🎉")


async def cmd_list(chat_id):
    items = [i for i in db.list_open() if i["type"] in ("task", "commitment")]
    if not items:
        await send_message(chat_id, "No open tasks or commitments. 🎉")
        return
    body = "\n\n".join(line_for(i) for i in items)
    await send_message(chat_id, f"<b>Open items</b>\n\n{body}\n\nMark done: /done &lt;id&gt;")


async def cmd_ideas(chat_id):
    items = db.list_by_type("idea")
    if not items:
        await send_message(chat_id, "No parked ideas yet. 💡")
        return
    body = "\n".join(f"💡 <b>#{i['id']}</b> {i['title']}" for i in items)
    await send_message(chat_id, f"<b>Parked ideas</b>\n\n{body}")


async def cmd_waiting(chat_id):
    items = db.list_waiting()
    if not items:
        await send_message(chat_id, "Not waiting on anyone. ⏳")
        return
    now = P.now()
    lines = []
    for i in items:
        since = P.parse_iso(i["created_at"])
        days = (now - since).days if since else 0
        who = f" · on {i['person']}" if i["person"] else ""
        age = f" ({days}d)" if days else ""
        lines.append(f"⏳ <b>#{i['id']}</b> {i['title']}{who}{age}")
    body = "\n".join(lines)
    await send_message(chat_id, f"<b>Waiting on others</b>\n\n{body}\n\nClear: /done &lt;id&gt;")


async def cmd_checkin(chat_id):
    await start_checkin(chat_id)


async def cmd_week(chat_id):
    await send_message(chat_id, build_timelog_text())


async def cmd_dashboard(chat_id):
    if not PUBLIC_URL:
        await send_message(chat_id, "Dashboard URL not configured yet (set PUBLIC_URL).")
        return
    url = f"{PUBLIC_URL}/dashboard?token={DASHBOARD_TOKEN}"
    await send_message(chat_id, f"📊 Your dashboard:\n{url}")


async def cmd_done(chat_id, arg):
    try:
        item_id = int(arg)
    except (TypeError, ValueError):
        await send_message(chat_id, "Usage: /done <id>")
        return
    await mark_done(chat_id, item_id)


async def cmd_help(chat_id):
    await send_message(
        chat_id,
        "<b>How to use</b>\n\n"
        "Type anything to capture it. I figure out if it's a task, a commitment, "
        "or an idea, pull out the deadline and effort, and schedule reminders so you "
        "start in time — not when it's already due.\n\n"
        "I also watch your <b>capacity</b> — if a new commitment overloads a day or "
        "your week, I'll warn you when you capture it. Each evening I check how much "
        "is left on what's in flight, so the numbers stay honest.\n\n"
        "/today — your briefing now (priorities, risks, capacity)\n"
        "/checkin — evening 'how much is left?' check-in\n"
        "/list — open tasks &amp; commitments\n"
        "/waiting — things you're waiting on others for\n"
        "/ideas — parked ideas\n"
        "/week — weekly review: done vs missed + timelog\n"
        "/done &lt;id&gt; — mark complete\n"
        "/dashboard — open the web view",
    )


# ----------------------------------------------------------------------------
# callbacks (button taps)
# ----------------------------------------------------------------------------
async def mark_done(chat_id, item_id):
    item = db.get_item(item_id)
    if not item:
        await send_message(chat_id, f"#{item_id} not found.")
        return
    now = P.now()
    # Log effort: whatever was still remaining is now spent, added to any prior actual.
    remaining = item["remaining_effort_min"]
    if remaining is None:
        remaining = item["effort_min"]
    actual = (item["actual_effort_min"] or 0) + (remaining or 0)
    db.update_item(
        item_id, status="done", done_at=now.isoformat(), remind_at=None,
        remaining_effort_min=0, actual_effort_min=actual,
    )
    db.add_event(item_id, "done", now.isoformat())
    await send_message(chat_id, f"✅ Done — <b>#{item_id}</b> {item['title']}")


async def handle_callback(chat_id, cb_id, data):
    action, _, sid = data.partition(":")
    item_id = int(sid) if sid.isdigit() else None
    now = P.now()

    if action == "done":
        await answer_callback(cb_id, "Marked done")
        await mark_done(chat_id, item_id)

    elif action == "start":
        item = db.get_item(item_id)
        due = P.parse_iso(item["due_at"]) if item else None
        nxt = P.snap_to_work_hours(due - timedelta(days=1)) if due else None
        db.update_item(item_id, remind_at=P.to_iso(nxt), sent_startby=1)
        db.add_event(item_id, "started", now.isoformat())
        await answer_callback(cb_id, "Nice — I'll check back later")

    elif action == "tonight":
        nxt = now.replace(hour=min(P.WORK_END - 1, 20), minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt = now + timedelta(hours=2)
        db.update_item(item_id, remind_at=P.to_iso(nxt))
        await answer_callback(cb_id, f"Reminding {P.fmt(nxt)}")

    elif action == "tomorrow":
        nxt = (now + timedelta(days=1)).replace(hour=P.WORK_START, minute=0, second=0, microsecond=0)
        db.update_item(item_id, remind_at=P.to_iso(nxt))
        await answer_callback(cb_id, f"Reminding {P.fmt(nxt)}")

    elif action == "pick":
        db.set_pending(chat_id, "set_time", item_id)
        await answer_callback(cb_id)
        await send_message(chat_id, "📅 Send the date & time (e.g. <i>Fri 3pm</i> or <i>tomorrow 10am</i>).")

    elif action == "ck":
        # data is "ck:<choice>:<id>"; sid still holds "<choice>:<id>"
        choice, _, cid = sid.partition(":")
        item = db.get_item(int(cid)) if cid.isdigit() else None
        if not item:
            await answer_callback(cb_id, "Item gone")
            return
        if choice == "custom":
            db.set_pending(chat_id, "checkin_remaining", item["id"])
            await answer_callback(cb_id)
            await send_message(chat_id, "How much is left? e.g. <i>2h</i>, <i>30m</i>, or <i>done</i>.")
            return
        if choice == "keep":
            rem = apply_checkin(item, _rem_of(item) or 0)
        else:  # "25" / "50" / "75" percent of the original estimate still remaining
            base = item["effort_min"] or _rem_of(item) or P.DEFAULT_EFFORT_MIN
            rem = apply_checkin(item, round(base * int(choice) / 100))
        await answer_callback(cb_id, f"Updated — {rem // 60}h{rem % 60:02d}m left")


# ----------------------------------------------------------------------------
# evening check-in (effort-truth loop that feeds capacity)
# ----------------------------------------------------------------------------
def active_checkin_items():
    """Open tasks/commitments that are in flight — worth asking 'how much is left?'."""
    now = P.now()
    soon = now + timedelta(days=2)
    out = []
    for i in db.list_open():
        if i["type"] not in ("task", "commitment") or not i["due_at"]:
            continue
        sb = P.parse_iso(i["start_by_at"])
        due = P.parse_iso(i["due_at"])
        if (sb and sb <= now) or (due and due <= soon):
            out.append(i)
    return out


def checkin_buttons(item_id):
    return [
        [button("✅ Done", f"done:{item_id}"), button("On track", f"ck:keep:{item_id}")],
        [button("¼ left", f"ck:25:{item_id}"), button("½ left", f"ck:50:{item_id}"),
         button("¾ left", f"ck:75:{item_id}")],
        [button("✏️ Custom", f"ck:custom:{item_id}")],
    ]


def _rem_of(item):
    r = item["remaining_effort_min"]
    return r if r is not None else item["effort_min"]


def apply_checkin(item, new_remaining):
    """Record a check-in: log work done since last, update remaining + actual effort."""
    now = P.now()
    before = _rem_of(item) or 0
    new_remaining = max(0, int(new_remaining))
    delta = max(0, before - new_remaining)  # effort spent since last check-in
    actual = (item["actual_effort_min"] or 0) + delta
    db.update_item(
        item["id"], remaining_effort_min=new_remaining,
        actual_effort_min=actual, last_checkin_at=now.isoformat(),
    )
    db.add_checkin(item["id"], now.isoformat(), before, new_remaining)
    db.add_event(item["id"], "checkin", now.isoformat())
    return new_remaining


async def start_checkin(chat_id):
    items = active_checkin_items()
    if not items:
        await send_message(chat_id, "🌙 Evening check-in: nothing in flight. Rest up. ✅")
        return
    await send_message(chat_id, "🌙 <b>Evening check-in</b> — how much is left on each?")
    for i in items:
        due = P.parse_iso(i["due_at"])
        rem = _rem_of(i)
        line = f"<b>#{i['id']}</b> {i['title']}"
        if rem:
            line += f"\n🕒 ~{rem // 60}h{rem % 60:02d}m left (est)"
        if due:
            line += f"\n⏰ due {P.fmt(due)}"
        await send_message(chat_id, line, checkin_buttons(i["id"]))


async def send_checkin():
    oc = owner_chat()
    if oc is not None:
        await start_checkin(oc)


# ----------------------------------------------------------------------------
# reminders + briefing
# ----------------------------------------------------------------------------
async def reminder_tick():
    oc = owner_chat()
    if oc is None:
        return
    now = P.now()
    for item in db.due_for_reminder(now.isoformat()):
        remind_at = P.parse_iso(item["remind_at"])
        start_by = P.parse_iso(item["start_by_at"])
        due = P.parse_iso(item["due_at"])

        # 1) explicit / snoozed reminder
        if remind_at and now >= remind_at:
            db.update_item(item["id"], remind_at=None)
            await _send_reminder(oc, item, "🔔 Reminder")
            continue
        # 2) start-by nudge (the important one)
        if start_by and not item["sent_startby"] and now >= start_by:
            db.update_item(item["id"], sent_startby=1)
            await _send_reminder(oc, item, "🟠 Start-by today")
            continue
        # 3) day-before check
        if due and not item["sent_daybefore"] and now >= due - timedelta(days=1):
            db.update_item(item["id"], sent_daybefore=1)
            await _send_reminder(oc, item, "⏳ Due tomorrow")
            continue
        # 4) due / overdue
        if due and not item["sent_due"] and now >= due:
            db.update_item(item["id"], sent_due=1)
            await _send_reminder(oc, item, "❗️Due now")

    # 5) stale "waiting on someone" nudges
    cutoff = now - timedelta(days=STALE_WAITING_DAYS)
    for item in db.list_waiting():
        since = P.parse_iso(item["waiting_since"]) or P.parse_iso(item["created_at"])
        if since and since <= cutoff:
            db.update_item(item["id"], waiting_since=now.isoformat())
            who = f" from {item['person']}" if item["person"] else ""
            await send_message(
                oc,
                f"⏳ Still waiting{who}: <b>{item['title']}</b>\n"
                f"Maybe follow up? Clear with /done {item['id']}.",
            )


async def _send_reminder(chat_id, item, header):
    due = P.parse_iso(item["due_at"])
    text = f"{header}: <b>{item['title']}</b>"
    if item["person"]:
        text += f"\n👤 for {item['person']}"
    if item["effort_min"]:
        text += f"\n🕒 ~{item['effort_min'] // 60}h" if item["effort_min"] >= 60 else f"\n🕒 ~{item['effort_min']}m"
    if due:
        text += f"\n⏰ due {P.fmt(due)}"
    db.add_event(item["id"], "reminded", P.now().isoformat())
    await send_message(chat_id, text, reminder_buttons(item["id"]))


def _is_at_risk(item, now):
    """At risk = its latest safe start time has passed but it isn't started/done yet."""
    if item["type"] not in ("task", "commitment"):
        return False
    sb = P.parse_iso(item["start_by_at"])
    due = P.parse_iso(item["due_at"])
    if due and due < now:
        return True   # already overdue
    return bool(sb and sb < now and not item["sent_startby"])


def build_briefing_text():
    now = P.now()
    today_end = now.replace(hour=23, minute=59)
    soon = now + timedelta(days=2)
    items = db.list_open()
    start_today, due_soon, commits, at_risk = [], [], [], []
    for i in items:
        due = P.parse_iso(i["due_at"])
        sb = P.parse_iso(i["start_by_at"])
        if _is_at_risk(i, now):
            at_risk.append(i)
        if i["type"] == "commitment" and due and due <= soon:
            commits.append(i)
        if sb and sb <= today_end and not i["sent_startby"]:
            start_today.append(i)
        elif due and due <= soon:
            due_soon.append(i)
    waiting = db.list_waiting()
    if not (start_today or due_soon or commits or at_risk):
        return ""
    parts = [f"☀️ <b>Good morning — {now.strftime('%A %d %b')}</b>"]
    # capacity line first — the "chief of staff" framing
    sched = [dict(r) for r in db.schedulable_open()]
    if sched:
        parts.append("\n" + C.summary_text(sched, now.date(), capacity_settings()))
    ar = {i["id"] for i in at_risk}  # shown once under At risk; don't repeat below
    if at_risk:
        parts.append("\n⚠️ <b>At risk</b>\n" + "\n".join(line_for(i) for i in at_risk))
    start_rest = [i for i in start_today if i["id"] not in ar]
    if start_rest:
        parts.append("\n<b>Start today</b>\n" + "\n".join(line_for(i) for i in start_rest))
    due_rest = [i for i in due_soon if i["id"] not in ar]
    if due_rest:
        parts.append("\n<b>Due soon</b>\n" + "\n".join(line_for(i) for i in due_rest))
    commit_rest = [i for i in commits if i["id"] not in ar]
    if commit_rest:
        parts.append("\n<b>Commitments</b>\n" + "\n".join(line_for(i) for i in commit_rest))
    if waiting:
        parts.append(f"\n⏳ {len(waiting)} waiting on others — /waiting to review")
    ideas = db.list_by_type("idea")
    if ideas:
        parts.append(f"\n💡 {len(ideas)} idea(s) parked — /ideas to review")
    return "\n".join(parts)


async def send_briefing():
    oc = owner_chat()
    if oc is None:
        return
    text = build_briefing_text()
    if text:
        await send_message(oc, text)


def _hours(mins):
    return f"{round((mins or 0) / 60, 1)}h"


def build_timelog_text():
    now = P.now()
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0)

    # --- weekly review summary: completed vs missed + logged effort ---
    completed = db.completed_between(monday.isoformat(), now.isoformat())
    missed = db.missed_before(now.isoformat())
    logged_min = sum((i["actual_effort_min"] or 0) for i in completed)

    summary = ["🗓 <b>Weekly review</b>"]
    summary.append(
        f"✅ {len(completed)} completed · ❗️ {len(missed)} missed · "
        f"🕒 {_hours(logged_min)} logged"
    )
    if missed:
        summary.append("\n<b>Missed (past due, still open)</b>")
        for i in missed:
            due = P.parse_iso(i["due_at"])
            who = f" · for {i['person']}" if i["person"] else ""
            summary.append(f"  · ❗️ #{i['id']} {i['title']}{who} (due {P.fmt(due)})")

    # --- per-day activity log ---
    rows = db.events_between(monday.isoformat(), now.isoformat())
    if rows:
        by_day = {}
        for r in rows:
            ts = P.parse_iso(r["ts"])
            key = ts.strftime("%a %d %b")
            verb = {"created": "captured", "done": "✅ finished", "started": "started",
                    "reminded": "reminded", "snoozed": "rescheduled",
                    "checkin": "🕒 checked in"}.get(r["kind"], r["kind"])
            by_day.setdefault(key, []).append(f"  · {verb}: {r['title'] or '(item)'}")
        summary.append("\n<b>Activity</b>")
        for day, lines in by_day.items():
            summary.append(f"\n<b>{day}</b>\n" + "\n".join(lines))
    elif not completed and not missed:
        return "No activity logged this week yet."

    return "\n".join(summary)


# ----------------------------------------------------------------------------
# routes
# ----------------------------------------------------------------------------
@app.get("/", response_class=PlainTextResponse)
async def health():
    return "TaskPilot is running."


@app.get("/setup", response_class=PlainTextResponse)
async def setup(token: str = ""):
    if token.strip() != DASHBOARD_TOKEN:
        raise HTTPException(403, "bad token")
    if not PUBLIC_URL:
        return "Set PUBLIC_URL env var first."
    res = await set_webhook(f"{PUBLIC_URL}/tg/{WEBHOOK_SECRET}", WEBHOOK_SECRET)
    if not res.get("ok"):
        err = res.get("description", str(res))
        hint = "Check env vars and retry."
        if res.get("error_code") == 404:
            hint = "TELEGRAM_BOT_TOKEN is missing or invalid."
        elif "resolve host" in err.lower() or "bad webhook" in err.lower():
            hint = (
                f"PUBLIC_URL ({PUBLIC_URL}) is not reachable from the internet. "
                "Use your real Railway domain or a running ngrok https URL — not localhost or a placeholder."
            )
        return f"setWebhook FAILED: {res}\n\n{hint}"
    return f"setWebhook -> {res}"


@app.post("/tg/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(403, "bad secret")
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") not in (WEBHOOK_SECRET, None):
        raise HTTPException(403, "bad header")
    update = await request.json()

    # button tap
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        if is_owner(chat_id):
            await handle_callback(chat_id, cq["id"], cq.get("data", ""))
        return JSONResponse({"ok": True})

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return JSONResponse({"ok": True})
    chat_id = msg["chat"]["id"]

    # transcribe voice -> Telegram doesn't transcribe; we capture caption/text only here.
    text = msg.get("text") or msg.get("caption") or ""
    if not text:
        await send_message(chat_id, "Send me text for now — voice transcription is on the roadmap.")
        return JSONResponse({"ok": True})

    if text.startswith("/"):
        cmd, _, arg = text[1:].partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "start":
            await cmd_start(chat_id)
        elif not is_owner(chat_id):
            await send_message(chat_id, "This is a private bot.")
        elif cmd == "today":
            await cmd_today(chat_id)
        elif cmd == "list":
            await cmd_list(chat_id)
        elif cmd == "ideas":
            await cmd_ideas(chat_id)
        elif cmd == "week":
            await cmd_week(chat_id)
        elif cmd == "waiting":
            await cmd_waiting(chat_id)
        elif cmd == "checkin":
            await cmd_checkin(chat_id)
        elif cmd == "dashboard":
            await cmd_dashboard(chat_id)
        elif cmd == "done":
            await cmd_done(chat_id, arg.strip())
        else:
            await cmd_help(chat_id)
        return JSONResponse({"ok": True})

    if not is_owner(chat_id):
        await send_message(chat_id, "This is a private bot.")
        return JSONResponse({"ok": True})

    # waiting for a date/time from a "Pick date & time" tap?
    pend = db.get_pending(chat_id)
    if pend and pend["action"] == "set_time":
        iso = llm.parse_when(text, P.now().isoformat(), str(P.TZ))
        dt = P.parse_iso(iso)
        db.clear_pending(chat_id)
        if dt:
            db.update_item(pend["item_id"], remind_at=P.to_iso(dt))
            db.add_event(pend["item_id"], "snoozed", P.now().isoformat())
            await send_message(chat_id, f"🔔 Reminder set for {P.fmt(dt)}")
        else:
            await send_message(chat_id, "Couldn't read that time — try again with /list then tap the item.")
        return JSONResponse({"ok": True})

    # waiting for a "how much is left?" answer from a check-in Custom tap?
    if pend and pend["action"] == "checkin_remaining":
        mins = P.parse_effort_min(text)
        item = db.get_item(pend["item_id"])
        db.clear_pending(chat_id)
        if item is None:
            await send_message(chat_id, "That item is gone.")
        elif mins is None:
            await send_message(chat_id, "Couldn't read that — try <i>2h</i>, <i>30m</i>, or <i>done</i>.")
        elif mins == 0:
            await mark_done(chat_id, item["id"])
        else:
            rem = apply_checkin(item, mins)
            await send_message(chat_id, f"🕒 Updated — {rem // 60}h{rem % 60:02d}m left on #{item['id']}.")
        return JSONResponse({"ok": True})

    await handle_capture(chat_id, text)
    return JSONResponse({"ok": True})


# ----------------------------------------------------------------------------
# dashboard API + page
# ----------------------------------------------------------------------------
@app.get("/api/items")
async def api_items(token: str = ""):
    if token.strip() != DASHBOARD_TOKEN:
        raise HTTPException(403, "bad token")
    now = P.now()
    soon = now + timedelta(days=2)
    buckets = {"overdue": [], "start_today": [], "due_soon": [],
               "commitments": [], "waiting": [], "ideas": [], "other": []}
    for i in db.list_open():
        d = dict(i)
        due = P.parse_iso(i["due_at"])
        sb = P.parse_iso(i["start_by_at"])
        d["due_fmt"] = P.fmt(due)
        if i["type"] == "waiting":
            buckets["waiting"].append(d)
        elif i["type"] == "idea":
            buckets["ideas"].append(d)
        elif due and due < now:
            buckets["overdue"].append(d)
        elif sb and sb <= now.replace(hour=23, minute=59) and not i["sent_startby"]:
            buckets["start_today"].append(d)
        elif i["type"] == "commitment" and due and due <= soon:
            buckets["commitments"].append(d)
        elif due and due <= soon:
            buckets["due_soon"].append(d)
        else:
            buckets["other"].append(d)

    # capacity snapshot for the dashboard gauge
    sched = [dict(r) for r in db.schedulable_open()]
    cfg = capacity_settings()
    cap_day, cap_week, wd = C._settings(cfg)
    load = C.spread_load(sched, now.date(), wd)
    cap = {
        "today_min": round(load.get(now.date(), 0)),
        "week_min": round(C.week_load(load, now.date())),
        "cap_day_min": cap_day,
        "cap_week_min": cap_week,
    }
    return {"buckets": buckets, "capacity": cap, "timelog": build_timelog_text()}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    with open(path, encoding="utf-8") as f:
        return f.read()
