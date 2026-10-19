"""Hook entry point for Claude Code and Codex (same wire format).

Reads the event JSON on stdin and prints `hookSpecificOutput.additionalContext`.
A hook must never stand between a person and their prompt: every failure is
logged and the hook exits 0 with no output.
"""
import json
import sys
import time
import traceback
from pathlib import Path

from . import config, context, sessions


def _emit(event: str, text: str) -> None:
    if text.strip():
        json.dump({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
                  sys.stdout, ensure_ascii=False)


def run(agent: str, payload: dict, cfg: dict) -> str:
    event = payload.get("hook_event_name", "")
    cwd = payload.get("cwd", "")
    sid = payload.get("session_id", "")
    deadline = time.monotonic() + cfg["hook_deadline_sec"]
    other = context.OTHER[agent]
    cache = sessions.Cache(cfg)
    cache.refresh({other}, deadline)
    cache.save()
    seen = context.Seen(cfg, f"{agent}-{sid}")
    parts = []
    if event == "SessionStart":
        if payload.get("source") in ("compact", "clear"):
            seen.items.clear()  # earlier injections are gone from the context
        if agent in cfg["inject_index_for"]:
            parts.append(context.index_text(cfg))
        parts.append(context.recent_digest(cfg, cache, agent, cwd))
    elif event == "UserPromptSubmit":
        prompt = payload.get("prompt", "")
        text, keys = context.recall_records(cfg, prompt, seen)
        parts.append(text)
        seen.items.update(keys)
        text, keys = context.recall_turns(cfg, cache, agent, prompt, seen)
        parts.append(text)
        seen.items.update(keys)
    seen.save()
    return "\n\n".join(p for p in parts if p)


def main(agent: str) -> int:
    cfg = config.load()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        _emit(payload.get("hook_event_name", ""), run(agent, payload, cfg))
    except Exception:  # noqa: BLE001 - see module docstring
        log = Path(cfg["cache_dir"]) / "hook.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(time.strftime("%F %T ") + agent + "\n" + traceback.format_exc() + "\n")
        except OSError:
            pass
    return 0
