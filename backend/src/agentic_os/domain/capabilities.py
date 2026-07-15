from __future__ import annotations

"""The explicit, inspectable capability vocabulary used as selection input.

Goal decomposition and agent capability declarations both draw capability
names from this catalog instead of inventing free-form strings, so a task's
required capabilities can later be matched against declared agent
capabilities without semantic guesswork.
"""

CAPABILITY_CATALOG: dict[str, str] = {
    "research": "Gather and synthesize source material relevant to a goal.",
    "writing": "Produce written artifacts such as drafts, reports, or summaries.",
    "review": "Evaluate produced work for correctness, completeness, and quality.",
}


def is_known_capability(name: str) -> bool:
    return name in CAPABILITY_CATALOG


def describe_capability(name: str) -> str:
    return CAPABILITY_CATALOG[name]
