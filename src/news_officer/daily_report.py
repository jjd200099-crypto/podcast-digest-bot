"""Explain an incomplete digest without pretending there were no releases."""

import re
from collections import Counter

from .models import DailyItem


def reader_episode(markdown: str, document_url: str = '') -> str:
    """Group one episode's reading and listening links below its prose."""
    content = render_daily_summary(markdown, reader_mode=True)
    lines, links = [], []
    for line in content.splitlines():
        clean = line.strip().removeprefix('**').removesuffix('**').strip()
        if re.match(r'^(?:链接|节目链接|收听链接)：', clean):
            value = clean.split('：', 1)[1].strip()
            links.append(f'[收听节目]({value})' if re.fullmatch(r'https?://\S+', value) else value)
            continue
        if clean.startswith('节目：') or (not any(v.strip() for v in lines) and clean and not clean.startswith('#')):
            line = '**' + clean + '**'
        lines.append(line)
    if document_url:
        lines.extend(['', f'[精读与完整中文对谈]({document_url})'])
    if links:
        lines.extend(['', *dict.fromkeys(links)])
    return '\n'.join(lines).strip()


def episode_document_url(episode, documents):
    exact = [d for d in documents if d.get('episode_id') == episode.id]
    # Older frozen document results have no episode_id. Match both exact title
    # and show, never positional order (some episodes have no reading document).
    matches = exact or [d for d in documents if not d.get('episode_id') and
                       d['title'].startswith(f'{episode.title}｜{episode.show}｜')]
    if len(matches) > 1:
        raise ValueError('Ambiguous episode document mapping')
    return matches[0]['url'] if matches else ''


def render_daily_summary(markdown: str, *, discovered: bool = False, reader_mode: bool = False) -> str:
    """Presentation migration only; never rewrite an immutable archived digest."""
    # The generator's structural marker is not a useful reader-facing heading.
    marker = '发现渠道：Podwise 扩展发现\n\n'
    if reader_mode:
        markdown = markdown.removeprefix(marker)
    elif discovered and not markdown.startswith(marker):
        markdown = marker + markdown
    markdown = markdown.replace('\n## 内容解读\n', '\n')
    markdown = re.sub(
        r'(?m)(^推荐理由：[^\r\n]*?)（AI \d+/40，投资 \d+/20，研究关联 \d+/5，增量 \d+/20，论据 \d+/15）[ \t]*$',
        r'\1', markdown)
    markdown = re.sub(r'(?m)^(推荐星级：[★☆]{5})（[1-5]/5，编辑推荐）[ \t]*$', r'\1', markdown)
    advice = {5: '值得编译', 4: '值得看全文', 3: '看摘要', 2: '无关', 1: '无关'}
    # Keep archived scores for ordering/audit, but show only the reading action.
    return re.sub(r'(?m)^推荐星级：([★☆]{5})(?:｜(?:值得编译|值得看全文|看摘要即可|看摘要|可跳过|无关))?[ \t]*$',
                  lambda m: '阅读建议：' + advice.get(m[1].count('★'), '未评级'), markdown)


def ranked_daily_items(items: list[DailyItem]) -> list[DailyItem]:
    """Stable high-to-low star order, also after persistence/restart."""
    def key(item):
        rating = re.search(r'(?m)^推荐星级：([★☆]{5})', item.message)
        return (item.status != 'summarized', -rating.group(1).count('★') if rating else 0)
    return sorted(items, key=key)


def coverage_report(items: list[DailyItem]) -> str | None:
    notices = [item.message for item in items if item.status == 'discovery_status']
    filtered = sum(item.status == 'discovery_filtered' for item in items)
    if filtered:
        notices.append(f'扩展发现另有 {filtered} 期已读取并归档全文，但未达到推荐门槛，未纳入正文。')
    tracked_filtered = sum(item.status == 'editorial_filtered' for item in items)
    if tracked_filtered:
        notices.append(f'关注列表另有 {tracked_filtered} 期已读取并归档全文，但与 AI 研究的相关性或信息密度不足，未纳入正文。')
    core_items = [i for i in items if i.status not in {'discovery_status', 'discovery_filtered', 'editorial_filtered'}]
    core = _coverage_report(core_items) if core_items or not (filtered or tracked_filtered) else None
    if core:
        notices.append(core)
    return '\n\n'.join(notices) or None


def _coverage_report(items: list[DailyItem]) -> str | None:
    counts = Counter(item.status for item in items)
    reasons = {
        "no_transcript": "未取得完整文字稿，本次不摘要、不评级",
        "unverified_date": "发布日期未核验，暂不纳入本期",
        "summary_format_error": "已有全文，摘要格式校验未通过，待重试",
    }
    pending = [item for item in items if item.status in reasons]
    filtered = counts['not_recommended']
    if not pending:
        if filtered:
            return (f"本次已生成 {counts['summarized']} 期推荐摘要；另有 {filtered} 期已有全文，"
                    "但未达到 AI、投资相关性或内容质量门槛，本次不推送。筛选不等于没有更新。")
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
    if filtered:
        lines.append(f"- {filtered} 期：已有全文，但未达到相关性或质量门槛，本次不推荐。")
    lines.extend(["", "待处理节目（最多列出 6 期）：", ""])
    for item in pending[:6]:
        # Metadata is untrusted display text, not Markdown instructions.
        title = (
            " ".join(item.episode.title.split())
            .replace("[", "（")
            .replace("]", "）")[:180]
        )
        detail = item.message if item.status == 'no_transcript' else ''
        lines.append(f"- {item.episode.show}｜{title}\n  {item.episode.url}" +
                     (f"\n  {detail}" if detail else ''))
    if len(pending) > 6:
        lines.append(f"- 另有 {len(pending) - 6} 期待处理。")
    lines.extend(
        [
            "",
            "以上为本次实际处理的候选。缺全文的节目保留待办，转写完成后补充摘要；不会仅因超出原24小时窗口而丢失。",
        ]
    )
    return "\n".join(lines)
