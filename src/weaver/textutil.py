"""Near-duplicate detection.

Nine seed resumes describe the same job nine slightly different ways. The graph
keeps every wording (audit value); the renderer must never show two of them.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Callable, Iterable, Sequence, TypeVar

from .domains import normalize

T = TypeVar("T")

# Words that carry no identity — ignored when comparing two claims.
_FILLER = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on", "or",
    "s", "that", "the", "to", "with", "across", "through", "including", "plus", "its",
    "it", "this", "these", "their", "our", "my", "i", "we", "was", "were", "is", "are",
    "be", "been", "own", "up", "out", "over", "under", "while", "also", "every", "all",
}

PREFIX_TOKENS = 4
CONTAINMENT_THRESHOLD = 0.72
RATIO_THRESHOLD = 0.82


def content_tokens(text: str) -> list[str]:
    return [t for t in normalize(text).split() if t and t not in _FILLER]


def containment(a: Sequence[str], b: Sequence[str]) -> float:
    """|A ∩ B| / |smaller| — catches a condensed restatement of a longer bullet."""
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / min(len(set_a), len(set_b))


def ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def same_opening(a: Sequence[str], b: Sequence[str], tokens: int = PREFIX_TOKENS) -> bool:
    """'Delivered AI concept visualization for …' three times is one bullet."""
    if len(a) < tokens or len(b) < tokens:
        return False
    return a[:tokens] == b[:tokens]


def is_near_duplicate(a: str, b: str) -> bool:
    tokens_a, tokens_b = content_tokens(a), content_tokens(b)
    if not tokens_a or not tokens_b:
        return normalize(a) == normalize(b)
    if same_opening(tokens_a, tokens_b):
        return True
    if min(len(tokens_a), len(tokens_b)) >= 4 and containment(tokens_a, tokens_b) >= CONTAINMENT_THRESHOLD:
        return True
    return ratio(a, b) >= RATIO_THRESHOLD


def dedupe(
    items: Iterable[T],
    key: Callable[[T], str] | None = None,
    seen: list[str] | None = None,
) -> list[T]:
    """Keep the longest wording of each distinct claim, in first-seen order."""
    get = key or (lambda item: str(item))
    kept: list[T] = []
    kept_keys: list[str] = []
    prior: list[str] = seen if seen is not None else []

    for item in items:
        text = (get(item) or "").strip()
        if not text:
            continue
        if any(is_near_duplicate(text, earlier) for earlier in prior):
            continue
        replaced = False
        for index, existing in enumerate(kept_keys):
            if is_near_duplicate(text, existing):
                if len(text) > len(existing):
                    kept[index] = item
                    kept_keys[index] = text
                replaced = True
                break
        if not replaced:
            kept.append(item)
            kept_keys.append(text)
    if seen is not None:
        seen.extend(kept_keys)
    return kept


def dedupe_terms(items: Iterable[str]) -> list[str]:
    """Skill-list dedupe: drop subsets and spelling variants ('modeling'/'modelling')."""
    kept: list[str] = []
    token_sets: list[set[str]] = []
    for raw in items:
        item = (raw or "").strip()
        if not item:
            continue
        tokens = set(content_tokens(item))
        if not tokens:
            continue
        replaced = False
        for index, existing in enumerate(token_sets):
            if tokens == existing or tokens <= existing:
                replaced = True
                break
            if existing < tokens:
                kept[index], token_sets[index] = item, tokens
                replaced = True
                break
            if ratio(item, kept[index]) >= 0.88:
                if len(item) > len(kept[index]):
                    kept[index], token_sets[index] = item, tokens
                replaced = True
                break
        if not replaced:
            kept.append(item)
            token_sets.append(tokens)
    return kept


def dedupe_names(items: Iterable[str]) -> list[str]:
    """Client/org dedupe: 'Caviar (YC)' and 'Caviar (Y Combinator)' are one client."""
    import re

    kept: list[str] = []
    bases: list[str] = []
    for raw in items:
        name = (raw or "").strip(" .")
        if not name:
            continue
        base = normalize(re.sub(r"\(.*?\)", " ", name))
        if not base:
            continue
        replaced = False
        for index, existing in enumerate(bases):
            if base == existing:
                if len(name) > len(kept[index]):
                    kept[index] = name
                replaced = True
                break
        if not replaced:
            kept.append(name)
            bases.append(base)
    return kept
