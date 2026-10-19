"""Readers for agent conversations, with an incremental cache.

Supported: Claude Code, Codex, Gemini CLI, GitHub Copilot CLI, and a journal
that any MCP client can append to (see mcp.py).

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

from .redact import redact

CACHE_VERSION = 4
U_CHARS = 600
A_CHARS = 900
MAX_TURNS = 40
_WANTED = re.compile(rb'"type": ?"(?:user|assistant|ai-title)"')


def _clip(text: str, n: int) -> str:
    text = redact(" ".join(text.split()))
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


# --- Gemini CLI ----------------------------------------------------------------

def _gemini_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def read_gemini_file(path: Path) -> dict:
    """~/.gemini/tmp/<project>/chats/session-*.jsonl: a header line, then
    messages, re-recorded under the same id as they grow, and `$set` lines
    that replace fields of the conversation."""
    header, messages = {}, {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if "$set" in d:
            if isinstance(d["$set"].get("messages"), list):
                messages = {m.get("id"): m for m in d["$set"]["messages"]}
        elif "sessionId" in d and "type" not in d:
            header = d
        elif d.get("id") and d.get("type"):
            messages[d["id"]] = d
    root = path.parent.parent / ".project_root"
    cwd = root.read_text(encoding="utf-8").strip() if root.is_file() else ""
    turns = []
    for m in messages.values():
        text = _gemini_text(m.get("content")).strip()
        if m.get("type") == "user" and _human_text(text):
            turns.append({"u": _clip(text, U_CHARS), "a": "", "t": m.get("timestamp", "")})
        elif m.get("type") == "gemini" and text and turns:
            turns[-1]["a"] = _clip(text, A_CHARS)
    return {"agent": "gemini", "id": header.get("sessionId", path.stem), "cwd": cwd,
            "title": "", "started": header.get("startTime", ""),
            "updated": turns[-1]["t"] if turns else header.get("lastUpdated", ""),
            "turns": _keep_tail(turns)}


# --- GitHub Copilot CLI -------------------------------------------------------------

def read_copilot_file(path: Path) -> dict:
    """<copilot home>/session-state/<id>/events.jsonl: typed events; the last
    non-empty assistant message before the next user message is the answer."""
    s = {"agent": "copilot", "id": path.parent.name, "cwd": "", "title": "",
         "started": "", "updated": "", "turns": []}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        kind, data = d.get("type"), d.get("data") or {}
        ts = d.get("timestamp", "")
        if kind == "session.start":
            s["cwd"] = (data.get("context") or {}).get("cwd", "")
            s["started"] = data.get("startTime", ts)
        elif kind == "user.message":
            text = data.get("content") or ""
            if _human_text(text):
                s["turns"].append({"u": _clip(text, U_CHARS), "a": "", "t": ts})
                s["updated"] = ts
        elif kind == "assistant.message" and s["turns"]:
            text = (data.get("content") or "").strip()
            if text:
                s["turns"][-1]["a"] = _clip(text, A_CHARS)
                s["updated"] = ts
    s["turns"] = _keep_tail(s["turns"])
    return s


# --- Journal: turns that MCP clients log themselves ------------------------------------

def journal_path(cfg: dict) -> Path:
    return Path(cfg["data_dir"]) / "journal.jsonl"


def append_journal(cfg: dict, entry: dict) -> None:
    p = journal_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_journal(cfg: dict, since_iso: str) -> list[dict]:
    p = journal_path(cfg)
    if not p.is_file():
        return []
    by = {}
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("t", "") < since_iso:
            continue
        agent = d.get("agent") or "mcp"
        key = (agent, d.get("session") or d.get("t", "")[:10])
        s = by.setdefault(key, {"agent": agent, "id": key[1], "cwd": d.get("cwd", ""),
                                "title": d.get("title", ""), "started": d["t"],
                                "updated": d["t"], "turns": []})
        s["turns"].append({"u": _clip(d.get("u", ""), U_CHARS),
                           "a": _clip(d.get("a", ""), A_CHARS), "t": d["t"]})
        s["updated"] = d["t"]
        s["title"] = s["title"] or d.get("title", "")
    for s in by.values():
        s["turns"] = _keep_tail(s["turns"])
    return list(by.values())


# --- Cache ---------------------------------------------------------------------

FILE_READERS = {"gemini": read_gemini_file, "copilot": read_copilot_file}


def _gemini_files(cfg: dict) -> list[Path]:
    return list(Path(cfg["gemini_home"]).glob("tmp/*/chats/session-*.json*"))


def _copilot_files(cfg: dict) -> list[Path]:
    out = []
    for home in cfg["copilot_homes"]:
        out += Path(home).expanduser().glob("session-state/*/events.jsonl")
    return out


FILE_SOURCES = {"gemini": _gemini_files, "copilot": _copilot_files}


class Cache:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.path = Path(cfg["cache_dir"]) / "sessions.json"
        self.data = {"version": CACHE_VERSION}
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if d.get("version") == CACHE_VERSION:
                self.data = d
        except (OSError, ValueError):
            pass
        for agent in ("claude", "codex", *FILE_READERS):
            self.data.setdefault(agent, {})
        self.journal = []

    def refresh(self, agents: set[str], deadline: float) -> None:
        since = time.time() - self.cfg["window_days"] * 86400
        for agent in agents:
            try:
                if agent == "claude":
                    self._refresh_claude(since, deadline)
                elif agent == "codex":
                    self._refresh_codex(int(since * 1000))
                elif agent in FILE_READERS:
                    self._refresh_files(agent, since, deadline)
            except OSError:
                continue  # one unreadable agent must not hide the others
        self.journal = read_journal(self.cfg, _iso_ms(int(since * 1000)))

    def _refresh_files(self, agent: str, since: float, deadline: float) -> None:
        live, old = {}, self.data[agent]
        for p in FILE_SOURCES[agent](self.cfg):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < since:
                continue
            key, entry = str(p), old.get(str(p))
            if entry is not None and entry.get("size") == st.st_size:
                live[key] = entry
            elif time.monotonic() < deadline:
                live[key] = {"size": st.st_size, "session": FILE_READERS[agent](p)}
            elif entry is not None:
                live[key] = entry
        self.data[agent] = live

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
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        elif agent in self.data and agent != "version":
            out = [e["session"] for e in self.data[agent].values() if e["session"]["turns"]]
        out += [s for s in self.journal if s["agent"] == agent]
        out.sort(key=lambda s: s.get("updated", ""), reverse=True)
        return out

    def agents(self) -> set[str]:
        """Every agent with at least one conversation in the window."""
        found = {a for a in ("claude", "codex", *FILE_READERS) if self.sessions(a)}
        return found | {s["agent"] for s in self.journal}
