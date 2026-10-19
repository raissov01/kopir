"""Paths and limits. Defaults fit a stock Claude Code + Codex install; any key
can be overridden in ~/.config/kopir/config.json."""
import json
import os
from pathlib import Path

HOME = Path.home()


def _claude_slug(path: Path) -> str:
    # Claude Code names a project directory after its absolute path.
    return str(path).replace("/", "-").replace(".", "-")


DEFAULTS = {
    # Memory records shared by every agent.
    "memory_dir": str(HOME / ".claude" / "projects" / _claude_slug(HOME) / "memory"),
    "claude_projects": str(HOME / ".claude" / "projects"),
    "codex_home": str(HOME / ".codex"),
    "cache_dir": str(HOME / ".cache" / "kopir"),
    # Codex thread sources that are a person typing; "exec" runs are scripts.
    "codex_sources": ["cli", "vscode"],
    # Index files never served as records.
    "index_files": ["MEMORY.md", "INDEX-full.md"],
    "window_days": 14,          # how far back chats are indexed
    "recent_hours": 48,         # what the session-start digest shows
    "recent_sessions": 6,
    "recall_records": 3,
    "recall_turns": 2,
    "min_score": 4.0,
    "min_hits": 2,
    "budget_records": 3600,     # characters
    "budget_turns": 1600,
    "budget_recent": 2600,
    "record_chars": 1300,
    "hook_deadline_sec": 3.0,
    # Agents that do not load MEMORY.md themselves get it at session start.
    "inject_index_for": ["codex"],
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    path = Path(os.environ.get("KOPIR_CONFIG", HOME / ".config" / "kopir" / "config.json"))
    if path.is_file():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    for key in ("memory_dir", "claude_projects", "codex_home", "cache_dir"):
        env = os.environ.get("KOPIR_" + key.upper())
        if env:
            cfg[key] = env
    return cfg
