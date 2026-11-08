from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from ssr_chat.modular_api import ModularRetrievalGateway, RetrievalInputError, load_retrieval_routes
from ssr_chat.modular_retrieval import ModularRetrievalPipeline, PipelineConfig, PipelineError


class PipelineCaptureTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / ".test-temp" / "pipeline-capture"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = self.root / "private-artifacts"
        self.config = PipelineConfig(Path(sys.executable), self.root, self.root, self.root,
                                     self.root / "dispatcher.json")
        self.payload = {"request": {"schema_version": "1.0.1", "request_id": "synthetic-id",
                                    "text": "包含中文的問題"}, "trusted_context": {"synthetic": True}}

    def invoke(self, pipeline, *, result=None, error=None, allowed=None):
        trace = []
        with patch("ssr_chat.modular_retrieval.subprocess.run", return_value=result, side_effect=error) as run:
            try:
                value = pipeline._invoke("planner", self.root, ["-m", "source.hgpt_ssr", "--input", "-"],
                                         self.payload, allowed or {0}, trace)
            except PipelineError as caught:
                return None, caught, trace, run
        return value, None, trace, run

    def call(self):
        calls = list(self.artifacts.iterdir())
        self.assertEqual(len(calls), 1)
        directory = calls[0]
        metadata = json.loads((directory / "result.json").read_bytes())
        return directory, metadata

    def test_capture_exact_binary_streams_input_and_owned_command_before_parse(self):
        pipeline = ModularRetrievalPipeline(self.config, artifact_dir=self.artifacts)
        stdout = b'{"status":"ok","value":"native output"}'
        stderr = b"PRIVATE_STDERR\x00\xff\n"
        with patch.dict(os.environ, {"PRIVATE_API_KEY": "PRIVATE_SECRET"}):
            result, error, trace, run = self.invoke(pipeline, result=subprocess.CompletedProcess([], 0, stdout, stderr))
        self.assertIsNone(error)
        self.assertEqual(result["status"], "ok")
        directory, metadata = self.call()
        self.assertEqual((directory / "stdin.bin").read_bytes(), run.call_args.kwargs["input"])
        self.assertEqual(json.loads((directory / "stdin.bin").read_bytes()), self.payload)
        self.assertEqual((directory / "stdout.bin").read_bytes(), stdout)
        self.assertEqual((directory / "stderr.bin").read_bytes(), stderr)
        self.assertEqual(metadata["command"], run.call_args.args[0])
        self.assertEqual(metadata["cwd"], str(run.call_args.kwargs["cwd"]))
        self.assertEqual(metadata["exit_code"], 0)
        self.assertTrue(metadata["output_complete"])
        self.assertIn("started_at", metadata)
        self.assertIn("completed_at", metadata)
        self.assertNotIn("PRIVATE_API_KEY", json.dumps(metadata))
        self.assertNotIn("PRIVATE_SECRET", json.dumps(metadata))
        self.assertNotIn("PRIVATE_API_KEY", run.call_args.kwargs["env"])
        serialized = json.dumps(trace)
        self.assertNotIn("PRIVATE_STDERR", serialized)
        self.assertNotIn(str(self.artifacts), serialized)
        self.assertEqual(trace[0]["artifact_capture"]["stderr_sha256"], hashlib.sha256(stderr).hexdigest())

    def test_nonzero_exit_and_invalid_json_both_keep_raw_outputs_before_error(self):
        for returncode, stdout, expected in ((7, b"not json", "component_failed"),
                                              (0, b"not json", "component_invalid_json")):
            with self.subTest(returncode=returncode):
                root = self.artifacts / str(returncode)
                pipeline = ModularRetrievalPipeline(self.config, artifact_dir=root)
                _, error, trace, _ = self.invoke(pipeline, result=subprocess.CompletedProcess([], returncode, stdout, b"private diagnostic"))
                self.assertEqual(error.code, expected)
                directory = next(root.iterdir())
                self.assertEqual((directory / "stdout.bin").read_bytes(), stdout)
                self.assertEqual((directory / "stderr.bin").read_bytes(), b"private diagnostic")
                metadata = json.loads((directory / "result.json").read_bytes())
                self.assertEqual(metadata["exit_code"], returncode)
                self.assertEqual(metadata["outcome"], "returned")
                self.assertNotIn("private diagnostic", json.dumps(trace))

    def test_timeout_retains_partial_binary_streams_and_unavailable_retains_start(self):
        cases = [(subprocess.TimeoutExpired("PRIVATE_COMMAND", 1, output=b"partial stdout", stderr=b"partial stderr"),
                  "component_timeout", "timeout", b"partial stdout", b"partial stderr"),
                 (OSError("PRIVATE_DIAGNOSTIC"), "component_unavailable", "unavailable", b"", b"")]
        for index, (failure, expected, outcome, stdout, stderr) in enumerate(cases):
            with self.subTest(outcome=outcome):
                root = self.artifacts / str(index)
                pipeline = ModularRetrievalPipeline(self.config, artifact_dir=root)
                _, error, trace, _ = self.invoke(pipeline, error=failure)
                self.assertEqual(error.code, expected)
                directory = next(root.iterdir())
                self.assertTrue((directory / "start.json").is_file())
                self.assertEqual((directory / "stdout.bin").read_bytes(), stdout)
                self.assertEqual((directory / "stderr.bin").read_bytes(), stderr)
                metadata = json.loads((directory / "result.json").read_bytes())
                self.assertIsNone(metadata["exit_code"])
                self.assertFalse(metadata["output_complete"])
                self.assertEqual(metadata["outcome"], outcome)
                self.assertNotIn("PRIVATE", json.dumps(trace))

    def test_opt_in_capture_failure_stops_before_process_without_fallback(self):
        pipeline = ModularRetrievalPipeline(self.config, artifact_dir=self.artifacts)
        original = self.artifacts.with_name("original-private-artifacts")
        self.artifacts.rename(original)
        _, error, trace, run = self.invoke(pipeline, result=subprocess.CompletedProcess([], 0, b"{}", b""))
        self.assertEqual(error.code, "artifact_capture_failed")
        run.assert_not_called()
        self.assertEqual(list(original.iterdir()), [])

    def test_default_keeps_existing_trace_shape_and_does_not_prepare_artifact_folders(self):
        with patch("ssr_chat.modular_retrieval.prepare_artifact_directory") as prepare:
            pipeline = ModularRetrievalPipeline(self.config)
            _, error, trace, _ = self.invoke(pipeline, result=subprocess.CompletedProcess([], 0, b"{}", b"private stderr"))
        self.assertIsNone(error)
        prepare.assert_not_called()
        self.assertNotIn("artifact_capture", trace[0])
        self.assertFalse(self.artifacts.exists())

    def test_each_call_gets_exclusive_directory_and_exact_pipeline_edge_payloads(self):
        pipeline = ModularRetrievalPipeline(self.config, artifact_dir=self.artifacts)
        request_id = self.payload["request"]["request_id"]
        plan = {"request_id": request_id, "schema_version": "1.0.1", "tasks": [{"task_id": "task-1"}]}
        domain_result = {"status": "ok", "domain": "channel_knowledge", "evidence": []}
        dispatched = {"request_id": request_id, "schema_version": "1.0.1",
                      "tasks": [{"task_id": "task-1", "result": domain_result}]}
        collection = {"request_id": request_id, "contract_version": "1.0.1", "status": "no_match", "partial": False}
        outputs = [json.dumps(value).encode("utf-8") for value in (plan, dispatched, collection)]
        with patch("ssr_chat.modular_retrieval.subprocess.run", side_effect=[
                subprocess.CompletedProcess([], 0, raw, b"") for raw in outputs]):
            report = pipeline.run(self.payload)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(list(self.artifacts.iterdir())), 3)
        directories = [self.artifacts / stage["artifact_capture"]["directory"] for stage in report["stages"]]
        self.assertEqual(len(set(directories)), 3)
        inputs = [json.loads((directory / "stdin.bin").read_bytes()) for directory in directories]
        self.assertEqual(inputs, [self.payload, plan, {"query_plan": plan, "domain_results": {"task-1": domain_result}}])
        for directory, expected in zip(directories, outputs):
            self.assertEqual((directory / "stdout.bin").read_bytes(), expected)

    def test_nonlocal_artifact_destination_is_rejected_before_any_subprocess(self):
        for target in (r"\\server\share\capture", "//server/share/capture", "file:///capture", "https://example.test"):
            with self.subTest(target=target), self.assertRaises(PipelineError):
                ModularRetrievalPipeline(self.config, artifact_dir=target)

    def test_gateway_capture_option_is_server_owned_and_not_exposed_in_health_profile(self):
        template = {"request": {"schema_version": "1.0.1"}, "trusted_context": {"synthetic": True}}
        path = self.root / "template.json"
        path.write_text(json.dumps(template), encoding="utf-8")
        with patch.object(PipelineConfig, "read", return_value=self.config):
            gateway = ModularRetrievalGateway.from_files("operator-pipeline.json", path, artifact_dir=self.artifacts)
        self.assertEqual(gateway._pipeline.artifact_dir, self.artifacts.resolve())
        self.assertNotIn(str(self.artifacts), json.dumps(gateway.profile))
        with patch("ssr_chat.modular_retrieval.subprocess.run") as run:
            with self.assertRaises(RetrievalInputError):
                gateway.retrieve({"request_id": "synthetic", "text": "question", "artifact_dir": str(self.root / "injected")})
            run.assert_not_called()

    def test_route_capture_roots_are_separate_and_default_calls_stay_compatible(self):
        for name in ("pipeline.json", "template.json"):
            (self.root / name).write_text("{}", encoding="utf-8")
        config = self.root / "routes.json"
        config.write_text(json.dumps({"schema_version": "1.0.1", "routes": {
            "/retrieve/channel": {"domain": "channel_knowledge", "pipeline_config": "pipeline.json", "template": "template.json"},
            "/retrieve/game": {"domain": "game_knowledge", "pipeline_config": "pipeline.json", "template": "template.json"},
        }}), encoding="utf-8")
        with patch.object(ModularRetrievalGateway, "from_files", return_value=Mock()) as build:
            load_retrieval_routes(config, artifact_root=self.artifacts)
        roots = [call.kwargs["artifact_dir"] for call in build.call_args_list]
        self.assertEqual(len(set(roots)), 2)
        self.assertTrue(all(root.parent == self.artifacts.resolve() for root in roots))
        with patch.object(ModularRetrievalGateway, "from_files", return_value=Mock()) as build:
            load_retrieval_routes(config)
        self.assertTrue(all("artifact_dir" not in call.kwargs for call in build.call_args_list))


if __name__ == "__main__":
    unittest.main()
