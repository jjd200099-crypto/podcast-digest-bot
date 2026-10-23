import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.research_agent import INSTRUCTIONS, TOOLS, plain_chat_text


class ResearchAgentPromptBoundaryTests(unittest.TestCase):
    def test_plain_qa_style(self):
        for rule in ("先给结论", "每点一句话", "飞书原生无序列表", "不堆标题", "不加括号解释", "不堆行话"):
            self.assertIn(rule, INSTRUCTIONS)

    def test_plain_formatter_keeps_english_names_and_claims(self):
        self.assertEqual(
            plain_chat_text("### **结论**\nSam Altman 在 OpenAI 写 investment memo。"),
            "结论\nSam Altman 在 OpenAI 写 investment memo。",
        )

    def test_instructions_redirect_system_changes_to_codex(self):
        self.assertIn("能力边界", INSTRUCTIONS)
        self.assertIn("我改不了代码", INSTRUCTIONS)
        self.assertIn("@codex", INSTRUCTIONS)
        self.assertIn("能做", INSTRUCTIONS)
        self.assertIn("不能做", INSTRUCTIONS)
        self.assertIn("record_editorial_feedback", INSTRUCTIONS)
        # Feature/code changes must not be framed as pending personal review that ships.
        self.assertIn("不要说「个人建议待审核」", INSTRUCTIONS)
        self.assertIn("不要用 record_editorial_feedback", INSTRUCTIONS)

    def test_source_list_requires_exact_count_every_name_and_plain_output(self):
        for rule in ("精确数量（逐项计数）", "每一个节目名称，不得遗漏",
                     "绝不提及 A/B、priority、筛选政策、价值筛选",
                     "包括「不再按 A/B…」等免责声明", "不要写「来源」引用区块",
                     "不要长篇开场"):
            self.assertIn(rule, INSTRUCTIONS)

    def test_editorial_feedback_tool_is_preference_only(self):
        tool = next(t for t in TOOLS if t["name"] == "record_editorial_feedback")
        description = tool["description"]
        self.assertIn("表达偏好", description)
        self.assertIn("不会改代码", description)
        self.assertIn("请勿用本工具", description)

    def test_feature_request_tool_defers_concrete_changes_to_codex(self):
        tool = next(t for t in TOOLS if t["name"] == "record_feature_request")
        self.assertIn("@codex", tool["description"])


if __name__ == "__main__":
    unittest.main()
