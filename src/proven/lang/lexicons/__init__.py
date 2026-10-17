"""Language-specific guard and clause lexicons.

Guards apply EVERY language's lexicon to every output: a model can answer in a language
other than the one it was asked in, and a verdict is a verdict in any of them.
"""
from __future__ import annotations

import re
from typing import Optional

from . import en, zh


class AnyPattern:
    """A read-only union of compiled patterns with the `search` contract guards use."""

    def __init__(self, *patterns: re.Pattern[str]):
        self.patterns = patterns

    def search(self, text: str) -> Optional[re.Match[str]]:
        for pattern in self.patterns:
            match = pattern.search(text or "")
            if match is not None:
                return match
        return None


OUTCOME_GUARD = AnyPattern(en.OUTCOME_PATTERN, zh.OUTCOME_PATTERN)
ADVICE_GUARD = AnyPattern(en.ADVICE_PATTERN, zh.ADVICE_PATTERN)

__all__ = ["ADVICE_GUARD", "AnyPattern", "OUTCOME_GUARD", "en", "zh"]
