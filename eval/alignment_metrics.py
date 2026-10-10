"""Score coverage-mechanism alignment against ground truth read from the wordings.

WHY THIS IS THE MEASUREMENT WE CAN DEFEND TODAY
-----------------------------------------------
`eval/harness.py` scores FINDINGS and correctly refuses to print a general accuracy
rate, because a finding's ground truth requires an insurance-qualified reviewer to
decide questions of legal construction. That reviewer does not exist yet.

Alignment ground truth is a different kind of question: not "what does this clause
mean in law" but "does this span contain the structural elements the mechanism
requires". A second reader checks that against the page in seconds, with no insurance
qualification. So this harness produces a real, checkable number now.

It is NARROWER than accuracy and must never be presented as accuracy. It reports:
  * precision  — when the engine binds an alignment, is it right
  * recall     — of the mechanisms known to be present, how many were found
  * rejection rate — how much the verifier threw away, which is the evidence that the
                     verifier is load-bearing rather than decorative

Run:  python -m eval.alignment_metrics
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from proven import align  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

from proven import corpus as corpus_store  # noqa: E402

LABELS = HERE / "alignment_labels.yaml"

# Other-insurance language inside a bound average alignment means the verifier let a
# contribution clause through. Scored as a false positive regardless of the label.
_CONTRIBUTION = re.compile(
    r"any other insurance|other insurances?\b|double insurance", re.IGNORECASE)


def load_pages(path: Path) -> list[str]:
    """Split an extracted wording on its page markers.

    Falls back to one page so a corpus file without markers still scores rather than
    silently contributing nothing.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    pages = [p for p in raw.split("=== PAGE ") if p.strip()]
    return pages or [raw]


def _norm_value(v) -> str | None:
    if v is None:
        return None
    return str(v).strip()


@dataclass
class DocScore:
    carrier: str
    jurisdiction: str
    proposals: int = 0
    verified: int = 0
    rejected: int = 0
    hits: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)
    value_mismatches: list[str] = field(default_factory=list)
    absent_violations: list[str] = field(default_factory=list)


def score_document(entry: dict) -> DocScore | None:
    # Resolved by corpus id. Returning None when the document is absent keeps this a
    # degraded mode rather than an error: a corpus that has not been fetched means
    # fewer documents scored, and the harness reports the smaller N instead of
    # claiming a number it did not measure.
    try:
        path = corpus_store.text_path(entry["corpus_id"])
    except corpus_store.ManifestError:
        return None
    if not path.exists():
        return None
    carrier = entry["carrier"]
    score = DocScore(carrier=carrier, jurisdiction=entry.get("jurisdiction", "?"))

    result = align.align_pages(load_pages(path), carrier=carrier)
    score.proposals = result.proposals
    score.verified = result.verified
    score.rejected = result.rejected

    # ── recall: every expectation must be met by some bound alignment ──────
    unclaimed = list(result.alignments)
    for exp in entry.get("expect", []) or []:
        mech = exp["mechanism"]
        want = {k: _norm_value(v) for k, v in (exp.get("values") or {}).items()}
        match = None
        for a in unclaimed:
            if a.mechanism != mech:
                continue
            # A value expectation of null means the wording states none. An alignment
            # that invented one does not satisfy it — that is the MSIG threshold case,
            # where a spurious 85% would cost the insured money.
            ok = True
            for k, v in want.items():
                got = _norm_value(a.values.get(k))
                if v is None and got is not None:
                    ok = False
                    break
                if v is not None and got != v:
                    ok = False
                    break
            if ok:
                match = a
                break
        if match is not None:
            unclaimed.remove(match)
            score.hits.append(f"{mech} {want or ''}".strip())
        else:
            present = [a for a in result.alignments if a.mechanism == mech]
            if present and want:
                got = {k: _norm_value(present[0].values.get(k)) for k in want}
                score.value_mismatches.append(
                    f"{mech}: wanted {want}, got {got}")
            else:
                score.missed.append(f"{mech} {want or ''}".strip())

    # ── precision: bound alignments the label set did not expect ──────────
    expected_mechs = {e["mechanism"] for e in (entry.get("expect") or [])}
    for a in result.alignments:
        if _CONTRIBUTION.search(a.quote):
            score.false_positives.append(
                f"{a.mechanism} p{a.page_no}: contribution language in a bound span")
        elif a.mechanism not in expected_mechs:
            score.false_positives.append(
                f"{a.mechanism} p{a.page_no}: not expected in this wording")

    # ── explicit absence: a bound alignment here is definitionally wrong ──
    for absent in entry.get("expect_absent", []) or []:
        mech = absent["mechanism"]
        if any(a.mechanism == mech for a in result.alignments):
            score.absent_violations.append(mech)

    return score


