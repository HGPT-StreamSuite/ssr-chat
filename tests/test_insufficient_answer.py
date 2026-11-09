"""「資料不足」回答：沒有引用時只以固定標準句放行；其他無引用回答照舊擋下。"""
import unittest

from ssr_chat.models import AnswerPrompt
from ssr_chat.modular_answer import STANDARD_INSUFFICIENT
from ssr_chat.ollama_model import OllamaModelClient

from test_modular_answer import ModularAnswerTests


class InsufficientAnswerTests(ModularAnswerTests):
    def test_uncited_refusal_is_published_as_standard_sentence(self):
        for raw in ("目前提供的資料不足，無法確認。", "目前提供的資料不足，無法確認訂閱一個月多少錢。",
                    "根據目前提供的資料，無法確認 Discord 的會員人數。"):
            with self.subTest(raw=raw):
                self.model.generate.return_value = raw
                result = self.answer()
                self.assertEqual(result["status"], "answered")
                self.assertEqual(result["answer"], STANDARD_INSUFFICIENT)
                self.assertEqual(result["cited_evidence_ids"], [])
                self.assertTrue(result["insufficient_evidence"])
                finished = [t for t in self.trace if t.get("phase") == "finished"][-1]
                self.assertEqual(finished["answer_normalized"], "standard_insufficient_evidence")
                self.assertEqual(finished["adapter_answer"], raw)

    def test_uncited_outside_fact_next_to_refusal_does_not_leak(self):
        self.model.generate.return_value = "RTX 4070 的記憶體是 12 GB。目前提供的資料不足，無法確認。"
        result = self.answer()
        self.assertEqual(result["answer"], STANDARD_INSUFFICIENT)
        self.assertNotIn("12", result["answer"])

    def test_cited_partial_answer_is_kept_as_is(self):
        text = "開台時間是 10:00 [E1]，但目前提供的資料不足，無法確認結束時間。"
        self.model.generate.return_value = text
        result = self.answer()
        self.assertEqual((result["status"], result["answer"], result["cited_evidence_ids"]), ("answered", text, ["E1"]))
        self.assertNotIn("insufficient_evidence", result)

    def test_unknown_citation_with_refusal_is_still_held(self):
        for raw in ("目前提供的資料不足，無法確認 [E999]。", "目前提供的資料不足 [E", "10:00"):
            with self.subTest(raw=raw):
                self.model.generate.return_value = raw
                result = self.answer()
                self.assertEqual(result["reason_code"], "answer_citations_invalid")
                self.assertIsNone(result["answer"])

    def test_prompt_forbids_combining_records_and_citing_refusals(self):
        prompt = AnswerPrompt(user_text="q", context_snapshot_id="s", context_messages=(), analysis={"intents": ["channel_knowledge"]},
                              query_results=({"status": "ok", "materials": [], "sources": []},), evidence_citations=True)
        system = OllamaModelClient("http://127.0.0.1:11434", "m").request_body(prompt)["messages"][0]["content"]
        for phrase in ("不要把多筆資料湊在一起推算", "不要附引用", "不要加入資料以外的建議"):
            self.assertIn(phrase, system)


del ModularAnswerTests

if __name__ == "__main__":
    unittest.main()
