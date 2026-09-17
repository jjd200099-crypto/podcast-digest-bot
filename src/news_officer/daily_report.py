"""Explain an incomplete digest without pretending there were no releases."""

from collections import Counter

from .models import DailyItem


def coverage_report(items: list[DailyItem]) -> str | None:
    counts = Counter(item.status for item in items)
    reasons = {
        "no_transcript": "未取得完整文字稿，本次不摘要、不评级",
        "unverified_date": "发布日期未核验，暂不纳入本期",
        "summary_format_error": "已有全文，摘要格式校验未通过，待重试",
    }
    pending = [item for item in items if item.status in reasons]
    if not pending:
        if counts["summarized"]:
            return None
        return (
            "本次没有新增可推送的摘要。已推送、冷却重试中或不在追踪时间窗内的节目"
            "不会重复处理；这不代表订阅源没有更新。"
        )
    lines = [
        "## 播客追踪状态",
        "",
        (
            f"本次已生成 {counts['summarized']} 期摘要，另有 {len(pending)} 期候选尚未完成。"
            "以下仅为节目与处理状态，不是内容摘要或推荐。"
        ),
        "",
    ]
    for status, reason in reasons.items():
        if counts[status]:
            lines.append(f"- {counts[status]} 期：{reason}。")
    lines.extend(["", "待处理节目（最多列出 6 期）：", ""])
    for item in pending[:6]:
        # Metadata is untrusted display text, not Markdown instructions.
        title = (
            " ".join(item.episode.title.split())
            .replace("[", "（")
            .replace("]", "）")[:180]
        )
        lines.append(f"- {item.episode.show}｜{title}\n  {item.episode.url}")
    if len(pending) > 6:
        lines.append(f"- 另有 {len(pending) - 6} 期待处理。")
    lines.extend(
        [
            "",
            "以上为本次实际处理的候选，不是全部更新清单。缺全文的节目将在追踪窗口内按重试策略复查。",
        ]
    )
    return "\n".join(lines)
