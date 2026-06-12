# PCS (Personal Chief of Staff) — V0+V1 Deploy-Ready Design

Date: 2026-06-12
Status: Approved for autonomous build (user authorized full completion in YOLO mode)

## Goal

Take the existing TaskPilot capture/reminder bot to a **deploy-ready V0+V1
feature-complete** Personal Chief of Staff, as described in `PCS/01..08`. User
pushes to GitHub and it runs on Railway and achieves the PCS goal: never miss
commitments, daily prioritization, capacity awareness, automatic timelog,
near-zero-friction capture.

## Scope (locked via clarifying questions)

In scope (V0+V1):
- Text capture + AI extraction (type / person / due / effort / commitment) — exists, kept.
- Backward-planned reminders (`start_by = due − effort − buffer`) — exists, kept.
- **Capacity engine** — per-day **and** weekly budgets; load computed by **spreading
  effort across working days in the `start_by … due` window (Approach B)**; on a new
  commitment that overflows a day or the week, **warn but still save** (lowest friction,
  never lose a capture).
- **Evening check-in** — recalc remaining effort per active item; feeds capacity truth.
- **Waiting** object — things blocked on someone else; stale-waiting nudges; `/waiting`.
- **Daily brief** — adds **risk** (at-risk items) and a **capacity summary** line.
- **Weekly review** — completed vs **missed** (past due + not done) + timelog with
  actual effort hours.
- **Dashboard** — adds capacity, waiting, overdue/missed visibility.

Deferred (V2+, explicitly out):
- Voice transcription, screenshot OCR (keep existing "send text for now" stub).
- Calendar integration; **Focus Block** object (most useful with a calendar).
- Negotiation assistant (capacity warning is its seed).
- Multi-user.

## Capacity model (Approach B — start→due spreading)

- Each open `task`/`commitment` with a `due_at` and effort contributes load.
- Effort used = `remaining_effort_min` (falls back to `effort_min`, then DEFAULT_EFFORT_MIN).
- Load spreads **evenly across working days** in `[start_by_date … due_date]`
  (weekends per `work_days` skipped). If no `start_by` (no window), the whole effort
  lands on the due date's day.
- Per-day load = sum of each item's daily share landing on that date.
- Per-week load = sum of daily shares within the ISO week (Mon–Sun).
- Budgets from settings: `cap_day_min` (default 360 = 6h), `cap_week_min`
  (default 1800 = 30h), `work_days` (default Mon–Fri).
- Overflow = any affected day > `cap_day_min` OR the affected week > `cap_week_min`.
  On capture: save item, then if it newly causes overflow, append a warning to the
  confirmation showing the offending day/week load vs budget.

## Data model changes (`db.py`)

`items` add (idempotent ALTER, try/except per column):
- `remaining_effort_min INTEGER` — initialized to `effort_min` at capture; updated by check-in.
- `actual_effort_min INTEGER` — accumulated logged time (true timelog).
- `last_checkin_at TEXT`.

New table `checkins(id, item_id, ts, remaining_before_min, remaining_after_min)`.

`type` recognizes `'waiting'` (freeform text column, no migration).

Settings keys (key/value, defaults applied on read): `cap_day_min`, `cap_week_min`,
`work_days`.

## Components / boundaries

- `planner.py` — single-item date math (unchanged core) + working-day helpers reused by capacity.
- `capacity.py` (new) — pure functions: `spread_load(items) -> {date: minutes}`,
  `week_load(...)`, `check_overflow(...)`, `summary_text(...)`. No I/O; takes plain dicts.
- `db.py` — storage + queries (capacity inputs, missed, waiting, checkin writes).
- `llm.py` — capture prompt gains `waiting` type + waiting-person detection.
- `app.py` — orchestration: capture, capacity warn, brief (risk+capacity), check-in
  job + `/checkin`, weekly review (missed), waiting nudges + `/waiting`, dashboard API.
- `dashboard.html` — capacity bar + waiting/missed sections.

## Error handling

- LLM parse failure already falls back to a safe `note` capture — kept; capacity math
  guards against null effort/dates and never raises into the webhook path.
- Schema migration idempotent; safe to run against existing `bot.db`.
- All new Telegram sends go through the existing mocked-in-tests client.

## Testing (automated + local smoke; Telegram & LLM mocked)

- `tests/` pytest suite: capacity math (spreading edge cases, overflow), planner,
  db migration + queries, capture flow, briefing (risk+capacity), weekly/missed,
  check-in, waiting, webhook routes via FastAPI TestClient.
- Local smoke: boot via TestClient, `GET /` health, POST a simulated webhook capture
  (LLM mocked), `GET /api/items`.
- Self-improve loop until suite green + smoke pass.

## Deploy (Railway)

- Procfile + Dockerfile validated; env vars + `/app/data` volume + `DB_PATH`;
  webhook self-registers on boot from `PUBLIC_URL`. README updated.
