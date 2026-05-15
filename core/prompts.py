"""File-backed system prompt loader.

Each LLM node loads its default system prompt from `prompts/<name>.md` at
import time. The node UI still exposes a `system_prompt` STRING input that
defaults to the file content but can be overridden per run.
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load(name: str) -> str:
    """Read prompts/<name>.md and return its raw text."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
