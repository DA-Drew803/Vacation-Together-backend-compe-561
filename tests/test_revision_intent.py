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
