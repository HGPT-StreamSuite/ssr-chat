"""A game name binding preserves the Query and rejects broader profiles."""
from copy import deepcopy
import json
import unittest

from ssr_chat.game_name_binding import PROFILE, BindingError, map_task, reject_numeric_text


class GameNameBindingTests(unittest.TestCase):
    def setUp(self):
        scope = {
            "domain": "game_knowledge", "platform": "synthetic-chat", "channel_id": "channel-a",
            "game_id": "synthetic-game", "session_id": "session001", "content_version": "v1",
        }
        selector = {key: {"mode": "exact", "value": value} for key, value in scope.items() if key != "domain"}
        selector = {"domain": "game_knowledge", **selector}
        # Noncanonical insertion order ensures no reconstruction or normalization.
        self.query = {
            "text": "  Synthetic Blade\r\n", "scope": scope, "intent": "game_lookup",
            "request_id": " synthetic-game-request ", "schema_version": "1.0.1",
            "conditions": {}, "actor_account_id": None, "person_id": None,
            "time_to": None, "time_from": None, "exclude_event_ids": ["excluded-b", "excluded-a"],
            "limit": 3, "access_scope": {"write_scopes": [], "read_scopes": [selector]}, "layer": "raw",
        }
        self.task = {"depends_on": [], "query": self.query, "task_id": "game-task", "domain": "game_knowledge"}

    def assert_error(self, code, task=None, **kwargs):
        with self.assertRaises(BindingError) as caught:
            map_task(self.task if task is None else task, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_query_intent_values_presence_and_nested_order_are_unchanged(self):
        before = deepcopy(self.task)
        mapped = map_task(self.task)
        self.assertEqual(mapped, self.query)
        self.assertEqual(mapped["intent"], "game_lookup")
        self.assertEqual(json.dumps(mapped, ensure_ascii=False), json.dumps(self.query, ensure_ascii=False))
        self.assertEqual(list(mapped), list(self.query))
        self.assertEqual(list(mapped["scope"]), list(self.query["scope"]))
        self.assertEqual(list(mapped["access_scope"]), ["write_scopes", "read_scopes"])
        self.assertEqual(mapped["exclude_event_ids"], ["excluded-b", "excluded-a"])
        self.assertEqual(self.task, before)

    def test_returned_nested_objects_are_independent_of_input(self):
        mapped = map_task(self.task)
        self.assertIsNot(mapped, self.query)
        self.assertIsNot(mapped["conditions"], self.query["conditions"])
        mapped["scope"]["game_id"] = "different-game"
        mapped["exclude_event_ids"].append("new-event")
        mapped["conditions"]["unsupported"] = True
        mapped["access_scope"]["read_scopes"][0]["content_version"]["value"] = "different-version"
        self.assertEqual(self.query["scope"]["game_id"], "synthetic-game")
        self.assertEqual(self.query["exclude_event_ids"], ["excluded-b", "excluded-a"])
        self.assertEqual(self.query["conditions"], {})
        self.assertEqual(self.query["access_scope"]["read_scopes"][0]["content_version"]["value"], "v1")
        self.query["access_scope"]["write_scopes"].append({"input": "only"})
        self.assertEqual(mapped["access_scope"]["write_scopes"], [])

    def test_nullable_identities_are_not_inferred_or_resolved(self):
        for actor, person in ((None, None), ("synthetic-actor", None), (None, "synthetic-person"),
                              ("unresolved-actor", "unresolved-person")):
            with self.subTest(actor=actor, person=person):
                task = deepcopy(self.task)
                task["query"].update(actor_account_id=actor, person_id=person)
                self.assertEqual(map_task(task), task["query"])

    def test_each_nonnull_identity_must_be_a_nonempty_string(self):
        for field in ("actor_account_id", "person_id"):
            for bad in ("", " \t", False, 0, [], {}):
                with self.subTest(field=field, value=bad):
                    task = deepcopy(self.task)
                    task["query"][field] = bad
                    self.assert_error("query_identity_invalid", task)

    def test_exact_profile_required(self):
        self.assertEqual(map_task(self.task, profile=PROFILE), self.query)
        for bad in (None, "", "default", "planner_game_name_retrieval_v1", PROFILE.upper()):
            with self.subTest(profile=bad):
                self.assert_error("binding_profile_unsupported", profile=bad)

    def test_task_shape_and_identity_are_closed(self):
        for field in self.task:
            task = deepcopy(self.task)
            del task[field]
            self.assert_error("task_fields_invalid", task)
        task = deepcopy(self.task)
        task["source_refs"] = []
        self.assert_error("task_fields_invalid", task)
        for bad in (None, "", " ", 0, False, []):
            task = deepcopy(self.task)
            task["task_id"] = bad
            self.assert_error("task_id_invalid", task)

    def test_dependencies_must_be_an_empty_list(self):
        for bad, code in ((["earlier-task"], "task_dependencies_unresolved"),
                          ([None], "task_dependencies_unresolved"),
                          (None, "task_dependencies_invalid"), ((), "task_dependencies_invalid"),
                          ("", "task_dependencies_invalid")):
            with self.subTest(dependencies=bad):
                task = deepcopy(self.task)
                task["depends_on"] = bad
                self.assert_error(code, task)

    def test_query_has_exactly_fourteen_fields(self):
        self.assertEqual(len(self.query), 14)
        for field in self.query:
            task = deepcopy(self.task)
            del task["query"][field]
            self.assert_error("query_fields_invalid", task)
        for extra in ("source_refs", "trusted_context", "profile", "filters"):
            task = deepcopy(self.task)
            task["query"][extra] = "private-value"
            self.assert_error("query_fields_invalid", task)

    def test_schema_version_intent_and_layer_have_no_fallback(self):
        for field, values, code in (
                ("schema_version", (None, "1.0", "1.0.2", 1), "query_version_unsupported"),
                ("intent", (None, "game_knowledge", "history", "LT", "GAME_LOOKUP", " game_lookup"),
                 "query_intent_unsupported"),
                ("layer", (None, "draft", "published", "RAW"), "query_layer_unsupported")):
            for bad in values:
                with self.subTest(field=field, value=bad):
                    task = deepcopy(self.task)
                    task["query"][field] = bad
                    self.assert_error(code, task)

    def test_request_and_text_require_content_without_normalizing_values(self):
        for field, code in (("request_id", "query_request_id_invalid"), ("text", "query_text_invalid")):
            for bad in (None, "", "\r\n\t", 0, False, [], {}):
                with self.subTest(field=field, value=bad):
                    task = deepcopy(self.task)
                    task["query"][field] = bad
                    self.assert_error(code, task)
        self.assertEqual(map_task(self.task)["text"], "  Synthetic Blade\r\n")
        self.assertEqual(map_task(self.task)["request_id"], " synthetic-game-request ")

    def test_scope_has_exact_fields_and_both_domains_match(self):
        for field in self.query["scope"]:
            task = deepcopy(self.task)
            del task["query"]["scope"][field]
            self.assert_error("query_scope_invalid", task)
        task = deepcopy(self.task)
        task["query"]["scope"]["unknown"] = None
        self.assert_error("query_scope_invalid", task)
        task = deepcopy(self.task)
        task["domain"] = "channel_knowledge"
        self.assert_error("task_domain_unsupported", task)
        task = deepcopy(self.task)
        task["query"]["scope"]["domain"] = "channel_knowledge"
        self.assert_error("query_domain_unsupported", task)

    def test_game_and_content_version_are_explicit_not_null_or_latest_defaults(self):
        for field, code in (("game_id", "query_game_id_required"),
                            ("content_version", "query_content_version_required")):
            for bad in (None, "", " \t", 0, False, [], {}):
                with self.subTest(field=field, value=bad):
                    task = deepcopy(self.task)
                    task["query"]["scope"][field] = bad
                    before = deepcopy(task)
                    self.assert_error(code, task)
                    self.assertEqual(task, before)

    def test_platform_null_is_preserved_and_nonstring_is_rejected(self):
        task = deepcopy(self.task)
        task["query"]["scope"]["platform"] = None
        self.assertIsNone(map_task(task)["scope"]["platform"])
        for bad in (False, 0, [], {}):
            task = deepcopy(self.task)
            task["query"]["scope"]["platform"] = bad
            self.assert_error("query_platform_invalid", task)

    def test_conditions_other_than_before_event_time_including_filters_are_rejected(self):
        for conditions in ({"filters": [{"field": "power", "op": "gte", "value": 90}]},
                           {"filters": []}, {"question_only": False}, {"unknown": None},
                           {"before_event_time": "2026-11-06T10:00:00Z", "unknown": None}):
            with self.subTest(conditions=conditions):
                task = deepcopy(self.task)
                task["query"]["conditions"] = conditions
                before = deepcopy(task)
                self.assert_error("query_conditions_unsupported", task)
                self.assertEqual(task, before)

    def test_only_before_event_time_is_preserved_for_native_validation(self):
        for before in ("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:00"):
            with self.subTest(before=before):
                task = deepcopy(self.task)
                task["query"]["conditions"] = {"before_event_time": before}
                mapped = map_task(task)
                self.assertEqual(mapped, task["query"])
                self.assertEqual(mapped["conditions"], {"before_event_time": before})
                self.assertIsNot(mapped["conditions"], task["query"]["conditions"])
                # This binding does not add a missing timezone or reinterpret it.
                self.assertEqual(mapped["conditions"]["before_event_time"], before)

    def test_before_event_time_requires_nonempty_string(self):
        for bad in (None, "", " \t", 0, False, [], {}):
            task = deepcopy(self.task)
            task["query"]["conditions"] = {"before_event_time": bad}
            self.assert_error("query_before_event_time_invalid", task)

    def test_explicit_numeric_syntax_is_rejected_by_public_guard_and_mapper(self):
        for text in ("Blade > 3", "Blade < 3", "Blade >= 3", "Blade = 3", "Blade ≤ 3", "Blade ≥ 3",
                     "power90", "POWER 90", "cost: cheap", "cost ： 2", "released_on:2026-11-06",
                     "cost -2", "大於 10", "小於1", "至少2", "至多3", "不超過 4"):
            with self.subTest(text=text):
                with self.assertRaises(BindingError) as caught:
                    reject_numeric_text(text)
                self.assertEqual(caught.exception.code, "numeric_text_unsupported")
                task = deepcopy(self.task)
                task["query"]["text"] = text
                before = deepcopy(task)
                self.assert_error("numeric_text_unsupported", task)
                self.assertEqual(task, before)

    def test_literal_unknown_and_numbered_names_are_not_rewritten(self):
        for text in ("Integration Blade", "Unknown Name", "Integration Blade 2", "2026 Blade",
                     "Mk. 90", "Empower 90", "Powerful90", "Costume 90", "power Blade 2"):
            with self.subTest(text=text):
                self.assertIsNone(reject_numeric_text(text))
                task = deepcopy(self.task)
                task["query"]["text"] = text
                self.assertEqual(map_task(task), task["query"])

    def test_public_numeric_guard_rejects_invalid_text_with_fixed_code(self):
        for bad in (None, "", " \n", 0, False, [], {}):
            with self.assertRaises(BindingError) as caught:
                reject_numeric_text(bad)
            self.assertEqual(caught.exception.code, "query_text_invalid")

    def test_container_errors_are_fixed_and_do_not_echo_input(self):
        for bad in (None, [], "private-task-text"):
            with self.assertRaises(BindingError) as caught:
                map_task(bad)
            self.assertEqual(caught.exception.code, "task_object_required")
        for field, bad, code in (("query", [], "query_object_required"),
                                  ("scope", [], "query_scope_invalid"),
                                  ("conditions", None, "query_conditions_invalid"),
                                  ("conditions", [], "query_conditions_invalid"),
                                  ("conditions", False, "query_conditions_invalid")):
            task = deepcopy(self.task)
            if field == "query":
                task[field] = bad
            else:
                task["query"][field] = bad
            self.assert_error(code, task)
        self.task["query"]["conditions"] = {"private-key": "private-source-text"}
        self.assert_error("query_conditions_unsupported")


if __name__ == "__main__":
    unittest.main()
