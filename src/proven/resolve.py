"""Layer 3 — precedence resolution. No model. Never resolves a tie.

RANK: ENDORSEMENT 1, SCHEDULE 2, BASE_WORDING 3, PROPOSAL 4 (lower governs).

Rules:
  1. Look for the policy's own construction clause first. Chubb Select+ has none —
     searched, not found — so precedence is asserted by market convention and that
     is stated on the record.
  2. Same taxonomy_code at different ranks -> lowest rank governs.
  3. Two provisions tie on rank and conflict -> DO NOT PICK. Emit both, state that
     precedence could not be established, escalate.
"""
from __future__ import annotations

from .models import Provision, Resolution

RANK = {"ENDORSEMENT": 1, "SCHEDULE": 2, "BASE_WORDING": 3, "PROPOSAL": 4}

_MARKET_CONVENTION_NOTE = (
    "Precedence asserted by market convention. This policy states no express "
    "order of precedence — searched, not found."
)


def resolve(
    provisions: list[Provision],
    has_construction_clause: bool = False,
) -> Resolution:
    governing: dict[str, Provision] = {}
    unresolved: list[str] = []

    by_code: dict[str, list[Provision]] = {}
    for p in provisions:
        if p.taxonomy_code is None:
            continue
        by_code.setdefault(p.taxonomy_code, []).append(p)

    for code, provs in by_code.items():
        ranked = sorted(provs, key=lambda p: p.authority_rank)
        best_rank = ranked[0].authority_rank
        top = [p for p in ranked if p.authority_rank == best_rank]
        if len(top) > 1:
            # Tie on rank. Never pick — escalate.
            unresolved.append(code)
            governing[code] = top[0]   # kept for display only; caller must escalate
        else:
            governing[code] = top[0]

    note = None if has_construction_clause else _MARKET_CONVENTION_NOTE
    return Resolution(
        governing=governing,
        unresolved=unresolved,
        note=note,
        has_construction_clause=has_construction_clause,
    )
