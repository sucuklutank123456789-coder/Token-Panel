import json
import os
import plistlib
import sys
import tempfile
import unittest
from unittest import mock

from tokenpanel import autostart, paths, pricing
from tokenpanel import store as store_mod
from tokenpanel.__main__ import console_encoding
from tokenpanel.store import Store, project_name


def write_jsonl(path, rows, partial=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
        if partial:
            fh.write(partial)


def claude_msg(mid, req, sid, entry, model="claude-opus-5-5", tools=(), ts="2026-10-04T10:00:00Z", **usage):
    content = [{"type": "tool_use", "name": n, "id": "x", "input": {}} for n in tools] or [
        {"type": "text", "text": "ok"}
    ]
    u = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    u.update(usage)
    return {
        "type": "assistant",
        "sessionId": sid,
        "entrypoint": entry,
        "cwd": "/home/u/proj",
        "gitBranch": "main",
        "requestId": req,
        "timestamp": ts,
        "message": {"id": mid, "model": model, "content": content, "usage": u},
    }


def codex_meta(tid, originator, cwd="/home/u/proj"):
    return {
        "timestamp": "2026-10-04T10:00:00Z",
        "type": "session_meta",
        "payload": {"id": tid, "originator": originator, "cwd": cwd, "git": {"branch": "dev"}},
    }


def codex_turn(turn, model, effort):
    return {"type": "turn_context", "payload": {"turn_id": turn, "model": model, "effort": effort}}


def codex_record(resp, turn, inp, cached, out, reasoning=0):
    u = {
        "input_tokens": inp,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": out,
        "reasoning_output_tokens": reasoning,
        "total_tokens": inp + out,
    }
    return {
        "timestamp": "2026-10-04T10:01:00Z",
        "type": "token_usage_record",
        "payload": {"response_id": resp, "turn_id": turn, "usage": u},
    }


def codex_call(name):
    return {"type": "response_item", "payload": {"type": "function_call", "name": name}}


class StoreTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(pricing, "_user", {})  # no user price file from this machine
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.claude = os.path.join(self.tmp.name, "claude")
        self.codex = os.path.join(self.tmp.name, "codex")
        self.opencode = os.path.join(self.tmp.name, "opencode")
        self.store = Store([self.claude], self.codex, [self.opencode])

    def tearDown(self):
        self.tmp.cleanup()

    def clients(self):
        s = self.store.summarize("all")
        return s, {(c.source, c.client): c for c in s.clients}

    def test_claude_split_lines_dedup_and_clients(self):
        p = os.path.join(self.claude, "projects", "-home-u-proj", "s1.jsonl")
        write_jsonl(
            p,
            [
                {"type": "user", "sessionId": "s1", "message": {"content": "hello world"}},
                # One response split over two lines: counted once, tools merged.
                claude_msg("m1", "r1", "s1", "cli", tools=["Bash"], input_tokens=10, output_tokens=5,
                           cache_read_input_tokens=100),
                claude_msg("m1", "r1", "s1", "cli", tools=["Read"], input_tokens=10, output_tokens=5,
                           cache_read_input_tokens=100),
                claude_msg("m2", "r2", "s1", "cli", model="claude-sonnet-5-5", output_tokens=20),
                {"type": "ai-title", "aiTitle": "Title", "sessionId": "s1"},
            ],
        )
        p2 = os.path.join(self.claude, "projects", "-home-u-proj", "s2.jsonl")
        write_jsonl(p2, [claude_msg("m3", "r3", "s2", "sdk-ts", output_tokens=7)])
        self.store.refresh()
        s, c = self.clients()
        self.assertEqual(s.total.total, 115 + 20 + 7)
        cli = c[("claude", "cli")]
        self.assertEqual(cli.label, "CLI")
        self.assertEqual(cli.usage.total, 135)
        t = cli.threads[0]
        self.assertEqual(t.title, "Title")
        self.assertEqual(t.branch, "main")
        self.assertEqual(set(t.model_usage), {"claude-opus-5-5", "claude-sonnet-5-5"})
        self.assertAlmostEqual(t.tools["Bash"].total, 57.5)
        self.assertAlmostEqual(t.tools["Read"].total, 57.5)
        # io leaves out cache reads: 10+5 + 20 + 7.
        self.assertEqual(s.total.value("io"), 42)
        self.assertEqual(s.total.value("raw"), 142)
        # The app method counts split lines separately: m1 twice (15+15) + 20 + 7.
        self.assertEqual(s.total.value("app"), 57)
        # Every metric splits into input and output.
        self.assertEqual(s.total.split("app"), (20, 37))
        self.assertEqual(s.total.split("io"), (10, 32))
        self.assertEqual(s.total.split("raw"), (110, 32))
        # A response split across lines is priced once: Opus 5.5 ($4 in, $0.20 cache read, $20 out) for m1
        # and m3, Sonnet 5.5 ($10 out) for m2.
        self.assertAlmostEqual(s.total.cost, (10 * 4 + 100 * 0.2 + 5 * 20 + 20 * 10 + 7 * 20) / 1e6)
        self.assertEqual(c[("claude", "sdk-ts")].usage.total, 7)

    def test_codex_records_models_tools_and_limits(self):
        p = os.path.join(self.codex, "sessions", "2026", "10", "04", "rollout-a.jsonl")
        rows = [
            codex_meta("t1", "zed"),
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text", "text": "<env>x</env>"}]}},
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "# Context from my IDE setup:\n\n## My request:\nselam\n"}],
            }},
            codex_turn("u1", "gpt-5.5", "high"),
            codex_call("exec_command"),
            codex_call("apply_patch"),
            codex_record("resp1", "u1", 1000, 800, 50, 10),
            # With records present token_count must be ignored (no double counting).
            {"timestamp": "2026-10-04T10:01:00Z", "type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 1050}, "last_token_usage": {"input_tokens": 1000,
                                                                                            "output_tokens": 50}},
                "rate_limits": {"plan_type": "plus", "primary": {"used_percent": 12.0, "window_minutes": 300,
                                                                 "resets_at": 1}}}},
            codex_turn("u2", "gpt-6", "low"),
            codex_record("resp2", "u2", 200, 0, 20),
        ]
        write_jsonl(p, rows)
        # Archived copy: the same response_ids must not be counted again.
        write_jsonl(os.path.join(self.codex, "archived_sessions", "rollout-a.jsonl"), rows)
        self.store.refresh()
        s, c = self.clients()
        zed = c[("codex", "zed")]
        self.assertEqual(zed.label, "ACP (Zed)")
        self.assertEqual(s.total.total, 1050 + 220)
        t = zed.threads[0]
        self.assertEqual(t.title, "selam")
        self.assertEqual(t.branch, "dev")
        self.assertEqual(t.model_usage["gpt-5.5"].cache_read, 800)
        self.assertEqual(t.model_usage["gpt-5.5"].input, 200)
        self.assertEqual(t.model_usage["gpt-6"].total, 220)
        self.assertEqual(t.efforts, {"high", "low"})
        self.assertAlmostEqual(t.tools["exec_command"].total, 525)
        self.assertEqual(t.usage.value("io"), 200 + 50 + 200 + 20)
        self.assertEqual(s.limits.plan, "plus")
        self.assertEqual(s.limits.primary.used_percent, 12.0)

    def test_codex_app_metric_follows_codex_counter(self):
        # The compaction call is recorded but not added to Codex's own counter.
        def tc(inp, cached, out, last_inp, last_cached, last_out):
            return {"timestamp": "2026-10-04T10:02:00Z", "type": "event_msg", "payload": {
                "type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out},
                    "last_token_usage": {"input_tokens": last_inp, "cached_input_tokens": last_cached,
                                         "output_tokens": last_out}}}}

        p = os.path.join(self.codex, "sessions", "2026", "10", "04", "rollout-c.jsonl")
        write_jsonl(p, [
            codex_meta("tc", "Codex Desktop"),
            codex_turn("u1", "gpt-5.5", "high"),
            codex_record("r1", "u1", 1000, 600, 100),
            tc(1000, 600, 100, 1000, 600, 100),
            {"type": "compacted", "payload": {"message": ""}},
            codex_record("r-compact", "u1", 5000, 1000, 300),
            tc(1000, 600, 100, 50, 0, 10),
            codex_record("r2", "u1", 400, 100, 20),
            tc(1400, 700, 120, 400, 100, 20),
        ])
        self.store.refresh()
        s, c = self.clients()
        u = c[("codex", "Codex Desktop")].usage
        self.assertEqual(u.value("app"), (1400 - 700) + 120)
        self.assertEqual(u.split("app"), (1400 - 700, 120))
        self.assertEqual(u.value("io"), (400 + 100) + (4000 + 300) + (300 + 20))

    def test_codex_legacy_token_count_fallback(self):
        p = os.path.join(self.codex, "sessions", "2025", "01", "01", "rollout-old.jsonl")

        def tc(total, last):
            return {"timestamp": "2025-01-01T10:00:00Z", "type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": total},
                         "last_token_usage": {"input_tokens": last, "output_tokens": 0}}}}

        write_jsonl(p, [codex_meta("old", "codex_cli_rs"), codex_turn("u", "gpt-5", ""), tc(100, 100), tc(100, 100),
                        tc(250, 150)])
        self.store.refresh()
        s, c = self.clients()
        self.assertEqual(c[("codex", "codex_cli_rs")].label, "CLI")
        self.assertEqual(s.total.total, 250)

    def test_incremental_read_and_partial_line(self):
        p = os.path.join(self.claude, "projects", "x", "s.jsonl")
        first = claude_msg("m1", "r1", "s", "claude-vscode", output_tokens=5)
        second = json.dumps(claude_msg("m2", "r2", "s", "claude-vscode", output_tokens=7))
        write_jsonl(p, [first], partial=second[:20])
        self.store.refresh()
        self.assertEqual(self.store.summarize("all").total.total, 5)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(second[20:] + "\n")
        os.utime(p, (1e9, 2e9))
        self.assertTrue(self.store.refresh())
        self.assertEqual(self.store.summarize("all").total.total, 12)
        self.assertFalse(self.store.refresh())

    def test_range_filter(self):
        p = os.path.join(self.claude, "projects", "x", "s.jsonl")
        write_jsonl(p, [claude_msg("m1", "r1", "s", "cli", ts="2020-01-01T00:00:00Z", output_tokens=5)])
        self.store.refresh()
        self.assertEqual(self.store.summarize("today").total.total, 0)
        self.assertEqual(self.store.summarize("all").total.total, 5)

    def test_several_codex_dirs(self):
        # e.g. Windows plus a WSL distribution; the same thread in both is counted once.
        other = os.path.join(self.tmp.name, "codex-wsl")
        rows = [codex_meta("t1", "codex-tui"), codex_turn("u1", "gpt-5.5", "high"),
                codex_record("r1", "u1", 100, 0, 10)]
        write_jsonl(os.path.join(self.codex, "sessions", "2026", "10", "04", "rollout-a.jsonl"), rows)
        write_jsonl(os.path.join(other, "sessions", "2026", "10", "04", "rollout-a.jsonl"), rows)
        write_jsonl(os.path.join(other, "sessions", "2026", "10", "04", "rollout-b.jsonl"),
                    [codex_meta("t2", "codex-tui"), codex_turn("u1", "gpt-5.5", "high"),
                     codex_record("r2", "u1", 50, 0, 5)])
        write_jsonl(os.path.join(other, "session_index.jsonl"), [{"id": "t2", "thread_name": "From WSL"}])
        store = Store([self.claude], [self.codex, other], [self.opencode])
        store.refresh()
        s = store.summarize("all")
        self.assertEqual(s.total.total, 110 + 55)
        titles = {t.title for c in s.clients for t in c.threads}
        self.assertIn("From WSL", titles)

    def test_daily_last_30_days(self):
        from datetime import datetime, timedelta

        now = datetime(2026, 10, 4, 12, 0).timestamp()

        def ts(days_ago, hour=10):
            return (datetime(2026, 10, 4, hour) - timedelta(days=days_ago)).astimezone().isoformat()

        p = os.path.join(self.claude, "projects", "x", "s.jsonl")
        write_jsonl(p, [
            claude_msg("m1", "r1", "s", "cli", ts=ts(0), output_tokens=5),
            claude_msg("m2", "r2", "s", "cli", ts=ts(0, 1), output_tokens=7),
            claude_msg("m3", "r3", "s", "cli", ts=ts(29), output_tokens=3),
            claude_msg("m4", "r4", "s", "cli", ts=ts(30), output_tokens=100),  # 31st day back: left out
        ])
        write_jsonl(os.path.join(self.codex, "sessions", "2026", "10", "02", "rollout-d.jsonl"), [
            codex_meta("td", "codex-tui"), codex_turn("u1", "gpt-5.5", "high"),
            dict(codex_record("rd", "u1", 40, 0, 2), timestamp=ts(2)),
        ])
        self.store.refresh()
        # The daily rows ignore the selected range.
        s = self.store.summarize("today", now=now)
        self.assertEqual(len(s.daily), store_mod.DAILY_DAYS)
        self.assertEqual(s.daily[-1].day.isoformat(), "2026-10-04")
        self.assertEqual(s.daily[0].day.isoformat(), "2026-09-05")
        self.assertEqual(s.daily[-1].by_source["claude"].total, 12)
        self.assertEqual(s.daily[-3].by_source["codex"].total, 42)
        self.assertEqual(s.daily[0].total.total, 3)
        self.assertEqual(sum(d.total.total for d in s.daily), 12 + 42 + 3)
        self.assertEqual(s.total.total, 12)

    def test_new_files_found_once_a_minute_known_files_every_tick(self):
        d = os.path.join(self.claude, "projects", "x")
        write_jsonl(os.path.join(d, "a.jsonl"), [claude_msg("m1", "r1", "a", "cli", output_tokens=5)])
        clock = [1000.0]
        with mock.patch.object(store_mod.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(store_mod.glob, "glob", wraps=store_mod.glob.glob) as walk:
            self.store.refresh()
            walks = walk.call_count
            self.assertGreater(walks, 0)

            # Appending to a known file is seen on the next tick without walking the directories.
            with open(os.path.join(d, "a.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(claude_msg("m2", "r2", "a", "cli", output_tokens=7)) + "\n")
            os.utime(os.path.join(d, "a.jsonl"), (1e9, 2e9))
            clock[0] += 5
            self.assertTrue(self.store.refresh())
            self.assertEqual(walk.call_count, walks)
            self.assertEqual(self.store.summarize("all").total.total, 12)

            # A new file waits for the next discovery...
            write_jsonl(os.path.join(d, "b.jsonl"), [claude_msg("m3", "r3", "b", "cli", output_tokens=3)])
            clock[0] += 5
            self.assertFalse(self.store.refresh())
            self.assertEqual(self.store.summarize("all").total.total, 12)
            # ...which happens after DISCOVER_SECONDS,
            clock[0] += store_mod.DISCOVER_SECONDS
            self.assertTrue(self.store.refresh())
            self.assertEqual(self.store.summarize("all").total.total, 15)

            # or right away when asked (the Refresh button).
            write_jsonl(os.path.join(d, "c.jsonl"), [claude_msg("m4", "r4", "c", "cli", output_tokens=1)])
            self.assertTrue(self.store.refresh(discover=True))
            self.assertEqual(self.store.summarize("all").total.total, 16)

            # A file that disappears is no longer stat'ed, but its usage is kept.
            os.remove(os.path.join(d, "c.jsonl"))
            self.store.refresh(discover=True)
            self.assertNotIn(os.path.join(d, "c.jsonl"), self.store.files)
            self.assertEqual(self.store.summarize("all").total.total, 16)


class PathsTest(unittest.TestCase):
    def test_project_name_windows_and_linux(self):
        self.assertEqual(project_name(r"C:\Users\ali\Projects\Token"), "Token")
        self.assertEqual(project_name("C:\\Users\\ali\\Projects\\Token\\"), "Token")
        self.assertEqual(project_name(r"C:\Users\ali"), "~ (home)")
        self.assertEqual(project_name(r"D:\work"), "work")
        self.assertEqual(project_name("/home/ali/Token"), "Token")
        self.assertEqual(project_name("/home/ali"), "~ (home)")
        self.assertEqual(project_name("/Users/ali"), "~ (home)")
        self.assertEqual(project_name("/Users/ali/Developer/Token"), "Token")
        self.assertEqual(project_name("/"), "/")
        self.assertEqual(project_name(""), "—")

    def test_claude_json_next_to_a_symlinked_claude_dir(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            real = os.path.join(home, "dotfiles", "claude-config")
            os.makedirs(real)
            try:
                os.symlink(real, os.path.join(home, ".claude"))
            except (OSError, NotImplementedError):
                self.skipTest("no symlinks here")
            found = paths.claude_json_paths(paths.default_claude_dirs())
            self.assertIn(os.path.join(home, ".claude.json"), found)
            self.assertIn(os.path.join(os.path.realpath(real), ".claude.json"), found)

    def test_opencode_db_env_adds_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a"), os.path.join(d, "b")
            os.makedirs(a)
            os.makedirs(b)
            for p in (os.path.join(a, "opencode.db"), os.path.join(b, "custom.db")):
                open(p, "w").close()
            with mock.patch.dict(os.environ, {"OPENCODE_DB": os.path.join(b, "custom.db")}):
                found = paths.opencode_dbs([a])
            self.assertEqual(sorted(os.path.basename(p) for p in found), ["custom.db", "opencode.db"])

    def test_default_dirs(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            os.environ.pop("CODEX_HOME", None)
            home = os.path.realpath(home)
            self.assertEqual(paths.default_claude_dirs(),
                             [os.path.join(home, ".claude"), os.path.join(home, ".config", "claude")])
            self.assertEqual(paths.default_codex_dirs(), [os.path.join(home, ".codex")])
            with mock.patch.object(paths, "wsl_homes", return_value=[os.path.join(home, "wsl", "ali")]):
                self.assertIn(os.path.join(home, "wsl", "ali", ".codex"), paths.default_codex_dirs(include_wsl=True))
                self.assertIn(os.path.join(home, "wsl", "ali", ".claude"), paths.default_claude_dirs(include_wsl=True))


class MacAutostartTest(unittest.TestCase):
    def test_launch_agent(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}), \
                mock.patch.object(sys, "platform", "darwin"):
            self.assertTrue(autostart.supported())
            self.assertEqual(autostart.label(), "Start at login")
            self.assertFalse(autostart.enabled())
            autostart.set_enabled(True)
            self.assertTrue(autostart.enabled())
            with open(autostart.agent_path(), "rb") as fh:
                agent = plistlib.load(fh)
            self.assertEqual(agent["Label"], autostart.AGENT_LABEL)
            self.assertTrue(agent["RunAtLoad"])
            self.assertEqual(agent["ProgramArguments"][-1], "--autostart")

            # A moved app bundle is followed on the next start.
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", "/Applications/TokenPanel.app/Contents/MacOS/TokenPanel"):
                autostart.sync()
                with open(autostart.agent_path(), "rb") as fh:
                    self.assertEqual(plistlib.load(fh)["ProgramArguments"],
                                     ["/Applications/TokenPanel.app/Contents/MacOS/TokenPanel", "--autostart"])

            autostart.set_enabled(False)
            self.assertFalse(autostart.enabled())
            autostart.set_enabled(False)  # already off


class ConsoleEncodingTest(unittest.TestCase):
    def test_code_pages(self):
        self.assertEqual(console_encoding(857), "cp857")
        self.assertEqual(console_encoding(65001), "utf-8")
        self.assertEqual(console_encoding(0), "utf-8")
        self.assertEqual(console_encoding(99999), "utf-8")

    def test_turkish_console_keeps_letters_and_replaces_symbols(self):
        line = "Yanıtla merhaba — ~ (home) · Claude Code 1.2K…"
        out = line.encode("cp857", errors="tokenpanel").decode("cp857")
        self.assertEqual(out, "Yanıtla merhaba - ~ (home) · Claude Code 1.2K...")


if __name__ == "__main__":
    unittest.main()
