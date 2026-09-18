"""Deterministic delivery checks, not a claim of semantic correctness."""

import re


def completeness_error(text: str) -> str | None:
    value = text.strip()
    if not value:
        return "Empty answer"
    last = value.splitlines()[-1].strip()
    if re.search(r"[:：]\s*$", last):
        return "Unfinished introduction: provide the promised content after the colon"
    if re.fullmatch(r"(?:[-*+]\s*|\d+[.)、]\s*|#{1,6}\s*.+)", last):
        return "Unfinished list or heading: provide its body"
    if len(re.findall(r"^\s*```", value, re.MULTILINE)) % 2:
        return "Unclosed code block"
    if re.fullmatch(r"(?:好的[，。]?|收到[，。]?|可以[，。]?)?\s*(?:我会|我来|我将|让我).{0,65}(?:整理|分析|检查|查询|总结|处理)(?:一下)?[。！!]*", value):
        return "Promise without delivery: do the work now or state the specific dependency"
    return None


def require_complete(text: str) -> None:
    if error := completeness_error(text):
        raise ValueError(error)
