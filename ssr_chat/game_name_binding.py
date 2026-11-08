"""Pure binding for the owner-approved synthetic game name retrieval profile.

This profile preserves game_lookup and allows only the native before_event_time
condition. The caller and reader retain full Query, source and authority checks.
"""
from __future__ import annotations

from copy import deepcopy
import re

__all__ = ("PROFILE", "BindingError", "map_task", "reject_numeric_text")

PROFILE = "planner_game_name_retrieval_synthetic_v1"
_TASK_FIELDS = frozenset({"task_id", "domain", "query", "depends_on"})
_QUERY_FIELDS = frozenset({
    "schema_version", "request_id", "text", "intent", "scope", "actor_account_id",
    "person_id", "time_from", "time_to", "exclude_event_ids", "limit", "layer",
    "conditions", "access_scope",
})
_SCOPE_FIELDS = frozenset({
    "domain", "platform", "channel_id", "game_id", "session_id", "content_version",
})
_NUMERIC_SYNTAX = re.compile(
    r"[<>=≤≥]"
    r"|(?<![A-Za-z0-9_])(?:power|cost|released_on)\s*(?:[:：]|[+-]?\d)"
    r"|(?:大於|小於|至少|至多|不超過)\s*[+-]?\d",
    re.IGNORECASE,
)


class BindingError(ValueError):
    """A fixed diagnostic code without copying private Query values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _nonempty_string(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def reject_numeric_text(text: str) -> None:
    """Reject explicit unsupported numeric syntax without deriving a filter.

    This is a limited lexical guard, not an arbitrary natural-language parser
    or a name resolver. A digit in an ordinary name does not itself reject it.
    """
    if not _nonempty_string(text):
        raise BindingError("query_text_invalid")
    if _NUMERIC_SYNTAX.search(text):
        raise BindingError("numeric_text_unsupported")


def map_task(task: dict, *, profile: str = PROFILE) -> dict:
    """Return an independent, unchanged Query for this exact retrieval profile.

    A profile name does not prove a source is synthetic or authorized. Source
    policy, ACL and remaining native Query semantics belong to caller/reader.
    No version, identity, scope or permission is inferred here.
    """
    if profile != PROFILE:
        raise BindingError("binding_profile_unsupported")
    if not isinstance(task, dict):
        raise BindingError("task_object_required")
    if set(task) != _TASK_FIELDS:
        raise BindingError("task_fields_invalid")
    if not _nonempty_string(task["task_id"]):
        raise BindingError("task_id_invalid")
    if task["domain"] != "game_knowledge":
        raise BindingError("task_domain_unsupported")
    if not isinstance(task["depends_on"], list):
        raise BindingError("task_dependencies_invalid")
    if task["depends_on"]:
        raise BindingError("task_dependencies_unresolved")

    query = task["query"]
    if not isinstance(query, dict):
        raise BindingError("query_object_required")
    if set(query) != _QUERY_FIELDS:
        raise BindingError("query_fields_invalid")
    if query["schema_version"] != "1.0.1":
        raise BindingError("query_version_unsupported")
    if not _nonempty_string(query["request_id"]):
        raise BindingError("query_request_id_invalid")
    reject_numeric_text(query["text"])
    if query["intent"] != "game_lookup":
        raise BindingError("query_intent_unsupported")
    if query["layer"] != "raw":
        raise BindingError("query_layer_unsupported")

    scope = query["scope"]
    if not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS:
        raise BindingError("query_scope_invalid")
    if scope["domain"] != "game_knowledge":
        raise BindingError("query_domain_unsupported")
    if not _nonempty_string(scope["game_id"]):
        raise BindingError("query_game_id_required")
    if not _nonempty_string(scope["content_version"]):
        raise BindingError("query_content_version_required")
    if scope["platform"] is not None and not isinstance(scope["platform"], str):
        raise BindingError("query_platform_invalid")

    for field in ("actor_account_id", "person_id"):
        if query[field] is not None and not _nonempty_string(query[field]):
            raise BindingError("query_identity_invalid")
    if not isinstance(query["conditions"], dict):
        raise BindingError("query_conditions_invalid")
    if set(query["conditions"]) - {"before_event_time"}:
        raise BindingError("query_conditions_unsupported")
    if ("before_event_time" in query["conditions"]
            and not _nonempty_string(query["conditions"]["before_event_time"])):
        raise BindingError("query_before_event_time_invalid")

    return deepcopy(query)
