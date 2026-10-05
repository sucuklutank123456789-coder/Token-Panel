import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from tokenpanel import limits, pricing
from tokenpanel.limits import LimitHit, estimate, parse_limit_message, read_official
from tokenpanel.model import Event, Usage
from tokenpanel.store import Store

H = 3600
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()


def ev(ts, cost):
    u = Usage(input=100, output=10)
    u.cost = cost
    return Event(source="claude", client="cli", thread_id="t", ts=ts, model="m", effort="", usage=u)


def line(text, ts, **extra):
    return {"type": "assistant", "sessionId": "s", "timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "isApiErrorMessage": True, "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]},
            **extra}


class ParseTest(unittest.TestCase):
    def test_formats(self):
        ts = datetime(2026, 6, 1, 13, 0, tzinfo=timezone.utc).timestamp()
        cases = {
            "Claude AI usage limit reached|1749924000": ("session", 1749924000),
            "You've hit your session limit · resets 4:10pm (UTC)": ("session", ts + 3 * H + 600),
            "You've hit your Sonnet limit · resets Jun 5 at 9am (UTC)": (
                "weekly_sonnet", datetime(2026, 6, 5, 9, tzinfo=timezone.utc).timestamp()),
            "You've hit your weekly limit · resets Jun 3 at 4pm (Europe/Berlin)": (
                "weekly", datetime(2026, 6, 3, 14, tzinfo=timezone.utc).timestamp()),
        }
        for text, (kind, resets) in cases.items():
            hit = parse_limit_message(line(text, ts), ts)
            self.assertEqual((hit.kind, hit.resets_at), (kind, resets), text)
        hit = parse_limit_message(line("x", ts, quotaLimits={"status": "rejected", "rateLimitType": "seven_day_opus",
                                                            "resetsAt": 1790175600}), ts)
        self.assertEqual((hit.kind, hit.resets_at), ("weekly_opus", 1790175600))
        for text in ("API Error: Server is temporarily limiting requests (not your usage limit)",
                     "API Error: Rate limit reached", "Sure, here is the limit you asked about."):
            self.assertIsNone(parse_limit_message(line(text, ts), ts), text)


class EstimateTest(unittest.TestCase):
    def test_official_snapshot_wins_while_its_window_lasts(self):
        snap = (NOW - 600, {"session": (37.0, NOW + 2 * H), "weekly": (12.0, NOW + 3 * 86400),
                            "weekly_opus": (50.0, NOW - 1)})  # this one already reset: left out
        est = estimate([ev(NOW - H, 1.0)], [], snap, NOW)
        self.assertTrue(est.official)
        self.assertEqual([(w.kind, w.used_pct) for w in est.windows], [("session", 37.0), ("weekly", 12.0)])
        self.assertEqual(est.windows[0].start, NOW - 3 * H)
        # Once every window in it has reset, the estimate takes over.
        est = estimate([ev(NOW - H, 1.0)], [], (NOW - 9 * H, {"session": (90.0, NOW - H)}), NOW)
        self.assertFalse(est.official)

    def test_calibrated_from_the_last_limit_hit(self):
        day = 86400
        events = [
            # Two days ago: $4 + $6 in one 5-hour window, then the limit message.
            ev(NOW - 2 * day, 4.0), ev(NOW - 2 * day + H, 6.0),
            # Now: a window that started 2 hours ago with $2.50 so far.
            ev(NOW - 2 * H, 1.5), ev(NOW - H, 1.0),
        ]
        hits = [LimitHit(NOW - 2 * day + 2 * H, "session")]
        est = estimate(events, hits, None, NOW)
        self.assertFalse(est.official)
        session = est.windows[0]
        self.assertEqual(session.kind, "session")
        self.assertEqual(session.start, NOW - 2 * H)
        self.assertAlmostEqual(session.cap, 10.0)
        self.assertAlmostEqual(session.cost, 2.5)
        self.assertAlmostEqual(session.used_pct, 25.0)
        week = est.windows[1]
        self.assertEqual(week.kind, "weekly")
        self.assertIsNone(week.used_pct)  # no weekly limit hit to calibrate from
        self.assertAlmostEqual(week.cost, 12.5)
        self.assertEqual(est.blocked_until, 0)

    def test_blocked_and_weekly_reset_anchor(self):
        reset = NOW + H
        # A weekly limit hit three weeks ago tells the reset weekday and time.
        old_reset = reset - 3 * limits.WEEK_SECONDS
        hits = [LimitHit(old_reset - 3 * H, "weekly", old_reset), LimitHit(NOW - 600, "session", reset)]
        est = estimate([ev(NOW - 2 * H, 1.0)], hits, None, NOW)
        self.assertEqual(est.blocked_until, reset)
        self.assertEqual(est.blocked_kind, "session")
        session, week = est.windows
        self.assertTrue(session.exact_end)
        self.assertEqual(session.end, reset)
        self.assertEqual(week.end, reset)  # the weekly reset repeats every 7 days
        self.assertTrue(week.exact_end)

    def test_nothing_to_show(self):
        self.assertIsNone(estimate([], [], None, NOW))


