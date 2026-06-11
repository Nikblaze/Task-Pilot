# TaskPilot — your Telegram capture + reminder bot

Type (or paste) anything into Telegram — a task, a promise you made someone, an idea —
and TaskPilot files it, figures out the deadline and effort, and reminds you **when you
need to start**, not when it's already due. A web dashboard (nothing to install) gives
you the bird's-eye view and your weekly timelog.

```
You (Telegram) → webhook → Claude Haiku parses → SQLite → scheduler → reminders back to you
                                                        ↘ /dashboard web page
```

---

## What you'll need (5 minutes of gathering)

1. **A Telegram bot token** — open Telegram, message **@BotFather**, send `/newbot`,
   pick a name and a username ending in `bot`. It replies with a token like
   `8012345678:AAH...`. Copy it.
2. **An Anthropic API key** — sign in at https://console.anthropic.com → *API keys* →
   *Create key*. Add a few dollars of credit under *Billing*; this bot spends a few
   **cents** a month (Haiku is $1 / $5 per million tokens and each note is tiny).
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
ANTHROPIC_API_KEY=sk-ant-...
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
- **`/today`** — your briefing on demand. **`/list`** — open items. **`/ideas`** —
  parked ideas. **`/week`** — this week's timelog. **`/done 12`** — mark item #12 complete.
- The **morning briefing** auto-sends at `WORK_START_HOUR` and surfaces what to start
  today, what's due soon, and your commitments.

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
