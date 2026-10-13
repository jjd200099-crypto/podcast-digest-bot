"""Shared strict-mode guards for daily summaries and reading documents."""

import os
import re


def strict_selection_enabled():
    return os.environ.get('NEWS_OFFICER_STRICT_SELECTION', '').lower() in {'1', 'true', 'yes', 'on'}


def strict_document_allowed(markdown):
    match = re.search(r'(?m)^推荐星级：([★☆]{5})', markdown)
    advice = re.search(r'(?m)^阅读建议：([^\n]+)', markdown)
    return bool(match and match[1].count('★') >= 4
                and advice and advice[1].strip() in {'值得编译', '值得看全文'})