class OfficialFileTest(unittest.TestCase):
    def test_read_official(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".claude.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"oauthAccount": {"x": 1}, "cachedUsageUtilization": {
                    "fetchedAtMs": 1790000000000, "utilization": {
                        "five_hour": {"utilization": 37.5, "resets_at": "2026-09-21T15:00:00+00:00"},
                        "seven_day": {"utilization": 12, "resets_at": 1790500000},
                        "seven_day_opus": None}}}, fh)
            fetched, kinds = read_official(path)
        self.assertEqual(fetched, 1790000000)
        self.assertEqual(kinds["session"], (37.5, datetime(2026, 9, 21, 15, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(kinds["weekly"], (12.0, 1790500000))
        self.assertNotIn("weekly_opus", kinds)
        self.assertIsNone(read_official("/nonexistent/.claude.json"))


class StoreWiringTest(unittest.TestCase):
    def test_limit_lines_and_official_file_reach_the_summary(self):
        patcher = mock.patch.object(pricing, "_user", {})
        patcher.start()
        self.addCleanup(patcher.stop)
        with tempfile.TemporaryDirectory() as d:
            claude = os.path.join(d, "claude")
            p = os.path.join(claude, "projects", "x", "s.jsonl")
            os.makedirs(os.path.dirname(p))
            ts = NOW - 600
            call = {"type": "assistant", "sessionId": "s", "entrypoint": "cli", "requestId": "r1",
                    "timestamp": datetime.fromtimestamp(NOW - H, timezone.utc).isoformat(),
                    "message": {"id": "m1", "model": "claude-opus-5-5", "content": [],
                                "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}}
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(call) + "\n")
                fh.write(json.dumps(line("You've hit your session limit · resets 4pm (UTC)", ts)) + "\n")
            store = Store([claude], [os.path.join(d, "codex")], [os.path.join(d, "opencode")])
            store.refresh()
            s = store.summarize("all", now=NOW)
            # The limit line is not a model call.
            self.assertEqual(s.total.input, 1_000_000)
            est = s.claude_limits
            self.assertFalse(est.official)
            self.assertEqual(est.blocked_kind, "session")
            self.assertAlmostEqual(est.windows[0].cap, 4.0)  # $4 of Opus 5.5 input before the limit
            # Claude Code's cached usage, when present, replaces the estimate.
            with open(os.path.join(claude, ".claude.json"), "w", encoding="utf-8") as fh:
                json.dump({"cachedUsageUtilization": {"fetchedAtMs": (NOW - 60) * 1000, "utilization": {
                    "five_hour": {"utilization": 100, "resets_at": NOW + 4 * H}}}}, fh)
            self.assertTrue(store.refresh())
            est = store.summarize("all", now=NOW).claude_limits
            self.assertTrue(est.official)
            self.assertEqual(est.windows[0].used_pct, 100)


if __name__ == "__main__":
    unittest.main()
