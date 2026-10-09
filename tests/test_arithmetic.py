"""Settlement arithmetic — the demo core. Order verified from Chubb cl 3.12."""
from decimal import Decimal as D

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from proven.arithmetic import settle


def decimals(lo, hi):
    return st.decimals(min_value=D(lo), max_value=D(hi), allow_nan=False,
                       allow_infinity=False, places=2)


# Generating exact-2-decimal-place values across a 0 .. 10,000,000 range is slow enough
# that Hypothesis's `too_slow` health check fires intermittently — it passes when this file
# runs alone and trips near the end of the full suite, which is load, not logic.
#
# The health check is suppressed rather than the range narrowed. Large sums insured are
# precisely where average and deductible ordering goes wrong, so shrinking the domain to
# quieten a timing warning would trade away the coverage the property exists to give. The
# property, the example count and the assertion are all unchanged.
WIDE_MONEY = settings(suppress_health_check=[HealthCheck.too_slow])


def test_average_order_matches_clause_3_12():
    """cl 3.12: deductible applies AFTER average. Order is not a preference."""
    s = settle(loss=D("60000"), sum_insured=D("100000"),
               value_at_risk=D("150000"), threshold_pct=D("0.85"),
               sub_limit=None, deductible=D("5000"), formula_span=None)
    assert s.average_triggered
    assert s.adequacy_ratio == D("100000") / D("150000")
    assert s.illustration == D("35000")     # 60000*2/3 = 40000, less 5000
    assert s.formula_basis_class == "MARKET_CONVENTION"


def test_msig_null_threshold_triggers_on_any_shortfall():
    """MSIG GC12 states no threshold. 99% adequacy must still trigger. A default of
    0.85 here would silently under-report every MSIG insured."""
    s = settle(loss=D("100000"), sum_insured=D("99000"),
               value_at_risk=D("100000"), threshold_pct=None,
               sub_limit=None, deductible=D("0"), formula_span=None)
    assert s.average_triggered
    assert s.threshold_basis == "NO_THRESHOLD_STATED"


def test_chubb_85_threshold_does_not_trigger_at_90pct():
    s = settle(loss=D("100000"), sum_insured=D("90000"),
               value_at_risk=D("100000"), threshold_pct=D("0.85"),
               sub_limit=None, deductible=D("0"), formula_span=None)
    assert not s.average_triggered


def test_liberty_stated_formula_grades_proven():
    """Liberty p.6 / cl 5 p.46 state the arithmetic. Quoting is not inventing."""
    s = settle(loss=D("60000"), sum_insured=D("100000"),
               value_at_risk=D("150000"), threshold_pct=None,
               sub_limit=None, deductible=D("0"),
               formula_span="span-liberty-p46-cl5")
    assert s.formula_basis_class == "PROVEN"
    assert s.formula_basis == "STATED_IN_WORDING"


def test_sub_limit_applies_after_average_before_deductible():
    s = settle(loss=D("60000"), sum_insured=D("100000"),
               value_at_risk=D("150000"), threshold_pct=D("0.85"),
               sub_limit=D("30000"), deductible=D("5000"), formula_span=None)
    # 40000 reduced -> capped at 30000 -> less 5000 = 25000
    assert s.illustration == D("25000")


@WIDE_MONEY
@given(loss=decimals(0, 10**7), si=decimals(1, 10**7), var=decimals(1, 10**7))
def test_illustration_never_exceeds_loss(loss, si, var):
    s = settle(loss, si, var, D("0.85"), None, D("0"), None)
    assert s.illustration <= loss


@WIDE_MONEY
@given(loss=decimals(0, 10**7), si=decimals(1, 10**7), var=decimals(1, 10**7))
def test_null_threshold_is_never_silently_defaulted(loss, si, var):
    """Whatever the inputs, a null threshold must not behave like Chubb's 85%."""
    s = settle(loss, si, var, None, None, D("0"), None)
    assert s.threshold_basis == "NO_THRESHOLD_STATED"
    if si < var:
        assert s.average_triggered


# ── Liberty's two average provisions ────────────────────────────────────────
# Caught by external review, not by us. An earlier revision recorded Liberty as
# having no average threshold at all, reading only General Condition 4. Liberty
# also carries "Average Relief" at p.7 and p.26 — named as a relief but actually an
# average condition, triggered when the sum insured is below 85% of reinstatement
# cost. Treating Liberty as unthresholded would remove an 85% cushion the policy
# grants, over-reporting every Liberty insured on a reinstatement claim. That is the
# exact mirror of the MSIG under-reporting error this taxonomy exists to prevent.

def test_liberty_has_two_average_provisions_on_different_bases():
    from proven.taxonomy import Taxonomy
    tax = Taxonomy.load()
    bases = {i.get("settlement_basis")
             for i in tax.average_instances_for("LIBERTY")}
    assert bases == {"VALUE", "REINSTATEMENT"}, (
        "Liberty carries an average on a value basis (GC4, no threshold) and one on "
        "a reinstatement basis (Average Relief, 85%). Losing either is a wrong "
        "answer in one direction or the other.")


def test_liberty_reinstatement_basis_keeps_its_85_percent_cushion():
    from decimal import Decimal

    from proven.taxonomy import Taxonomy
    tax = Taxonomy.load()
    assert tax.average_threshold("LIBERTY", "REINSTATEMENT") == Decimal("0.85")
    # ...and the value basis genuinely has none.
    assert tax.average_threshold("LIBERTY", "VALUE") is None


def test_unknown_basis_is_ambiguous_and_never_silently_picked():
    from proven.taxonomy import Taxonomy
    tax = Taxonomy.load()
    # Basis unknown -> both provisions are candidates, thresholds disagree.
    assert tax.average_basis_ambiguous("LIBERTY", None) is True
    # Basis known -> exactly one governs, no ambiguity.
    assert tax.average_basis_ambiguous("LIBERTY", "REINSTATEMENT") is False
    assert tax.average_basis_ambiguous("LIBERTY", "VALUE") is False
    # Carriers with a single provision are never ambiguous.
    assert tax.average_basis_ambiguous("CHUBB", None) is False
    assert tax.average_basis_ambiguous("MSIG", None) is False


def test_ambiguous_basis_resolves_to_the_harsher_threshold():
    """A caller that ignores the ambiguity must under-claim, never over-claim."""
    from proven.taxonomy import Taxonomy
    tax = Taxonomy.load()
    # None means "any shortfall bites", which is harsher than 85%.
    assert tax.average_threshold("LIBERTY", None) is None


def test_chubb_and_msig_thresholds_are_unchanged_by_the_basis_work():
    from decimal import Decimal

    from proven.taxonomy import Taxonomy
    tax = Taxonomy.load()
    assert tax.average_threshold("CHUBB") == Decimal("0.85")
    assert tax.average_threshold("MSIG") is None
    assert tax.average_formula_stated("CHUBB") is False
    assert tax.average_formula_stated("MSIG") is False
    assert tax.average_formula_stated("LIBERTY") is True
