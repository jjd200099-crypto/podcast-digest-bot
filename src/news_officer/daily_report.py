"""Explain an incomplete digest without pretending there were no releases."""

import json
import math
import os
import re
from collections import Counter

from .models import DailyItem
from .strict_selection import strict_document_allowed, strict_selection_enabled
from .summarizer import remove_editorial_leadins


def source_numbers(text):
    """Extract numeric expressions without an episode-specific vocabulary."""
    compact = re.sub(r'\s+', '', text)
    numerals = r'[零〇一二三四五六七八九十百千万亿两]+'
    values = set(re.findall(r'\d+(?:[.,]\d+)*|' + numerals +
                            r'(?=[个年月周日天人项倍亿万岁秒分])', compact))
    for start, end in re.findall('(' + numerals + r')[到至](' + numerals + ')', compact):
        values.update((start, end))
    return values


def missing_source_numbers(source, notes):
    """Keep digit boundaries; Chinese quantities can share a written numeral."""
    missing = source_numbers(source) - source_numbers(notes)
    compact = re.sub(r'\s+', '', notes)
    return {value for value in missing if value.isascii() or value not in compact}


def summary_paragraph_spans(markdown):
    """Locate prose only, retaining every surrounding byte of the layout."""
    return [match for match in re.finditer(r'(?m)^[^\n]+$', markdown)
            if match.group().strip() and not match.group().strip().startswith(
                ('#', '**', '节目：', '时长：', '链接：', '节目链接：', '收听链接：', '推荐理由：', '阅读建议：', '['))]


def required_prose_names(source):
    """Protect attribution and numbered names, allowing secondary examples to go."""
    names = re.findall(r'[A-Z][A-Za-z0-9]*(?:[ -][A-Za-z0-9]+)*', source)
    required = {name for name in names if re.search(r'\d', name)}
    if names and source.startswith(names[0]):
        required.add(names[0])
    required.update(re.findall(r'([A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*)?)\s*(?:认为|称|说|表示|主张|提醒|建议)', source))
    return sorted(required)


def claim_owner_counts(source):
    """Keep distinct attributed claims from silently changing their owner."""
    return Counter(re.findall(r'([A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*)?)\s*(?:也|更)?(?:认为|称|说|表示|主张|提醒|建议|强调)', source))


def compact_ratio():
    """Ratio is configurable only for the opt-in compact edition."""
    ratio = float(os.environ.get('NEWS_OFFICER_DAILY_COMPACT_TARGET_RATIO', '0.4'))
    if not 0 < ratio < 1:
        raise ValueError('Compact target ratio must be between zero and one')
    return ratio


def validate_conditionals(source, short):
    """Protect conditional scope against telegraphic clipping."""
    for connector in ('若', '如果', '否则', '只有', '除非'):
        if connector in source and connector not in short:
            raise ValueError('Retain conditional connector: ' + connector)
    duration = r'(?:数|几|多|\d+|[一二三四五六七八九十两]+)(?:天|周|月|年)'
    for match in re.finditer(duration + r'(?:或' + duration + r')?([\u4e00-\u9fff]{2,4})(?=若|如果)', short):
        if not match.group(1).startswith('的'):
            raise ValueError('Use 的 between a duration modifier and the activity noun in a conditional subject: ' + match.group())
    source_clauses = re.findall(r'(?:若|如果)[^。！？；]+', source)
    short_clauses = re.findall(r'(?:若|如果)[^。！？；]+', short)
    if len(short_clauses) < len(source_clauses):
        raise ValueError('Retain all conditional antecedents')
    for index, clause in enumerate(short_clauses):
        if not re.search(r'则|那么|才|就|，', clause):
            raise ValueError('Conditional needs explicit result connector 则/才/就 or comma')
        if (index < len(source_clauses) and re.search(r'能(?!力)', re.split(r'，|则|那么|就|才', source_clauses[index], maxsplit=1)[0])
                and not re.search(r'能(?!力)', re.split(r'，|则|那么|就|才', clause, maxsplit=1)[0])):
            raise ValueError('Retain conditional modal 能 and grammatical function words')


def validate_unclipped_prose(source, short):
    """Reject novel contractions made by deleting the middle of verb phrases."""
    verbs = '降低|提高|减少|增加|推动|削弱|建立|完成|保持|解决|发展|进行'
    for phrase in re.findall(r'(?:' + verbs + r')[\u4e00-\u9fff]{2}', source):
        clipped = phrase[0] + phrase[-1]
        if clipped in short and clipped not in source:
            raise ValueError('Do not clip grammatical verb/object phrase: ' + phrase + '; keep function words and complete words')


