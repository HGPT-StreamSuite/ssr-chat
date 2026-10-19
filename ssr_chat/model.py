from __future__ import annotations

from .models import AnswerPrompt


class FixtureModelClient:
    """Deterministic, evidence-only model stand-in for offline tests."""

    def generate(self, prompt: AnswerPrompt) -> str:
        successful = [
            result
            for result in prompt.query_results
            if result.get("status") == "ok"
        ]
        material_lines = [
            str(material.get("text", "")).strip()
            for result in successful
            for material in result.get("materials", [])
            if material.get("text")
        ]
        if not material_lines:
            if any(
                result.get("status") == "tool_error"
                for result in prompt.query_results
            ):
                return "查詢工具暫時無法提供資料；目前沒有足夠可確認的內容，暫不作答。"
            return "本機 fixture 沒有找到足夠資料，已留下知識缺口；暫不作答。"

        answer = "；".join(material_lines)
        failed = [
            result
            for result in prompt.query_results
            if result.get("status") != "ok"
        ]
        if failed:
            answer += "；其他查詢未取得資料，暫不補猜。"
        return answer
