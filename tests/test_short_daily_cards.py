"""Short reader-facing daily cards: gold sample caps, expansions, compact skip."""
import unittest
from unittest.mock import Mock, patch

from news_officer.daily_report import compact_paragraphs, reader_episode_v4
from news_officer.models import DailyItem, Episode
from news_officer.summarizer import (
    DAILY_BODY_HARD_MAX_CHARS,
    DAILY_CARD_COMBINED_HARD_MAX_CHARS,
    DAILY_TITLE_HARD_MAX_CHARS,
    _valid_editorial_summary,
    _valid_short_daily_summary,
    _valid_v4_summary,
    strip_daily_card_expansions,
)

GOLD_TITLE = "Cowork 有个容易漏算的变量：企业做的任务越多，单个任务反而可能越省 token。"
GOLD_BODY = (
    "Adam Jacob，The Enterprise AI Show，11 月 18 日。建议直接听 36:17–40:11。"
    "Swamp Club 创始人举了代码审查的例子：模型只判断该跑哪些测试，跑测试和存结果交给普通代码，"
    "他说 token 成本降到约十分之一，不含人工复核和整体交付成本。"
    "这是单一案例、自报数字，流程变便宜不等于每个任务给模型商带来的收入不变。"
)
GOLD_CARD = GOLD_TITLE + "\n\n" + GOLD_BODY
GOLD_SUMMARY = (
    f"## {GOLD_TITLE}\n"
    "节目：The Enterprise AI Show｜Cowork token cost\n"
    "链接：https://example.com/episode\n"
    "时长：50 分钟\n"
    f"简介：{GOLD_BODY}\n"
    "推荐理由：用代码审查例子说明任务增加时单任务 token 成本可能下降。\n"
    "推荐星级：★★★★☆"
)
LONG_QUOTE_IMPLICATION = (
    GOLD_BODY
    + "两句原话：“We turn it back into code again.”（把固定流程写回代码。）"
    "“Here is the schema it must match.”（模型的输出必须符合规定格式。）"
    "**对 Anthropic/OpenAI 的含义：**Cowork 能否拉动 ARR，要同时看付费任务数和每个任务的模型收入。"
    "不能把席位增长直接乘上不变的 token 消耗。NBER 的逾 50 万开发者研究也有类似落差："
    "自主编码工具累计带来的提交量 +240%，真正发布的版本 +30%。"
)


