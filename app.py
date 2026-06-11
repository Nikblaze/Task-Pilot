"""TaskPilot — personal capture + reminder bot.

One process serves three things:
  1. POST /tg/{secret}      Telegram webhook (capture, commands, button taps)
  2. background scheduler    reminder tick (every 60s) + daily briefing (08:00)
  3. GET  /dashboard         a web page you open in any browser (nothing to install)
"""
import os
from contextlib import asynccontextmanager
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

import db
import llm
import planner as P
from telegram_api import send_message, answer_callback, set_webhook, button

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "change-me")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

TYPE_EMOJI = {"task": "✅", "commitment": "🤝", "idea": "💡", "note": "📝"}


# ----------------------------------------------------------------------------
# lifespan: init db + start scheduler + register webhook
# ----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler = AsyncIOScheduler(timezone=str(P.TZ))
    scheduler.add_job(reminder_tick, "interval", seconds=60, id="tick")
    scheduler.add_job(send_briefing, "cron", hour=P.WORK_START, minute=0, id="briefing")
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

    item_id = db.add_item(
        type=parsed["type"], title=parsed["title"], raw=text,
        person=parsed["person"], due_at=P.to_iso(due), effort_min=effort,
        start_by_at=P.to_iso(start_by), remind_at=P.to_iso(remind_at),
        status="open", created_at=now.isoformat(),
    )
    db.add_event(item_id, "created", now.isoformat())

    # confirmation
    em = TYPE_EMOJI.get(parsed["type"], "•")
    msg = f"{em} Captured <b>#{item_id}</b> — {parsed['type']}\n<b>{parsed['title']}</b>"
    if parsed["person"]:
        msg += f"\n👤 {parsed['person']}"
    if due:
        msg += f"\n⏰ due {P.fmt(due)}"
    if effort:
        msg += f"  ·  ~{effort // 60}h {effort % 60}m".replace(" 0m", "")
    if start_by:
        msg += f"\n🟠 I'll nudge you to start by {P.fmt(start_by)}"
    elif explicit:
        msg += f"\n🔔 reminder set for {P.fmt(explicit)}"
    elif parsed["type"] in ("idea", "note"):
        msg += "\n💡 parked — no deadline, won't nag you"
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
        "Commands: /today  /week  /list  /ideas  /dashboard  /help",
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
        "/today — your briefing now\n"
        "/list — open tasks &amp; commitments\n"
        "/ideas — parked ideas\n"
        "/week — this week's timelog\n"
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
    db.update_item(item_id, status="done", done_at=now.isoformat(), remind_at=None)
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


def build_briefing_text():
    now = P.now()
    today_end = now.replace(hour=23, minute=59)
    soon = now + timedelta(days=2)
    items = db.list_open()
    start_today, due_soon, commits = [], [], []
    for i in items:
        due = P.parse_iso(i["due_at"])
        sb = P.parse_iso(i["start_by_at"])
        if i["type"] == "commitment" and due and due <= soon:
            commits.append(i)
        if sb and sb <= today_end and not i["sent_startby"]:
            start_today.append(i)
        elif due and due <= soon:
            due_soon.append(i)
    if not (start_today or due_soon or commits):
        return ""
    parts = [f"☀️ <b>Good morning — {now.strftime('%A %d %b')}</b>"]
    if start_today:
        parts.append("\n<b>Start today</b>\n" + "\n".join(line_for(i) for i in start_today))
    if due_soon:
        parts.append("\n<b>Due soon</b>\n" + "\n".join(line_for(i) for i in due_soon))
    if commits:
        parts.append("\n<b>Commitments</b>\n" + "\n".join(line_for(i) for i in commits))
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


def build_timelog_text():
    now = P.now()
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0)
    rows = db.events_between(monday.isoformat(), now.isoformat())
    if not rows:
        return "No activity logged this week yet."
    by_day = {}
    for r in rows:
        ts = P.parse_iso(r["ts"])
        key = ts.strftime("%a %d %b")
        verb = {"created": "captured", "done": "✅ finished", "started": "started",
                "reminded": "reminded", "snoozed": "rescheduled"}.get(r["kind"], r["kind"])
        by_day.setdefault(key, []).append(f"  · {verb}: {r['title'] or '(item)'}")
    out = ["🗓 <b>This week</b>"]
    for day, lines in by_day.items():
        out.append(f"\n<b>{day}</b>\n" + "\n".join(lines))
    return "\n".join(out)


# ----------------------------------------------------------------------------
# routes
# ----------------------------------------------------------------------------
@app.get("/", response_class=PlainTextResponse)
async def health():
    return "TaskPilot is running."


@app.get("/setup", response_class=PlainTextResponse)
async def setup(token: str = ""):
    if token != DASHBOARD_TOKEN:
        raise HTTPException(403, "bad token")
    if not PUBLIC_URL:
        return "Set PUBLIC_URL env var first."
    res = await set_webhook(f"{PUBLIC_URL}/tg/{WEBHOOK_SECRET}", WEBHOOK_SECRET)
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

    await handle_capture(chat_id, text)
    return JSONResponse({"ok": True})


# ----------------------------------------------------------------------------
# dashboard API + page
# ----------------------------------------------------------------------------
@app.get("/api/items")
async def api_items(token: str = ""):
    if token != DASHBOARD_TOKEN:
        raise HTTPException(403, "bad token")
    now = P.now()
    soon = now + timedelta(days=2)
    buckets = {"overdue": [], "start_today": [], "due_soon": [],
               "commitments": [], "ideas": [], "other": []}
    for i in db.list_open():
        d = dict(i)
        due = P.parse_iso(i["due_at"])
        sb = P.parse_iso(i["start_by_at"])
        d["due_fmt"] = P.fmt(due)
        if i["type"] == "idea":
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
    return {"buckets": buckets, "timelog": build_timelog_text()}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    with open(path, encoding="utf-8") as f:
        return f.read()
