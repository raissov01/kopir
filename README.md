# kopir

*Көпір* is Kazakh for "bridge".

kopir gives all your AI agents one shared memory and lets each of them see
what you discussed with the others. Tell Claude Code something, and Codex,
Gemini CLI, Copilot or Cursor know it the next time you open them.

It is a small Python package that uses only the standard library. It runs no
model, vector database or server, and it is wired in through each agent's
hooks and MCP.

## Supported agents

| Agent | Sees the others | Is seen by the others | How |
|---|---|---|---|
| Claude Code | automatic | automatic | hooks + transcripts |
| Codex CLI / IDE | automatic | automatic | hooks + SQLite threads |
| Gemini CLI | automatic | automatic | hooks + chat files, MCP |
| GitHub Copilot CLI | via MCP tools | automatic | transcripts, MCP |
| Cursor, Windsurf, Claude Desktop, Cline, Zed, … | via MCP tools | when it calls `log_turn` | MCP |

- **Hooks** run on session start and before every prompt. Their output
  lands in the agent's context without the agent doing anything.
- **Transcripts** are read in place and never modified. Each conversation
  becomes a list of turns: what you typed and the agent's last visible
  answer.
- **MCP** works with any client that speaks the Model Context Protocol. The
  client gets five tools: `recall`, `recent`, `show`, `remember` and
  `log_turn`.

## What the agents get

- **At session start:** a digest of the other agents' conversations from the
  last 48 hours, with chats from the current project first. Agents that do
  not load the memory index themselves get it too.
- **On every prompt:** up to 3 memory records and up to 2 turns from other
  agents' chats, whichever match the prompt. Nothing is injected twice into
  the same session.
- **One store:** one Markdown file per fact, with `type`, `source` (which
  agent wrote it) and `observed_at`. `kopir check` validates the store.

```
Claude Code ─┐                      ┌─ Codex
Gemini CLI ──┤── hooks ──► kopir ◄──┤── MCP ── Cursor, Windsurf, Copilot, …
             │                │     │
     transcripts, SQLite      ▼
                     memory/*.md  (one file per fact)
```

## Install

Requires Python 3.10+.

```sh
git clone https://github.com/raissov01/kopir ~/kopir
~/kopir/bin/kopir install                      # every agent it finds
~/kopir/bin/kopir install --mcp cursor copilot # also register the MCP server
```

`install` looks for each supported agent and wires in only the ones it finds:

- **Hooks** go into `~/.claude/settings.json`, `~/.codex/hooks.json` and
  `~/.gemini/settings.json`.
- **Memory-format instructions** go into `~/.codex/AGENTS.md` and
  `~/.gemini/GEMINI.md`, inside a marked block.
- **Codex** gets the memory directory added to its sandbox `writable_roots`.
- **`--mcp`** adds a `kopir` entry to that client's `mcpServers`.

Before its first change, `install` backs up each file it touches to
`*.kopir-bak`. To remove only what kopir added:

```sh
~/kopir/bin/kopir install --uninstall
```

Some agents gate new hooks or folders behind trust:

- **Codex** asks you to trust new hooks. Start `codex` once and choose
  *Trust all and continue*. Until then, `codex exec` skips the hooks
  silently.
- **Gemini CLI** runs hooks only in trusted folders.

### Any other MCP client

Add this to the client's MCP configuration:

```json
{
  "mcpServers": {
    "kopir": { "command": "python3", "args": ["/home/you/kopir/bin/kopir", "mcp"] }
  }
}
```

kopir names the client after the `clientInfo` it sends, such as `cursor` or
`windsurf`. That name is what the other agents see on its turns and in the
`source` of the records it writes. A client that cannot be hooked will not
call `recall` or `log_turn` unless asked to. Add a line like this to its rules
or system prompt:

> Before answering about my projects or earlier work, call kopir `recall`.
> After finishing a task, call kopir `log_turn` with my request and your answer.

## Use

The hooks run on their own. The CLI shows the same data by hand:

```sh
kopir recall "deploy the bot"                  # matching memory records
kopir recall "deploy the bot" --agent claude   # + other agents' turns Claude would get
kopir recent --agent codex                     # what Codex gets at session start
kopir show gemini 3f2a                         # one conversation, all turns
kopir check --all                              # validate records and links
kopir backfill --source claude                 # add missing source / observed_at
kopir index                                    # refresh the conversation cache
kopir mcp                                      # MCP server on stdio
kopir config                                   # effective configuration
```

## How it works

**Readers**

- Claude Code JSONL transcripts are append-only. kopir resumes each file from
  the byte offset where it stopped last time.
- Codex threads are read from its SQLite state in read-only mode. Only
  threads a person started (`cli`, `vscode`) count; `codex exec` runs are
  treated as scripts and skipped.
- Gemini CLI `chats/session-*.jsonl` files and Copilot CLI
  `session-state/*/events.jsonl` files are re-read when their size changes.
- MCP clients append to `~/.local/share/kopir/journal.jsonl`.

The cache lives in `~/.cache/kopir/`.

**Matching** uses BM25. Kazakh, Russian and English mix freely in chats, so
every token is first transliterated into one Latin skeleton, so that
`алма парк` and `alma-park` meet. It is then cut to a 6-letter stem, so that
inflected forms such as `тіркелуді` and `тіркелу` meet. Common conversational
words in all three languages are dropped.

A hit has to pass three checks:

- it shares two distinct terms with the query;
- one of those terms is in its head (a record's name and description, or a
  turn's question);
- its score clears a floor that scales with corpus size.

**Privacy**

- Before any text leaves its own agent, credentials are redacted. That covers
  known token shapes, `NAME_KEY=value` assignments, passwords in URLs and long
  mixed-case opaque strings.
- `remember` refuses a record that contains a credential.
- Everything stays on your machine. kopir makes no network calls.

**Safety**

- A hook has a 3-second internal deadline.
- If a hook fails, it logs the error to `~/.cache/kopir/hook.log` and exits 0
  with no output, so it never blocks a prompt.
- Injected text is labelled as context and is never executed.

## Configuration

To override any key, put it in `~/.config/kopir/config.json`. Path keys can
also be set with `KOPIR_*` environment variables, such as `KOPIR_MEMORY_DIR`.

| key | default | |
|---|---|---|
| `memory_dir` | Claude Code's auto-memory if present, else `~/.local/share/kopir/memory` | shared records |
| `codex_home` / `gemini_home` / `copilot_homes` | standard locations (snap included) | where to read |
| `recent_hours` | 48 | session-start digest window |
| `window_days` | 14 | how far back chats are indexed |
| `recall_records` / `recall_turns` | 3 / 2 | per-prompt limits |
| `min_score` / `min_hits` | 4.0 / 2 | match thresholds |
| `budget_records` / `budget_turns` / `budget_recent` | 3600 / 1600 / 2600 | characters |
| `inject_index_for` | everything but Claude Code | agents that get `MEMORY.md` at start |
| `codex_sources` | `["cli", "vscode"]` | Codex thread sources to read |

## Record format

```markdown
---
name: project-printer
description: Office printer on floor three, queue at cups.local
metadata:
  type: project          # user | feedback | project | reference
  source: codex          # the agent (or `user`) it came from
  observed_at: 2026-10-19
---

The fact. For feedback and project records, add **Why:** and **How to apply:** lines.
Link related records with [[their-name]].
```

`MEMORY.md` in the same directory is the index: one line per record. It works
best kept short (rules and a project map) while kopir pulls up the rest on
demand.

## Adding an agent

An agent needs two things:

1. **A reader** in `kopir/sessions.py`: a function that turns one
   conversation file into `{agent, id, cwd, title, started, updated, turns}`.
2. **An entry** in `kopir/agents.py` that names its hooks file and events,
   instructions file or MCP config.

If the agent's hooks read `hookSpecificOutput.additionalContext` from stdout,
as Claude Code, Codex and Gemini CLI do, the existing hook works unchanged.
Pull requests are welcome.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests use synthetic transcripts and databases for every agent, plus a
temporary memory directory.

## Credits

kopir is a much lighter take on ideas from
[svod-memory](https://github.com/ivbala1/svod-memory): plain Markdown records,
a small deterministic per-prompt selection, and required provenance.

## License

MIT