def compact_paragraphs(markdown, rewriter=None, audit=None):
    """Compress prose with validation; fallback selects complete source sentences."""
    spans = summary_paragraph_spans(markdown)
    originals = [span.group() for span in spans]
    if not originals:
        return markdown
    ratio = compact_ratio()
    errors = []
    paragraphs = None
    count_failures = 0
    for _ in range(6):
        if rewriter is None:
            break
        candidate = None
        try:
            candidate = rewriter.rewrite_paragraphs(originals, errors)
            if not isinstance(candidate, list) or len(candidate) < 2:
                count_failures += 1
                if count_failures == 2:
                    errors.append('Expected exactly two paragraphs; using fallback')
                    break
                raise ValueError('Return exactly two summary paragraphs')
            candidate = [re.sub(r'(?<=[\u4e00-\u9fff，。；：]) +| +(?=[\u4e00-\u9fff，。；：])', '', p).strip()
                         if isinstance(p, str) else p for p in candidate]
            if not all(isinstance(p, str) for p in candidate):
                raise ValueError('Paragraphs must be strings')
            if len([p for p in candidate if p.strip()]) < 2:
                count_failures += 1
                if count_failures == 2:
                    errors.append('Expected exactly two nonempty paragraphs; using fallback')
                    break
                raise ValueError('Return exactly two nonempty summary paragraphs')
            issues = []
            if any(name not in ' '.join(candidate) for source in originals for name in required_prose_names(source)):
                issues.append('Keep required English proper nouns in their original spelling')
            source_length = sum(map(len, originals))
            output_length = sum(map(len, candidate))
            if not ratio * .85 <= output_length / source_length <= ratio * 1.15:
                issues.append(f'Use natural prose at ratio {ratio}: source={source_length}, output={output_length}, allowed={math.ceil(source_length * ratio * .85)}–{math.floor(source_length * ratio * 1.15)}; cut at least {max(0, output_length - round(source_length * ratio))} characters to reach target, including EVERY English letter, punctuation and space')
            pairs = (zip(originals, candidate, strict=True) if len(originals) == len(candidate)
                     else [(' '.join(originals), ' '.join(candidate))])
            if any(not p or '\n' in p or re.match(r'\s*(?:[-•#*]|\d+[.、)])', p) for p in candidate):
                issues.append('Keep natural prose paragraphs without bullets or extra newlines')
            for source, short in pairs:
                if (not isinstance(short, str) or not short.strip() or '\n' in short
                        or re.match(r'\s*(?:[-•#*]|\d+[.、)])', short)):
                    issues.append('Keep natural prose paragraphs without bullets or extra newlines')
                    continue
                for validate in (validate_conditionals, validate_unclipped_prose):
                    try:
                        validate(source, short)
                    except ValueError as error:
                        issues.append(str(error))
                if missing_source_numbers(source, short):
                    issues.append('Missing source numbers: ' + str(sorted(missing_source_numbers(source, short))))
                if source_numbers(short) - source_numbers(source):
                    issues.append('Do not introduce numbers')
                source_owners, short_owners = claim_owner_counts(source), claim_owner_counts(short)
                for owner, count in source_owners.items():
                    if short_owners[owner] < count:
                        issues.append(f'Retain distinct claim ownership: {owner} needs {count} explicit attributions; do not attach the later claim to another subject')
                names = required_prose_names(source)
                if any(name not in short for name in names):
                    issues.append('Keep required English proper nouns in their original spelling')
                for hedge in ('认为', '可能', '若', '称', '不认同', '仅'):
                    required = bool(re.search(r'(?<!不)仅', source)) if hedge == '仅' else hedge in source
                    present = bool(re.search(r'(?<!不)仅', short)) if hedge == '仅' else hedge in short
                    if required and not present:
                        issues.append('Retain attribution/hedging: ' + hedge)
            if issues:
                raise ValueError('\n'.join(issues))
            paragraphs = candidate
            break
        except ValueError as error:
            errors.append(str(error) + '\nPrevious candidate to revise: ' + json.dumps(candidate, ensure_ascii=False))
        except Exception as error:  # noqa: BLE001 - Do not retry transport failures here.
            errors.append(str(error))
            break
    fallback = paragraphs is None
    if fallback:
        paragraphs = []
        for source in originals:
            sentences = re.findall(r'[^。！？]+[。！？]?(?:[”’」』])?', source)
            # Whole sentences preserve attribution and conditional scope. Protect all
            # numeric, English-name and hedge sentences before filling the budget.
            chosen = {0}
            for index, sentence in enumerate(sentences):
                if (source_numbers(sentence) or re.search(r'[A-Z]', sentence)
                        or any(word in sentence for word in ('认为', '可能', '若', '如果', '否则', '只有', '除非', '则', '称', '不认同', '仅'))):
                    chosen.add(index)
            for index in range(len(sentences)):
                if sum(len(sentences[i]) for i in chosen) >= len(source) * ratio:
                    break
                chosen.add(index)
            paragraphs.append(''.join(sentence for index, sentence in enumerate(sentences) if index in chosen))
    paragraphs = exactly_two_paragraphs(paragraphs)
    replacements = ['\n\n'.join(paragraphs)] + [''] * (len(spans) - 1)
    result = markdown
    for span, short in reversed(list(zip(spans, replacements, strict=True))):
        result = result[:span.start()] + short + result[span.end():]
    if audit is not None:
        audit.append({'model': getattr(rewriter, 'model', None), 'fallback': fallback,
                      'target_ratio': ratio, 'validation_errors': errors, 'before_lengths': list(map(len, originals)),
                      'after_lengths': list(map(len, paragraphs)), 'paragraphs': paragraphs,
                      'missing_numbers': sorted(missing_source_numbers(' '.join(originals), ' '.join(paragraphs)))})
    return result


