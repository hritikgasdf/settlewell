"""Catalog metadata only: 30 unique ids, required keys, legacy mapping. Not an end-to-end claim."""

from backend.eval_cases import CASES, REST
from backend.incident_catalog import INCIDENTS, MONEY_OUT, PLAN_CLARITY

REQUIRED = {"number", "title", "plan_ref", "plan_trigger", "plan_action", "plan_gate", "plan_clarity", "legacy", "gaps", "variants"}
VARIANT_KEYS = {"name", "inputs", "expected_action", "expected_rule", "money_out"}


def test_there_are_thirty_unique_ids_in_order():
    assert [incident["number"] for incident in INCIDENTS] == list(range(1, 31))


def test_every_incident_has_the_required_metadata():
    for incident in INCIDENTS:
        assert REQUIRED <= incident.keys(), incident["number"]
        assert incident["plan_ref"] == f"build-plan.md §2.5 row {incident['number']}"
        assert incident["plan_clarity"] in PLAN_CLARITY
        assert incident["variants"], incident["number"]
        for variant in incident["variants"]:
            assert VARIANT_KEYS <= variant.keys(), (incident["number"], variant.get("name"))
            assert isinstance(variant["inputs"], dict)
            assert variant["money_out"] in MONEY_OUT
            assert variant["expected_rule"] is None or variant["expected_rule"].startswith("R-")


def test_incomplete_plan_rows_name_their_gaps():
    for incident in INCIDENTS:
        if incident["plan_clarity"] != "clear":
            assert incident["gaps"], incident["number"]


def test_legacy_mapping_matches_the_existing_fixtures():
    legacy = {case["number"]: case for case in CASES + REST}
    for incident in INCIDENTS:
        mapped = incident["legacy"]
        assert mapped["number"] == incident["number"]
        assert legacy[incident["number"]]["kind"] == mapped["kind"], incident["number"]
