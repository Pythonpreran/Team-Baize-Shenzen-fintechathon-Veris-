"""Prior-year and renewal policy PDFs for the renewal-comparison demo.

The two documents are IDENTICAL except for one meaningful change: the renewal adds a
Communicable Disease Exclusion endorsement. The renewal engine must find that change by
reading the text — the answer is not hardcoded anywhere in the UI.

Built with the shared minimal PDF writer so the text layer is exactly what the extractor
sees.
"""
from __future__ import annotations

from pathlib import Path

from pdfbuild import build_pdf

# Lines common to both the prior and the renewal policy. Deliberately stable so the diff
# is exactly one added endorsement.
_COMMON = [
    "POLICY SCHEDULE",
    "Insured Name: Hock Lee Foods Pte Ltd",
    "Business Description: Manufacture of prepared meals; wholesale",
    "Insured Premises: Unit 03-11, 21 Tai Seng Avenue, Singapore 534054",
    "Operative Sections: PROPERTY, PUBLIC LIABILITY",
    "Sum Insured Stock in trade: SGD 100,000",
    "Sum Insured Plant, machinery and equipment: SGD 180,000",
    "Limit of Liability Public Liability: SGD 1,000,000",
    "Deductible: SGD 5,000",
    "",
    "ENDORSEMENTS ATTACHING TO AND FORMING PART OF THIS POLICY",
    "Endorsement No. 1 - Fire and Allied Perils Extension: applicable to all "
    "insured property.",
    "Endorsement No. 2 - Cold Storage Deterioration Extension: applicable to "
    "refrigerated stock.",
]

PRIOR_LINES = [
    "PRIOR YEAR POLICY (2025/2026)",
    "Policy Number: SG-HLF-2025",
    "Period of Insurance: from 2025-03-01 to 2026-02-28",
] + _COMMON

CURRENT_LINES = [
    "RENEWAL POLICY (2026/2027)",
    "Policy Number: SG-HLF-2026",
    "Period of Insurance: from 2026-03-01 to 2027-02-28",
] + _COMMON + [
    "Endorsement No. 3 - Communicable Disease Exclusion: This policy does not "
    "cover any loss, damage, liability, cost or expense arising from any "
    "communicable disease.",
]


def prior_policy_pdf() -> bytes:
    return build_pdf([PRIOR_LINES])


def current_policy_pdf() -> bytes:
    return build_pdf([CURRENT_LINES])


def write_demo_files(demo_dir: Path) -> tuple[Path, Path]:
    """Write both PDFs into the demo-documents directory. Returns the two paths."""
    prior = demo_dir / "renewal-prior-policy.pdf"
    current = demo_dir / "renewal-current-policy.pdf"
    prior.write_bytes(prior_policy_pdf())
    current.write_bytes(current_policy_pdf())
    return prior, current


if __name__ == "__main__":
    here = Path(__file__).resolve()
    demo = here.parents[2] / "demo-documents"
    p, c = write_demo_files(demo)
    print("wrote", p)
    print("wrote", c)
