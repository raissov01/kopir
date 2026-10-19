import argparse
import json
import time

from . import __version__, config, context, hook, install, memory, sessions


def _cache(cfg: dict, agents=("claude", "codex"), deadline: float = 1e18) -> sessions.Cache:
    cache = sessions.Cache(cfg)
    cache.refresh(set(agents), deadline)
    cache.save()
    return cache


def cmd_recall(cfg, a):
    text, _ = context.recall_records(cfg, a.query, None)
    print(text or "(no matching records)")
    if a.agent:
        cache = _cache(cfg, [context.OTHER[a.agent]])
        turns, _ = context.recall_turns(cfg, cache, a.agent, a.query, None)
        if turns:
            print("\n" + turns)


def cmd_recent(cfg, a):
    cache = _cache(cfg, [context.OTHER[a.agent]])
    cfg = {**cfg, "recent_hours": a.hours}
    print(context.recent_digest(cfg, cache, a.agent, a.cwd) or "(nothing recent)")


def cmd_show(cfg, a):
    cache = _cache(cfg, [a.agent])
    for s in cache.sessions(a.agent):
        if s["id"].startswith(a.id):
            print(f"# {s['title']}\n{s['cwd']} · {s['started']} → {s['updated']}\n")
            for t in s["turns"]:
                print(f"> {t['u']}\n\n{t['a']}\n")
            return 0
    print(f"no {a.agent} session starting with {a.id} in the last {cfg['window_days']} days")
    return 1


def cmd_index(cfg, a):
    t = time.monotonic()
    cache = _cache(cfg)
    for agent in ("claude", "codex"):
        ss = cache.sessions(agent)
        print(f"{agent}: {len(ss)} sessions, {sum(len(s['turns']) for s in ss)} turns")
    print(f"{time.monotonic() - t:.1f}s, cache {cache.path}")


def cmd_check(cfg, a):
    issues = memory.check(cfg["memory_dir"], cfg["index_files"])
    for level, f, msg in issues:
        if level == "error" or a.all:
            print(f"{level:5} {f}: {msg}")
    errors = sum(1 for i in issues if i[0] == "error")
    warns = len(issues) - errors
    print(f"{errors} errors, {warns} warnings{'' if a.all else ' (--all lists them)'}")
    return 1 if errors else 0


def cmd_backfill(cfg, a):
    changed = memory.backfill(cfg["memory_dir"], cfg["index_files"], dry_run=a.dry_run,
                              source=a.source)
    for line in changed:
        print(line)
    print(f"{len(changed)} records {'would change' if a.dry_run else 'updated'}")


def cmd_install(cfg, a):
    for line in install.install(cfg, uninstall=a.uninstall):
        print(line)
    if not a.uninstall:
        cmd_index(cfg, a)


def cmd_hook(cfg, a):
    return hook.main(a.agent)


def cmd_config(cfg, a):
    print(json.dumps(cfg, indent=2, ensure_ascii=False))


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="kopir", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    agents = ("claude", "codex")

    s = sub.add_parser("recall", help="memory records (and other-agent turns) for a query")
    s.add_argument("query")
    s.add_argument("--agent", choices=agents, help="also search the other agent's chats")
    s.set_defaults(fn=cmd_recall)

    s = sub.add_parser("recent", help="what the session-start hook would show")
    s.add_argument("--agent", choices=agents, default="claude", help="the agent asking")
    s.add_argument("--hours", type=int, default=48)
    s.add_argument("--cwd", default="")
    s.set_defaults(fn=cmd_recent)

    s = sub.add_parser("show", help="print one conversation")
    s.add_argument("agent", choices=agents)
    s.add_argument("id", help="session id or its prefix")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("index", help="build or refresh the conversation cache")
    s.set_defaults(fn=cmd_index)

    s = sub.add_parser("check", help="validate memory records")
    s.add_argument("--all", action="store_true", help="list warnings too")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("backfill", help="add missing metadata.source / observed_at")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--source", default="", help="source for records without a session id")
    s.set_defaults(fn=cmd_backfill)

    s = sub.add_parser("install", help="add hooks to Claude Code and Codex")
    s.add_argument("--uninstall", action="store_true")
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("hook", help="hook entry point (reads event JSON on stdin)")
    s.add_argument("--agent", choices=agents, required=True)
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("config", help="print the effective configuration")
    s.set_defaults(fn=cmd_config)

    a = p.parse_args(argv)
    return a.fn(config.load(), a) or 0
