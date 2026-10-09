"""The abstention gate — four deterministic checks."""
from proven.gates import gate
from proven.models import Finding, Operand, Span


def _page_text(_doc, _page):
    return "the quick brown fox jumps over the lazy dog"


def _finding(**kw):
    base = dict(rule_id="R", rule_version=1, output_state="DOCUMENTED",
                evidence_class="PROVEN", headline="h")
    base.update(kw)
    return Finding(**base)


def test_span_fidelity_pass():
    span = Span(id="s", document_id="d", page_no=1, char_start=4, char_end=9,
                quoted_text="quick")
    f = _finding(spans=[span])
    out = gate(f, _page_text)
    assert out.output_state == "DOCUMENTED"


def test_span_fidelity_fail_drops_finding():
    span = Span(id="s", document_id="d", page_no=1, char_start=4, char_end=9,
                quoted_text="WRONG")
    f = _finding(spans=[span])
    out = gate(f, _page_text)
    assert out.output_state == "DROPPED"
    assert out.drop_reason == "SPAN_FIDELITY_FAIL"


def test_operand_completeness_yields_information_missing():
    op = Operand(name="x", value=None, unit=None, span_id=None,
                 evidence_class="UNESTABLISHED")
    f = _finding(required_operands=[op])
    out = gate(f, _page_text)
    assert out.output_state == "INFORMATION_MISSING"


def test_unresolved_precedence_escalates():
    f = _finding(precedence_unresolved=True)
    out = gate(f, _page_text)
    assert out.output_state == "REQUIRES_AUTHORISED_REVIEW"


def test_boundary_gate_escalates_out_of_provision_span():
    """Gate 2: a span whose enclosing provision is not one the finding cites is out
    of bounds and escalates rather than being asserted."""
    span = Span(id="s", document_id="d", page_no=1, char_start=4, char_end=9,
                quoted_text="quick")
    f = _finding(spans=[span])
    out = gate(f, _page_text, within_provision=lambda _f: False)
    assert out.output_state == "REQUIRES_AUTHORISED_REVIEW"


def test_boundary_gate_passes_in_provision_span():
    span = Span(id="s", document_id="d", page_no=1, char_start=4, char_end=9,
                quoted_text="quick")
    f = _finding(spans=[span])
    out = gate(f, _page_text, within_provision=lambda _f: True)
    assert out.output_state == "DOCUMENTED"
