"""Pure intent binding for the owner-approved synthetic chat-history profile.

The planner and reader keep their own validation and authority checks. This
binding neither resolves identities nor supplies scope, ACL or query defaults.
"""
from __future__ import annotations

from copy import deepcopy

__all__ = ("PROFILE", "BindingError", "map_task")

PROFILE = "planner_chat_history_to_reader_history_synthetic_v1"
_TASK_FIELDS = frozenset({"task_id", "domain", "query", "depends_on"})
_QUERY_FIELDS = frozenset({
    "schema_version", "request_id", "text", "intent", "scope", "actor_account_id",
    "person_id", "time_from", "time_to", "exclude_event_ids", "limit", "layer",
    "conditions", "access_scope",
})
_SCOPE_FIELDS = frozenset({
    "domain", "platform", "channel_id", "game_id", "session_id", "content_version",
})
_CONDITIONS = frozenset({
    "current_event_id", "session_ids", "question_only", "before_event_time",
})


class BindingError(ValueError):
    """A fixed diagnostic code, without copying private task data into errors."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def map_task(task: dict, *, profile: str = PROFILE) -> dict:
    """Copy an eligible task's Query and change only its existing intent value.

    The caller must bind this synthetic profile to its approved request and
    sources. The reader still validates the complete Query, ACL and any actor
    plus person relationship; this function never guesses those relationships.
    """
    if profile != PROFILE:
        raise BindingError("binding_profile_unsupported")
    if not isinstance(task, dict):
        raise BindingError("task_object_required")
    if set(task) != _TASK_FIELDS:
        raise BindingError("task_fields_invalid")
    if not isinstance(task["task_id"], str) or not task["task_id"].strip():
        raise BindingError("task_id_invalid")
    if task["domain"] != "chat_history":
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
    if query["intent"] != "chat_history":
        raise BindingError("query_intent_unsupported")
    scope = query["scope"]
    if not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS:
        raise BindingError("query_scope_invalid")
    if scope["domain"] != "chat_history":
        raise BindingError("query_domain_unsupported")

    identities = (query["actor_account_id"], query["person_id"])
    if any(value is not None and (not isinstance(value, str) or not value.strip())
           for value in identities):
        raise BindingError("query_identity_invalid")
    if all(value is None for value in identities):
        raise BindingError("query_identity_required")

    conditions = query["conditions"]
    if not isinstance(conditions, dict):
        raise BindingError("query_conditions_invalid")
    if set(conditions) - _CONDITIONS:
        raise BindingError("query_conditions_unsupported")
    if "question_only" in conditions and type(conditions["question_only"]) is not bool:
        raise BindingError("query_question_only_invalid")

    mapped = deepcopy(query)
    mapped["intent"] = "history"
    return mapped
