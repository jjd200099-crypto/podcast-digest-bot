import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_summarizer import EPISODE, TRANSCRIPT, VALID_SUMMARY, summarizer_for

from news_officer.deepread import INSTRUCTIONS as DEEPREAD
from news_officer.document_transcript import INSTRUCTIONS as TRANSCRIPT_PROMPT
from news_officer.glossary import GLOSSARY_INSTRUCTIONS


class EnglishTermTests(unittest.TestCase):
    def test_document_prompts_preserve_common_terms_and_names(self):
        for prompt in (DEEPREAD, TRANSCRIPT_PROMPT):
            for rule in ("investment memo", "人名", "公司", "产品", "不音译"):
                self.assertIn(rule, prompt)

    def test_daily_generation_and_compression_include_shared_rule(self):
        instance = summarizer_for(VALID_SUMMARY, '{"paragraphs": ["Sam Altman 在 OpenAI 写 investment memo。", "这份文档记录判断。"]}')
        with patch.dict("os.environ", {"NEWS_OFFICER_DAILY_LAYOUT": "compact"}):
            instance.summarize(EPISODE, TRANSCRIPT)
        instance.rewrite_paragraphs(["Sam Altman 在 OpenAI 写 investment memo。", "这份文档记录判断。"], [])
        self.assertEqual(len(instance.client.responses.calls), 2)
        for call in instance.client.responses.calls:
            self.assertIn(GLOSSARY_INSTRUCTIONS, call["instructions"])
