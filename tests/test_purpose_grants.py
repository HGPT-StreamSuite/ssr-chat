from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

from ssr_chat import purpose_grants as pg
from ssr_chat.modular_answer import EvidenceAnswerGateway, PurposePolicy, content_digest
from ssr_chat.modular_api import ModularRetrievalGateway


def event(n, text=None, domain="channel_knowledge", actor=None):
    scope = {"domain": domain, "platform": "manual", "channel_id": "room", "game_id": None,
             "session_id": "s1" if actor else None, "content_version": None}
    e = {"record_id": f"rec-{n}", "source_id": "src", "source_event_id": f"ev-{n}", "revision": 1,
         "event_time": f"2026-11-01T10:00:{n % 60:02d}Z", "scope": scope, "layer": "raw",
         "text": text or f"公告第 {n} 則：開播時間 {n} 點。", "quality_flags": ["manual_registration"],
         "source_kind": "local_data",
         "source_refs": [{"source_id": "src", "record_id": None, "revision": None, "locator": f"bundle/rec-{n}.txt",
                          "transformation_version": None}]}
    if actor:
        e["actor_account_id"] = actor
    return e


class PurposeGrantToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.events = [event(1), event(2), event(3)]
        self.evidence = self.write_evidence(self.events)
        self.review = self.dir / "review.md"
        self.policy = self.dir / "policy.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_evidence(self, events, name="evidence.jsonl"):
        path = self.dir / name
        path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
        return path

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pg.main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()

    def tick(self, *record_ids):
        text = self.review.read_text(encoding="utf-8")
        lines, current = [], None
        for line in text.splitlines():
            if line.startswith("## ") and "（第" in line:
                current = line.split(". ", 1)[1].split("（", 1)[0]
            if "<!-- grant:" in line:
                mark = "x" if current in record_ids else " "
                line = "- [" + mark + line[4:]
            lines.append(line)
        self.review.write_text("\n".join(lines), encoding="utf-8")

    def gate(self, policy_value, events):
        pipeline = Mock()
        pipeline.run.return_value = {"status": "complete", "request_id": "r", "collection": {
            "contract_version": "1.0.1", "partial": False, "evidence": deepcopy(events)}}
        scope = events[0]["scope"]
        retrieval = ModularRetrievalGateway(pipeline, {"request": {"schema_version": "1.0.1", "scope": scope, "layer": "raw"},
            "trusted_context": {"access_scope": {"read_scopes": [{"domain": scope["domain"]}], "write_scopes": []}}},
            expected_domain=scope["domain"])
        trace = []
        result = EvidenceAnswerGateway(retrieval, PurposePolicy(policy_value), None, trace, prepare_only=True).answer(
            {"request_id": "r", "text": "開播時間？"})
        return result, trace

    def test_list_defaults_to_nothing_checked(self):
        code, out, _ = self.cli("list", "--evidence", self.evidence, "--out", self.review)
        self.assertEqual(code, 0, out)
        text = self.review.read_text(encoding="utf-8")
        self.assertEqual(text.count("- [ ] 允許送進 AI"), 3)
        self.assertNotIn("- [x]", text)
        self.assertIn("公告第 2 則", text)
        side = json.loads(pg.sidecar_path(self.review).read_text(encoding="utf-8"))
        self.assertEqual(len(side["records"]), 3)
        self.assertNotIn("text", json.dumps(side["records"][0]["identity"]))

    def test_apply_writes_only_checked_records_and_policy_passes_gate(self):
        self.cli("list", "--evidence", self.evidence, "--out", self.review, "--policy-id", "tianya-channel")
        self.tick("rec-1", "rec-3")
        code, out, err = self.cli("apply", "--review", self.review, "--policy", self.policy)
        self.assertEqual(code, 0, err)
        value = json.loads(self.policy.read_text(encoding="utf-8"))
        self.assertEqual(value["policy_id"], "tianya-channel")
        self.assertEqual([g["record_id"] for g in value["grants"]], ["rec-1", "rec-3"])
        self.assertEqual(value["grants"][0]["content_sha256"], content_digest(self.events[0]))
        result, trace = self.gate(value, self.events)
        self.assertEqual([r["record_id"] for r in result["provided_evidence"]], ["rec-1", "rec-3"])
        self.assertEqual([r["record_id"] for r in result["excluded_evidence"]], ["rec-2"])
        prompt = json.dumps([t for t in trace if t.get("phase") == "prepared"][0]["prompt"], ensure_ascii=False)
        self.assertNotIn("公告第 2 則", prompt)

    def test_changed_content_invalidates_grant(self):
        self.cli("list", "--evidence", self.evidence, "--out", self.review)
        self.tick("rec-1", "rec-2")
        self.cli("apply", "--review", self.review, "--policy", self.policy)
        changed = [event(1, "公告第 1 則：改成晚上 9 點。"), event(2), event(3)]
        changed_path = self.write_evidence(changed, "changed.jsonl")
        code, out, _ = self.cli("check", "--evidence", changed_path, "--policy", self.policy)
        self.assertEqual(code, 1)
        self.assertIn("內容已變失效 1", out)
        self.assertIn("失效：rec-1", out)
        result, _ = self.gate(json.loads(self.policy.read_text(encoding="utf-8")), changed)
        self.assertEqual([r["record_id"] for r in result["provided_evidence"]], ["rec-2"])
        # 重新 list：失效的不預先勾選，仍有效的保持勾選
        self.cli("list", "--evidence", changed_path, "--policy", self.policy, "--out", self.review)
        text = self.review.read_text(encoding="utf-8")
        self.assertIn("授權已失效", text)
        self.assertEqual(text.count("- [x]"), 1)

    def test_check_passes_when_all_grants_still_match(self):
        self.cli("list", "--evidence", self.evidence, "--out", self.review)
        self.tick("rec-2")
        self.cli("apply", "--review", self.review, "--policy", self.policy)
        code, out, _ = self.cli("check", "--evidence", self.evidence, "--policy", self.policy)
        self.assertEqual(code, 0, out)
        self.assertIn("有效授權 1", out)

    def test_removed_record_is_reported_and_dropped(self):
        self.cli("list", "--evidence", self.evidence, "--out", self.review)
        self.tick("rec-3")
        self.cli("apply", "--review", self.review, "--policy", self.policy)
        fewer = self.write_evidence(self.events[:2], "fewer.jsonl")
        code, out, _ = self.cli("check", "--evidence", fewer, "--policy", self.policy)
        self.assertEqual(code, 1)
        self.assertIn("記錄不存在：rec-3", out)
        self.cli("list", "--evidence", fewer, "--policy", self.policy, "--out", self.review)
        self.assertIn("apply 後會移除", self.review.read_text(encoding="utf-8"))
        code, _, _ = self.cli("apply", "--review", self.review, "--policy", self.policy)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.policy.read_text(encoding="utf-8"))["grants"], [])

    def test_apply_backs_up_existing_policy(self):
        self.cli("list", "--evidence", self.evidence, "--out", self.review)
        self.cli("apply", "--review", self.review, "--policy", self.policy)
        self.tick("rec-1")
        code, out, _ = self.cli("apply", "--review", self.review, "--policy", self.policy)
        self.assertEqual(code, 0)
        backups = list(self.dir.glob("policy.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8"))["grants"], [])

    def test_tampered_review_is_rejected(self):
        self.cli("list", "--evidence", self.evidence, "--out", self.review)
        text = self.review.read_text(encoding="utf-8")
        second = [l for l in text.splitlines() if "grant:" in l][1]
        cases = {
            "deleted": "\n".join(l for l in text.splitlines() if l != second),
            "duplicated": text.replace(second, second + "\n" + second),
            "odd_mark": text.replace("- [ ] 允許送進 AI", "- [v] 允許送進 AI", 1),
            "other_review": text.replace("review_id：", "review_id：0000000000000000 舊："),
        }
        for name, bad in cases.items():
            with self.subTest(name):
                self.review.write_text(bad, encoding="utf-8")
                code, _, err = self.cli("apply", "--review", self.review, "--policy", self.policy)
                self.assertEqual(code, 2, name)
                self.assertTrue(err.startswith("錯誤："))
                self.assertFalse(self.policy.exists())

    def test_actor_domains_keep_actor_in_grant(self):
        chats = [event(1, "觀眾問：幾點開台？", "chat_history", actor="viewer-a"),
                 event(2, "觀眾問：今天玩什麼？", "chat_history", actor="viewer-b")]
        path = self.write_evidence(chats, "chat.jsonl")
        self.cli("list", "--evidence", path, "--out", self.review)
        text = self.review.read_text(encoding="utf-8")
        self.assertIn("發言者：viewer-a", text)
        self.tick("rec-2")
        code, _, err = self.cli("apply", "--review", self.review, "--policy", self.policy)
        self.assertEqual(code, 0, err)
        grant = json.loads(self.policy.read_text(encoding="utf-8"))["grants"][0]
        self.assertEqual(grant["actor_account_id"], "viewer-b")
        PurposePolicy(json.loads(self.policy.read_text(encoding="utf-8")))

    def test_too_many_checked_is_rejected(self):
        many = [event(n) for n in range(1, pg.MAX_GRANTS + 2)]
        self.cli("list", "--evidence", self.write_evidence(many, "many.jsonl"), "--out", self.review)
        text = self.review.read_text(encoding="utf-8").replace("- [ ] 允許送進 AI", "- [x] 允許送進 AI")
        self.review.write_text(text, encoding="utf-8")
        code, _, err = self.cli("apply", "--review", self.review, "--policy", self.policy)
        self.assertEqual(code, 2)
        self.assertIn(str(pg.MAX_GRANTS), err)


