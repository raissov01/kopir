"""MCP server (stdio, JSON-RPC 2.0, one message per line).

This is how agents without command hooks join the bridge: Cursor, Windsurf,
Claude Desktop, Copilot, Zed, Cline and anything else that speaks MCP. They
read the shared memory and the other agents' chats, write records, and log
their own turns so the hook agents can see them in return.
"""
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

from . import __version__, agents, config, context, memory, sessions
from .redact import has_secret

PROTOCOL = "2025-06-18"

TOOLS = [
    {"name": "recall",
     "description": "Search the shared memory and other AI agents' recent conversations "
                    "for what is relevant to a question. Call it before answering anything "
                    "about the user's projects, preferences or earlier work.",
     "inputSchema": {"type": "object", "required": ["query"],
                     "properties": {"query": {"type": "string"}}}},
    {"name": "recent",
     "description": "What the user discussed with other AI agents (Claude Code, Codex, "
                    "Gemini CLI, Copilot, ...) in the last hours, newest first.",
     "inputSchema": {"type": "object",
                     "properties": {"hours": {"type": "integer", "default": 48},
                                    "cwd": {"type": "string"}}}},
    {"name": "show",
     "description": "Print one conversation of another agent in full, by agent name and "
                    "the id (or id prefix) that `recent` or `recall` showed.",
     "inputSchema": {"type": "object", "required": ["agent", "id"],
                     "properties": {"agent": {"type": "string"}, "id": {"type": "string"}}}},
    {"name": "remember",
     "description": "Save one durable fact to the shared memory every agent reads: who the "
                    "user is (user), how they want agents to work (feedback), a project "
                    "decision or state (project), or a pointer to something external "
                    "(reference). Never store secrets or what the code or git history already "
                    "records. Saving an existing name replaces that record.",
     "inputSchema": {"type": "object", "required": ["name", "description", "type", "body"],
                     "properties": {
                         "name": {"type": "string", "description": "short-kebab-case slug"},
                         "description": {"type": "string", "description": "one line, used for recall"},
                         "type": {"type": "string", "enum": sorted(memory.TYPES)},
                         "body": {"type": "string", "description": "the fact; for feedback and "
                                  "project add **Why:** and **How to apply:** lines"}}}},
    {"name": "log_turn",
     "description": "Log one exchange of this conversation (the user's request and your final "
                    "answer, summarised) so other AI agents can see it. Call it after finishing "
                    "a substantive task.",
     "inputSchema": {"type": "object", "required": ["ask", "answer"],
                     "properties": {"ask": {"type": "string"}, "answer": {"type": "string"},
                                    "title": {"type": "string"}, "cwd": {"type": "string"},
                                    "session": {"type": "string"}}}},
]

def client_agent(info: dict) -> str:
    """'Claude Code' -> 'claude', 'cursor-vscode' -> 'cursor', ..."""
    name = (info.get("name") or "mcp").lower()
    if name.startswith("claude-ai") or "desktop" in name and "claude" in name:
        return "claude-desktop"  # not Claude Code: its chats are not on disk
    for known in (*agents.AGENTS, *agents.MCP_CLIENTS):
        if known in name:
            return known
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:32] or "mcp"


class Server:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.agent = "mcp"

    def _cache(self) -> sessions.Cache:
        cache = sessions.Cache(self.cfg)
        cache.refresh(set(agents.AGENTS), time.monotonic() + 10)
        cache.save()
        return cache

    # --- tools ---

    def recall(self, query: str) -> str:
        records, _ = context.recall_records(self.cfg, query, None)
        turns, _ = context.recall_turns(self.cfg, self._cache(), self.agent, query, None)
        return "\n\n".join(p for p in (records, turns) if p) or "Nothing relevant found."

    def recent(self, hours: int = 48, cwd: str = "") -> str:
        cfg = {**self.cfg, "recent_hours": int(hours)}
        text = context.recent_digest(cfg, self._cache(), self.agent, cwd)
        return text or f"No conversations with other agents in the last {hours} h."

    def show(self, agent: str, id: str) -> str:
        cache = self._cache()
        for s in cache.sessions(agent):
            if s["id"].startswith(id):
                out = [f"# {s['title']}", f"{s['cwd']} · {s['started']} → {s['updated']}", ""]
                for t in s["turns"]:
                    out += [f"> {t['u']}", "", t["a"], ""]
                return "\n".join(out)
        raise ValueError(f"no {agent} conversation starting with {id}")

    def remember(self, name: str, description: str, type: str, body: str) -> str:
        if type not in memory.TYPES:
            raise ValueError(f"type must be one of {sorted(memory.TYPES)}")
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
        if not slug or not description.strip() or not body.strip():
            raise ValueError("name, description and body must not be empty")
        if has_secret(f"{description}\n{body}"):
            raise ValueError("this looks like it contains a credential; not saved")
        root = Path(self.cfg["memory_dir"])
        root.mkdir(parents=True, exist_ok=True)
        stem = f"{type}_{slug.replace('-', '_')}"
        path = root / f"{stem}.md"
        existed = path.exists()
        one_line = " ".join(description.split())
        path.write_text(
            f"---\nname: {slug}\ndescription: {one_line}\nmetadata:\n  type: {type}\n"
            f"  source: {self.agent}\n  observed_at: {dt.date.today().isoformat()}\n---\n\n"
            f"{body.strip()}\n", encoding="utf-8")
        index = root / "MEMORY.md"
        text = index.read_text(encoding="utf-8") if index.is_file() else "# Memory Index\n"
        if f"]({path.name})" not in text:
            index.write_text(text.rstrip("\n") + f"\n- [{slug}]({path.name}) — {one_line}\n",
                             encoding="utf-8")
        return f"{'Updated' if existed else 'Saved'} {path}"

    def log_turn(self, ask: str, answer: str, title: str = "", cwd: str = "",
                 session: str = "") -> str:
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sessions.append_journal(self.cfg, {"agent": self.agent, "session": session or now[:10],
                                           "cwd": cwd, "title": title, "t": now,
                                           "u": ask, "a": answer})
        return "Logged."

    # --- protocol ---

    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            self.agent = client_agent(params.get("clientInfo") or {})
            result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "kopir", "version": __version__},
                      "instructions": "Shared memory and a bridge to the user's other AI "
                                      "agents. Use `recall` before answering about earlier "
                                      "work, `remember` for durable facts, `log_turn` after "
                                      "a finished task."}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            fn = getattr(self, params.get("name", ""), None)
            if params.get("name") not in {t["name"] for t in TOOLS} or fn is None:
                return _error(mid, -32602, f"unknown tool {params.get('name')}")
            try:
                text, failed = fn(**(params.get("arguments") or {})), False
            except (TypeError, ValueError, OSError) as exc:
                text, failed = str(exc), True
            result = {"content": [{"type": "text", "text": text}], "isError": failed}
        elif method == "ping":
            result = {}
        elif mid is None:
            return None  # notification
        else:
            return _error(mid, -32601, f"method not found: {method}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(stdin=sys.stdin, stdout=sys.stdout) -> int:
    server = Server(config.load())
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "parse error")
        else:
            reply = server.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0
