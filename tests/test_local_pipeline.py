"""Synchronous input/ownership boundary tests; native execution is verified separately."""
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from ssr_chat.local_pipeline import effective_files, execute, main, write_json


class LocalPipelineBoundaryTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / ".test-temp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_exclusive_result_does_not_overwrite_existing_evidence(self):
        path = self.root / "receipt.json"
        write_json(path, {"original": True})
        with self.assertRaises(FileExistsError):
            write_json(path, {"original": False})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"original": True})

    def test_incomplete_or_nonboolean_configuration_is_refused(self):
        input_path = self.root / "input.json"
        input_path.write_text('{"request_id":"x","text":"/channel launch"}', encoding="utf-8")
        for body in ('{"schema_version":"1.0.1","synthetic":false}', '{"schema_version":"1.0.1","synthetic":"yes"}'):
            config = self.root / "config.json"
            config.write_text(body, encoding="utf-8")
            with self.subTest(body=body), patch("ssr_chat.local_pipeline.load_retrieval_routes") as routes:
                with self.assertRaisesRegex(ValueError, "explicit_local_run_configuration_required"):
                    execute(config, input_path, self.root / "run")
                routes.assert_not_called()
                self.assertFalse((self.root / "run").exists())

    def test_model_generation_is_not_an_available_cli_mode(self):
        with self.assertRaises(SystemExit) as error:
            main(["--config", "unused", "--input", "unused", "--run-dir", "unused", "--mode", "generate"])
        self.assertEqual(error.exception.code, 2)

    def test_library_rejects_unsupported_modes_before_reading_or_creating_files(self):
        for mode in ("generate", "", None, True, {}, ["prepare"]):
            with self.subTest(mode=mode), patch("ssr_chat.local_pipeline._file") as read:
                with self.assertRaisesRegex(ValueError, "^offline_mode_invalid$"):
                    execute("unread-config", "unread-input", self.root / "must-not-exist", mode=mode)
                read.assert_not_called()
                self.assertFalse((self.root / "must-not-exist").exists())

    def bundle(self):
        checkout = Path(__file__).resolve().parents[1]
        names = ("hgpt-ssr-query-planner", "hgpt-ssr-dispatcher", "hgpt-ssr-channel-search", "hgpt-ssr-evidence-collector")
        repos = {"ssr-chat": str(checkout), **{n: str(checkout.parent / "components" / n) for n in names}}
        config_dir = self.root / "config"
        config_dir.mkdir()
        io_dir = self.root / "native-io"
        io_dir.mkdir()
        data = self.root / "synthetic.jsonl"
        data.write_text('{"synthetic":true}\n', encoding="utf-8")
        def save(name, value):
            path = config_dir / name
            path.write_text(json.dumps(value), encoding="utf-8")
            return path
        reader = save("reader.json", {"domain": "channel_knowledge", "sources": [{"kind": "snapshot", "source_kind": "synthetic", "path": str(data)}]})
        command = [sys.executable, "-B", "-X", "utf8", "-m", "ssr_chat.native_capture", "--repo", repos["hgpt-ssr-channel-search"], "--config", str(reader), "--artifact-root", str(io_dir)]
        dispatcher = save("dispatcher.json", {"adapters": {"channel_knowledge": {"kind": "cli", "command": command, "cwd": str(checkout)}}})
        pipeline = save("pipeline.json", {"python": sys.executable, "planner_repo": repos[names[0]], "dispatcher_repo": repos[names[1]], "collector_repo": repos[names[3]], "dispatcher_config": str(dispatcher)})
        template = save("template.json", {})
        retrieval = save("retrieval.json", {"routes": {"/retrieve/channel-knowledge": {"pipeline_config": str(pipeline), "template": str(template)}}})
        purpose = save("purpose.json", {})
        answer = save("answer.json", {"routes": {"/answer/channel-knowledge": {"purpose_policy": str(purpose)}}})
        entry = save("entry.json", {})
        config = {"schema_version": "1.0.1", "synthetic": True, "repos": repos, "retrieval_routes_config": str(retrieval), "answer_routes_config": str(answer), "entry_routes_config": str(entry)}
        path = save("operator.json", config)
        return path, config, pipeline, dispatcher, reader

    def test_effective_files_pin_actual_config_and_synthetic_input_bytes(self):
        path, config, *_ = self.bundle()
        files = effective_files(path, config)
        self.assertIn(str(self.root / "synthetic.jsonl"), files)
        self.assertEqual(len(files), 10)

    def test_alternate_planner_cannot_be_snapshotted_as_canonical_code(self):
        path, config, pipeline, *_ = self.bundle()
        value = json.loads(pipeline.read_text(encoding="utf-8"))
        value["planner_repo"] = str(self.root / "alternate-planner")
        pipeline.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "component_source_binding_mismatch"):
            effective_files(path, config)

    def test_dispatcher_cannot_execute_a_different_reader(self):
        path, config, _, dispatcher, _ = self.bundle()
        value = json.loads(dispatcher.read_text(encoding="utf-8"))
        value["adapters"]["channel_knowledge"]["command"][7] = str(self.root / "alternate-reader")
        dispatcher.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "native_reader_source_binding_mismatch"):
            effective_files(path, config)

    def test_relative_component_identity_cannot_change_resolution_base(self):
        path, config, pipeline, *_ = self.bundle()
        value = json.loads(pipeline.read_text(encoding="utf-8"))
        value["planner_repo"] = os.path.relpath(config["repos"]["hgpt-ssr-query-planner"], Path.cwd())
        pipeline.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "pipeline_component_source_binding_mismatch"):
            effective_files(path, config)

    def test_local_data_source_is_accepted_and_mock_source_is_refused(self):
        path, config, _, _, reader = self.bundle()
        value = json.loads(reader.read_text(encoding="utf-8"))
        value["sources"][0]["source_kind"] = "local_data"
        reader.write_text(json.dumps(value), encoding="utf-8")
        self.assertIn(str(reader.resolve()), effective_files(path, config))
        value["sources"][0]["source_kind"] = "mock"
        reader.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "only_explicit_local_snapshots_allowed"):
            effective_files(path, config)

    def test_alternate_checkout_is_rejected_before_source_or_tool_subprocess(self):
        path, config, *_ = self.bundle()
        config["repos"]["ssr-chat"] = str(self.root / "ssr-chat")
        path.write_text(json.dumps(config), encoding="utf-8")
        request = self.root / "input.json"
        request.write_text('{"request_id":"x","text":"/channel launch"}', encoding="utf-8")
        with patch("ssr_chat.local_pipeline.source_snapshot") as snapshot:
            with self.assertRaisesRegex(ValueError, "canonical_workspace_binding_mismatch"):
                execute(path, request, self.root / "execution")
        snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
