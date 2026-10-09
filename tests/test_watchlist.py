"""Change detection: noticing that something is different, without crying wolf.

Every test here is a way this went wrong in practice or would have. The counts these
functions produce are read out in an email, so an off-by-one is not a cosmetic defect — it
is a message telling a business owner they opened a branch they did not open.
"""
from proven import watchlist as w

BID = "T09HL1234A"


def fact(fact_type, value, *, src="disc-web", author="SELF", severity="MODERATE",
         policy=None, idx=1, name="Company's own website"):
    """A discovered fact in the shape `discovery.DiscoveredFact.as_dict()` produces."""
    return {
        "id": f"DISC-WEB-{fact_type}-{idx}",
        "fact_type": fact_type,
        "label": f"{fact_type.title()} on the website",
        "value": value,
        "previous_value": policy,
        "claim_scope": "DISCOVERY_HINT",
        "severity": severity,
        "extraction": "PATTERN",
        "source": {"id": src, "name": name, "author_class": author},
    }


def baseline_facts():
    return [
        fact("LOCATION", "#03-11, 21 Tai Seng Avenue", idx=1,
             policy="Unit 03-11, 21 Tai Seng Avenue"),
        fact("LOCATION", "#03-12, 21 Tai Seng Avenue", idx=2,
             policy="Unit 03-11, 21 Tai Seng Avenue"),
        fact("HEADCOUNT", "14", idx=3, policy="11"),
        fact("ACTIVITY", "Manufacture of prepared meals and dishes", idx=4),
    ]


# ── the first look is a baseline, never an alert ─────────────────────────────

def test_first_observation_raises_nothing():
    """On a cold start every fact is "new" because there is nothing to compare against.
    Reporting them would mean the first check after every restart announces branches that
    have been there for years."""
    out = w.Watchlist().observe(BID, baseline_facts())

    assert out["first_observation"] is True
    assert out["changes"] == []
    assert out["summary"] == []
    assert out["facts_tracked"] == 4
    assert out["previous_observed_at"] is None


def test_baseline_is_still_recorded_so_the_next_look_can_compare():
    wl = w.Watchlist()
    wl.observe(BID, baseline_facts())
    assert wl.tracked_count(BID) == 4
    assert wl.last_seen(BID)


def test_nothing_changed_says_nothing():
    wl = w.Watchlist()
    wl.observe(BID, baseline_facts())
    out = wl.observe(BID, baseline_facts())

    assert out["first_observation"] is False
    assert out["changes"] == []


# ── identity is not the fact id ──────────────────────────────────────────────

def test_renumbered_and_reordered_ids_are_not_changes():
    """DiscoveredFact.id is `DISC-WEB-LOCATION-{len(facts)+1}` — an index. Insert one
    unrelated fact and every later id shifts. Diffing on ids would report the whole page as
    new on every run."""
    wl = w.Watchlist()
    base = baseline_facts()
    wl.observe(BID, base)

    churned = [dict(base[3], id="DISC-WEB-X-1"), dict(base[0], id="DISC-WEB-X-9"),
               dict(base[2], id="DISC-WEB-X-4"), dict(base[1], id="DISC-WEB-X-7")]
    out = wl.observe(BID, churned)

    assert out["changes"] == []


def test_the_same_fact_twice_in_one_observation_counts_once():
    """Defence in depth for a real upstream bug: a page mentioned one address in both the
    locations list and a news post, `re.findall` returned it twice, and one new branch became
    "2 new branches"."""
    wl = w.Watchlist()
    wl.observe(BID, baseline_facts())

    dupe = "#05-01, 9 Kaki Bukit Road 1"
    out = wl.observe(BID, baseline_facts() + [
        fact("LOCATION", dupe, idx=5), fact("LOCATION", dupe, idx=6)])

    news = [s for s in out["summary"] if s["code"] == "NEW_LOCATION"]
    assert len(news) == 1
    assert news[0]["count"] == 1


# ── new premises: the change this whole feature exists for ───────────────────

