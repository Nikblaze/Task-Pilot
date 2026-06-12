# TaskPilot — your Personal Chief of Staff (Telegram)

Type (or paste) anything into Telegram — a task, a promise you made someone, something
you're waiting on, an idea — and TaskPilot files it, figures out the deadline and effort,
and reminds you **when you need to start**, not when it's already due. It also watches
your **capacity**: if a new commitment overloads a day or your week, it warns you on the
spot. Each evening it asks how much is left on what's in flight, so the numbers stay
honest, and Friday's review shows what you finished vs missed. A web dashboard (nothing
to install) gives the bird's-eye view.

```
You (Telegram) → webhook → LLM parses → SQLite → scheduler → reminders / briefs / check-ins
                                                       ↘ /dashboard web page (capacity + buckets)
```

### What it does (V0+V1)

- **Capture** anything in <5s; AI extracts type, person, deadline, effort.
- **Backward-planned reminders** — `start_by = due − effort − buffer`, snapped to work hours.
- **Capacity engine** — effort spread across the start→due window; warns on day/week overflow.
- **Evening check-in** (`/checkin`, auto at `WORK_END_HOUR−1`) — recalc remaining effort.
- **Waiting** — track things you're blocked on; stale-wait nudges; `/waiting`.
- **Daily brief** (`/today`, auto at `WORK_START_HOUR`) — priorities, **at-risk**, capacity.
- **Weekly review** (`/week`) — completed vs **missed**, plus a timelog with logged hours.

Voice/screenshot capture, calendar integration, and a negotiation assistant are the
documented next steps (see `PCS/07_Roadmap` and `docs/superpowers/`).

---

## What you'll need (5 minutes of gathering)

1. **A Telegram bot token** — open Telegram, message **@BotFather**, send `/newbot`,
   pick a name and a username ending in `bot`. It replies with a token like
   `8012345678:AAH...`. Copy it.
2. **An LLM API key** — Sarvam (recommended), Groq, or any OpenAI-compatible provider.
   Sarvam: https://dashboard.sarvam.ai → create a key. Use model `sarvam-30b`.
3. **Two random secrets you invent** — any long random strings, one for
   `WEBHOOK_SECRET` and one for `DASHBOARD_TOKEN`. (Mash the keyboard, ~30 chars each.)

---

## Where to deploy

The bot must stay **always-on** so reminders fire. Easiest first, cheapest last:

| Host | Effort | Cost | Note |
|------|--------|------|------|
| **Railway** (recommended) | lowest | ~$5/mo after free trial credit | always-on, 2-click volume |
| **Fly.io** | medium | free small-machine allowance | always-on, uses the Dockerfile |
| **Render** | low | free tier exists | ⚠️ free tier sleeps — see note below |
| **Any $5 VPS** | higher | ~$5/mo | full control |

This guide uses **Railway**. The other hosts use the same env vars and the included
`Dockerfile`.

---

## Step-by-step (Railway)

**1. Put this folder on GitHub.** Create a new repo and upload all these files
(or use the GitHub Desktop app — no command line needed).

**2. Create the project.** Go to https://railway.app → *New Project* →
*Deploy from GitHub repo* → pick your repo. Railway detects Python and starts building.

**3. Add a volume** (so your data survives restarts). In the service → *Variables/Settings*
→ *Volumes* → *New Volume*, mount path `/app/data`. Then add a variable
`DB_PATH=/app/data/bot.db`.

**4. Add the rest of the variables.** Service → *Variables* → paste these (fill in yours):

```
TELEGRAM_BOT_TOKEN=8012345678:AAH...
API_KEY=sk_...your-sarvam-or-groq-key...
MODEL=sarvam-30b
LLM_BASE_URL=https://api.sarvam.ai/v1
WEBHOOK_SECRET=your-long-random-string
DASHBOARD_TOKEN=your-other-long-random-string
TIMEZONE=Asia/Kolkata
DB_PATH=/app/data/bot.db
```

(Optional: `BUFFER_DAYS=2` if you want a Monday deadline to nudge you Saturday rather
than Sunday. `WORK_START_HOUR` also sets the time your morning briefing arrives.)

**5. Get your public URL.** Service → *Settings* → *Networking* → *Generate Domain*.
You'll get something like `https://taskpilot-production.up.railway.app`. Copy it.

**6. Tell the app its own URL.** Add one more variable:

```
PUBLIC_URL=https://taskpilot-production.up.railway.app
```

Save — Railway redeploys, and on startup the app registers the Telegram webhook for you.
(If you ever want to re-register manually, just visit
`https://YOUR-URL/setup?token=YOUR_DASHBOARD_TOKEN` in a browser.)

**7. Activate it.** Open Telegram, find your bot, send **`/start`**. The first person to
`/start` becomes the owner — that's you, and the bot ignores everyone else.

**8. Use it.** Send a message:

```
commit to investigate caching for Sneha, by Mon, 3h
```

It replies confirming the commitment, the deadline, and when it'll nudge you to start.
Try also: `deploy payments API friday eod` and `idea: batch the webhook calls`.

**9. Open your dashboard.** Send `/dashboard` (it sends you the link), or open
`https://YOUR-URL/dashboard?token=YOUR_DASHBOARD_TOKEN` in Chrome and bookmark it.

That's it — you're live.

---

## Daily use

- **Capture:** just type. No need to pick a category — it's inferred.
- **Reminders** arrive with buttons: *Done* · *Starting now* · *Remind tonight* ·
  *Tomorrow 9am* · *Pick date & time*. Tap one; no typing needed.
- **`/today`** — briefing on demand (priorities, at-risk, capacity). **`/checkin`** —
  evening "how much is left?" check-in. **`/list`** — open items. **`/waiting`** —
  things you're waiting on others for. **`/ideas`** — parked ideas. **`/week`** — weekly
  review (done vs missed + timelog). **`/done 12`** — mark item #12 complete.
- The **morning briefing** auto-sends at `WORK_START_HOUR`; the **evening check-in**
  auto-sends at `WORK_END_HOUR − 1`.

### Capacity tuning (optional)

Defaults: **6h/day**, **30h/week**, working days **Mon–Fri**. These live in the
`settings` table (keys `cap_day_min`, `cap_week_min`, `work_days`) so they can be changed
without redeploying. `STALE_WAITING_DAYS` (env, default `3`) controls when a "waiting"
item nudges you. See `docs/superpowers/DECISIONS.md` for every default and why.

---

## How the start-by reminder is calculated

```
start_by = deadline − effort − safety buffer    (then snapped into working hours)
```

So `due Monday 6pm, effort 3h, buffer 1 day` → start-by Sunday afternoon. The bot warns
you *then*, plus a day-before check and a due-time alert. Items with no deadline (ideas)
get no reminders and never nag you.

---

## Notes & next steps

- **Render free tier sleeps** after 15 min idle, which would silence reminders. If you
  use it, add a free uptime pinger (e.g. cron-job.org) hitting `https://YOUR-URL/`
  every 10 minutes to keep it awake.
- **Voice notes:** capture currently reads text and captions. Voice-to-text is a clean
  next addition (download the Telegram voice file → transcribe → feed into the same
  parser). Left out of v1 to keep deployment simple.
- **Want a fully free LLM?** Swap `llm.py` to call a local Ollama model or any
  OpenAI-compatible endpoint — the parsing prompt is model-agnostic. Haiku is the
  reliable, near-free default.
- **Security:** the dashboard is protected by a token in the URL. Fine for personal use;
  add real auth if you ever expose it more widely.
