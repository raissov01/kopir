import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kopir import agents, config, context, hook, install, mcp, memory, rank, sessions  # noqa: E402
from kopir.redact import redact  # noqa: E402
from kopir.textnorm import tokens  # noqa: E402

RECORD = """---
name: {name}
description: {desc}
metadata:
  type: project
  originSessionId: abc
  modified: 2026-09-13T11:51:07.065Z
---

{body}
"""


def write_record(root: Path, stem: str, desc: str, body: str) -> Path:
    p = root / f"{stem}.md"
    p.write_text(RECORD.format(name=stem, desc=desc, body=body), encoding="utf-8")
    return p


def claude_line(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False) + "\n"


def human(text, ts="2026-10-19T10:00:00Z", **kw):
    return claude_line(**{"type": "user", "isSidechain": False, "origin": {"kind": "human"},
                          "cwd": "/w/app", "timestamp": ts,
                          "message": {"role": "user", "content": text}, **kw})


def answer(text, ts="2026-10-19T10:01:00Z", **kw):
    return claude_line(**{"type": "assistant", "isSidechain": False, "cwd": "/w/app",
                          "timestamp": ts,
                          "message": {"content": [{"type": "text", "text": text}]}, **kw})


def make_codex(home: Path, threads):
    """threads: [(id, source, cwd, title, updated_ms, [(turn, type, json)])]"""
    st = sqlite3.connect(home / "state_5.sqlite")
    st.execute("create table threads (id text, source text, archived int, cwd text, title text,"
               " name text, created_at_ms int, updated_at_ms int)")
    hi = sqlite3.connect(home / "thread_history_1.sqlite")
    hi.execute("create table thread_items (thread_id text, turn_id text, item_id text,"
               " rollout_ordinal int, created_at_ms int, item_json text, item_type text)")
    for tid, source, cwd, title, upd, items in threads:
        st.execute("insert into threads values (?,?,0,?,?,NULL,?,?)",
                   (tid, source, cwd, title, upd - 1000, upd))
        for n, (turn, typ, js) in enumerate(items):
            hi.execute("insert into thread_items values (?,?,?,?,?,?,?)",
                       (tid, turn, f"i{n}", n, upd - 900 + n, json.dumps(js), typ))
    st.commit()
    hi.commit()


def umsg(text):
    return {"type": "userMessage", "content": [{"type": "text", "text": text}]}


def amsg(text, phase):
    return {"type": "agentMessage", "text": text, "phase": phase}


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mem = self.tmp / "memory"
        self.proj = self.tmp / "claude" / "-w-app"
        self.codex = self.tmp / "codex"
        for d in (self.mem, self.proj, self.codex):
            d.mkdir(parents=True)
        self.cfg = {**config.DEFAULTS, "memory_dir": str(self.mem),
                    "claude_projects": str(self.tmp / "claude"),
                    "codex_home": str(self.codex), "cache_dir": str(self.tmp / "cache"),
                    "gemini_home": str(self.tmp / "gemini"), "data_dir": str(self.tmp / "data"),
                    "copilot_homes": [str(self.tmp / "copilot")],
                    "recent_hours": 10 ** 6, "window_days": 10 ** 5}


class TextTests(unittest.TestCase):
    def test_cyrillic_and_latin_spellings_meet(self):
        self.assertEqual(tokens("алма парк"), tokens("alma-park"))
        self.assertEqual(tokens("Қадам"), tokens("qadam"))
        self.assertEqual(tokens("кодекс"), tokens("codex"))

    def test_kazakh_inflections_share_a_stem(self):
        self.assertEqual(tokens("тіркелуді"), tokens("тіркелу"))

    def test_stopwords_dropped(self):
        self.assertEqual(tokens("и в на мен және the"), [])


class RankTests(unittest.TestCase):
    def test_needs_two_shared_terms(self):
        docs = [tokens("printer office floor three"), tokens("deploy server script")]
        self.assertEqual(rank.search("printer", docs, limit=3, min_score=0, min_hits=2)[0][1], 0)
        self.assertEqual(rank.search("printer deploy", docs, limit=3, min_score=0, min_hits=2), [])
        hits = rank.search("printer office", docs, limit=3, min_score=0, min_hits=2)
        self.assertEqual([i for _, i in hits], [0])


