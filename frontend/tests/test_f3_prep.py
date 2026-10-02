"""F3 mappings: cleaning ticks that cannot move, and the contract form → API names."""

import datetime as dt

from frontend import prep

PLAN = [
    {"action_id": "C001", "kind": "DROP_DUPLICATE_ROWS", "column": None},
    {"action_id": "C002", "kind": "TRIM_WHITESPACE", "column": "utm_source"},
    {"action_id": "C003", "kind": "NORMALISE_CASE", "column": "utm_source"},
]
# Measured on the real backend: after applying C001+C002, NORMALISE_CASE came back as C001.
RENUMBERED = [{"action_id": "C001", "kind": "NORMALISE_CASE", "column": "utm_source"}]


def test_ticks_are_sent_in_the_plans_order():
    ticked = [prep.fingerprint(PLAN[2]), prep.fingerprint(PLAN[0])]
    assert prep.to_apply(PLAN, ticked) == (["C001", "C003"], [])


def test_a_tick_never_moves_to_a_renumbered_action():
    ticked = [prep.fingerprint(PLAN[0])]                  # "drop duplicates", id C001
    send, stale = prep.to_apply(RENUMBERED, ticked)        # C001 is now "normalise case"
    assert send == []
    assert stale == ["C001|DROP_DUPLICATE_ROWS|"]


def test_default_roles_follow_the_proposal_and_ignore_the_rest():
    proposal = {"key": ["date", "campaign"], "date": "date", "dimensions": ["channel", "cost"],
                "measures": [{"column": "ctr"}]}
    roles = prep.default_roles(proposal, ["date", "campaign", "channel", "cost", "ctr", "notes"])
    assert roles == {"date": "date", "campaign": "ignore", "channel": "dimension",
                     "cost": "dimension", "ctr": "measure", "notes": "ignore"}
    # The key is its own list, not a role: it includes the date (F3 browser finding).
    assert "key" not in roles.values()


def test_only_agreed_aggregations_are_defaults_never_suggestions():
    proposal = {"measures": [{"column": "ctr", "agg": "", "suggested_agg": "none"},
                             {"column": "cost", "agg": "sum", "suggested_agg": "sum"}]}
    assert prep.proposal_aggs(proposal) == {"cost": "sum"}
    assert set(prep.suggestions(proposal)) == {"ctr", "cost"}


def test_the_form_maps_to_the_apis_names_and_blanks_are_left_out():
    form = {
        "grain": "  one row per campaign per day ",
        "key": ["date", "campaign"],
        "roles": {"date": "date", "campaign": "dimension", "channel": "dimension",
                  "cost": "measure", "ctr": "measure", "notes": "ignore"},
        "aggregations": {"cost": "sum", "ctr": None},
        "definitions": {"cost": " spend, before GST ", "ctr": "   "},
        "per": {"cost": [], "ctr": ["campaign"]},
        "window_start": "2026-08-01", "window_end": None,
        "caveats": "Brand campaigns paused 3-5 Sep\n\n  ",
        "forks": {"tax_basis": "net_excl_gst", "timezone": None},
    }
    contract, forks = prep.contract_body(form)
    assert contract == {
        "grain": "one row per campaign per day",
        "primary_key": ["date", "campaign"], "date_column": "date",
        "measures": ["cost", "ctr"], "dimensions": ["campaign", "channel"],
        "aggregations": {"cost": "sum"},
        "measure_definitions": {"cost": "spend, before GST"},
        "measure_per": {"ctr": ["campaign"]},
        "analysis_window_start": "2026-08-01",
        "caveats": ["Brand campaigns paused 3-5 Sep"],
    }
    assert forks == {"tax_basis": "net_excl_gst"}


def test_an_empty_form_sends_no_grain_or_date_and_states_no_caveats():
    contract, forks = prep.contract_body({})
    assert contract == {"primary_key": [], "measures": [], "dimensions": [], "caveats": []}
    assert forks == {}


def test_dates_round_trip_and_two_date_columns_are_found():
    assert prep.date_text(dt.date(2026, 8, 1)) == "2026-08-01"
    assert prep.text_date("2026-08-01") == dt.date(2026, 8, 1)
    assert prep.text_date("bad") is None and prep.text_date(None) is None
    assert prep.date_columns({"a": "date", "b": "measure", "c": "date"}) == ["a", "c"]
