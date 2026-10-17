"""Evidence-bound portfolio privacy notices.

Country packs remain the source of any legal statement.  This module never fills a missing
primary-source quote with a generic assertion: an unverified pack entry becomes an honest
notice that the country-specific statement is withheld.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from . import jurisdiction


def notices(countries: Iterable[object]) -> list[dict[str, Any]]:
    """Return one owner-safe privacy notice per selected policy country."""
    out: list[dict[str, Any]] = []
    for raw_country in dict.fromkeys(str(value or "").upper() for value in countries):
        if not raw_country or raw_country == jurisdiction.GENERIC:
            continue
        try:
            pack = jurisdiction.pack(raw_country)
        except jurisdiction.PackError:
            out.append({
                "country": raw_country, "state": "INFORMATION_MISSING", "source": None,
                "required_document": "a jurisdiction pack with a primary-source data-protection entry",
                "notice": "No country-specific privacy statement is shown until a verified jurisdiction pack is available.",
            })
            continue
        entry = pack.data_protection
        if entry is None or not entry.owner_facing:
            out.append({
                "country": raw_country, "state": "INFORMATION_MISSING", "source": None,
                "required_document": "a primary-source data-protection entry for this jurisdiction",
                "notice": "The country-specific privacy statement is withheld because its primary-source verification is incomplete.",
            })
            continue
        out.append({
            "country": raw_country, "state": "DOCUMENTED", "source": entry.provenance(),
            "notice": "This notice is limited to the cited jurisdiction-pack statement and does not replace authorised privacy advice.",
        })
    return out