class RedactTests(unittest.TestCase):
    def test_credentials_go_hashes_stay(self):
        self.assertEqual(redact("API_KEY=Abc123def456ghi789 rest"), "API_KEY=[redacted] rest")
        self.assertEqual(redact("ghp_" + "a1" * 18), "[redacted]")
        self.assertEqual(redact("postgres://u:pw1234@h/db"), "postgres://u:[redacted]@h/db")
        self.assertIn("[redacted]", redact("x " + "Ab1" * 12))
        commit = "9634956a1b2c3d4e5f60718293a4b5c6d7e8f901"
        self.assertEqual(redact(f"commit {commit}"), f"commit {commit}")

    def test_head_match_needed(self):
        docs = [tokens("deploy notes") + tokens("printer office printer office"),
                tokens("office printer") + tokens("unrelated body")]
        heads = [set(tokens("deploy notes")), set(tokens("office printer"))]
        hits = rank.search("office printer", docs, limit=3, min_score=0, min_hits=2, heads=heads)
        self.assertEqual([i for _, i in hits], [1])


class MemoryTests(Env):
    def test_recall_check_backfill(self):
        write_record(self.mem, "project_printer", "Office printer on floor three",
                     "The printer queue is at cups.local. See [[project_missing]].")
        write_record(self.mem, "project_deploy", "Deploy script for the bot", "Run ./deploy.sh")
        (self.mem / "MEMORY.md").write_text("- [P](project_printer.md)\n- [X](gone.md)\n")
        text, keys = context.recall_records(self.cfg, "where is the office printer", None)
        self.assertIn("project_printer.md", text)
        self.assertNotIn("project_deploy", text)
        self.assertEqual(keys, ["rec:project_printer"])

        issues = memory.check(str(self.mem), self.cfg["index_files"])
        msgs = {(lvl, m) for lvl, _, m in issues}
        self.assertIn(("error", "links missing gone.md"), msgs)
        self.assertIn(("warn", "[[project_missing]] does not resolve"), msgs)
        self.assertIn(("warn", "no metadata.source"), msgs)

        mtime = (self.mem / "project_deploy.md").stat().st_mtime
        self.assertEqual(len(memory.backfill(str(self.mem), [], dry_run=True)), 2)
        memory.backfill(str(self.mem), self.cfg["index_files"], dry_run=False)
        rec = memory.read(self.mem / "project_deploy.md")
        self.assertEqual(rec.meta["source"], "claude")
        self.assertEqual(rec.meta["observed_at"], "2026-09-13")
        self.assertEqual(rec.meta["type"], "project")
        self.assertEqual(rec.body.strip(), "Run ./deploy.sh")
        self.assertEqual((self.mem / "project_deploy.md").stat().st_mtime, mtime)
        self.assertEqual(memory.backfill(str(self.mem), self.cfg["index_files"], dry_run=False), [])


class ClaudeReaderTests(Env):
    def test_turns_resume_and_skip_noise(self):
        f = self.proj / "s1.jsonl"
        f.write_text(
            claude_line(type="ai-title", aiTitle="Printer setup")
            + human("set up the office printer")
            + claude_line(type="user", isSidechain=False, origin={"kind": "human"},
                          message={"content": [{"type": "tool_result", "content": "x"}]})
            + answer("Checking cups first.")
            + answer("Printer added at cups.local.")
            + human("<command-name>/clear</command-name>")
            + human("summary", isCompactSummary=True)
            + answer("subagent noise", isSidechain=True)
        )
        e = sessions.read_claude_file(f, None, time.monotonic() + 5)
        turns = sessions.claude_turns(e)
        self.assertEqual(e["session"]["title"], "Printer setup")
        self.assertEqual([(t["u"], t["a"]) for t in turns],
                         [("set up the office printer", "Printer added at cups.local.")])
        with f.open("a") as fh:
            fh.write(human("now the scanner", ts="2026-10-19T11:00:00Z") + '{"type":"assist')
        e = sessions.read_claude_file(f, e, time.monotonic() + 5)
        self.assertEqual([t["u"] for t in sessions.claude_turns(e)],
                         ["set up the office printer", "now the scanner"])
        self.assertEqual(e["offset"], f.stat().st_size - len('{"type":"assist'))


