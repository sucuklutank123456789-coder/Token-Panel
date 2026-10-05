import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from tokenpanel import pricing
from tokenpanel.store import Store

SCHEMA = """
CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, directory TEXT, title TEXT,
                      version TEXT, time_created INTEGER, time_updated INTEGER);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER,
                   time_updated INTEGER, data TEXT);
"""

T0 = 1_790_000_000_000  # ms


class OpenCodeTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(pricing, "_user", {})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "opencode")
        os.makedirs(self.dir)
        self.db = os.path.join(self.dir, "opencode.db")
        con = sqlite3.connect(self.db)
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        con.commit()
        self.con = con
        self.addCleanup(con.close)
        self.store = Store([os.path.join(self.tmp.name, "none")], [os.path.join(self.tmp.name, "none")], [self.dir])
        self.n = 0

    def session(self, sid, title, parent=None, directory="/home/u/site"):
        self.con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
                         (sid, "p", parent, directory, title, "1.18.34", T0, T0))

    def message(self, mid, sid, model):
        data = {"role": "assistant", "modelID": model, "providerID": "x", "path": {"cwd": "/home/u/site"},
                "tokens": {"input": 0, "output": 0}}
        self.con.execute("INSERT INTO message VALUES (?,?,?,?,?)", (mid, sid, T0, T0, json.dumps(data)))

    def part(self, mid, sid, data):
        self.n += 1
        pid = f"prt_{self.n:04d}"
        t = T0 + self.n * 1000
        self.con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (pid, mid, sid, t, t, json.dumps(data)))

    def step(self, mid, sid, inp, out, reasoning=0, read=0, write=0, cost=0.0):
        self.part(mid, sid, {"type": "step-finish", "cost": cost, "tokens": {
            "input": inp, "output": out, "reasoning": reasoning, "cache": {"read": read, "write": write}}})

    def summary(self):
        self.con.commit()
        self.store.refresh(discover=True)
        return self.store.summarize("all")

    def test_steps_tools_subagents_and_prices(self):
        self.session("ses_a", "Build the site")
        self.session("ses_child", "Child session - 2026", parent="ses_a")
        self.message("msg_1", "ses_a", "claude-sonnet-5-5")
        self.part("msg_1", "ses_a", {"type": "step-start"})
        self.part("msg_1", "ses_a", {"type": "tool", "tool": "bash", "state": {"status": "completed"}})
        self.part("msg_1", "ses_a", {"type": "text", "text": "hello"})
        self.step("msg_1", "ses_a", 1000, 100, reasoning=50, read=4000)
        self.part("msg_1", "ses_a", {"type": "tool", "tool": "edit", "state": {"status": "completed"}})
        self.step("msg_1", "ses_a", 2000, 200)
        # A subagent session using a model the panel has no price for: OpenCode's own cost is used.
        self.message("msg_2", "ses_child", "some-local-model")
        self.step("msg_2", "ses_child", 300, 30, cost=0.25)
        s = self.summary()

        oc = [c for c in s.clients if c.source == "opencode"]
        self.assertEqual(len(oc), 1)
        self.assertEqual(oc[0].label, "All clients")
        self.assertEqual(len(oc[0].threads), 1)  # the subagent's usage is part of its parent conversation
        t = oc[0].threads[0]
        self.assertEqual(t.title, "Build the site")
        self.assertEqual(t.project, "site")
        self.assertEqual(t.calls, 3)
        self.assertEqual(set(t.tools), {"bash", "edit", "Reply (no tools)"})
        u = s.by_source["opencode"]
        self.assertEqual(u.input, 3300)
        self.assertEqual(u.output, 150 + 200 + 30)  # reasoning counted as output, like the other tools
        self.assertEqual(u.reasoning, 50)
        self.assertEqual(u.cache_read, 4000)
        self.assertEqual(u.value("app"), 3300 + 380)
        # Sonnet 5.5 at $2 in / $0.20 cache read / $10 out, plus OpenCode's $0.25 for the unknown model.
        expected = (3000 * 2 + 4000 * 0.2 + 350 * 10) / 1e6 + 0.25
        self.assertAlmostEqual(u.cost, expected)
        self.assertEqual(u.unpriced, 0)

    def test_incremental_reads(self):
        self.session("ses_a", "One")
        self.message("msg_1", "ses_a", "gpt-5.5")
        self.step("msg_1", "ses_a", 100, 10)
        self.assertEqual(self.summary().by_source["opencode"].input, 100)
        self.step("msg_1", "ses_a", 200, 20)
        self.assertEqual(self.summary().by_source["opencode"].input, 300)
        # Nothing new: nothing changes and nothing is counted twice.
        self.assertFalse(self.store.refresh(discover=True))
        self.assertEqual(self.store.summarize("all").by_source["opencode"].input, 300)

    def test_unreadable_or_missing_database(self):
        with open(os.path.join(self.dir, "opencode-beta.db"), "wb") as fh:
            fh.write(b"not a database")
        self.session("ses_a", "One")
        self.message("msg_1", "ses_a", "gpt-5.5")
        self.step("msg_1", "ses_a", 100, 10)
        self.assertEqual(self.summary().by_source["opencode"].input, 100)


if __name__ == "__main__":
    unittest.main()