def exactly_two_paragraphs(paragraphs):
    """Merge surplus prose; split a lone paragraph only at sentence boundaries."""
    if len(paragraphs) >= 2:
        merged = paragraphs[1].strip()
        for paragraph in paragraphs[2:]:
            right = paragraph.strip()
            separator = ' ' if re.search(r'[A-Za-z0-9]$', merged) and re.match(r'[A-Za-z0-9]', right) else ''
            merged += separator + right
        return [paragraphs[0], merged]
    prose = paragraphs[0]
    sentences = re.findall(r'[^。！？]+[。！？]?(?:[”’」』])?', prose)
    if len(sentences) >= 2:
        split = max(1, len(sentences) // 2)
        return [''.join(sentences[:split]), ''.join(sentences[split:])]
    # An indivisible claim stays in a single paragraph.
    return [prose]


def normalize_duration(value):
    """Round positive known durations to minutes; retain unparseable text."""
    value = str(value or '').strip()
    seconds = None
    if re.fullmatch(r'\d+(?:\.\d+)?', value):
        seconds = float(value)
    elif re.fullmatch(r'\d+:\d{2}(?::\d{2})?', value):
        parts = [int(part) for part in value.split(':')]
        seconds = sum(part * 60 ** index for index, part in enumerate(reversed(parts)))
    else:
        match = re.fullmatch(r'(?:(\d+)\s*小时)?\s*(?:(\d+)\s*分(?:钟)?)?\s*(?:(\d+)\s*秒)?', value)
        if match and any(match.groups()):
            hours, minutes, secs = (int(part or 0) for part in match.groups())
            seconds = hours * 3600 + minutes * 60 + secs
    if seconds is not None:
        return f'{round(seconds / 60)} 分钟' if seconds > 0 else '未提供'
    return value or '未提供'


def episode_summary(item, *, compact=False):
    """In compact mode, prefer archived seconds to generated duration text."""
    if not compact:
        return item.message
    episode = item.episode
    if episode.duration_seconds is not None:
        duration = normalize_duration(episode.duration_seconds)
    elif episode.duration_string:
        duration = normalize_duration(episode.duration_string)
    else:
        match = re.search(r'(?m)^时长：([^\n]*)', item.message)
        duration = normalize_duration(match[1] if match else None)
    line = '时长：' + duration
    if re.search(r'(?m)^时长：', item.message):
        return re.sub(r'(?m)^时长：[^\n]*', lambda _: line, item.message)
    lines = item.message.splitlines()
    index = next((i + 1 for i, value in enumerate(lines) if value.startswith('节目：')), 1)
    lines.insert(index, line)
    return '\n'.join(lines)


def clean_daily_lines(markdown):
    return '\n'.join(line.rstrip() for line in markdown.split('\n'))


def reader_episode(markdown: str, document_url: str = '', *, fulltext: bool = True,
                   rewriter=None, rewrite_audit=None, compact: bool = False) -> str:
    """Group one episode's reading and listening links below its prose."""
    content = render_daily_summary(markdown, reader_mode=True)
    if compact and not re.search(r'(?m)^时长：', content):
        lines = content.splitlines()
        index = next((i + 1 for i, line in enumerate(lines) if line.startswith('节目：')), 1)
        lines.insert(index, '时长：未提供')
        content = '\n'.join(lines)
    if compact:
        content = re.sub(r'(?m)^时长：[^\n]*',
                         lambda m: '时长：' + normalize_duration(m[0][3:]), content)
        content = compact_paragraphs(content, rewriter, rewrite_audit)
    lines, links = [], []
    for line in content.splitlines():
        clean = line.strip().removeprefix('**').removesuffix('**').strip()
        if re.match(r'^(?:链接|节目链接|收听链接)：', clean):
            value = clean.split('：', 1)[1].strip()
            links.append(f'[收听节目]({value})' if re.fullmatch(r'https?://\S+', value) else value)
            continue
        if compact and clean.startswith('节目：'):
            names = clean[3:].split('｜')
            if len(names) == 2 and names[0].strip() == names[1].strip():
                clean = '节目：' + names[0].strip()
                line = clean
        if clean.startswith('节目：') or (not any(v.strip() for v in lines) and clean and not clean.startswith('#')):
            line = '**' + clean + '**'
        lines.append(line)
    if document_url and (not strict_selection_enabled() or strict_document_allowed(markdown)):
        label = '精读和完整中文对谈' if fulltext else '精读'
        lines.extend(['', f'[{label}]({document_url})'])
    if links:
        lines.extend(['', *dict.fromkeys(links)])
    rendered = '\n'.join(lines).strip()
    if compact:
        metadata = []
        body = []
        for line in lines:
            if line.strip().startswith(('推荐理由：', '阅读建议：')):
                metadata.append(line.strip())
            else:
                body.append(line)
        duration = next((i for i, line in enumerate(body) if line.strip().startswith('时长：')), None)
        metadata.sort(key=lambda line: not line.startswith('推荐理由：'))
        if duration is not None:
            body[duration + 1:duration + 1] = metadata
        else:
            body = metadata + body
        rendered = '\n'.join(body).strip()
        rendered = re.sub(r'\n{3,}', '\n\n', rendered)
    return clean_daily_lines(rendered) if compact else rendered


def render_reader_daily(items, documents, *, fulltext: bool = True, ab_preview: bool = False,
                        rewriter=None, rewrite_audit=None, compact: bool = False) -> str:
    """Render fresh episode results without consulting any frozen delivery."""
    blocks = [reader_episode(episode_summary(item, compact=compact), episode_document_url(item.episode, documents),
                             fulltext=fulltext, rewriter=rewriter, rewrite_audit=rewrite_audit,
                             compact=compact) for item in ranked_daily_items(items)
              if item.status == 'summarized']
    if ab_preview:
        blocks.insert(0, 'A/B 测试版，不是正式日报')
    return '\n\n'.join(blocks)


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
        markdown = remove_editorial_leadins(markdown)
    elif discovered and not markdown.startswith(marker):
        markdown = marker + markdown
    markdown = markdown.replace('\n## 内容解读\n', '\n')
    markdown = re.sub(
        r'(?m)(^推荐理由：[^\r\n]*?)（AI \d+/40，投资 \d+/20，研究关联 \d+/5，增量 \d+/20，论据 \d+/15）[ \t]*$',
        r'\1', markdown)
    markdown = re.sub(r'(?m)^(推荐星级：[★☆]{5})（[1-5]/5，编辑推荐）[ \t]*$', r'\1', markdown)
    if '<!-- strict-selection-v1 -->' in markdown:
        markdown = markdown.replace('<!-- strict-selection-v1 -->\n', '')
        markdown = re.sub(r'(?m)^推荐星级：[^\n]*\n?', '', markdown)
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


def compact_reader_episode(content: str, level: int, listening_url: str) -> str:
    """Drop whole optional lines/paragraphs; retain a complete summary and links."""
    lines = [line for line in content.splitlines()
             if not line.strip().startswith(('阅读建议：', '推荐理由：'))]
    if level == 1:
        result = '\n'.join(lines).strip()
    else:
        paragraphs = re.split(r'\n\s*\n', '\n'.join(lines).strip())
        heading, prose, links = [], [], []
        for paragraph in paragraphs:
            # Preserve link lines even when metadata and prose share a paragraph.
            body = []
            for line in paragraph.splitlines():
                if re.search(r'https?://', line):
                    links.append(line)
                elif line.strip().startswith(('**', '#', '时长：', '节目：', '补齐旧节目')):
                    heading.append(line)
                elif line.strip():
                    body.append(line)
            if body:
                prose.append('\n'.join(body))
        if not prose:
            # No identifiable summary: do not manufacture or discard the body.
            return content
        summary = prose[0]
        if level == 3:
            # A whole first sentence is an extractive summary, never a byte slice.
            sentence = re.match(r'.*?[。！？](?:[”’」』])?', summary, re.DOTALL)
            if sentence:
                summary = sentence.group(0)
        result = '\n\n'.join([*heading, summary, *dict.fromkeys(links)])
    if listening_url not in result:
        result += f'\n\n[收听节目]({listening_url})'
    return result