class CodexReaderTests(Env):
    def test_threads_and_final_answers(self):
        now = int(time.time() * 1000)
        make_codex(self.codex, [
            ("t1", "cli", "/w/app", "fix login\nmore", now, [
                ("a", "userMessage", umsg("fix the login bug")),
                ("a", "agentMessage", amsg("looking", "commentary")),
                ("a", "agentMessage", amsg("Fixed: token expiry", "final_answer")),
                ("a", "agentMessage", amsg("late commentary", "commentary")),
            ]),
            ("t2", "exec", "/w/app", "scripted", now, [("b", "userMessage", umsg("x"))]),
        ])
        cache = sessions.Cache(self.cfg)
        cache.refresh({"codex"}, time.monotonic() + 5)
        ss = cache.sessions("codex")
        self.assertEqual([s["id"] for s in ss], ["t1"])
        self.assertEqual(ss[0]["title"], "fix login")
        self.assertEqual(ss[0]["turns"][0]["a"], "Fixed: token expiry")


class OtherReaderTests(Env):
    def test_gemini_chat_file(self):
        chats = self.tmp / "gemini" / "tmp" / "app" / "chats"
        chats.mkdir(parents=True)
        (chats.parent / ".project_root").write_text("/w/app")
        rows = [{"sessionId": "g1", "startTime": "2026-10-19T09:00:00Z", "kind": "main"},
                {"$set": {"messages": [{"id": "m0", "type": "user", "timestamp": "t0",
                                        "content": [{"text": "<session_context>x"}]}]}},
                {"id": "m1", "type": "user", "timestamp": "2026-10-19T09:01:00Z",
                 "content": [{"text": "rename the printer queue"}]},
                {"id": "m2", "type": "gemini", "timestamp": "t2", "content": ""},
                {"id": "m2", "type": "gemini", "timestamp": "t2", "content": "Renamed to office-1"}]
        (chats / "session-1.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        cache = sessions.Cache(self.cfg)
        cache.refresh({"gemini"}, time.monotonic() + 5)
        [s] = cache.sessions("gemini")
        self.assertEqual((s["id"], s["cwd"]), ("g1", "/w/app"))
        self.assertEqual([(t["u"], t["a"]) for t in s["turns"]],
                         [("rename the printer queue", "Renamed to office-1")])

    def test_copilot_events_and_redaction(self):
        d = self.tmp / "copilot" / "session-state" / "c1"
        d.mkdir(parents=True)
        ev = [{"type": "session.start", "data": {"startTime": "s", "context": {"cwd": "/w/app"}}},
              {"type": "user.message", "timestamp": "2026-10-19T08:00:00Z",
               "data": {"content": "use OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123"}},
              {"type": "assistant.message", "timestamp": "t", "data": {"content": "Looking"}},
              {"type": "assistant.message", "timestamp": "t", "data": {"content": "Done", "toolRequests": []}},
              {"type": "assistant.message", "timestamp": "t", "data": {"content": ""}}]
        (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in ev) + "\n")
        cache = sessions.Cache(self.cfg)
        cache.refresh({"copilot"}, time.monotonic() + 5)
        [s] = cache.sessions("copilot")
        self.assertEqual(s["turns"][0]["a"], "Done")
        self.assertNotIn("sk-abc", s["turns"][0]["u"])
        self.assertNotIn("sk-abc", cache.path.read_text() if cache.path.exists() else "")