class ReaderPagingTests(unittest.TestCase):
    """用假的讀取元件確認：空白查詢瀏覽全部、以 source_event_id 分頁、失敗不當成空清單。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.events = [event(n) for n in range(1, 231)]
        (self.dir / "events.json").write_text(json.dumps(self.events, ensure_ascii=False), encoding="utf-8")
        (self.dir / "reader.py").write_text(
            "import json,sys\n"
            "q=json.load(sys.stdin)\n"
            "mode=open('mode.txt').read().strip() if __import__('os').path.exists('mode.txt') else 'ok'\n"
            "if mode=='fail':\n print(json.dumps({'status':'denied','reason_code':'x'}));sys.exit(4)\n"
            "assert q['text']=='' and q['conditions']=={} and q['layer']=='raw'\n"
            "ev=[e for e in json.load(open('events.json',encoding='utf-8')) if e['source_event_id'] not in q['exclude_event_ids']]\n"
            "ev=ev[:q['limit']]\n"
            "print(json.dumps({'status':'ok' if ev else 'no_match','evidence':ev},ensure_ascii=False))\n",
            encoding="utf-8")
        self.spec = self.dir / "reader.json"
        self.spec.write_text(json.dumps({"domain": "channel_knowledge", "command": [sys.executable, "-B", "-X", "utf8", "reader.py"],
                                         "cwd": ".", "scope": self.events[0]["scope"]}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_pages_through_all_records(self):
        records = pg.fetch_records(pg.read_reader_spec(self.spec))
        self.assertEqual(len(records), 230)
        self.assertEqual(len({r["record_id"] for r in records}), 230)

    def test_reader_failure_is_an_error_not_empty_list(self):
        (self.dir / "mode.txt").write_text("fail", encoding="utf-8")
        with self.assertRaises(pg.GrantToolError):
            pg.fetch_records(pg.read_reader_spec(self.spec))

    def test_spec_rejects_non_raw_layer(self):
        bad = json.loads(self.spec.read_text(encoding="utf-8"))
        bad["layer"] = "published"
        self.spec.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(pg.GrantToolError):
            pg.read_reader_spec(self.spec)


if __name__ == "__main__":
    unittest.main()
