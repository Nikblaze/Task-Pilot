"""LLM layer. Uses an OpenAI-compatible API to turn a messy one-line note into structure.

Configure via env vars:
  API_KEY       — provider API key (preferred name)
  SARVAM_API_KEY / GROQ_API_KEY / ANTHROPIC_API_KEY — also accepted
  MODEL         — model id (auto-defaults from LLM_BASE_URL if omitted)
  LLM_BASE_URL  — OpenAI-compatible base URL (auto-defaults from key var)

Only two tiny calls exist, both with small payloads.
Everything else (briefings, reminders) is computed deterministically in Python.
"""
import os
import json
from openai import OpenAI

# import anthropic
# MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
# _client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

_KEY_VARS = ("API_KEY", "SARVAM_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY")
_DEFAULT_BASE_URLS = {
    "GROQ_API_KEY": "https://api.groq.com/openai/v1",
    "SARVAM_API_KEY": "https://api.sarvam.ai/v1",
}
_DEFAULT_MODELS = {
    "https://api.groq.com/openai/v1": "llama-3.3-70b-versatile",
    "https://api.sarvam.ai/v1": "sarvam-30b",
}


def _resolve_api_key():
    for name in _KEY_VARS:
        val = os.getenv(name, "").strip()
        if val:
            return val, name
    return "", ""


def _resolve_base_url(key_var):
    explicit = os.getenv("LLM_BASE_URL", "").strip()
    if explicit:
        return explicit
    if key_var in _DEFAULT_BASE_URLS:
        return _DEFAULT_BASE_URLS[key_var]
    return "https://api.sarvam.ai/v1"


API_KEY, _API_KEY_VAR = _resolve_api_key()
LLM_BASE_URL = _resolve_base_url(_API_KEY_VAR)
MODEL = os.getenv("MODEL") or _DEFAULT_MODELS.get(LLM_BASE_URL, "sarvam-30b")

_client = None


def _get_client():
    global _client
    if _client is None:
        if not API_KEY:
            raise RuntimeError(
                "No LLM API key set. Add API_KEY (or SARVAM_API_KEY / GROQ_API_KEY) in env."
            )
        _client = OpenAI(api_key=API_KEY, base_url=LLM_BASE_URL)
    return _client

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
        resp = _get_client().chat.completions.create(
            model=MODEL, max_tokens=400,
            messages=[{"role": "system", "content": _CAPTURE_SYSTEM},
                        {"role": "user", "content": f"Current datetime: {now_iso} ({tz_name}).\nNote: {note}"}],
        )
        text = resp.choices[0].message.content
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
        resp = _get_client().chat.completions.create(
            model=MODEL, max_tokens=60,
            messages=[{"role": "system", "content": _WHEN_SYSTEM},
                        {"role": "user", "content": f"Current datetime: {now_iso} ({tz_name}).\nPhrase: {phrase}"}],
        )
        text = resp.choices[0].message.content.strip()
        return None if text.lower().startswith("null") else text
    except Exception:
        return None
