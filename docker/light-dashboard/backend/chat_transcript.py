"""Turning a raw Hermes transcript into something a chat panel can render.

`state.db`'s `messages` table is the agent's own working record, not a display
format. A turn that uses tools writes an assistant row with *empty* content and
a `tool_calls` blob, followed by one row per tool result whose content is that
tool's raw JSON return value. Rendered verbatim — which is what this dashboard
did — every tool result becomes a chat bubble full of JSON, and every tool-call
turn becomes an empty bubble. On a working session that is more than half of
what the panel shows.

What a chat panel wants instead is a typed list: prose from the user and the
agent, and everything the agent *did* collapsed to one labelled line each,
openable when someone wants the detail. That is what `shape()` produces.

The same summarizing is reused live by the streaming endpoint, which sees the
same tool calls and results as they happen rather than after the fact.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

# Every column of `messages` worth reading. The table carries far more than the
# four fields this dashboard used to select — the reasoning trace and the
# tool-call linkage are what make a turn legible, and both were being dropped.
WANTED_COLUMNS = [
    "id",
    "role",
    "content",
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "timestamp",
    "reasoning_content",
    "reasoning",
    "finish_reason",
]

# What we cannot render without. Older gateway schemas may lack the rest, so the
# column list is intersected with what the DB actually has before querying.
REQUIRED_COLUMNS = ["id", "role", "content", "timestamp"]

# The gateway reports reasoning deltas through the tool-progress channel under
# this reserved name rather than as a tool of its own.
THINKING_TOOL_NAME = "_thinking"

# A chip line, not a paragraph. Long enough to carry "3 results" or the first
# line of an error, short enough that a run of tool calls stays scannable.
SUMMARY_LIMIT = 140
VALUE_LIMIT = 60

# Keys whose integer value already answers "what came back".
COUNT_KEYS = ("total_count", "count", "total", "num_results")

# Bookkeeping keys that say nothing on their own — every successful call has
# them, so spending summary width on them buys nothing.
UNINTERESTING_KEYS = ("ok", "success", "status_code")


def select_columns(present: List[str]) -> List[str]:
    """The WANTED columns this database actually has, or the bare minimum."""
    have = set(present or [])
    chosen = [c for c in WANTED_COLUMNS if c in have]
    return chosen if chosen else list(REQUIRED_COLUMNS)


def shape(rows: Any) -> List[Dict[str, Any]]:
    """Raw `messages` rows -> the typed entries the chat panel renders.

    Three kinds come out: `user`, `assistant` (with an optional `thinking`
    trace), and `tool` (a name, a one-line summary, and the full raw result
    kept in `detail` for expansion). Assistant rows that carry only tool calls
    are dropped — their calls are already represented by the tool entries that
    follow, and left in they render as empty bubbles.
    """
    records = [dict(r) for r in (rows or [])]
    calls = _index_tool_calls(records)
    out: List[Dict[str, Any]] = []

    for row in records:
        role = row.get("role")
        base = {"id": row.get("id"), "timestamp": row.get("timestamp")}

        if role == "user":
            out.append(dict(base, kind="user", role="user", content=_text_of(row.get("content"))))

        elif role == "assistant":
            content = _text_of(row.get("content")).strip()
            thinking = (row.get("reasoning_content") or row.get("reasoning") or "").strip()
            if not content:
                # A turn that only called tools. Its calls already show as the
                # tool entries below it, so the row itself would render as an
                # empty bubble — but the reasoning attached to it is the label
                # for the tool run that follows, and worth keeping as a step.
                if thinking:
                    out.append(dict(base, kind="thinking", role="assistant", content="", thinking=thinking))
                continue
            out.append(dict(base, kind="assistant", role="assistant", content=content, thinking=thinking))

        elif role == "tool":
            call = calls.get(row.get("tool_call_id")) or {}
            raw = _text_of(row.get("content"))
            status, summary = summarize_result(raw)
            out.append(dict(
                base,
                kind="tool",
                role="tool",
                tool_name=row.get("tool_name") or call.get("name") or "tool",
                args=call.get("args", ""),
                status=status,
                summary=summary,
                detail=raw,
            ))

        # session_meta and anything else the gateway records is bookkeeping,
        # not conversation.

    return out


def summarize_result(raw: Any) -> Tuple[str, str]:
    """One tool result -> (status, one-line summary).

    Status is "error" when the payload says so in any of the shapes Hermes
    tools use — an `error` key, or an explicit `success`/`ok` false — so a
    failed call is visible without opening it.
    """
    text = _text_of(raw).strip()
    if not text:
        return "ok", ""

    data = _load_json(text)
    if data is None:
        return "ok", _clip(_collapse(text), SUMMARY_LIMIT)

    if isinstance(data, dict):
        error = data.get("error")
        if error:
            return "error", _clip(_collapse(str(error)), SUMMARY_LIMIT)
        if data.get("success") is False or data.get("ok") is False:
            return "error", _clip(_describe_dict(data) or "failed", SUMMARY_LIMIT)
        return "ok", _clip(_describe_dict(data), SUMMARY_LIMIT)

    if isinstance(data, list):
        return "ok", f"{len(data)} item{'' if len(data) == 1 else 's'}"

    return "ok", _clip(_collapse(str(data)), SUMMARY_LIMIT)


def summarize_args(raw: Any) -> str:
    """A tool call's arguments -> a short `k=v, k=v` line.

    Accepts either the JSON string the transcript stores or the already-decoded
    dict the live event stream hands over.
    """
    data = raw if isinstance(raw, (dict, list)) else _load_json(_text_of(raw))
    if isinstance(data, dict):
        return _clip(_join_pairs(data, skip=()), SUMMARY_LIMIT)
    if isinstance(data, list):
        return f"{len(data)} arg{'' if len(data) == 1 else 's'}"
    text = _collapse(_text_of(raw))
    return _clip(text, SUMMARY_LIMIT)


# --- internals ---


def _index_tool_calls(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """call_id -> {name, args}, read off the assistant rows that requested them.

    A tool result row names its tool but not what it was asked to do; the
    arguments live one row earlier, in the assistant turn's `tool_calls`.
    """
    index: Dict[str, Dict[str, str]] = {}
    for row in records:
        raw = row.get("tool_calls")
        parsed = raw if isinstance(raw, list) else _load_json(raw if isinstance(raw, str) else "")
        if not isinstance(parsed, list):
            continue
        for call in parsed:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id") or call.get("call_id")
            if not call_id:
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            index[call_id] = {
                "name": fn.get("name") or call.get("name") or "",
                "args": summarize_args(fn.get("arguments")),
            }
    return index


def _text_of(value: Any) -> str:
    """Message content as plain text.

    Multimodal turns are stored as a JSON list of parts; a chat bubble wants
    the prose out of them, not the envelope. Only lists that actually look like
    content parts get unwrapped — a tool result is very often a JSON array of
    its own, and reading that as a message would silently blank it.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        if value.startswith("["):
            parsed = _load_json(value)
            if _is_content_parts(parsed):
                return _join_parts(parsed)
        return value
    if isinstance(value, list):
        return _join_parts(value) if _is_content_parts(value) else json.dumps(value)
    return str(value)


