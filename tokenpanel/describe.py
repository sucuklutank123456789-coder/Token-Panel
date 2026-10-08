"""Wording shared by the panel and `--dump`, so both say the same thing."""

from __future__ import annotations

from . import fmt
from .model import Usage


def cost_text(u: Usage) -> str:
    """'API cost ≈ $12.40', with a note when some tokens have no known price."""
    text = f"API cost ≈ {fmt.money(u.cost)}"
    if u.unpriced:
        text += f" (+ {fmt.short(u.unpriced)} tokens of models without a known price)"
    return text



UNREADABLE_NOTE = (
    "{n} compressed Codex logs (older than 7 days) could not be read: the zstandard Python package is missing. "
    "Reinstall Token Panel, or run: pip install zstandard"
)
