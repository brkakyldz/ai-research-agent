"""Prompt loading.

Prompts are markdown files on disk, one directory per language, rather than
string literals in the node that uses them. Two reasons: editing the Turkish
instructions is a text edit rather than a code change, and a diff of a prompt
reads as a diff of prose.

They are read once and cached - they cannot change while the process runs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

PROMPT_DIR = Path(__file__).resolve().parent

PromptName = Literal["summarize", "rank"]


@lru_cache(maxsize=8)
def load_prompt(name: PromptName, language: str) -> str:
    path = PROMPT_DIR / language / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"no {name} prompt for language {language!r} at {path}")
    return path.read_text(encoding="utf-8").strip()


def available_languages() -> list[str]:
    return sorted(p.name for p in PROMPT_DIR.iterdir() if p.is_dir() and not p.name.startswith("_"))
