"""The agents kopir bridges, and how each one is wired in.

`hooks` agents run kopir on session start and before every prompt and get its
output as context. Every agent listed here also has a transcript reader in
sessions.py, so it shows up in the others' digests. Agents without either
(Cursor, Windsurf, Claude Desktop, ...) join through the MCP server.
"""
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()


@dataclass(frozen=True)
class Agent:
    name: str
    label: str
    home: str                      # config key or path whose presence means installed
    hooks_file: str = ""           # settings file that takes a "hooks" object
    start_event: str = ""
    prompt_event: str = ""
    timeout: int = 0               # in the unit the agent expects
    instructions_file: str = ""    # global context file for the memory format
    mcp_file: str = ""             # JSON file with an "mcpServers" object


AGENTS = {
    "claude": Agent("claude", "Claude Code", "~/.claude",
                    hooks_file="~/.claude/settings.json", start_event="SessionStart",
                    prompt_event="UserPromptSubmit", timeout=10),
    "codex": Agent("codex", "Codex", "codex_home",
                   hooks_file="{codex_home}/hooks.json", start_event="SessionStart",
                   prompt_event="UserPromptSubmit", timeout=10,
                   instructions_file="{codex_home}/AGENTS.md"),
    "gemini": Agent("gemini", "Gemini CLI", "gemini_home",
                    hooks_file="{gemini_home}/settings.json", start_event="SessionStart",
                    prompt_event="BeforeAgent", timeout=10000,
                    instructions_file="{gemini_home}/GEMINI.md",
                    mcp_file="{gemini_home}/settings.json"),
    # Copilot has one home per install (config "copilot_homes"); see install.py.
    "copilot": Agent("copilot", "Copilot CLI", "~/.copilot",
                     mcp_file="<each copilot home>/mcp-config.json"),
}

# MCP-only clients: kopir can register itself in their config.
MCP_CLIENTS = {
    "cursor": ("Cursor", "~/.cursor/mcp.json"),
    "windsurf": ("Windsurf", "~/.codeium/windsurf/mcp_config.json"),
    "claude-desktop": ("Claude Desktop", "~/.config/Claude/claude_desktop_config.json"),
}

START_EVENTS = {a.start_event for a in AGENTS.values() if a.start_event}
PROMPT_EVENTS = {a.prompt_event for a in AGENTS.values() if a.prompt_event}


def label(name: str) -> str:
    if name in AGENTS:
        return AGENTS[name].label
    if name in MCP_CLIENTS:
        return MCP_CLIENTS[name][0]
    return name


def path(template: str, cfg: dict) -> Path:
    if not template:
        return Path()
    if template in cfg:
        return Path(cfg[template]).expanduser()
    return Path(template.format(**{k: v for k, v in cfg.items() if isinstance(v, str)})).expanduser()


def installed(cfg: dict) -> list[Agent]:
    out = []
    for a in AGENTS.values():
        if a.name == "copilot":
            if any(Path(h).expanduser().is_dir() for h in cfg["copilot_homes"]):
                out.append(a)
        elif path(a.home, cfg).is_dir():
            out.append(a)
    return out
