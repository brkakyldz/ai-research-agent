"""Prompt loading, and the one list a prompt and a check both read.

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

# `judge_grounding` exists in English only: the judge reads an English body and
# a Turkish or English summary, and the instruction language is the judge's,
# not the summary's (PLAN-EVALS E3.2).
PromptName = Literal["summarize", "rank", "judge_grounding"]

# The tags the summariser is asked to prefer. One list, formatted into both
# summarize prompts and read by `evals.checks.tag_vocabulary`, so the prompt
# and the measurement cannot disagree about what "in the vocabulary" means.
#
# It exists because two thirds of the tags on both real runs were used exactly
# once (67.6% and 67.3% singletons, `docs/evals.md`): with no list to prefer,
# the model wrote each story's own words as its tags, and a filter row built
# on those filters nothing. The prompt still lets the model add a tag when none
# of these fits - a vocabulary that cannot grow is a vocabulary that is wrong
# by next quarter - so the share outside the list is measured, not asserted.
TAG_VOCABULARY: tuple[str, ...] = (
    "openai",
    "anthropic",
    "google",
    "meta",
    "microsoft",
    "nvidia",
    "hugging-face",
    "models",
    "agents",
    "open-source",
    "funding",
    "policy",
    "research",
    "safety",
    "hardware",
    "infrastructure",
    "tooling",
    "coding",
    "robotics",
    "benchmarks",
    "enterprise",
    "apps",
    "data",
    "multimodal",
    "security",
)


def tag_vocabulary_line() -> str:
    """The list as the prompt states it: backticked, comma-separated."""
    return ", ".join(f"`{tag}`" for tag in TAG_VOCABULARY)


@lru_cache(maxsize=8)
def load_prompt(name: PromptName, language: str) -> str:
    path = PROMPT_DIR / language / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"no {name} prompt for language {language!r} at {path}")
    return path.read_text(encoding="utf-8").strip()


def available_languages() -> list[str]:
    return sorted(p.name for p in PROMPT_DIR.iterdir() if p.is_dir() and not p.name.startswith("_"))
