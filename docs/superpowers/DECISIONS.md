# Decision Log — PCS V0+V1 build (2026-06-12)

Autonomous build. "In confusion use the safest and recommended option" — each such
choice is logged here.

| # | Decision | Choice | Why / safest |
|---|----------|--------|--------------|
| 1 | Capacity definition | Per-day **and** weekly budgets | User-selected; catches both single-day spikes and weekly overload |
| 2 | Overflow behavior at capture | Warn + still save | User-selected; never lose a capture, preserves <5s friction |
| 3 | Evening check-in in this increment | Yes | User-selected; without it capacity numbers are garbage-in |
| 4 | Focus Block | Deferred to V2 | User-selected; needs a calendar to be useful |
| 5 | Load bucketing | Approach B (spread start→due across working days) | User-selected; reuses existing start_by, no fake spikes, gives forward warnings |
| 6 | Overall scope | V0+V1 feature-complete | User-selected; voice/screenshot/calendar/negotiation deferred |
| 7 | Testing | Automated pytest + local smoke; Telegram & LLM **mocked** | User-selected; deterministic, no public URL/token spend |
| 8 | Deploy target | Railway | User-selected; matches existing README |
| 9 | Default day budget | `cap_day_min=360` (6h focus/day) | Safe realistic default; configurable via settings |
| 10 | Default week budget | `cap_week_min=1800` (30h focus/week) | 6h × 5 working days; configurable |
| 11 | Working days | Mon–Fri (`work_days=0,1,2,3,4`) | Common default; weekends excluded from spreading |
| 12 | Evening check-in time | `WORK_END_HOUR − 1` (default 20:00) | Safe: end of working day, before WORK_END so it isn't snapped away |
| 13 | Stale-waiting threshold | 3 days since created/last nudge | Conservative; avoids nag spam |
| 14 | Missed definition | `due_at < now` AND status != done | Matches PCS "missed commitments" metric |
| 15 | `__pycache__/llm.cpython-313.pyc` tracked despite .gitignore | Untrack via `git rm --cached` | Was committed before ignore added; should not be in repo |
| 16 | Brainstorm approval gate | Overridden | User explicitly authorized full autonomous completion (YOLO) |
| 17 | Test runner deps | Add `pytest`, `pytest-asyncio` to requirements (dev) | Needed for suite; harmless in prod image |
| 18 | LLM model | Switch `sarvam-30b` → `sarvam-105b`; raise capture max_tokens 400→2000, when 60→1500 | 30b is a reasoning model that exhausts tokens mid-reasoning and returns `content=null` → every capture silently fell back to "note/no date". 105b finishes (~1400 tok) and emits valid JSON. Added `reasoning_content` fallback + ISO regex in `parse_when`. |