def test_two_new_branches_are_one_piece_of_news_counted_correctly():
    wl = w.Watchlist()
    wl.observe(BID, baseline_facts())

    out = wl.observe(BID, baseline_facts() + [
        fact("LOCATION", "#05-01, 9 Kaki Bukit Road 1, Singapore 417818", idx=5,
             severity="HIGH", policy="Unit 03-11, 21 Tai Seng Avenue"),
        fact("LOCATION", "#02-14, 15 Jurong Port Road, Singapore 619092", idx=6,
             severity="HIGH", policy="Unit 03-11, 21 Tai Seng Avenue"),
    ])

    assert len(out["alertable_changes"]) == 2
    assert len(out["summary"]) == 1
    news = out["summary"][0]
    assert news["code"] == "NEW_LOCATION"
    assert news["count"] == 2
    assert news["severity"] == "HIGH"
    assert news["policy_says"] == "Unit 03-11, 21 Tai Seng Avenue"


def test_a_change_already_reported_is_not_reported_again():
    """The scheduler re-checks every few minutes and a new branch stays on the website until
    the policy is updated. Without this the same news is emailed twelve times an hour."""
    wl = w.Watchlist()
    wl.observe(BID, baseline_facts())
    grown = baseline_facts() + [fact("LOCATION", "#05-01, 9 Kaki Bukit Road 1", idx=5)]

    assert len(wl.observe(BID, grown)["alertable_changes"]) == 1
    assert wl.observe(BID, grown)["alertable_changes"] == []
    assert wl.observe(BID, grown)["alertable_changes"] == []


# ── singleton values change rather than accumulate ───────────────────────────

def test_headcount_going_up_is_a_change_not_an_addition_and_a_removal():
    wl = w.Watchlist()
    wl.observe(BID, baseline_facts())

    grown = [f for f in baseline_facts() if f["fact_type"] != "HEADCOUNT"]
    grown.append(fact("HEADCOUNT", "17", idx=3, policy="11"))
    out = wl.observe(BID, grown)

    assert len(out["summary"]) == 1
    entry = out["summary"][0]
    assert entry["code"] == "CHANGED_HEADCOUNT"
    assert entry["previous_value"] == "14"
    assert entry["new_value"] == "17"
    assert not any(c["kind"] == "GONE" for c in out["changes"])


def test_two_sources_reporting_the_same_type_do_not_overwrite_each_other():
    """The website says "a team of 14" (everybody); MOM says 4 (foreign work permits). Those
    are different measurements, not competing values, so singleton-ness is per source."""
    wl = w.Watchlist()
    both = [fact("HEADCOUNT", "14", idx=1),
            fact("HEADCOUNT", "4", src="disc-mom", author="AUTHORITY", idx=2,
                 name="MOM Work Pass Online")]
    wl.observe(BID, both)
    out = wl.observe(BID, both)

    assert wl.tracked_count(BID) == 2
    assert out["changes"] == []


def test_headcount_wrapped_in_prose_compares_as_a_number():
    wl = w.Watchlist()
    wl.observe(BID, [fact("HEADCOUNT", "4", src="disc-mom", idx=1)])
    out = wl.observe(BID, [fact("HEADCOUNT", "Total Foreign Workers: 4",
                                src="disc-mom", idx=1)])
    assert out["changes"] == []


# ── a model in the loop means rewording is not a change ──────────────────────

def test_a_reworded_description_does_not_raise_an_alert():
    """The extractor includes a language model, so prose comes back phrased differently
    between runs. Alerting on that would teach the owner to ignore us."""
    wl = w.Watchlist()
    wl.observe(BID, baseline_facts())

    reworded = [f for f in baseline_facts() if f["fact_type"] != "ACTIVITY"]
    reworded.append(fact("ACTIVITY", "prepared meals and dishes manufacturing", idx=4))
    out = wl.observe(BID, reworded)

    assert out["alertable_changes"] == []
    assert out["summary"] == []
    assert out["changes"], "the difference should still be recorded"
    assert all(c["not_alertable_because"] for c in out["changes"])


def test_the_same_address_captured_more_fully_is_not_a_new_address():
    """A regex stops at a comma, a model runs to the postcode. Same premises. If the regex
    stops matching after a redesign, the model's fuller version must not read as a new
    branch."""
    wl = w.Watchlist()
    wl.observe(BID, [fact("LOCATION", "#03-11, 21 Tai Seng Avenue", idx=1)])
    out = wl.observe(BID, [
        fact("LOCATION", "#03-11, 21 Tai Seng Avenue, Singapore 534054", idx=1)])

    assert out["alertable_changes"] == []


