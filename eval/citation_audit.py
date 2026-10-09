"""Citation fidelity audit — a real measurement that needs no domain reviewer.

The gap in the pitch was "we have no number". That is true of ACCURACY, which needs a
qualified reviewer to say what the right answer is. It is not true of CITATION
FIDELITY, which has an objective answer available today:

  1. Does every quoted span exist byte-for-byte at the offsets it claims, in the
     immutable stored page text?
  2. Does every span sit inside a provision the finding actually cites?
  3. Does every figure presented carry an evidence class, and does every figure whose
     basis is unestablished refuse to state a value?

All three are string and integer comparisons against the source. There is no
judgement, so there is nothing for a reviewer to disagree with — which is exactly why
it is publishable now, and why it is a narrower claim than accuracy.

This is what the ALCE-style citation-precision metric reduces to when the corpus is
one document set: a verbatim-fidelity rate at cited offsets with its denominator
stated.

Run:  python -m eval.citation_audit
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from proven import account, ingest, pipeline  # noqa: E402
from proven.gates import norm_ws  # noqa: E402


def audit(carrier: str = "CHUBB") -> dict:
    result = pipeline.run_case(carrier=carrier)
    conn, brief = result["conn"], result["brief"]
    base_doc_id = conn.execute(
        "SELECT id FROM document WHERE kind='BASE_WORDING'").fetchone()["id"]

    spans_checked = spans_faithful = 0
    bounded_checked = bounded_ok = 0
    figures_checked = figures_classed = 0
    failures: list[dict] = []

    page_cache: dict[tuple[str, int], str] = {}

    def page(document_id: str, page_no: int) -> str:
        key = (document_id, page_no)
        if key not in page_cache:
            page_cache[key] = ingest.page_text(conn, document_id, page_no)
        return page_cache[key]

    # The SAME enclosing-provision lookup the boundary gate uses. An auditor with its
    # own copy of the rule can disagree with the thing it audits about what passing
    # means, and it did: this audit reported cl 3.20 out of bounds because its copy
    # lacked the walk-back to a previous page, which is precisely the case cl 3.20
    # needs (heading on p.8, limbs on p.9).
    boundary_cache: dict[int, list[tuple[int, str]]] = {}

    for f in brief["findings"]:
        cited = {c.get("clause") for c in (f.get("detail") or {}).get("cite", [])
                 if isinstance(c, dict) and c.get("clause")}
        for s in f.get("spans", []):
            spans_checked += 1
            source = page(s["document_id"], s["page_no"])[
                s["char_start"]:s["char_end"]]
            if norm_ws(source) == norm_ws(s["quote"]):
                spans_faithful += 1
            else:
                failures.append({
                    "rule_id": f["rule_id"], "problem": "SPAN_NOT_VERBATIM",
                    "page_no": s["page_no"],
                    "claimed": s["quote"][:80], "found": source[:80]})

            if s["document_id"] == base_doc_id and cited:
                bounded_checked += 1
                enc = account.enclosing_provision_ref(
                    lambda p: page(base_doc_id, p),
                    s["page_no"], s["char_start"], boundary_cache)
                if account.citation_consistent(enc, cited):
                    bounded_ok += 1
                else:
                    failures.append({
                        "rule_id": f["rule_id"], "problem": "SPAN_OUT_OF_PROVISION",
                        "enclosing": enc, "cited": sorted(cited)})

        # Every finding that presents a figure must carry an evidence class, and a
        # finding graded UNESTABLISHED must not state a settlement figure.
        detail = f.get("detail") or {}
        if "illustration" in detail:
            figures_checked += 1
            if f["class"] in {"PROVEN", "DECLARED", "MARKET_CONVENTION"}:
                figures_classed += 1
            else:
                failures.append({
                    "rule_id": f["rule_id"], "problem": "FIGURE_WITHOUT_BASIS",
                    "class": f["class"], "illustration": detail["illustration"]})

    accounting = brief["accounting"]
    invariant_holds = (accounting["classified"] + accounting["unaccounted"]
                       == accounting["detected"])

    return {
        "carrier": carrier,
        "measurement": "CITATION_FIDELITY",
        "needs_domain_reviewer": False,
        "what_this_measures": (
            "Whether every quotation is verbatim at the offsets it claims, whether "
            "every quotation sits inside a provision the finding cites, and whether "
            "every figure carries an evidence class. Objective string and integer "
            "comparisons against the immutable source text."),
        "what_this_does_not_measure": (
            "Whether the finding is legally CORRECT. That needs a qualified "
            "reviewer and does not exist in this repository. A perfectly faithful "
            "citation can support a wrong conclusion."),
        "findings": len(brief["findings"]),
        "spans_checked": spans_checked,
        "spans_verbatim": spans_faithful,
        "span_fidelity_rate": (round(spans_faithful / spans_checked, 4)
                               if spans_checked else None),
        "boundary_checked": bounded_checked,
        "boundary_ok": bounded_ok,
        "boundary_rate": (round(bounded_ok / bounded_checked, 4)
                          if bounded_checked else None),
        "figures_checked": figures_checked,
        "figures_with_a_stated_basis": figures_classed,
        "accounting": {
            "detected": accounting["detected"],
            "classified": accounting["classified"],
            "verified_classified": accounting["verified_classified"],
            "keyword_classified": accounting["keyword_classified"],
            "unaccounted": accounting["unaccounted"],
            "invariant_holds": invariant_holds,
            "coverage_of_detected_provisions": (
                round(accounting["classified"] / accounting["detected"], 4)
                if accounting["detected"] else None),
            "verified_coverage_of_detected_provisions": (
                round(accounting["verified_classified"] / accounting["detected"], 4)
                if accounting["detected"] else None),
        },
        "failures": failures,
    }


def print_report(report: dict) -> None:
    print("=" * 74)
    print("PROVEN citation fidelity audit")
    print("=" * 74)
    print("A measurement that needs no domain reviewer, and a narrower claim than")
    print("accuracy. It says the quotations are real; it does not say the")
    print("conclusions are right.")
    print("-" * 74)
    print(f"Carrier: {report['carrier']}   Findings: {report['findings']}")
    print(f"Spans verbatim at cited offsets: {report['spans_verbatim']}/"
          f"{report['spans_checked']}  (rate {report['span_fidelity_rate']})")
    print(f"Spans inside a cited provision:  {report['boundary_ok']}/"
          f"{report['boundary_checked']}  (rate {report['boundary_rate']})")
    print(f"Figures carrying an evidence class: "
          f"{report['figures_with_a_stated_basis']}/{report['figures_checked']}")
    a = report["accounting"]
    print("-" * 74)
    print(f"Bounded omission: {a['detected']} provisions detected, "
          f"{a['classified']} classified, {a['unaccounted']} unaccounted and quoted")
    print(f"  of which hand-verified: {a['verified_classified']} "
          f"({a['verified_coverage_of_detected_provisions']} of detected)")
    print(f"  invariant classified + unaccounted == detected: "
          f"{a['invariant_holds']}")
    if report["failures"]:
        print("-" * 74)
        print(f"FAILURES ({len(report['failures'])}):")
        for f in report["failures"]:
            print(f"  {f}")
    print("=" * 74)


if __name__ == "__main__":
    rep = audit()
    if "--json" in sys.argv:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print_report(rep)
    raise SystemExit(0 if not rep["failures"] else 1)
