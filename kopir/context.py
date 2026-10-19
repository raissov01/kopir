"""What kopir injects: a digest of the other agents' recent chats at session
start, and the matching memory records and past turns on every prompt."""
import calendar
import json
import re
import time
from pathlib import Path

from . import agents, memory, rank, sessions
from .redact import redact
from .textnorm import tokens


def others(cache: sessions.Cache, agent: str) -> list[dict]:
    """Conversations of every agent except the one asking, newest first."""
    out = [s for a in sorted(cache.agents()) if a != agent for s in cache.sessions(a)]
    out.sort(key=lambda s: s.get("updated", ""), reverse=True)
    return out


def _short_path(p: str) -> str:
    home = str(Path.home())
    return "~" + p[len(home):] if p.startswith(home) else p


def _epoch(iso: str) -> float | None:
    try:
        return calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _when(iso: str) -> str:
    # "2026-10-19T14:02:11Z" -> "10-19 19:02" in local time.
    t = _epoch(iso)
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t is not None else iso[5:16]


def _age_hours(iso: str) -> float:
    t = _epoch(iso)
    return (time.time() - t) / 3600 if t is not None else 1e9


def _related(cwd: str, other: str) -> bool:
    if not cwd or not other:
        return False
    home = str(Path.home())
    if cwd == home or other == home:
        return cwd == other
    return cwd.startswith(other) or other.startswith(cwd)


# --- Seen state: do not inject the same thing twice into one session ------------

class Seen:
    def __init__(self, cfg: dict, session_id: str):
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "none")
        self.path = Path(cfg["cache_dir"]) / "seen" / f"{safe}.json"
        try:
            self.items = set(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            self.items = set()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(self.items)), encoding="utf-8")


# --- Session start ----------------------------------------------------------------

def recent_digest(cfg: dict, cache: sessions.Cache, agent: str, cwd: str) -> str:
    recent = [s for s in others(cache, agent) if _age_hours(s["updated"]) <= cfg["recent_hours"]]
    if not recent:
        return ""
    # Already newest first; a stable sort lifts this project's chats to the top.
    recent.sort(key=lambda s: not _related(cwd, s["cwd"]))
    lines = [f"## Other AI agents: conversations in the last {cfg['recent_hours']} h "
             f"(kopir; `kopir show <agent> <id>` prints one in full)"]
    used = len(lines[0])
    for s in recent[: cfg["recent_sessions"]]:
        last = s["turns"][-1]
        title = s["title"] or s["turns"][0]["u"][:80]
        block = (f"- [{agents.label(s['agent'])}] {_when(s['updated'])} · "
                 f"{_short_path(s['cwd'])} · «{title}» "
                 f"({len(s['turns'])} turns, id {s['id'][:8]})\n"
                 f"  last ask: {last['u'][:220]}\n"
                 f"  answer: {last['a'][:320] or '(none yet)'}")
        if used + len(block) > cfg["budget_recent"]:
            break
        lines.append(block)
        used += len(block)
    return "\n".join(lines) if len(lines) > 1 else ""


def index_text(cfg: dict) -> str:
    p = Path(cfg["memory_dir"]) / "MEMORY.md"
    if not p.is_file():
        return ""
    return (f"## Shared memory index ({_short_path(str(p.parent))})\n"
            + p.read_text(encoding="utf-8").strip())


# --- Per prompt -------------------------------------------------------------------

def recall_records(cfg: dict, prompt: str, seen: Seen | None) -> tuple[str, list[str]]:
    recs = memory.load(cfg["memory_dir"], cfg["index_files"])
    hits = rank.search(prompt, [r.doc() for r in recs], limit=cfg["recall_records"] + 3,
                       min_score=cfg["min_score"], min_hits=cfg["min_hits"],
                       heads=[set(r.head_tokens()) for r in recs])
    parts, names, used = [], [], 0
    for _, i in hits:
        r = recs[i]
        key = "rec:" + r.stem
        if seen is not None and key in seen.items:
            continue
        body = redact(r.body.strip())
        if len(body) > cfg["record_chars"]:
            body = body[: cfg["record_chars"] - 1] + "…"
        stamp = " · ".join(x for x in (r.meta.get("source"), r.meta.get("observed_at")) if x)
        block = f"### {r.path.name}{f' ({stamp})' if stamp else ''}\n{r.description}\n\n{body}"
        if used + len(block) > cfg["budget_records"]:
            continue
        parts.append(block)
        names.append(key)
        used += len(block)
        if len(parts) >= cfg["recall_records"]:
            break
    if not parts:
        return "", []
    head = f"## Memory records matching this prompt (kopir, from {_short_path(cfg['memory_dir'])})"
    return head + "\n\n" + "\n\n".join(parts), names


def recall_turns(cfg: dict, cache: sessions.Cache, agent: str, prompt: str,
                 seen: Seen | None) -> tuple[str, list[str]]:
    flat = [(s, t) for s in others(cache, agent) for t in s["turns"]]
    if not flat:
        return "", []
    heads = [tokens(f"{s['title']} {t['u']}") for s, t in flat]
    docs = [h * 2 + tokens(t["a"]) for h, (s, t) in zip(heads, flat)]
    hits = rank.search(prompt, docs, limit=cfg["recall_turns"] + 3,
                       min_score=cfg["min_score"], min_hits=cfg["min_hits"],
                       heads=[set(h) for h in heads])
    parts, keys, used = [], [], 0
    for _, i in hits:
        s, t = flat[i]
        key = f"turn:{s['agent']}:{s['id']}:{t['t']}"
        if seen is not None and key in seen.items:
            continue
        block = (f"- [{agents.label(s['agent'])}] {_when(t['t'])} · {_short_path(s['cwd'])} · "
                 f"«{(s['title'] or s['turns'][0]['u'])[:70]}» (id {s['id'][:8]})\n"
                 f"  asked: {t['u'][:400]}\n  answered: {t['a'][:600]}")
        if used + len(block) > cfg["budget_turns"]:
            continue
        parts.append(block)
        keys.append(key)
        used += len(block)
        if len(parts) >= cfg["recall_turns"]:
            break
    if not parts:
        return "", []
    return "## Related turns from other AI agents (kopir)\n" + "\n".join(parts), keys