def main() -> int:
    spec = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
    scores = [s for s in (score_document(d) for d in spec["documents"]) if s]

    print("=" * 74)
    print("PROVEN — coverage-mechanism alignment")
    print("=" * 74)
    print("Ground truth read from the wordings by the team, not reviewed by an")
    print("insurance-qualified expert. This measures whether a bound span contains")
    print("the structural elements the mechanism requires. It is NOT an accuracy")
    print("rate for findings and must not be compared with one.")
    print("-" * 74)

    tot_hits = tot_missed = tot_fp = tot_prop = tot_ver = tot_rej = 0
    for s in scores:
        tot_hits += len(s.hits)
        tot_missed += len(s.missed) + len(s.value_mismatches)
        tot_fp += len(s.false_positives) + len(s.absent_violations)
        tot_prop += s.proposals
        tot_ver += s.verified
        tot_rej += s.rejected

    def ratio(a: int, b: int) -> float:
        return round(a / b, 3) if b else 0.0

    precision = ratio(tot_hits, tot_hits + tot_fp)
    recall = ratio(tot_hits, tot_hits + tot_missed)

    print(f"Documents scored      {len(scores)}")
    print(f"Proposals offered     {tot_prop}")
    print(f"Bound (verified)      {tot_ver}")
    print(f"Rejected by verifier  {tot_rej}   "
          f"rejection rate {ratio(tot_rej, tot_prop)}")
    print("-" * 74)
    print(f"Correct alignments    {tot_hits}")
    print(f"Missed / wrong value  {tot_missed}")
    print(f"False positives       {tot_fp}")
    print(f"PRECISION             {precision}")
    print(f"RECALL                {recall}")
    print("-" * 74)

    sg = [s for s in scores if s.jurisdiction == "SG"]
    my = [s for s in scores if s.jurisdiction == "MY"]
    india = [s for s in scores if s.jurisdiction == "IN"]

    def jurisdiction_recall(rows: list[DocScore]) -> tuple[float, int, int]:
        hits = sum(len(s.hits) for s in rows)
        missed = sum(len(s.missed) + len(s.value_mismatches) for s in rows)
        return ratio(hits, hits + missed), hits, missed

    sg_recall, sg_hits, sg_miss = jurisdiction_recall(sg)
    my_recall, my_hits, my_miss = jurisdiction_recall(my)
    in_recall, in_hits, in_miss = jurisdiction_recall(india)
    print("GENERALISATION — the claim that matters")
    print(f"  Singapore, read by hand      recall {sg_recall}"
          f"  ({sg_hits}/{sg_hits + sg_miss})")
    print(f"  Malaysia, NEVER hand-mapped  recall {my_recall}"
          f"  ({my_hits}/{my_hits + my_miss})")
    print("  The Malaysian row is the point: no clause map exists for these carriers,")
    print("  so anything found there was found from structure alone.")
    if india:
        print(f"  India, targeted health terms recall {in_recall}"
              f"  ({in_hits}/{in_hits + in_miss})")
    print("-" * 74)

    print("CLAIM LANGUAGE THE EVIDENCE SUPPORTS:")
    print(f"  On {tot_hits + tot_missed} mechanism instances across {len(scores)} real")
    print(f"  published wordings, structural alignment achieved precision {precision}")
    print(f"  and recall {recall}, rejecting {tot_rej} of {tot_prop} proposals. Ground")
    print("  truth was read from the documents by the team and is checkable against")
    print("  the cited pages by any second reader. This is a measurement of clause")
    print("  identification, NOT of findings, and no general accuracy rate is claimed.")
    print("-" * 74)

    for s in scores:
        tag = ("" if s.jurisdiction == "SG" else
               "  [MY - generalisation test]" if s.jurisdiction == "MY" else
               f"  [{s.jurisdiction} - targeted health terms]")
        print(f"\n{s.carrier}{tag}")
        print(f"   proposals {s.proposals}  bound {s.verified}  "
              f"rejected {s.rejected}")
        for h in s.hits:
            print(f"   ok      {h}")
        for m in s.missed:
            print(f"   MISSED  {m}")
        for v in s.value_mismatches:
            print(f"   VALUE   {v}")
        for f in s.false_positives:
            print(f"   FP      {f}")
        for a in s.absent_violations:
            print(f"   ABSENT-VIOLATION  {a} bound where verified absent")

    print()
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
