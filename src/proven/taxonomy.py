"""Taxonomy loader. Reads proven/taxonomy/taxonomy.yaml and exposes the
per-carrier facts the engine must never hardcode:

  * average threshold        — 0.85 for Chubb, null (any shortfall) for MSIG/Liberty
  * average formula presence — stated verbatim by Liberty, absent for Chubb/MSIG

Thresholds and day counts are per (carrier, clause instance) facts. Any code that
reads them from a single global constant is wrong by construction, so they are read
from the taxonomy instances here.

The exact (carrier, clause_ref) -> taxonomy_code map is the accounting classifier
and lives in account.py alongside the clause regex.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml

# parents[2] is the repository root. See the note in align.py: the previous
# `parents[3] / "proven"` resolved outside the checkout.
TAXONOMY_PATH = (
    Path(__file__).resolve().parents[2] / "taxonomy" / "taxonomy.yaml"
)


class Taxonomy:
    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def load(cls, path: Path | str = TAXONOMY_PATH) -> "Taxonomy":
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def _average_instances(self) -> list[dict]:
        return (
            self.data.get("ADEQ", {})
            .get("ADEQ.AVERAGE_CONDITION", {})
            .get("instances", [])
        )

    def average_instances_for(
        self, carrier: str, settlement_basis: Optional[str] = None,
    ) -> list[dict]:
        """Every average provision that could apply. Returns a LIST because one
        wording can carry more than one.

        Liberty carries two: General Condition 4 on a VALUE basis with no threshold,
        and "Average Relief" on a REINSTATEMENT basis with an 85% threshold. An
        instance whose basis is ANY applies regardless.
        """
        out = []
        for inst in self._average_instances():
            if inst.get("carrier") != carrier:
                continue
            basis = inst.get("settlement_basis", "ANY")
            if settlement_basis is None or basis == "ANY" \
                    or basis == settlement_basis:
                out.append(inst)
        return out

    def average_basis_ambiguous(
        self, carrier: str, settlement_basis: Optional[str] = None,
    ) -> bool:
        """True when more than one average provision could apply and the documents
        do not establish which basis governs.

        This is not a defensive check, it is the abstention rule applied to our own
        clause data. Liberty's two average provisions carry DIFFERENT thresholds
        (none, and 85%). Picking one silently would either over-report the insured
        by removing a cushion the policy grants, or under-report by applying one it
        does not. Both are wrong answers, so the engine declines to choose and the
        finding escalates instead.
        """
        matches = self.average_instances_for(carrier, settlement_basis)
        if len(matches) < 2:
            return False
        thresholds = {
            None if m.get("threshold_pct") is None else str(m["threshold_pct"])
            for m in matches
        }
        return len(thresholds) > 1

    def average_threshold(
        self, carrier: str, settlement_basis: Optional[str] = None,
    ) -> Optional[Decimal]:
        """The average threshold, or None where the clause states no threshold and
        any shortfall bites. None is a FACT, not a missing value.

        Call `average_basis_ambiguous` FIRST. Where two provisions disagree and the
        basis is unknown this returns the MORE CONSERVATIVE threshold — the one less
        favourable to the insured — so that a caller which ignores the ambiguity
        under-claims rather than over-claims. It never silently returns whichever
        instance happens to be listed first.
        """
        matches = self.average_instances_for(carrier, settlement_basis)
        if not matches:
            return None
        if len(matches) == 1:
            t = matches[0].get("threshold_pct")
            return None if t is None else Decimal(str(t))
        # Ambiguous: None ("any shortfall") is the harshest, so it wins.
        if any(m.get("threshold_pct") is None for m in matches):
            return None
        return min(Decimal(str(m["threshold_pct"])) for m in matches)

    def average_formula_stated(
        self, carrier: str, settlement_basis: Optional[str] = None,
    ) -> bool:
        """True only where the wording states the average arithmetic verbatim.
        Liberty states it on both bases; Chubb cl 4.2 and MSIG GC12 say
        'proportionately reduced' and stop."""
        matches = self.average_instances_for(carrier, settlement_basis)
        return any(m.get("formula_stated_in_document", False) for m in matches)

    def has_general_declaration_clause(self, carrier: str) -> bool:
        """MSIG present; Chubb and Liberty absent as a general clause."""
        instances = (
            self.data.get("COND", {})
            .get("COND.DECLARATION_CLAUSE_GENERAL", {})
            .get("instances", [])
        )
        for inst in instances:
            if inst.get("carrier") == carrier:
                return bool(inst.get("present", False))
        return False
