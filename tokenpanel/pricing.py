"""API price estimate: what the tokens would cost at the providers' pay-as-you-go API prices.

Subscriptions (Claude Pro/Max, ChatGPT Plus/Pro) are billed differently; this is the "API equivalent".
Prices are USD per million tokens. A JSON file can add or override models, see `user_prices_path()`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

from .model import Usage


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float  # 5-minute cache writes (Claude); OpenAI cache writes (GPT-5.6 and later)
    cache_write_1h: float | None = None  # Claude 1-hour cache writes
    # Requests with more input than this are billed at 2x input and 1.5x output (OpenAI's 1M-context models).
    long_context: int | None = None

    @classmethod
    def claude(cls, inp: float, out: float, read: float) -> Price:
        # Cache writes: 1.25x input for 5 minutes, 2x for 1 hour.
        return cls(inp, out, read, inp * 1.25, inp * 2)

    @classmethod
    def openai(cls, inp: float, cached: float, out: float, long_context: bool = False) -> Price:
        # Cache writes are reported (and billed at 1.25x input) from GPT-5.6 on; older models report none.
        return cls(inp, out, cached, inp * 1.25, None, 272_000 if long_context else None)


# platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-05).
_CLAUDE = {
    ("claude-fable-5-1", "claude-mythos-5-1"): Price.claude(10, 50, 0.25),
    ("claude-fable-5", "claude-mythos-5"): Price.claude(10, 50, 1),
    ("claude-opus-5-5",): Price.claude(4, 20, 0.20),
    ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5"): Price.claude(
        5, 25, 0.50
    ),
    ("claude-opus-4-1", "claude-opus-4", "claude-opus-4-0", "claude-3-opus"): Price.claude(15, 75, 1.50),
    ("claude-sonnet-5-5", "claude-sonnet-5"): Price.claude(2, 10, 0.20),
    (
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-sonnet-4",
        "claude-sonnet-4-0",
        "claude-3-7-sonnet",
        "claude-3-5-sonnet",
    ): Price.claude(3, 15, 0.30),
    ("claude-haiku-4-5",): Price.claude(1, 5, 0.10),
    ("claude-3-5-haiku",): Price.claude(0.80, 4, 0.08),
}

# OpenAI models used by Codex: input, cached input, output. Reasoning tokens are billed (and logged) as output.
# From developers.openai.com/api/docs/pricing and the model pages, 2026-10-05. These were read through search
# results (the pages could not be fetched directly), so they are less certain than the Claude prices above;
# prices.json can correct them. "gpt-6" without a suffix has no official page and stays unpriced.
_OPENAI = {
    ("gpt-6-astra",): Price.openai(10, 1.00, 50, long_context=True),
    ("gpt-6.1-sol",): Price.openai(2, 0.10, 10, long_context=True),
    ("gpt-6-sol",): Price.openai(2, 0.20, 10, long_context=True),
    ("gpt-6-luna",): Price.openai(0.10, 0.01, 0.50, long_context=True),
    ("gpt-5.6-sol",): Price.openai(4, 0.40, 20, long_context=True),
    ("gpt-5.6-luna",): Price.openai(0.20, 0.02, 1.20, long_context=True),
    ("gpt-5.5",): Price.openai(5, 0.50, 30, long_context=True),
    ("gpt-5.4",): Price.openai(2.50, 0.25, 15, long_context=True),
    ("gpt-5.3-codex", "gpt-5.2", "gpt-5.2-codex"): Price.openai(1.75, 0.175, 14),
    ("gpt-5.1", "gpt-5.1-codex", "gpt-5.1-codex-max", "gpt-5", "gpt-5-codex"): Price.openai(1.25, 0.125, 10),
    ("gpt-5.1-codex-mini", "gpt-5-mini", "gpt-5-codex-mini"): Price.openai(0.25, 0.025, 2),
    ("codex-mini-latest",): Price.openai(1.50, 0.375, 6),
    ("o3", "gpt-4.1"): Price.openai(2, 0.50, 8),
    ("o4-mini",): Price.openai(1.10, 0.275, 4.40),
}

PRICES: dict[str, Price] = {name: price for names, price in (_CLAUDE | _OPENAI).items() for name in names}

# Fast mode (Claude Opus 5.5 / 5 / 4.8) is billed at twice the standard rates, caching included.
FAST_MULTIPLIER = 2.0

_DATE = re.compile(r"[-@]\d{8}$")


def normalize(model: str) -> str:
    """Strips provider prefixes, date snapshots and context tags: "anthropic/claude-sonnet-4-5-20250929[1m]",
    "us.anthropic.claude-opus-4-1-20250805-v1:0" and "claude-sonnet-4-5" all become "claude-sonnet-4-5"."""
    m = model.strip().lower()
    m = m.rsplit("/", 1)[-1]
    m = re.sub(r"\[[^\]]*\]$", "", m)
    m = re.sub(r"-v\d+(:\d+)?$", "", m)
    m = re.sub(r"^(?:[a-z]{2,4}\.)?anthropic\.", "", m)
    return _DATE.sub("", m)


def user_prices_path() -> str:
    if os.environ.get("TOKENPANEL_PRICES"):
        return os.environ["TOKENPANEL_PRICES"]
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "tokenpanel", "prices.json")


def load_user_prices(path: str | None = None) -> dict[str, Price]:
    """{"model-id": {"input": 1.25, "output": 10, "cache_read": 0.125, "cache_write": 1.25}, ...}"""
    path = path or user_prices_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    out = {}
    if not isinstance(data, dict):
        return out
    for name, p in data.items():
        if not isinstance(p, dict):
            continue
        try:
            inp, outp = float(p["input"]), float(p["output"])
            read = float(p.get("cache_read", inp))
            write = float(p.get("cache_write", inp))
            write_1h = float(p["cache_write_1h"]) if "cache_write_1h" in p else None
        except (KeyError, TypeError, ValueError):
            continue  # skip a malformed entry, keep the rest
        out[normalize(name)] = Price(inp, outp, read, write, write_1h)
    return out


_user: dict[str, Price] | None = None


def lookup(model: str) -> Price | None:
    global _user
    if _user is None:
        _user = load_user_prices()
    key = normalize(model)
    return _user.get(key) or PRICES.get(key)


def price_usage(u: Usage, model: str, write_1h: int = 0, fast: bool = False) -> None:
    """Sets u.cost (USD) and u.unpriced (tokens of a model with no known price)."""
    p = lookup(model) if model else None
    if p is None:
        u.cost = 0.0
        u.unpriced = u.input + u.cache_write + u.output
        return
    write_1h = min(write_1h, u.cache_write)
    write_1h_price = p.cache_write_1h if p.cache_write_1h is not None else p.cache_write
    in_mult = out_mult = 1.0
    if p.long_context and u.input + u.cache_read + u.cache_write > p.long_context:
        in_mult, out_mult = 2.0, 1.5
    cost = (
        in_mult
        * (
            u.input * p.input
            + (u.cache_write - write_1h) * p.cache_write
            + write_1h * write_1h_price
            + u.cache_read * p.cache_read
        )
        + out_mult * u.output * p.output
    ) / 1_000_000
    u.cost = cost * (FAST_MULTIPLIER if fast else 1.0)
    u.unpriced = 0.0
