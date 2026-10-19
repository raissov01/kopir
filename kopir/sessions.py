"""Readers for Claude Code and Codex conversations, with an incremental cache.

A conversation is reduced to turns: what the person typed and the agent's
last visible answer to it. Tool calls, thinking and subagent chatter are
dropped. Claude transcripts are append-only JSONL, so each file is resumed
from the byte offset where the previous read stopped; Codex threads live in
SQLite and are re-read only when their `updated_at_ms` moves.
"""
import json
import os
import re
import sqlite3
import time
from pathlib import Path

CACHE_VERSION = 2
U_CHARS = 600
A_CHARS = 900
MAX_TURNS = 40
_WANTED = re.compile(rb'"type": ?"(?:user|assistant|ai-title)"')


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _keep_tail(turns: list) -> list:
    # The first turns say what the conversation was about, the last ones
    # where it ended up.
    if len(turns) <= MAX_TURNS:
        return turns
    return turns[:4] + turns[-(MAX_TURNS - 4):]


def _human_text(text: str) -> bool:
    t = text.lstrip()
    return bool(t) and not t.startswith("<")


# --- Claude Code -----------------------------------------------------------

def _claude_user_text(d: dict) -> str | None:
    if d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
        return None
    origin = d.get("origin") or {}
    if origin.get("kind", "human") != "human":
        return None
    content = (d.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return None
        content = " ".join(c.get("text", "") for c in content
                           if isinstance(c, dict) and c.get("type") == "text")
    if not isinstance(content, str) or not _human_text(content):
        return None
    return content


def _claude_assistant_text(d: dict) -> str:
    if d.get("isSidechain"):
        return ""
    content = (d.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return ""
    return " ".join(c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text").strip()


def read_claude_file(path: Path, entry: dict | None, deadline: float) -> dict:
    """Resume `entry` (or start fresh) and read the complete lines added since."""
    size = path.stat().st_size
    if entry is None or size < entry.get("offset", 0):
        entry = {"offset": 0, "pending": None, "last_a": "",
                 "session": {"agent": "claude", "id": path.stem, "cwd": "",
                             "title": "", "started": "", "updated": "", "turns": []}}
    s = entry["session"]
    with path.open("rb") as fh:
        fh.seek(entry["offset"])
        while True:
            if time.monotonic() > deadline:
                break
            raw = fh.readline()
            if not raw.endswith(b"\n"):
                break
            entry["offset"] += len(raw)
            # Cheap filter first: most lines are tool output and attachments.
            if not _WANTED.search(raw):
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            kind = d.get("type")
            if kind == "ai-title":
                s["title"] = d.get("aiTitle") or s["title"]
                continue
            ts = d.get("timestamp", "")
            if d.get("cwd") and not s["cwd"]:
                s["cwd"] = d["cwd"]
            if kind == "user":
                text = _claude_user_text(d)
                if text is None:
                    continue
                if entry["pending"]:
                    s["turns"].append({**entry["pending"], "a": _clip(entry["last_a"], A_CHARS)})
                    s["turns"] = _keep_tail(s["turns"])
                entry["pending"] = {"u": _clip(text, U_CHARS), "t": ts}
                entry["last_a"] = ""
                s["started"] = s["started"] or ts
                s["updated"] = ts
            elif kind == "assistant":
                text = _claude_assistant_text(d)
                if text:
                    entry["last_a"] = text
                    if ts:
                        s["updated"] = ts
    entry["size"] = size
    return entry


def claude_turns(entry: dict) -> list:
    turns = list(entry["session"]["turns"])
    if entry.get("pending"):
        turns.append({**entry["pending"], "a": _clip(entry.get("last_a", ""), A_CHARS)})
    return turns


# --- Codex -------------------------------------------------------------------

def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    con.row_factory = sqlite3.Row
    return con


def _iso_ms(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + "Z"


def codex_threads(codex_home: str, sources: list[str], since_ms: int) -> list[sqlite3.Row]:
    state = Path(codex_home) / "state_5.sqlite"
    if not state.is_file():
        state = next(iter(sorted(Path(codex_home).glob("state_*.sqlite"), reverse=True)), None)
        if state is None:
            return []
    marks = ",".join("?" * len(sources))
    with _ro(state) as con:
        return con.execute(
            f"select id, cwd, title, name, created_at_ms, updated_at_ms from threads "
            f"where source in ({marks}) and archived = 0 and updated_at_ms >= ? "
            f"order by updated_at_ms desc", (*sources, since_ms)).fetchall()


def read_codex_thread(con: sqlite3.Connection, row: sqlite3.Row) -> dict:
    items = con.execute(
        "select turn_id, item_type, item_json, created_at_ms from thread_items "
        "where thread_id = ? and item_type in ('userMessage', 'agentMessage') "
        "order by created_at_ms, rollout_ordinal", (row["id"],)).fetchall()
    turns, by_turn = [], {}
    for it in items:
        try:
            d = json.loads(it["item_json"])
        except ValueError:
            continue
        turn = by_turn.get(it["turn_id"])
        if turn is None:
            turn = {"u": "", "a": "", "final": False, "t": _iso_ms(it["created_at_ms"])}
            by_turn[it["turn_id"]] = turn
            turns.append(turn)
        if it["item_type"] == "userMessage":
            text = " ".join(c.get("text", "") for c in d.get("content") or []
                            if isinstance(c, dict) and c.get("type") == "text")
            if _human_text(text):
                turn["u"] = _clip((turn["u"] + " " + text).strip(), U_CHARS)
        else:
            final = d.get("phase") == "final_answer"
            if final or not turn["final"]:
                turn["a"] = _clip(d.get("text", ""), A_CHARS)
                turn["final"] = turn["final"] or final
    turns = [{"u": t["u"], "a": t["a"], "t": t["t"]} for t in turns if t["u"]]
    title = row["name"] or (row["title"] or "").split("\n")[0]
    return {"agent": "codex", "id": row["id"], "cwd": row["cwd"] or "",
            "title": _clip(title, 90), "started": _iso_ms(row["created_at_ms"]),
            "updated": _iso_ms(row["updated_at_ms"]), "turns": _keep_tail(turns)}


# --- Cache ---------------------------------------------------------------------

class Cache:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.path = Path(cfg["cache_dir"]) / "sessions.json"
        self.data = {"version": CACHE_VERSION, "claude": {}, "codex": {}}
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if d.get("version") == CACHE_VERSION:
                self.data = d
        except (OSError, ValueError):
            pass

    def refresh(self, agents: set[str], deadline: float) -> None:
        since = time.time() - self.cfg["window_days"] * 86400
        if "claude" in agents:
            self._refresh_claude(since, deadline)
        if "codex" in agents:
            self._refresh_codex(int(since * 1000))

    def _refresh_claude(self, since: float, deadline: float) -> None:
        root = Path(self.cfg["claude_projects"])
        live = {}
        files = []
        for p in root.glob("*/*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime >= since:
                files.append((st.st_mtime, p))
        # Newest first, so a tight deadline still covers what matters.
        for _, p in sorted(files, reverse=True):
            key = str(p)
            entry = self.data["claude"].get(key)
            if entry is not None and entry.get("size") == p.stat().st_size:
                live[key] = entry
                continue
            if time.monotonic() > deadline:
                if entry is not None:
                    live[key] = entry
                continue
            live[key] = read_claude_file(p, entry, deadline)
        self.data["claude"] = live

    def _refresh_codex(self, since_ms: int) -> None:
        home = Path(self.cfg["codex_home"])
        hist = home / "thread_history_1.sqlite"
        if not hist.is_file():
            return
        try:
            rows = codex_threads(str(home), self.cfg["codex_sources"], since_ms)
            live = {}
            with _ro(hist) as con:
                for row in rows:
                    old = self.data["codex"].get(row["id"])
                    if old is not None and old.get("updated_ms") == row["updated_at_ms"]:
                        live[row["id"]] = old
                        continue
                    live[row["id"]] = {"updated_ms": row["updated_at_ms"],
                                       "session": read_codex_thread(con, row)}
        except sqlite3.Error:
            return  # Codex is writing; keep the previous view.
        self.data["codex"] = live

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def sessions(self, agent: str) -> list[dict]:
        out = []
        if agent == "claude":
            for e in self.data["claude"].values():
                s = dict(e["session"])
                s["turns"] = claude_turns(e)
                if s["turns"]:
                    out.append(s)
        else:
            out = [e["session"] for e in self.data["codex"].values() if e["session"]["turns"]]
        out.sort(key=lambda s: s.get("updated", ""), reverse=True)
        return out
