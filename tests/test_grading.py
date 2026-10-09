"""Evidence-class grading — derived, never set by a caller."""
import inspect

from proven.grading import grade_finding, grade_operand
from proven.models import Operand


def _op(cls):
    return Operand(name="x", value="1", unit=None, span_id="s", evidence_class=cls)


def test_self_authored_stock_grades_declared():
    assert grade_operand("span-1", "SELF") == "DECLARED"


def test_third_party_and_authority_grade_proven():
    assert grade_operand("span-1", "THIRD_PARTY") == "PROVEN"
    assert grade_operand("span-1", "AUTHORITY") == "PROVEN"
    assert grade_operand("span-1", "INSURER") == "PROVEN"


def test_missing_operand_never_defaults():
    assert grade_operand(None, "AUTHORITY") == "UNESTABLISHED"


def test_evidence_class_cannot_be_set_by_caller():
    for fn in (grade_operand, grade_finding):
        assert "evidence_class" not in inspect.signature(fn).parameters


def test_finding_all_proven():
    assert grade_finding([_op("PROVEN"), _op("PROVEN")], None) == "PROVEN"


def test_finding_with_declared_operand_is_declared():
    assert grade_finding([_op("PROVEN"), _op("DECLARED")], None) == "DECLARED"


def test_unestablished_outranks_everything():
    assert grade_finding([_op("DECLARED"), _op("UNESTABLISHED")], None) \
        == "UNESTABLISHED"


def test_market_convention_only_for_computational_findings():
    """A non-computational finding with a null computation span must NOT be demoted
    to MARKET_CONVENTION — that is only for findings that actually compute."""
    assert grade_finding([_op("PROVEN")], None, has_computation=False) == "PROVEN"
    assert grade_finding([_op("PROVEN")], None, has_computation=True) \
        == "MARKET_CONVENTION"


def test_market_convention_outranks_declared():
    assert grade_finding([_op("DECLARED")], None, has_computation=True) \
        == "MARKET_CONVENTION"
