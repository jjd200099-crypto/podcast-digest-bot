"""Deterministic guards complement the contextual model audit."""
import json
import re
import unicodedata

EN = dict(zip(['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen'], range(20)))
EN.update(dict(zip(['twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety'], range(20, 100, 10))))
ZH = dict(zip('零一二三四五六七八九', range(10)))
ZH['两'] = 2


def chinese_number(value):
    total = current = 0
    units = {'十': 10, '百': 100, '千': 1000, '万': 10000, '亿': 100000000}
    if not any(c in units for c in value):
        return int(''.join(str(ZH[c]) for c in value))
    for c in value:
        if c in ZH:
            current = ZH[c]
        elif units[c] >= 10000:
            total = (total + current) * units[c]
            current = 0
        else:
            total += (current or 1) * units[c]
            current = 0
    return total + current


def normalized(text):
    text = unicodedata.normalize('NFKC', text).casefold().replace(',', '')
    words = '|'.join(sorted(EN, key=len, reverse=True))
    text = re.sub(rf'\b(hundred|thousand|million) and (?=(?:{words})\b)', r'\1 ', text)
    pattern = rf'\b(?:{words})(?:[ -]+(?:{words}|hundred|thousand|million))*\b'

    def english(match):
        total = current = 0
        for word in re.split('[ -]+', match[0]):
            if word in EN:
                current += EN[word]
            elif word == 'hundred':
                current = (current or 1) * 100
            else:
                total += (current or 1) * {'thousand': 1000, 'million': 1000000}[word]
                current = 0
        return str(total + current)

    text = re.sub(pattern, english, text)
    text = text.replace('百分之', 'PERCENT_PREFIX')
    text = re.sub(r'一(?=些|起|直|定|样|般|致|旦|方面)', ' ', text)
    text = re.sub(r'[零一二两三四五六七八九十百千万亿]+', lambda m: str(chinese_number(m[0])), text)
    text = re.sub(r'(\d+)(?:点| point )(\d+)', r'\1.\2', text)
    text = re.sub(r'PERCENT_PREFIX\s*(\d+(?:\.\d+)?)', r'\1%', text)
    return re.sub(r'\bpercent(?:age)?\b', '%', text)


def numbers(text):
    text = normalized(text)
    # Propagate the trailing percent unit to both endpoints of a range.
    text = re.sub(r'(\d+(?:\.\d+)?)\s*(?:–|—|-|to|到|至)\s*(\d+(?:\.\d+)?)\s*%',
                  r'\1% \2%', text)
    text = re.sub(r'(\d+(?:\.\d+)?)\s*%\s*(?:–|—|-|to|到|至)\s*(\d+(?:\.\d+)?)(?![\d.])',
                  r'\1% \2%', text)
    values = {str(float(n)) for n in re.findall(r'\d+(?:\.\d+)?', text)}
    values.update(str(float(n)) + '%' for n in re.findall(r'(\d+(?:\.\d+)?)\s*%', text))
    return values


def compact_evidence(state, tokens, draft, primary):
    """Digest plus cited chunks and 300-character immediate neighbors only."""
    digests = primary.get('digests', {}) | {d['document_id']: d['digest'] for d in primary.get('documents', [])}
    cited = {c['id'] for p in draft['points'] for c in p['citations']}
    passages = {}
    for token in tokens:
        chunks = state.chunks(token)
        for index, (identity, text) in enumerate(chunks):
            if identity not in cited:
                continue
            passages[identity] = text
            for neighbor, tail in ((index - 1, True), (index + 1, False)):
                if 0 <= neighbor < len(chunks):
                    nid, body = chunks[neighbor]
                    passages.setdefault(nid, body[-300:] if tail else body[:300])
    return {'digests': digests, 'passages': passages,
            'complete_coverage': all(len(state.read_coverage.get(t, set())) == len(state.chunks(t)) for t in tokens)}


def deterministic_audit(state, draft, evidence, tokens):
    allowed = numbers('\n'.join(evidence['digests'].values()) + '\n' + '\n'.join(evidence['passages'].values()))
    full = normalized('\n'.join(state.corpus()[t].text for t in tokens) + json.dumps(evidence, ensure_ascii=False))
    goal = state.task['goal'] if state.task else state.message.text
    terms = re.findall(r'[a-z][a-z0-9_]+|[\u3400-\u9fff]{2,}', goal.casefold())
    terms = [t for t in terms if t not in {'多少', '怎么', '那他呢', '那她呢'}]
    errors = {}
    for i, point in enumerate(draft['points']):
        if numbers(point['text']) - allowed:
            errors[i] = 'number absent from digest/cited evidence'
        if (re.search(r'未披露|没提到|没有说|未提及|没有披露|没有提到|not (?:mentioned|disclosed|stated)', point['text'], re.IGNORECASE)
                and (not evidence['complete_coverage'] or any(t in full for t in terms))):
            errors[i] = 'absence claim contradicted by query terms or incomplete coverage'
    return errors
