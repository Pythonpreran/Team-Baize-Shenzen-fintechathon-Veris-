"""Carrier premium-at-risk computation — NOT ROUTED. Kept as the record of a removal.

READ THIS BEFORE THE REST OF THE FILE.

NOTHING IN THIS MODULE IS SERVED BY ANY ENDPOINT. `/api/carrier/premium-at-risk` refuses
instead, and names the operands it is missing. The only caller is
`api._unused_premium_at_risk`, which is unrouted and marked `# pragma: no cover`.

WHY IT IS STILL HERE. It computes an implied premium by multiplying an exposure gap by an
ILLUSTRATIVE market-average rate over `DEMO_PORTFOLIO`, which is six invented businesses.
Both operands are unestablished: the gap needs figures that sit behind the consent boundary,
and the rate does not exist anywhere in this repository. A product whose entire claim is
that every number traces to a document cannot serve a revenue headline built on two guesses
— that is the one figure that would discredit all the others.

Deleting the file would leave no record of the decision. Keeping it routed would have been
the dishonesty. So it stays, unrouted, with this notice at the top, and the arithmetic below
is still unit-tested because the arithmetic was never the problem — the OPERANDS were.

The prose below is the original commercial argument, preserved as written. It describes what
this WOULD say given a real book and a real rate. It is not what the product says today.
────────────────────────────────────────────────────────────────────────────────────────

THE SENTENCE THIS WAS WRITTEN TO PRODUCE (and the API no longer serves):
    "400 of your 3,000 SME policies have drifted. Here is the premium that
     should be on the book and isn't."

WHAT THIS COMPUTES. For each SME in the book that has drifted, we compute:
  - The exposure differential: evidenced value minus insured value
  - The implied premium shortfall: exposure differential × rate-per-mille
  - Aggregate across the book: total premium the carrier is not collecting

This is NOT a price. It is a measurement of the gap between what the documents say
the business is worth and what the policy covers. The rate is an illustrative
market-average rate, clearly labelled. A real quotation requires an underwriter.

WHY THIS MATTERS TO A CARRIER. A carrier's SME book is typically repriced once a year at
renewal. Between renewals, businesses grow — stock increases, staff are hired, premises
are added. The premium stays flat while the carrier carries unpriced risk. Fixing that
at scale — without adding a single broker — is incremental premium on the existing book
with near-zero acquisition cost. That is the commercial argument.

WHAT IT IS NOT:
  - Not a risk score. It does not say a business is BAD.
  - Not a premium quotation. A real quote needs a rating engine + underwriter.
  - Not computed from private data. Every figure traces to a document or a published
    wording.
  - Not a black mark. The carrier sees priorities and revenue opportunity, not a
    violations list.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


# Illustrative market-average rates (per mille of sum insured, annual).
# These are NOT quotations. They are published market averages for Singapore SME
# property insurance, used to convert a sum-insured gap into an implied premium gap.
# Clearly labelled as illustrative everywhere they appear.
_RATES_PER_MILLE: dict[str, Decimal] = {
    "stock": Decimal("2.8"),           # stock-in-trade, fire + named perils
    "plant": Decimal("3.2"),           # plant & machinery
    "buildings": Decimal("1.8"),       # building reinstatement
    "premises": Decimal("2.5"),        # contents at premises
    "workforce": Decimal("4.5"),       # per-head WIC (illustrative)
}

RATE_NOTICE = (
    "Rates are ILLUSTRATIVE market averages for Singapore SME property insurance. "
    "They are not quotations, not binding, and not specific to any insurer's appetite. "
    "A real premium requires an underwriter. These exist only to convert a documented "
    "exposure gap into a revenue-scale number the carrier can act on."
)


@dataclass
class ExposureGap:
    """One gap between what is evidenced and what is insured."""

    account_id: str
    business_name: str
    interest: str                    # stock | plant | buildings | workforce | premises
    evidenced_value: Decimal         # what the documents say it's worth
    insured_value: Decimal           # what the policy covers
    gap_sgd: Decimal                 # the difference
    implied_premium_sgd: Decimal     # gap × illustrative rate
    basis: str                       # how we know (document source)

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "business_name": self.business_name,
            "interest": self.interest,
            "evidenced_value": str(self.evidenced_value),
            "insured_value": str(self.insured_value),
            "gap_sgd": str(self.gap_sgd),
            "implied_premium_sgd": str(self.implied_premium_sgd),
            "basis": self.basis,
        }


def compute_premium_at_risk(accounts: list[dict]) -> dict:
    """Compute premium-at-risk across a portfolio of SME accounts.

    Each account carries its exposure gaps (from the orchestrator/actions). We sum
    them into one number: the premium the carrier should be collecting and isn't.

    Returns the headline number plus the breakdown by account and interest.
    """
    gaps: list[ExposureGap] = []

    for account in accounts:
        account_id = account.get("account_id", "")
        name = account.get("business_name", "")
        exposures = account.get("exposures", [])

        for exp in exposures:
            interest = exp.get("interest", "")
            evidenced = Decimal(str(exp.get("evidenced_value", 0) or 0))
            insured = Decimal(str(exp.get("insured_value", 0) or 0))
            gap = max(Decimal(0), evidenced - insured)
            if gap <= 0:
                continue

            rate = _RATES_PER_MILLE.get(interest, Decimal("2.5"))
            implied = (gap * rate / Decimal("1000")).quantize(Decimal("0.01"))

            gaps.append(ExposureGap(
                account_id=account_id,
                business_name=name,
                interest=interest,
                evidenced_value=evidenced,
                insured_value=insured,
                gap_sgd=gap,
                implied_premium_sgd=implied,
                basis=exp.get("basis", "documents on file"),
            ))

    total_premium = sum(g.implied_premium_sgd for g in gaps)
    total_exposure = sum(g.gap_sgd for g in gaps)
    accounts_affected = len({g.account_id for g in gaps})

    by_interest: dict[str, dict] = {}
    for g in gaps:
        if g.interest not in by_interest:
            by_interest[g.interest] = {"gap_sgd": Decimal(0),
                                       "premium_sgd": Decimal(0), "count": 0}
        by_interest[g.interest]["gap_sgd"] += g.gap_sgd
        by_interest[g.interest]["premium_sgd"] += g.implied_premium_sgd
        by_interest[g.interest]["count"] += 1

    return {
        "headline": (
            f"{accounts_affected} of {len(accounts)} SME policies have drifted. "
            f"The premium that should be on the book: S${total_premium:,.0f}/year."
        ),
        "total_premium_at_risk_sgd": str(total_premium),
        "total_exposure_gap_sgd": str(total_exposure),
        "accounts_total": len(accounts),
        "accounts_drifted": accounts_affected,
        "drift_rate_pct": (
            f"{(accounts_affected / len(accounts) * 100):.0f}%"
            if accounts else "0%"
        ),
        "by_interest": {
            k: {"gap_sgd": str(v["gap_sgd"]),
                "premium_sgd": str(v["premium_sgd"]),
                "count": v["count"]}
            for k, v in sorted(by_interest.items(),
                               key=lambda x: -x[1]["premium_sgd"])
        },
        "gaps": [g.as_dict() for g in sorted(
            gaps, key=lambda g: -g.implied_premium_sgd)],
        "rates_used": {k: str(v) for k, v in _RATES_PER_MILLE.items()},
        "rate_notice": RATE_NOTICE,
        "synthetic": True,
        "notice": (
            "DEMONSTRATION. Account data is synthetic. The computation is real: "
            "for each business whose documented value exceeds its insured value, "
            "the gap is converted to an implied premium at illustrative market rates. "
            "This is the revenue currently absent from the book — not a risk judgement "
            "and not a quotation."
        ),
    }


# ── demo portfolio with exposure data ────────────────────────────────────────

DEMO_PORTFOLIO: list[dict] = [
    {
        "account_id": "ACC-10231",
        "business_name": "Hock Lee Foods Pte Ltd",
        "exposures": [
            {"interest": "stock", "evidenced_value": 150000, "insured_value": 100000,
             "basis": "Management accounts vs policy schedule"},
            {"interest": "plant", "evidenced_value": 180000, "insured_value": 180000,
             "basis": "Supplier invoice + policy schedule"},
            {"interest": "premises", "evidenced_value": 2, "insured_value": 1,
             "basis": "ACRA registry vs schedule (locations count)"},
        ],
    },
    {
        "account_id": "ACC-10244",
        "business_name": "Kimly Metalworks Pte Ltd",
        "exposures": [
            {"interest": "plant", "evidenced_value": 320000, "insured_value": 240000,
             "basis": "Asset register vs policy schedule"},
            {"interest": "workforce", "evidenced_value": 28, "insured_value": 22,
             "basis": "CPF record vs WICA declaration"},
        ],
    },
    {
        "account_id": "ACC-10250",
        "business_name": "Tampines Cold Chain LLP",
        "exposures": [
            {"interest": "stock", "evidenced_value": 420000, "insured_value": 300000,
             "basis": "Inventory system vs policy schedule"},
            {"interest": "buildings", "evidenced_value": 850000, "insured_value": 650000,
             "basis": "Reinstatement assessment vs sum insured"},
        ],
    },
    {
        "account_id": "ACC-10262",
        "business_name": "Serangoon Dental Group",
        "exposures": [
            {"interest": "premises", "evidenced_value": 3, "insured_value": 2,
             "basis": "ACRA branches vs schedule (locations count)"},
        ],
    },
    {
        "account_id": "ACC-10275",
        "business_name": "Jurong Auto Services",
        "exposures": [
            {"interest": "stock", "evidenced_value": 95000, "insured_value": 80000,
             "basis": "Accounts vs policy schedule"},
            {"interest": "plant", "evidenced_value": 210000, "insured_value": 160000,
             "basis": "Equipment invoices vs policy schedule"},
        ],
    },
    {
        "account_id": "ACC-10288",
        "business_name": "Bugis Print & Signage",
        "exposures": [],  # no gap — adequately insured
    },
    {
        "account_id": "ACC-10301",
        "business_name": "West Coast Marine Supplies",
        "exposures": [
            {"interest": "stock", "evidenced_value": 180000, "insured_value": 120000,
             "basis": "Warehouse inventory vs policy schedule"},
        ],
    },
    {
        "account_id": "ACC-10315",
        "business_name": "Bishan Tech Solutions",
        "exposures": [],  # no gap
    },
    {
        "account_id": "ACC-10322",
        "business_name": "Orchard Electronics Pte Ltd",
        "exposures": [
            {"interest": "stock", "evidenced_value": 260000, "insured_value": 200000,
             "basis": "Accounts vs policy schedule"},
        ],
    },
    {
        "account_id": "ACC-10340",
        "business_name": "Pasir Ris Packaging Co",
        "exposures": [
            {"interest": "plant", "evidenced_value": 145000, "insured_value": 120000,
             "basis": "Asset register vs policy schedule"},
        ],
    },
]