def test_a_genuinely_different_unit_is_still_new():
    """The containment rule must not swallow real changes."""
    wl = w.Watchlist()
    wl.observe(BID, [fact("LOCATION", "#03-11, 21 Tai Seng Avenue", idx=1)])
    out = wl.observe(BID, [
        fact("LOCATION", "#03-11, 21 Tai Seng Avenue", idx=1),
        fact("LOCATION", "#03-12, 21 Tai Seng Avenue", idx=2)])

    assert len(out["alertable_changes"]) == 1


# ── absence is weak evidence ─────────────────────────────────────────────────

def test_a_disappeared_fact_is_recorded_but_never_alerted():
    """A fact vanishes when a branch closes, and equally when a redesign defeats a regex or
    the model does not mention it this time. Presence is grounded; absence is not."""
    wl = w.Watchlist()
    wl.observe(BID, baseline_facts())

    fewer = [f for f in baseline_facts()
             if f["value"] != "#03-12, 21 Tai Seng Avenue"]
    out = wl.observe(BID, fewer)

    gone = [c for c in out["changes"] if c["kind"] == "GONE"]
    assert len(gone) == 1
    assert gone[0]["alertable"] is False
    assert gone[0]["not_alertable_because"]
    assert out["summary"] == []


def test_a_failed_fetch_does_not_report_every_fact_as_gone():
    """When a whole source returns nothing it is an unreachable page, not a closed business.
    Reporting each fact as gone would be pure noise."""
    wl = w.Watchlist()
    wl.observe(BID, baseline_facts())
    out = wl.observe(BID, [])

    assert out["changes"] == []
    assert out["summary"] == []


# ── the dedupe fingerprint ───────────────────────────────────────────────────

def test_dedupe_key_ignores_the_order_facts_arrived_in():
    news = [fact("LOCATION", "#05-01, 9 Kaki Bukit Road 1", idx=5),
            fact("LOCATION", "#02-14, 15 Jurong Port Road", idx=6)]

    a = w.Watchlist()
    a.observe(BID, baseline_facts())
    key_a = a.observe(BID, baseline_facts() + news)["summary"][0]["dedupe_key"]

    b = w.Watchlist()
    b.observe(BID, baseline_facts())
    key_b = b.observe(BID, baseline_facts() + list(reversed(news)))[
        "summary"][0]["dedupe_key"]

    assert key_a == key_b


def test_different_news_gets_a_different_key():
    a = w.Watchlist()
    a.observe(BID, baseline_facts())
    key_a = a.observe(BID, baseline_facts() + [
        fact("LOCATION", "#05-01, 9 Kaki Bukit Road 1", idx=5)])["summary"][0][
            "dedupe_key"]

    b = w.Watchlist()
    b.observe(BID, baseline_facts())
    key_b = b.observe(BID, baseline_facts() + [
        fact("LOCATION", "#02-14, 15 Jurong Port Road", idx=5)])["summary"][0][
            "dedupe_key"]

    assert key_a != key_b


# ── normalisation ────────────────────────────────────────────────────────────

def test_location_normalisation_ignores_punctuation_differences():
    assert (w.normalise_value("LOCATION", "#03-11, 21 Tai Seng Avenue")
            == w.normalise_value("LOCATION", "03-11 21 TAI SENG AVENUE"))


def test_headcount_normalisation_keeps_only_the_number():
    assert w.normalise_value("HEADCOUNT", "Total Foreign Workers: 4") == "4"
    assert w.normalise_value("HEADCOUNT", "1,200 staff") == "1200"


def test_normalisation_is_safe_on_missing_values():
    assert w.normalise_value("LOCATION", None) == ""
    assert w.normalise_value("HEADCOUNT", "") == ""


# ── housekeeping ─────────────────────────────────────────────────────────────

def test_forget_makes_the_next_look_a_baseline_again():
    wl = w.Watchlist()
    wl.observe(BID, baseline_facts())
    wl.forget(BID)
    assert wl.observe(BID, baseline_facts())["first_observation"] is True


def test_the_store_is_bounded():
    """An in-process cache keyed on a business identifier must not grow without limit."""
    wl = w.Watchlist(max_businesses=3)
    for i in range(10):
        wl.observe(f"BIZ-{i:02d}", baseline_facts())
    assert wl.stats()["businesses_watched"] <= 3


def test_stats_says_the_memory_is_process_local():
    wl = w.Watchlist()
    wl.observe(BID, baseline_facts())
    stats = wl.stats()
    assert stats["businesses_watched"] == 1
    assert "LOCATION" in stats["alertable_types"]
    assert "restart" in stats["notice"].lower()
