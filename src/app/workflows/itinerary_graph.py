from __future__ import annotations

import logging
from inspect import signature
from typing import Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from app.schemas.itinerary import ItineraryDraft, TripContext
from app.services.openai_itinerary import (
    ItineraryDraftProvider,
    ItineraryProviderConfigurationError,
    ItineraryProviderError,
    ItineraryProviderInputError,
    ItineraryProviderResponseError,
    ItineraryRevisionProvider,
    ItineraryRevisionUnsupportedError,
    RevisionIntent,
    parse_revision_intent,
)

MAX_GRAPH_ATTEMPTS = 2


class ItineraryGraphError(RuntimeError):
    """Raised when the bounded itinerary workflow cannot produce a valid draft."""


class ItineraryGraphState(TypedDict, total=False):
    operation: Literal["generate", "revise"]
    context: TripContext
    current: ItineraryDraft
    current_version: int
    instruction: str
    provider: ItineraryDraftProvider | ItineraryRevisionProvider
    allow_persisted_metadata_revisions: bool
    max_attempts: int
    attempt: int
    retryable: bool
    result: ItineraryDraft
    error: str
    error_exception: Exception
    interpreted_revision: RevisionIntent


def _prepare(state: ItineraryGraphState) -> ItineraryGraphState:
    logger.info("itinerary_graph node=prepare operation=%s", state.get("operation"))
    operation = state.get("operation")
    if operation not in {"generate", "revise"}:
        raise ItineraryGraphError("Unsupported itinerary workflow operation")
    if "context" not in state or "provider" not in state:
        raise ItineraryGraphError("Itinerary workflow is missing required context")
    if operation == "revise" and (
        "current" not in state
        or "current_version" not in state
        or not state.get("instruction", "").strip()
    ):
        raise ItineraryGraphError("Itinerary revision workflow is missing current itinerary data")
    return {
        **state,
        "attempt": 0,
        "max_attempts": max(1, state.get("max_attempts", MAX_GRAPH_ATTEMPTS)),
        "error": "",
        "error_exception": None,
        "retryable": False,
    }


def _invoke_provider(state: ItineraryGraphState) -> ItineraryGraphState:
    provider = state["provider"]
    context = state["context"]
    attempt = state.get("attempt", 0) + 1
    logger.info(
        "itinerary_graph node=invoke_provider operation=%s attempt=%s",
        state["operation"],
        attempt,
    )
    try:
        if state["operation"] == "generate":
            result = cast(ItineraryDraftProvider, provider).generate(context)
        else:
            revision_provider = cast(ItineraryRevisionProvider, provider)
            revise_kwargs: dict[str, object] = {}
            if (
                "allow_persisted_metadata_revisions"
                in signature(revision_provider.revise).parameters
            ):
                revise_kwargs["allow_persisted_metadata_revisions"] = state.get(
                    "allow_persisted_metadata_revisions", False
                )
            if "interpreted_intent" in signature(revision_provider.revise).parameters:
                revise_kwargs["interpreted_intent"] = state["interpreted_revision"]
            result = revision_provider.revise(
                context,
                state["current"],
                state["current_version"],
                state["instruction"],
                **revise_kwargs,
            )
        if not isinstance(result, ItineraryDraft) or not result.items:
            raise ItineraryProviderResponseError(
                "The itinerary provider returned an empty or invalid draft"
            )
        return {
            **state,
            "attempt": attempt,
            "result": result,
            "error": "",
            "error_exception": None,
            "retryable": False,
        }
    except (ItineraryProviderError, ValueError) as error:
        retryable = not isinstance(
            error,
            (
                ItineraryProviderConfigurationError,
                ItineraryProviderInputError,
                ItineraryRevisionUnsupportedError,
            ),
        )
        return {
            **state,
            "attempt": attempt,
            "error": str(error) or "Itinerary provider failed",
            "error_exception": error,
            "retryable": retryable,
        }


