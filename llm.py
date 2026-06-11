"""LLM layer. Uses Claude Haiku to turn a messy one-line note into structure.

Only two tiny calls exist, both with small payloads, so cost is a few cents/month.
Everything else (briefings, reminders) is computed deterministically in Python.
"""
import os
import json
import anthropic

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

_CAPTURE_SYSTEM = """You convert a short personal work note into JSON for a task tracker.
Return ONLY a JSON object, no prose, no markdown fences.

Fields:
- "type": one of "task", "commitment", "idea", "note".
    commitment = the user promised something to a named person.
    task = something the user must do, with or without a deadline.
    idea = something to explore later, no deadline.
    note = anything else.
- "title": a concise imperative summary (max ~10 words).
- "person": the other person's name if this involves/was promised to someone, else null.
- "due": ISO 8601 datetime in the given timezone if a deadline is stated or implied, else null.
    Resolve relative dates ("Mon", "tomorrow", "EOD Friday") against the given current datetime.
    If a date has no time, assume 18:00 local (end of day).
- "effort_minutes": integer estimate if the user states effort ("3h", "half day"), else null.
    half day = 240, full day = 480.
- "explicit_reminder": ISO 8601 datetime ONLY if the user explicitly asks to be reminded
    at a specific time, else null.
"""

_WHEN_SYSTEM = """Return ONLY an ISO 8601 datetime (in the given timezone) for the user's
phrase, or the bare word null. No prose. If no time of day is given, assume 09:00 local."""


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no json found")
    return json.loads(text[start:end + 1])


def parse_capture(note, now_iso, tz_name):
    """Returns a dict with type/title/person/due/effort_minutes/explicit_reminder."""
    try:
        msg = _client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=_CAPTURE_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Current datetime: {now_iso} (timezone {tz_name}).\nNote: {note}",
            }],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        data = _extract_json(text)
    except Exception:
        data = {}
    # Safe defaults so a parsing hiccup never drops a capture.
    return {
        "type": data.get("type") or "note",
        "title": (data.get("title") or note).strip()[:200],
        "person": data.get("person"),
        "due": data.get("due"),
        "effort_minutes": data.get("effort_minutes"),
        "explicit_reminder": data.get("explicit_reminder"),
    }


def parse_when(phrase, now_iso, tz_name):
    """Parse a snooze/reschedule phrase into an ISO datetime, or None."""
    try:
        msg = _client.messages.create(
            model=MODEL,
            max_tokens=60,
            system=_WHEN_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Current datetime: {now_iso} (timezone {tz_name}).\nPhrase: {phrase}",
            }],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        return None if text.lower().startswith("null") else text
    except Exception:
        return None