class ShortDailyCardTests(unittest.TestCase):
    def test_gold_sample_lengths_are_within_recalibrated_caps(self):
        self.assertEqual(len(GOLD_TITLE), 43)
        self.assertEqual(len(GOLD_BODY), 181)
        self.assertEqual(len(GOLD_CARD), 226)
        self.assertLessEqual(len(GOLD_TITLE), DAILY_TITLE_HARD_MAX_CHARS)
        self.assertLessEqual(len(GOLD_BODY), DAILY_BODY_HARD_MAX_CHARS)
        self.assertLessEqual(len(GOLD_CARD), DAILY_CARD_COMBINED_HARD_MAX_CHARS)

    def test_gold_summary_is_accepted_and_old_v4_points_remain_archival(self):
        self.assertTrue(_valid_short_daily_summary(GOLD_SUMMARY))
        self.assertTrue(_valid_editorial_summary(GOLD_SUMMARY))
        self.assertFalse(_valid_v4_summary(GOLD_SUMMARY))

    def test_gold_card_renders_title_and_one_body_paragraph(self):
        episode = Episode(
            "gold", "Cowork token cost", "https://example.com/episode",
            "The Enterprise AI Show")
        rendered = reader_episode_v4(
            DailyItem(episode, "summarized", GOLD_SUMMARY), 1, "https://example.com/doc")
        self.assertIn(f"**1. {GOLD_TITLE}**", rendered)
        self.assertIn(GOLD_BODY, rendered)
        self.assertNotIn("要点总结", rendered)
        self.assertNotIn("推荐理由", rendered)
        self.assertNotIn("- 简介：", rendered)
        self.assertIn("[精读和完整中文对谈](https://example.com/doc)", rendered)
        self.assertIn("[收听节目](https://example.com/episode)", rendered)

    def test_quote_and_implication_block_is_rejected_and_stripped(self):
        stuffed = GOLD_SUMMARY.replace(GOLD_BODY, LONG_QUOTE_IMPLICATION)
        self.assertFalse(_valid_short_daily_summary(stuffed))
        stripped = strip_daily_card_expansions(GOLD_TITLE + "\n\n" + LONG_QUOTE_IMPLICATION)
        self.assertEqual(stripped, GOLD_TITLE + "\n\n" + GOLD_BODY)
        self.assertNotIn("两句原话", stripped)
        self.assertNotIn("的含义", stripped)
        self.assertNotIn("NBER", stripped)
        self.assertIn("不含人工复核和整体交付成本", stripped)

        episode = Episode(
            "gold", "Cowork token cost", "https://example.com/episode",
            "The Enterprise AI Show")
        rendered = reader_episode_v4(DailyItem(episode, "summarized", stuffed), 1)
        self.assertIn("不含人工复核和整体交付成本", rendered)
        self.assertNotIn("两句原话", rendered)
        self.assertNotIn("对 Anthropic/OpenAI 的含义", rendered)
        self.assertNotIn("NBER", rendered)

    def test_prompt_keeps_material_hedges_and_moves_expansions_to_deepread(self):
        from test_summarizer import EPISODE, TRANSCRIPT, summarizer_for

        from news_officer.deepread import INSTRUCTIONS as DEEPREAD

        instance = summarizer_for(GOLD_SUMMARY)
        with patch.dict("os.environ", {"NEWS_OFFICER_DAILY_LAYOUT": "v4"}):
            self.assertEqual(instance.summarize(EPISODE, TRANSCRIPT), GOLD_SUMMARY)
        instructions = instance.client.responses.calls[0]["instructions"]
        self.assertIn("不含人工复核和整体交付成本", instructions)
        self.assertIn("单一案例", instructions)
        self.assertIn("目标 160–200 字", instructions)
        self.assertIn("硬上限 220 字", instructions)
        self.assertIn("合计硬上限 260 字", instructions)
        self.assertIn("不要写“要点总结：”", instructions)
        self.assertIn("对 … 的含义", instructions)
        self.assertNotIn("篇幅可稍长", instructions)
        self.assertIn("日报卡片只保留一句判断", DEEPREAD)
        self.assertIn("不得因此缩短", DEEPREAD)

    def test_compact_rewriter_skips_already_short_single_paragraph(self):
        markdown = f"## {GOLD_TITLE}\n节目：Show｜Title\n\n{GOLD_BODY}\n\n推荐理由：有具体例子。"
        model = Mock()
        audit = []
        self.assertEqual(compact_paragraphs(markdown, model, audit), markdown)
        model.rewrite_paragraphs.assert_not_called()
        self.assertEqual(audit[0]["skipped"], "already-within-short-card-caps")

    def test_trim_keeps_longest_prefix_not_last_sentence(self):
        from news_officer.summarizer import trim_daily_card_body

        body = (
            "Shopify CEO Tobi Lütke 把 AI 带来的变化描述为一次从“手写代码”到“定义问题、约束系统并验证结果”的职业重构。"
            "他以 Shopify 内部的 agentic factories、代码仓库改造和 internal tools 为例，说明 AI 不只是提升现有开发流程的速度，也让此前不值得启动的大型重构变成低成本实验。"
            "节目开头围绕 Jensen Huang 的讨论则完全建立在主持人与 Lütke 对未经证实传闻的推测上，不能视为事实信息。"
        )
        trimmed = trim_daily_card_body(body)
        self.assertTrue(trimmed.startswith("Shopify CEO Tobi Lütke"))
        self.assertIn("agentic factories", trimmed)
        self.assertNotIn("Jensen Huang", trimmed)
        self.assertLessEqual(len(trimmed), 220)

