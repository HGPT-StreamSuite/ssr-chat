"""warm_workers execution of the real planner, dispatcher, collector and channel reader (synthetic data only)."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from ssr_chat.modular_retrieval import ModularRetrievalPipeline, PipelineConfig, PipelineError

CHECKOUT = Path(__file__).resolve().parents[1]
COMPONENTS = CHECKOUT.parent / "components"
NAMES = ("hgpt-ssr-query-planner", "hgpt-ssr-dispatcher", "hgpt-ssr-channel-search", "hgpt-ssr-evidence-collector")
TIMING_KEYS = {"pid", "started_at", "finished_at", "duration_ms", "elapsed_ms", "completed_at"}


def without_timing(value):
    if isinstance(value, dict):
        return {key: without_timing(child) for key, child in value.items() if key not in TIMING_KEYS}
    if isinstance(value, list):
        return [without_timing(child) for child in value]
    return value


@unittest.skipUnless(all((COMPONENTS / name).is_dir() for name in NAMES), "component repositories are not checked out")
class WarmRetrievalTests(unittest.TestCase):
    def setUp(self):
        parent = CHECKOUT / ".test-temp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.template = json.loads((CHECKOUT / "fixtures/retrieval/channel-raw.template.json").read_text(encoding="utf-8"))
        scope = self.template["trusted_context"]["access_scope"]
        reader = COMPONENTS / "hgpt-ssr-channel-search"
        dispatcher = {"schema_version": "1.0.1", "access_scope": scope, "adapters": {"channel_knowledge": {
            "kind": "cli", "cwd": str(reader), "timeout_seconds": 15, "access_scope": scope,
            "command": [sys.executable, "-B", "-X", "utf8", "source/ssr.py", "--config",
                        str(reader / "fixtures/synthetic/config.json"), "--request", "-"]}}}
        (self.root / "dispatcher.json").write_text(json.dumps(dispatcher), encoding="utf-8")

    def pipeline(self, mode):
        config = {"schema_version": "1.0.1", "python": sys.executable, "dispatcher_config": str(self.root / "dispatcher.json"),
                  "planner_repo": str(COMPONENTS / NAMES[0]), "dispatcher_repo": str(COMPONENTS / NAMES[1]),
                  "collector_repo": str(COMPONENTS / NAMES[3]), "execution": mode}
        path = self.root / f"pipeline-{mode}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        pipeline = ModularRetrievalPipeline(PipelineConfig.read(path))
        self.addCleanup(pipeline.close)
        return pipeline

    def request(self, number, text):
        value = deepcopy(self.template)
        value["request"].update(request_id=f"warm-{number}", text=text)
        return value

    def run_questions(self, pipeline, texts):
        return [pipeline.run(self.request(index, text), expected_domain="channel_knowledge")
                for index, text in enumerate(texts)]

    def test_warm_results_equal_subprocess_results_apart_from_timing(self):
        texts = ["請查詢本頻道 launch", "請查詢本頻道 zzqqxx不存在", "請查詢本頻道 launch"]
        cold = self.run_questions(self.pipeline("subprocess"), texts)
        warm = self.run_questions(self.pipeline("warm_workers"), texts)
        self.assertEqual([item["collection"]["status"] for item in cold], ["ok", "no_match", "ok"])
        for left, right in zip(cold, warm):
            for key in ("status", "plan", "dispatch", "collection", "answer_generation", "public_output"):
                self.assertEqual(without_timing(left[key]), without_timing(right[key]), key)
            # Planner and collector wire bytes are identical, not just equal after parsing.
            for index in (0, 2):
                self.assertEqual(left["stages"][index]["stdout_sha256"], right["stages"][index]["stdout_sha256"])
            self.assertEqual([stage["exit_code"] for stage in left["stages"]], [stage["exit_code"] for stage in right["stages"]])

    def test_every_stage_and_the_reader_stay_warm_between_questions(self):
        warm, cold = self.pipeline("warm_workers"), self.pipeline("subprocess")
        reports = self.run_questions(warm, ["請查詢本頻道 launch"] * 3)
        for report in reports:
            self.assertEqual([stage.get("execution") for stage in report["stages"]], ["warm_workers"] * 3)
        reader_pids = [report["dispatch"]["tasks"][0]["execution"]["pid"] for report in reports]
        self.assertEqual(len(set(reader_pids)), 1)
        cold_pids = [report["dispatch"]["tasks"][0]["execution"]["pid"]
                     for report in self.run_questions(cold, ["請查詢本頻道 launch"] * 2)]
        self.assertEqual(len(set(cold_pids)), 2)
        self.assertNotIn("execution", self.run_questions(cold, ["請查詢本頻道 launch"])[0]["stages"][0])

    def test_unknown_adapter_command_falls_back_to_an_ordinary_subprocess(self):
        dispatcher = json.loads((self.root / "dispatcher.json").read_text(encoding="utf-8"))
        command = dispatcher["adapters"]["channel_knowledge"]["command"]
        command.insert(1, "-u")  # a flag outside the recognised set: must run as an ordinary subprocess
        (self.root / "dispatcher.json").write_text(json.dumps(dispatcher), encoding="utf-8")
        reports = self.run_questions(self.pipeline("warm_workers"), ["請查詢本頻道 launch"] * 2)
        self.assertEqual([report["collection"]["status"] for report in reports], ["ok", "ok"])
        pids = {report["dispatch"]["tasks"][0]["execution"]["pid"] for report in reports}
        self.assertEqual(len(pids), 2)

    def test_reader_timeout_and_failure_use_the_same_dispatcher_reason_codes(self):
        reader = self.root / "slow_reader.py"
        reader.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(30)\n", encoding="utf-8")
        for body, reason in (("import sys, time\nsys.stdin.read()\ntime.sleep(30)\n", "adapter_timeout"),
                             ("import sys\nsys.stdin.read()\nsys.stdout.write('x' * 10_000_000)\n", "adapter_output_limit")):
            reader.write_text(body, encoding="utf-8")
            dispatcher = json.loads((self.root / "dispatcher.json").read_text(encoding="utf-8"))
            adapter = dispatcher["adapters"]["channel_knowledge"]
            adapter.update(cwd=str(self.root), timeout_seconds=1.5, command=[sys.executable, "-B", "-X", "utf8", "slow_reader.py"])
            (self.root / "dispatcher.json").write_text(json.dumps(dispatcher), encoding="utf-8")
            results = {}
            for mode in ("subprocess", "warm_workers"):
                report = self.run_questions(self.pipeline(mode), ["請查詢本頻道 launch"])[0]
                task = report["dispatch"]["tasks"][0]
                results[mode] = (task["result"]["status"], task["result"].get("reason_code"),
                                 task["execution"]["transport_reason_code"], report["status"])
            with self.subTest(reason=reason):
                self.assertEqual(results["warm_workers"], results["subprocess"])
                self.assertEqual(results["warm_workers"][2], reason)

    def test_stage_timeout_is_reported_as_component_timeout(self):
        sleeper = self.root / "sleeper.py"
        sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        config = PipelineConfig(Path(sys.executable), self.root, self.root, self.root, self.root / "dispatcher.json",
                                timeout=1.0, execution="warm_workers")
        pipeline = ModularRetrievalPipeline(config)
        self.addCleanup(pipeline.close)
        with self.assertRaisesRegex(PipelineError, "component_timeout"):
            pipeline._invoke("planner", self.root, ["sleeper.py"], {}, {0}, [])


class ExecutionSettingTests(unittest.TestCase):
    def test_default_is_subprocess_and_unknown_modes_are_rejected(self):
        self.assertEqual(PipelineConfig(Path(sys.executable), Path("."), Path("."), Path("."), Path("d.json")).execution, "subprocess")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for entry in ("source/hgpt_ssr/__main__.py", "source/run_hgpt_ssr.py", "run.py"):
                (root / entry).parent.mkdir(parents=True, exist_ok=True)
                (root / entry).write_text("# placeholder", encoding="utf-8")
            (root / "dispatcher.json").write_text(json.dumps({"schema_version": "1.0.1", "adapters": {
                "channel_knowledge": {"kind": "cli", "timeout_seconds": 5}}}), encoding="utf-8")
            base = {"schema_version": "1.0.1", "python": sys.executable, "planner_repo": ".", "dispatcher_repo": ".",
                    "collector_repo": ".", "dispatcher_config": "dispatcher.json"}
            for value, expected in ((None, "subprocess"), ("subprocess", "subprocess"), ("warm_workers", "warm_workers")):
                config = {**base, **({} if value is None else {"execution": value})}
                (root / "pipeline.json").write_text(json.dumps(config), encoding="utf-8")
                self.assertEqual(PipelineConfig.read(root / "pipeline.json").execution, expected)
            for bad in ("warm", "", None, True, ["warm_workers"]):
                (root / "pipeline.json").write_text(json.dumps({**base, "execution": bad}), encoding="utf-8")
                with self.subTest(bad=bad), self.assertRaisesRegex(PipelineError, "execution_mode_invalid"):
                    PipelineConfig.read(root / "pipeline.json")


if __name__ == "__main__":
    unittest.main()
