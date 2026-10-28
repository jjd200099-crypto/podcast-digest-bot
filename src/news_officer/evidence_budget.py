"""Context budget reserved for evidence, leaving room for tools and dialogue."""
import math
import os
import re

MAX_EVIDENCE_TOKENS = int(os.environ.get('QA_EVIDENCE_TOKENS', '120000'))


def estimate_tokens(text):
    # No tokenizer dependency in requirements. Han/Kana/Hangul cost roughly one
    # token per character; round each non-CJK span up instead of down.
    return sum(len(part) if re.match(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]', part)
               else math.ceil(len(part) / 4)
               for part in re.findall(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+|[^\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+', text))
