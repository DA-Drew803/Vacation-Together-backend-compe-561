import pytest

from app.services.openai_itinerary import RevisionIntentKind, parse_revision_intent


@pytest.mark.parametrize(
    ("instruction", "day", "expected_constraints"),
    [
        ("Add a dog cafe to Day 3.", 3, ()),
        ("Make Day 4 cheaper.", 4, ("lower-cost option",)),
        (
            "I'm tired and want something relaxing on Day 5.",
            5,
            ("low physical effort", "relaxed activity with unstructured time"),
        ),
        ("It's raining. Give us something indoors on Day 2.", 2, ("indoor or weather-protected",)),
        (
            "One of us isn't feeling well but we still want a low-effort activity on Day 8.",
            8,
            ("low physical effort", "minimal walking", "easy access"),
        ),
        (
            "My parents are exhausted from walking on Day 4.",
            4,
            ("low physical effort", "minimal walking", "seating or places to rest"),
        ),
        (
            "We only have two hours before our flight on Day 8.",
            8,
            ("short duration", "nearby with minimal transfer time"),
        ),
    ],
)
def test_situational_revision_intent_extracts_travel_constraints(
    instruction: str,
    day: int,
    expected_constraints: tuple[str, ...],
) -> None:
    intent = parse_revision_intent(instruction, [])

    assert intent.kind in {
        RevisionIntentKind.SEMANTIC,
        RevisionIntentKind.COST_ONLY,
        RevisionIntentKind.CHEAPER_DAY,
    }
    assert intent.target_day == day
    assert intent.preserve_unrelated_days is True
    for constraint in expected_constraints:
        assert constraint in intent.situational_constraints


def test_situational_interpretation_has_no_target_day_without_explicit_scope() -> None:
    intent = parse_revision_intent("I am tired but still want to see something", [])

    assert intent.target_day is None
    assert intent.preserve_unrelated_days is False
    assert "low physical effort" in intent.situational_constraints


@pytest.mark.parametrize(
    ("instruction", "kind", "target_day", "target_text"),
    [
        ("Add more seafood restaurants", RevisionIntentKind.SEMANTIC, None, "seafood"),
        ("Make the itinerary more romantic", RevisionIntentKind.SEMANTIC, None, "romantic"),
        ("Add more free time", RevisionIntentKind.SEMANTIC, None, "free time"),
        ("Make Day 2 more photography focused", RevisionIntentKind.SEMANTIC, 2, "photography"),
        ("Add more outdoor activities to Day 2", RevisionIntentKind.SEMANTIC, 2, "outdoor"),
        ("Reduce food costs", RevisionIntentKind.COST_ONLY, None, "food"),
        ("Make Day 4 cheaper", RevisionIntentKind.CHEAPER_DAY, 4, None),
        ("Keep the trip under $650 per person", RevisionIntentKind.COST_ONLY, None, None),
    ],
)
def test_broad_revision_prompts_extract_goals_without_field_locking(
    instruction: str,
    kind: RevisionIntentKind,
    target_day: int | None,
    target_text: str | None,
) -> None:
    intent = parse_revision_intent(instruction, [])

    assert intent.kind == kind
    assert intent.target_day == target_day
    assert intent.target_text == target_text


def test_explicit_preservation_and_replacement_constraints_are_recorded() -> None:
    intent = parse_revision_intent(
        "Keep Portland Head Light but replace everything else on Day 2 with outdoor activities",
        [],
    )

    assert intent.target_day == 2
    assert intent.explicit_preserve_terms == ("Portland Head Light",)
    assert intent.replacement_text is not None


def test_budget_goal_is_not_treated_as_an_activity_to_preserve() -> None:
    intent = parse_revision_intent("Keep the trip under $650 per person", [])

    assert intent.budget_target_cents == 65_000
    assert intent.explicit_preserve_terms == ()


def test_generic_trip_protections_are_not_treated_as_named_activities() -> None:
    intent = parse_revision_intent(
        "Make the itinerary less packed and add more free time. "
        "Preserve the most important activities, destination, budget, and all travel days.",
        [],
    )

    assert intent.target_text == "free time"
    assert intent.explicit_preserve_terms == ()
