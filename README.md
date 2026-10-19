# kopir

*Көпір* is Kazakh for "bridge".

kopir gives Claude Code and Codex one shared memory and lets each of them see
what you discussed with the other. It is a pair of hooks and a small CLI:
Python standard library only, with no model, no vector database and no server.

- **Session start:** the agent gets a short digest of the other agent's
  conversations from the last 48 hours. Chats from the current project come
  first. Codex also gets the memory index, because Claude Code loads that
  index itself.
- **Every prompt:** the agent gets the memory records that match the prompt
  (up to 3) and the matching turns from the other agent's chats (up to 2).
  Nothing is injected twice into the same session.
- **One store:** the store is Claude Code's auto-memory directory, one
  Markdown file per fact. Codex is told how to write records there and is
  allowed to write to it.
- **Provenance:** every record carries `metadata.source` (`claude`, `codex`
  or `user`) and `metadata.observed_at`. `kopir check` validates the store.

```
you ──► Claude Code ──hook──► kopir ◄──hook── Codex ◄── you
                                │
        ~/.claude/projects/*/*.jsonl      ~/.codex/*.sqlite
        memory/*.md  (shared records, one file per fact)
```

## Why

If you use both agents on the same work, each one starts blind to what the
other just did. Pasting summaries between them by hand gets old fast. Loading
the whole memory index into every session also gets expensive once it grows
past a few hundred lines.

kopir is a much lighter take on the ideas in
[svod-memory](https://github.com/ivbala1/svod-memory). It keeps the plain
Markdown records, the small deterministic per-prompt selection and the
required provenance. It is built for one person on one machine, so it leaves
out the Git sync, per-record probes and eval baselines.

## Install

Requires Python 3.10+, Claude Code and/or Codex CLI (0.16x, with hooks).

```sh
git clone https://github.com/raissov01/kopir ~/kopir
~/kopir/bin/kopir install          # hooks for both agents + first index
~/kopir/bin/kopir backfill --dry-run --source claude
~/kopir/bin/kopir backfill --source claude
```

`install` does the following:

- adds `SessionStart` and `UserPromptSubmit` hooks to
  `~/.claude/settings.json` and `~/.codex/hooks.json`;
- adds a marked block with the record format to `~/.codex/AGENTS.md`;
- adds the memory directory to Codex's `writable_roots`.

Before its first change, it backs up each file it touches to `*.kopir-bak`.

**Codex asks you to trust new hooks.** Start `codex` once and choose
*Trust all and continue*, or review the hooks in `/hooks`. Until you do,
`codex exec` skips them silently.

Uninstall removes only what kopir added:

```sh
~/kopir/bin/kopir install --uninstall
```

## Use

The hooks run on their own. The CLI is for looking at the same data by hand:

```sh
kopir recall "deploy the bot"            # matching memory records
kopir recall "deploy the bot" --agent claude   # + Codex turns Claude would see
kopir recent --agent codex               # what Codex sees at session start
kopir show codex 01a121f8                # one conversation, all turns
kopir check --all                        # validate records and links
kopir index                              # refresh the conversation cache
kopir config                             # effective configuration
```

## How it works

**Conversations.** Each chat becomes a list of turns: what you typed and the
agent's last visible answer to it. Tool calls, reasoning, subagents and
context-compaction summaries are dropped.

- Claude Code transcripts are append-only JSONL. kopir resumes each file from
  the byte offset where it stopped last time, so a refresh costs only the new
  lines.
- Codex threads are read from `state_5.sqlite` and `thread_history_1.sqlite`
  in read-only mode. Only threads you started yourself are read (`cli` and
  `vscode` sources); `codex exec` runs are scripts and are skipped.

The cache lives in `~/.cache/kopir/sessions.json`.

**Matching.** kopir ranks with BM25. Kazakh, Russian and English mix freely
in these chats, so every token goes through two steps first:

1. It is transliterated into one Latin skeleton, so `алма парк` and
   `alma-park` meet.
2. It is cut to a 6-letter stem, so inflected forms such as `тіркелуді` and
   `тіркелу` meet.

A hit needs two distinct query terms and a score floor that scales with
corpus size. A record's name and description weigh three times its body.

**Safety.** A hook has a 3-second internal deadline. It logs any failure to
`~/.cache/kopir/hook.log`, and it always exits 0 with no output on error,
so it never blocks a prompt. Injected text is labelled as context; nothing
from it is run.

## Configuration

Defaults fit a stock install. To override any key, put it in
`~/.config/kopir/config.json`. Path keys can also be set with `KOPIR_*`
environment variables, such as `KOPIR_MEMORY_DIR`.

| key | default | |
|---|---|---|
| `memory_dir` | `~/.claude/projects/-home-<you>/memory` | shared records |
| `recent_hours` | 48 | session-start digest window |
| `window_days` | 14 | how far back chats are indexed |
| `recall_records` / `recall_turns` | 3 / 2 | per-prompt limits |
| `min_score` / `min_hits` | 4.0 / 2 | match thresholds |
| `budget_records` / `budget_turns` / `budget_recent` | 3600 / 1600 / 2600 | characters |
| `inject_index_for` | `["codex"]` | agents that get `MEMORY.md` at start |
| `codex_sources` | `["cli", "vscode"]` | Codex thread sources to read |

## Record format

```markdown
---
name: project-printer
description: Office printer on floor three, queue at cups.local
metadata:
  type: project          # user | feedback | project | reference
  source: codex          # claude | codex | user
  observed_at: 2026-10-19
---

The fact. For feedback and project records, add **Why:** and **How to apply:** lines.
Link related records with [[their-name]].
```

`MEMORY.md` in the same directory is the index: one line per record. It works
best kept short (rules and a project map) while kopir pulls up the rest on
demand.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests use synthetic Claude transcripts, a synthetic Codex database and a
temporary memory directory.

## License

MIT
