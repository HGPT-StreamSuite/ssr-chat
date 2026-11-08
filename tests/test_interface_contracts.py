"""Machine-readable local contracts checked against actual bounded entrances; no sockets."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ssr_chat.api import dispatch
from ssr_chat.cli import build_parser
from ssr_chat.review_native import INTERFACE_VERSION, TOOLS, validate_request
from ssr_chat.review_web import ReviewError, ReviewWorkspace, source_version

REPO = Path(__file__).resolve().parents[1]


class InterfaceContractTests(unittest.TestCase):
    def setUp(self):
        self.cases = json.loads((REPO / "fixtures/contracts/review-native.v1.cases.json").read_text(encoding="utf-8"))

    def test_examples_have_registered_tools_and_match_public_schema(self):
        schema = json.loads((REPO / "contracts/review-native.v1.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(self.cases["interface_version"], INTERFACE_VERSION)
        self.assertEqual(set(schema["properties"]["request"]["properties"]["tool"]["enum"]), set(TOOLS))
        for case in self.cases["valid"]:
            with self.subTest(case=case["id"]):
                validate_request(case["request"])
        self.assertEqual(set(self.cases["envelope_example"]), set(schema["required"]))

    def test_invalid_contract_cases_fail_before_native_registration(self):
        parent = REPO / ".test-temp"
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            app = ReviewWorkspace(Path(directory))
            self.assertEqual(app.meta()["default_flow"], "native")
            self.assertEqual(app.meta()["native"]["interface_version"], INTERFACE_VERSION)
            for case in self.cases["invalid"]:
                envelope = {**deepcopy(self.cases["envelope_example"]), "workspace_id": app.workspace_id,
                            "request": case["request"]}
                with self.subTest(case=case["id"]), patch("ssr_chat.review_web.NativeReview") as native:
                    with self.assertRaises(ReviewError) as error:
                        app.run_native(envelope)
                    self.assertEqual((error.exception.status, error.exception.code), (case["http_status"], case["reason_code"]))
                    native.assert_not_called()
            self.assertEqual(app.native_receipts, {})
            self.assertEqual(app.model.calls, 0)

    def test_unconfigured_api_routes_do_not_fall_back_to_chat_or_model(self):
        runtime = Mock()
        for path, reason in (("/query", "modular_entry_not_configured"),
                             ("/retrieve/channel-knowledge", "modular_retrieval_not_configured"),
                             ("/answer/channel-knowledge", "modular_answer_not_configured"),
                             ("/twitch/replay", "twitch_replay_not_configured"),
                             ("/twitch/archive", "twitch_archive_not_configured")):
            with self.subTest(path=path):
                status, result = dispatch(runtime, "POST", path, b'{"request_id":"x","text":"launch"}')
                self.assertEqual((status, result["reason_code"]), (503, reason))
        runtime.handle.assert_not_called()

    def test_cli_inventory_covers_every_registered_command(self):
        # Argparse's registrations are the public CLI contract, including the delegated twitch-live parser.
        import argparse
        commands = set()
        for action in build_parser()._actions:
            if isinstance(action, argparse._SubParsersAction):
                commands.update(action.choices)
        catalog = json.loads((REPO / "contracts/interfaces.v1.json").read_text(encoding="utf-8"))
        self.assertEqual({item["command"] for item in catalog["cli"]}, commands)
        for item in catalog["cli"]:
            self.assertTrue((REPO / item["implementation"]).is_file(), item)
            self.assertTrue((REPO / item["test_source"]).is_file(), item)

    def test_review_source_identity_changes_when_interface_contract_changes(self):
        parent = REPO / ".test-temp"
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            root = Path(directory)
            (root / "contracts").mkdir()
            for name in ("pyproject.toml", "uv.lock", "README.md", "standard.md"):
                (root / name).write_text("synthetic", encoding="utf-8")
            contract = root / "contracts" / "example.json"
            contract.write_text('{"version":1}', encoding="utf-8")
            with patch("ssr_chat.review_web.REPO", root), patch("ssr_chat.review_web.STANDARD", root / "standard.md"):
                before = source_version()["fingerprint"]
                contract.write_text('{"version":2}', encoding="utf-8")
                self.assertNotEqual(source_version()["fingerprint"], before)


if __name__ == "__main__":
    unittest.main()