def _is_content_parts(value: Any) -> bool:
    """Whether a list is a multimodal content array rather than plain data."""
    if not isinstance(value, list) or not value:
        return False
    return all(
        isinstance(part, dict) and ("text" in part or "type" in part)
        for part in value
    )


def _join_parts(parts: List[Any]) -> str:
    out = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text)
        elif part.get("type") in ("image_url", "input_image"):
            out.append("[image]")
    return "\n".join(out)


def _load_json(text: str) -> Optional[Any]:
    stripped = (text or "").strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return None


def _describe_dict(data: Dict[str, Any]) -> str:
    """The shortest honest answer to "what did this return"."""
    for key in COUNT_KEYS:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return f"{value} result{'' if value == 1 else 's'}"
    return _join_pairs(data, skip=UNINTERESTING_KEYS) or "done"


def _join_pairs(data: Dict[str, Any], skip: Tuple[str, ...], limit: int = 3) -> str:
    """`k=v` for the first few keys, with the bulky ones demoted.

    A payload like `{"content": "<2kB of file>", "total_lines": 64}` reads far
    better as "total_lines=64" first — the long value would otherwise eat the
    whole line and push out the keys that actually summarize the call.
    """
    compact, bulky = [], []
    for key, value in data.items():
        if key in skip:
            continue
        if isinstance(value, (str, int, float, bool)):
            rendered = f"{key}={_clip(_collapse(str(value)), VALUE_LIMIT)}"
            (bulky if isinstance(value, str) and len(value) > VALUE_LIMIT else compact).append(rendered)
        elif isinstance(value, list):
            compact.append(f"{key}[{len(value)}]")
        elif isinstance(value, dict):
            compact.append(f"{key}{{{len(value)}}}")
    return ", ".join((compact + bulky)[:limit])


def _collapse(text: str) -> str:
    return " ".join((text or "").split())


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