def _next_step(state: ItineraryGraphState) -> Literal["invoke_provider", "finish", "fail"]:
    if state.get("result") is not None:
        return "finish"
    if state.get("retryable") and state.get("attempt", 0) < state["max_attempts"]:
        return "invoke_provider"
    return "fail"


def _finish(state: ItineraryGraphState) -> ItineraryGraphState:
    logger.info(
        "itinerary_graph node=finish operation=%s attempt=%s",
        state.get("operation"),
        state.get("attempt"),
    )
    return state


def _interpret_revision(state: ItineraryGraphState) -> ItineraryGraphState:
    logger.info(
        "itinerary_graph node=interpret_revision target_day=%s",
        state.get("interpreted_revision").target_day if state.get("interpreted_revision") else None,
    )
    context = state["context"]
    intent = parse_revision_intent(state["instruction"], context.interests)
    return {**state, "interpreted_revision": intent}


def _after_prepare(state: ItineraryGraphState) -> Literal["interpret_revision", "invoke_provider"]:
    return "interpret_revision" if state.get("operation") == "revise" else "invoke_provider"


def _fail(state: ItineraryGraphState) -> ItineraryGraphState:
    logger.info(
        "itinerary_graph node=fail operation=%s attempt=%s",
        state.get("operation"),
        state.get("attempt"),
    )
    error = state.get("error_exception")
    if error is not None:
        raise error
    raise ItineraryGraphError(
        f"Itinerary workflow failed after {state.get('attempt', 0)} attempt(s): "
        f"{state.get('error', 'unknown provider error')}"
    )


def _build_graph():
    graph = StateGraph(ItineraryGraphState)
    graph.add_node("prepare", _prepare)
    graph.add_node("interpret_revision", _interpret_revision)
    graph.add_node("invoke_provider", _invoke_provider)
    graph.add_node("finish", _finish)
    graph.add_node("fail", _fail)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges(
        "prepare",
        _after_prepare,
        {
            "interpret_revision": "interpret_revision",
            "invoke_provider": "invoke_provider",
        },
    )
    graph.add_edge("interpret_revision", "invoke_provider")
    graph.add_conditional_edges(
        "invoke_provider",
        _next_step,
        {
            "invoke_provider": "invoke_provider",
            "finish": "finish",
            "fail": "fail",
        },
    )
    graph.add_edge("finish", END)
    graph.add_edge("fail", END)
    return graph.compile()


ITINERARY_GRAPH = _build_graph()
logger = logging.getLogger(__name__)


def run_itinerary_generation_graph(
    context: TripContext,
    provider: ItineraryDraftProvider,
    *,
    max_attempts: int = MAX_GRAPH_ATTEMPTS,
) -> ItineraryDraft:
    logger.info("itinerary_graph operation=generate node=prepare")
    result = ITINERARY_GRAPH.invoke(
        {
            "operation": "generate",
            "context": context,
            "provider": provider,
            "max_attempts": max_attempts,
        }
    )
    return cast(ItineraryDraft, result["result"])


def run_itinerary_revision_graph(
    context: TripContext,
    current: ItineraryDraft,
    current_version: int,
    instruction: str,
    provider: ItineraryRevisionProvider,
    *,
    allow_persisted_metadata_revisions: bool = False,
    max_attempts: int = MAX_GRAPH_ATTEMPTS,
) -> ItineraryDraft:
    logger.info(
        "itinerary_graph operation=revise node=prepare version=%s instruction_length=%s",
        current_version,
        len(instruction),
    )
    result = ITINERARY_GRAPH.invoke(
        {
            "operation": "revise",
            "context": context,
            "current": current,
            "current_version": current_version,
            "instruction": instruction,
            "provider": provider,
            "allow_persisted_metadata_revisions": allow_persisted_metadata_revisions,
            "max_attempts": max_attempts,
        }
    )
    return cast(ItineraryDraft, result["result"])