class McpTests(Env):
    def rpc(self, server, method, params=None, mid=1):
        return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def call(self, server, tool, **args):
        r = self.rpc(server, "tools/call", {"name": tool, "arguments": args})["result"]
        return r["content"][0]["text"], r["isError"]

    def test_cursor_logs_and_claude_sees_it(self):
        server = mcp.Server(self.cfg)
        init = self.rpc(server, "initialize", {"clientInfo": {"name": "cursor-vscode"},
                                               "protocolVersion": "2025-06-18"})
        self.assertEqual(init["result"]["serverInfo"]["name"], "kopir")
        self.assertEqual(server.agent, "cursor")
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = {t["name"] for t in self.rpc(server, "tools/list")["result"]["tools"]}
        self.assertEqual(names, {"recall", "recent", "show", "remember", "log_turn"})

        text, err = self.call(server, "remember", name="Office Printer", type="project",
                              description="Office printer queue is office-1",
                              body="Queue renamed to office-1 on floor three.")
        self.assertFalse(err, text)
        rec = memory.read(self.mem / "project_office_printer.md")
        self.assertEqual((rec.meta["source"], rec.type), ("cursor", "project"))
        self.assertIn("project_office_printer.md", (self.mem / "MEMORY.md").read_text())
        _, err = self.call(server, "remember", name="k", type="reference", description="key",
                           body="token ghp_" + "a1" * 18)
        self.assertTrue(err)
        _, err = self.call(server, "remember", name="k", type="nope", description="d", body="b")
        self.assertTrue(err)

        self.call(server, "log_turn", ask="why is the office printer offline",
                  answer="The office printer queue was paused; resumed it", title="printer")
        ctx = hook.run("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "c9",
                                  "prompt": "is the office printer queue working?"}, self.cfg)
        self.assertIn("[Cursor]", ctx)
        self.assertIn("project_office_printer.md", ctx)
        text, _ = self.call(server, "recent", hours=1)
        self.assertNotIn("[Cursor]", text)  # an agent is not shown its own chats

    def test_protocol_errors(self):
        server = mcp.Server(self.cfg)
        self.assertEqual(self.rpc(server, "nope")["error"]["code"], -32601)
        r = self.rpc(server, "tools/call", {"name": "handle", "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)
        out = io.StringIO()
        mcp_in = io.StringIO('{"jsonrpc":"2.0","id":7,"method":"ping"}\nnot json\n')
        os.environ["KOPIR_CACHE_DIR"] = str(self.tmp / "cache")
        try:
            mcp.serve(mcp_in, out)
        finally:
            del os.environ["KOPIR_CACHE_DIR"]
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(lines[0], {"jsonrpc": "2.0", "id": 7, "result": {}})
        self.assertEqual(lines[1]["error"]["code"], -32700)


class HookTests(Env):
    def test_claude_sees_codex_and_codex_sees_claude(self):
        now = int(time.time() * 1000)
        make_codex(self.codex, [("t1", "vscode", "/w/app", "printer driver", now, [
            ("a", "userMessage", umsg("install the printer driver for the office")),
            ("a", "agentMessage", amsg("Installed hplip; office printer works", "final_answer")),
        ])])
        (self.proj / "s1.jsonl").write_text(human("deploy the bot to the server")
                                            + answer("Deployed with ./deploy.sh"))
        (self.mem / "MEMORY.md").write_text("- index line\n")

        start = hook.run("claude", {"hook_event_name": "SessionStart", "cwd": "/w/app",
                                    "session_id": "c1", "source": "startup"}, self.cfg)
        self.assertIn("[Codex]", start)
        self.assertIn("printer driver", start)
        self.assertNotIn("index line", start)  # Claude loads MEMORY.md itself

        ask = {"hook_event_name": "UserPromptSubmit", "cwd": "/w/app", "session_id": "c1",
               "prompt": "did the office printer driver get installed?"}
        self.assertIn("Installed hplip", hook.run("claude", ask, self.cfg))
        self.assertEqual(hook.run("claude", ask, self.cfg), "")  # already injected

        start = hook.run("codex", {"hook_event_name": "SessionStart", "cwd": "/w/app",
                                   "session_id": "x1"}, self.cfg)
        self.assertIn("index line", start)
        self.assertIn("deploy the bot", start)

    def test_failure_is_silent(self):
        os.environ["KOPIR_CONFIG"] = str(self.tmp / "missing.json")
        os.environ["KOPIR_CACHE_DIR"] = str(self.tmp / "cache")
        out = io.StringIO()
        sys_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO("not json")
            with redirect_stdout(out):
                self.assertEqual(hook.main("claude"), 0)
        finally:
            sys.stdin = sys_stdin
            del os.environ["KOPIR_CONFIG"], os.environ["KOPIR_CACHE_DIR"]
        self.assertEqual(out.getvalue(), "")
        self.assertIn("Traceback", (self.tmp / "cache" / "hook.log").read_text())


class InstallTests(Env):
    def test_hooks_roundtrip_keeps_foreign_entries(self):
        path = self.tmp / "settings.json"
        foreign = {"hooks": [{"type": "command", "command": "notify.sh"}]}
        path.write_text(json.dumps({"theme": "dark", "hooks": {"SessionStart": [foreign]}}))
        self.assertTrue(install.set_hooks(path, "python3 /x/bin/kopir hook --agent claude"))
        self.assertFalse(install.set_hooks(path, "python3 /x/bin/kopir hook --agent claude"))
        d = json.loads(path.read_text())
        self.assertEqual(len(d["hooks"]["SessionStart"]), 2)
        self.assertEqual(len(d["hooks"]["UserPromptSubmit"]), 1)
        self.assertTrue(install.set_hooks(path, None))
        self.assertEqual(json.loads(path.read_text()),
                         {"theme": "dark", "hooks": {"SessionStart": [foreign]}})
        self.assertTrue((self.tmp / "settings.json.kopir-bak").is_file())

    def test_gemini_hooks_and_mcp_entry(self):
        path = self.tmp / "gemini-settings.json"
        g = agents.AGENTS["gemini"]
        install.set_hooks(path, "python3 /x/bin/kopir hook --agent gemini",
                          (g.start_event, g.prompt_event), g.timeout)
        d = json.loads(path.read_text())
        self.assertEqual(set(d["hooks"]), {"SessionStart", "BeforeAgent"})
        self.assertEqual(d["hooks"]["BeforeAgent"][0]["hooks"][0]["timeout"], 10000)
        self.assertTrue(install.set_mcp(path, True))
        self.assertFalse(install.set_mcp(path, True))
        self.assertEqual(json.loads(path.read_text())["mcpServers"]["kopir"]["args"][-1], "mcp")
        self.assertTrue(install.set_mcp(path, False))
        self.assertEqual(json.loads(path.read_text())["mcpServers"], {})

    def test_gemini_prompt_event_recalls(self):
        write_record(self.mem, "project_printer", "Office printer on floor three", "cups.local")
        ctx = hook.run("gemini", {"hook_event_name": "BeforeAgent", "session_id": "g",
                                  "prompt": "where is the office printer"}, self.cfg)
        self.assertIn("project_printer.md", ctx)

    def test_agents_block_and_writable_root(self):
        agents = self.tmp / "AGENTS.md"
        agents.write_text("# Mine\n")
        install.set_agents_block(agents, "/m")
        install.set_agents_block(agents, "/m")
        self.assertEqual(agents.read_text().count(install.MARK_BEGIN), 1)
        install.set_agents_block(agents, None)
        self.assertEqual(agents.read_text(), "# Mine\n")

        toml = self.tmp / "config.toml"
        original = ('model = "x"\n[sandbox_workspace_write]\nnetwork_access = true\n'
                    'writable_roots = [\n  "/a",  # keep\n]\n')
        toml.write_text(original)
        self.assertEqual(install.set_writable_root(toml, "/m", True), "added")
        self.assertEqual(install.set_writable_root(toml, "/m", True), "already listed")
        self.assertIn('"/m",  # kopir', toml.read_text())
        install.set_writable_root(toml, "/m", False)
        self.assertEqual(toml.read_text(), original)


if __name__ == "__main__":
    unittest.main()
