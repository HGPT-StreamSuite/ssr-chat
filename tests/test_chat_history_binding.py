"""The synthetic binding changes intent only and never broadens a query."""
from copy import deepcopy
import json
import unittest

from ssr_chat.chat_history_binding import PROFILE, BindingError, map_task


class ChatHistoryBindingTests(unittest.TestCase):
    def setUp(self):
        scope = {
            "domain": "chat_history", "platform": "synthetic-chat", "channel_id": "channel-a",
            "game_id": "synthetic-game", "session_id": "session001", "content_version": "v1",
        }
        selector = {key: {"mode": "exact", "value": value} for key, value in scope.items() if key != "domain"}
        selector = {"domain": "chat_history", **selector}
        # Deliberately noncanonical order proves the mapper does not rebuild a Query.
        self.query = {
            "request_id": "synthetic-request", "conditions": {}, "schema_version": "1.0.1",
            "text": "  Synthetic history question about LT.\r\n", "scope": scope,
            "intent": "chat_history", "person_id": None, "actor_account_id": "synthetic-actor",
            "time_to": "2026-11-06T12:00:00+00:00", "time_from": "2026-11-06T10:00:00Z",
            "exclude_event_ids": ["current-event", "other-event"], "limit": 7, "layer": "raw",
            "access_scope": {"write_scopes": [], "read_scopes": [selector]},
        }
        self.task = {"query": self.query, "depends_on": [], "domain": "chat_history", "task_id": "task-a"}

    def assert_error(self, code, task=None, **kwargs):
        with self.assertRaises(BindingError) as caught:
            map_task(self.task if task is None else task, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_only_intent_changes_and_every_key_order_is_retained(self):
        before = deepcopy(self.task)
        expected = deepcopy(self.query)
        expected["intent"] = "history"
        mapped = map_task(self.task)
        self.assertEqual(mapped, expected)
        self.assertEqual(json.dumps(mapped, ensure_ascii=False), json.dumps(expected, ensure_ascii=False))
        self.assertEqual(list(mapped), list(self.query))
        self.assertEqual(list(mapped["scope"]), list(self.query["scope"]))
        self.assertEqual(list(mapped["access_scope"]), list(self.query["access_scope"]))
        self.assertEqual(self.task, before)

    def test_question_only_absent_false_true_remain_distinct(self):
        for present, value in ((False, None), (True, False), (True, True)):
            with self.subTest(present=present, value=value):
                task = deepcopy(self.task)
                conditions = task["query"]["conditions"]
                conditions.update(current_event_id="current-event", session_ids=["session001"],
                                  before_event_time="2026-11-06T11:00:00Z")
                if present:
                    conditions["question_only"] = value
                mapped = map_task(task)
                self.assertEqual(mapped["conditions"], conditions)
                self.assertEqual(list(mapped["conditions"]), list(conditions))
                self.assertEqual("question_only" in mapped["conditions"], present)
                if present:
                    self.assertIs(mapped["conditions"]["question_only"], value)

    def test_mutating_result_cannot_mutate_input_and_input_cannot_mutate_result(self):
        self.query["conditions"]["session_ids"] = ["session001"]
        mapped = map_task(self.task)
        mapped["scope"]["channel_id"] = "different-channel"
        mapped["exclude_event_ids"].append("new-event")
        mapped["access_scope"]["read_scopes"][0]["platform"]["value"] = "different-platform"
        mapped["conditions"]["session_ids"].append("other-session")
        self.assertEqual(self.query["scope"]["channel_id"], "channel-a")
        self.assertEqual(self.query["exclude_event_ids"], ["current-event", "other-event"])
        self.assertEqual(self.query["access_scope"]["read_scopes"][0]["platform"]["value"], "synthetic-chat")
        self.assertEqual(self.query["conditions"]["session_ids"], ["session001"])
        self.query["access_scope"]["write_scopes"].append({"new": "input-only"})
        self.assertEqual(mapped["access_scope"]["write_scopes"], [])

    def test_actor_person_or_both_are_preserved_without_resolving_links(self):
        for actor, person in (("synthetic-actor", None), (None, "synthetic-person"),
                              ("unresolved-actor", "unresolved-person")):
            with self.subTest(actor=actor, person=person):
                task = deepcopy(self.task)
                task["query"].update(actor_account_id=actor, person_id=person)
                mapped = map_task(task)
                self.assertEqual(mapped["actor_account_id"], actor)
                self.assertEqual(mapped["person_id"], person)
                self.assertEqual(set(mapped), set(self.query))

    def test_identity_must_be_present_and_each_nonnull_value_a_nonempty_string(self):
        task = deepcopy(self.task)
        task["query"].update(actor_account_id=None, person_id=None)
        self.assert_error("query_identity_required", task)
        for field in ("actor_account_id", "person_id"):
            for bad in ("", " \t", 0, False, [], {}):
                with self.subTest(field=field, value=bad):
                    task = deepcopy(self.task)
                    task["query"][field] = bad
                    self.assert_error("query_identity_invalid", task)

    def test_only_the_explicit_profile_is_supported(self):
        self.assertEqual(map_task(self.task, profile=PROFILE)["intent"], "history")
        for profile in (None, "", "default", "planner_chat_history_to_reader_history_v1", "LT"):
            with self.subTest(profile=profile):
                self.assert_error("binding_profile_unsupported", profile=profile)

    def test_task_requires_exact_fields_and_nonempty_identity(self):
        for field in self.task:
            task = deepcopy(self.task)
            del task[field]
            self.assert_error("task_fields_invalid", task)
        task = deepcopy(self.task)
        task["source_refs"] = []
        self.assert_error("task_fields_invalid", task)
        for bad in (None, "", " ", 1, False, []):
            task = deepcopy(self.task)
            task["task_id"] = bad
            self.assert_error("task_id_invalid", task)

    def test_unresolved_or_nonlist_dependencies_are_rejected(self):
        for dependencies, code in ((["prior-task"], "task_dependencies_unresolved"),
                                    ([None], "task_dependencies_unresolved"),
                                    (None, "task_dependencies_invalid"),
                                    ((), "task_dependencies_invalid"),
                                    ("", "task_dependencies_invalid")):
            with self.subTest(dependencies=dependencies):
                task = deepcopy(self.task)
                task["depends_on"] = dependencies
                self.assert_error(code, task)

    def test_query_requires_all_fourteen_fields_without_additions(self):
        self.assertEqual(len(self.query), 14)
        for field in self.query:
            task = deepcopy(self.task)
            del task["query"][field]
            self.assert_error("query_fields_invalid", task)
        for extra in ("source_refs", "trusted_context", "profile", "session_ids"):
            task = deepcopy(self.task)
            task["query"][extra] = []
            self.assert_error("query_fields_invalid", task)

    def test_version_and_domains_are_exact(self):
        for bad in (None, "1.0", "1.0.2", 1):
            task = deepcopy(self.task)
            task["query"]["schema_version"] = bad
            self.assert_error("query_version_unsupported", task)
        task = deepcopy(self.task)
        task["domain"] = "channel_knowledge"
        self.assert_error("task_domain_unsupported", task)
        task = deepcopy(self.task)
        task["query"]["scope"]["domain"] = "channel_knowledge"
        self.assert_error("query_domain_unsupported", task)

    def test_no_recent_history_alias_or_unknown_intent_falls_back(self):
        for intent in ("recent_utterance", "last_said", "recent", "question_history", "history", "LT", "unknown", None):
            with self.subTest(intent=intent):
                task = deepcopy(self.task)
                task["query"]["intent"] = intent
                self.assert_error("query_intent_unsupported", task)

    def test_conditions_are_allowlisted_and_question_only_is_strict_bool(self):
        for key in ("LT", "revision_view", "source_refs", "unknown"):
            task = deepcopy(self.task)
            task["query"]["conditions"][key] = "untrusted"
            self.assert_error("query_conditions_unsupported", task)
        for bad in (None, 0, 1, "false", [], {}):
            task = deepcopy(self.task)
            task["query"]["conditions"]["question_only"] = bad
            self.assert_error("query_question_only_invalid", task)

    def test_invalid_container_shapes_are_rejected_with_fixed_codes(self):
        for bad in (None, [], "private-task-text"):
            with self.subTest(task=bad):
                with self.assertRaises(BindingError) as caught:
                    map_task(bad)
                self.assertEqual(caught.exception.code, "task_object_required")
        for field, bad, code in (("query", [], "query_object_required"),
                                  ("scope", [], "query_scope_invalid"),
                                  ("conditions", [], "query_conditions_invalid")):
            task = deepcopy(self.task)
            if field == "query":
                task[field] = bad
            else:
                task["query"][field] = bad
            self.assert_error(code, task)
        for change in ("missing", "extra"):
            task = deepcopy(self.task)
            if change == "missing":
                del task["query"]["scope"]["session_id"]
            else:
                task["query"]["scope"]["unknown"] = "private-value"
            self.assert_error("query_scope_invalid", task)

    def test_rejections_do_not_mutate_or_expose_private_values(self):
        self.task["query"]["conditions"]["private-condition"] = "private-source-content"
        before = deepcopy(self.task)
        with self.assertRaises(BindingError) as caught:
            map_task(self.task)
        self.assertEqual(self.task, before)
        self.assertEqual(str(caught.exception), "query_conditions_unsupported")
        self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
