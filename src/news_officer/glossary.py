"""Source-aware terminology for summaries and Chinese reading copies."""

import re

GLOSSARY_INSTRUCTIONS = (
    "\n术语表：原稿使用的常用英文术语和专有名词保留英文，不自行创造中文译名。investment memo、技术与投资术语，以及人名、公司、产品、模型和论文名称使用原文英文拼写，不音译、不替换为中文名、不添加括号中文释义；中文原名仍保留中文，不猜英文名。"
    "world action model（含 world-action model）统一写 world action model，"
    "不要写世界—动作模型、世界-动作模型或世界动作模型；"
    "world model、flow matching、agentic harness 等也保留英文。"
)


def preserve_english_terms(text, source):
    """Correct only glossary aliases supported by the source, never its archive."""
    if re.search(r"\bworld[ -]action models?\b", source, re.IGNORECASE):
        return re.sub(r"世界[—-]?动作模型|world-action model", "world action model", text)
    return text


def preserve_glossary_values(value, source):
    """Normalize generated text while retaining structure and evidence IDs."""
    if isinstance(value, str):
        return preserve_english_terms(value, source)
    if isinstance(value, list):
        return [preserve_glossary_values(item, source) for item in value]
    if isinstance(value, dict):
        return {key: preserve_glossary_values(item, source) for key, item in value.items()}
    return value
