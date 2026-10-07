"""Read-only, bounded discovery outside the user's subscribed RSS catalog."""

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .models import Episode
from .podwise import PodwiseTranscriptProvider, _number
from .rss import _safe_https_url

TOPICS = ('OpenAI', 'Anthropic', 'DeepMind', 'foundation model', 'AI research',
          'AI founder', 'Fireworks', 'reinforcement learning', 'reasoning model',
          'AI agents', '人工智能', '大模型')
PODCAST_TOPICS = ('artificial intelligence', 'AI research', 'AI founder')


@dataclass
class DiscoveryResult:
    episodes: list[Episode]
    notice: str
    successful_requests: int = 0
    failed_requests: int = 0


class PodwiseDiscovery:
    def __init__(self, token, *, topics=TOPICS, pages=3, popular_limit=100,
                 podcast_topics=PODCAST_TOPICS, catalog_limit=100, candidate_limit=200, store=None):
        if not token:
            raise ValueError('Podwise discovery requires PODWISE_API_TOKEN')
        if not (1 <= pages <= 10 and 1 <= popular_limit <= 100
                and 1 <= catalog_limit <= 200 and 1 <= candidate_limit <= 1000):
            raise ValueError('Invalid Podwise discovery limits')
        self.api = PodwiseTranscriptProvider(token, timeout=15, processing_store=store)
        self.store = store
        self.topics, self.podcast_topics = tuple(topics), tuple(podcast_topics)
        self.pages, self.popular_limit = pages, popular_limit
        self.catalog_limit, self.candidate_limit = catalog_limit, candidate_limit

    def _read(self, request):
        path, params = request
        try:
            result = self.api._get(path, params)
            if result is None or not isinstance(result.get('result'), (list, dict)):
                raise ValueError('Invalid discovery response')
            return result, None
        except Exception as error:  # noqa: BLE001 - never expose provider bodies/secrets
            return None, type(error).__name__

    @staticmethod
    def _episode(row, now, cutoff):
        if not isinstance(row, dict) or type(row.get('seq')) is not int or row['seq'] <= 0:
            return None, 'invalid'
        if any(word in str(row.get('linkType', '')).casefold() for word in ('upload', 'private')):
            return None, 'invalid'  # Public podcast discovery, not personal uploads.
        published = _number(row.get('publishTime'))
        if isinstance(row.get('publishTime'), bool) or published is None:
            return None, 'invalid'
        try:
            when = datetime.fromtimestamp(published, UTC)
        except (ValueError, OverflowError, OSError):
            return None, 'invalid'
        if not cutoff <= when <= now:
            return None, 'outside'
        title, show, link = (row.get(k) for k in ('title', 'podcastName', 'link'))
        if not all(isinstance(v, str) and v.strip() for v in (title, show, link)):
            return None, 'invalid'
        if not _safe_https_url(link):
            return None, 'invalid'
        duration = _number(row.get('duration'))
        if duration is not None and duration <= 0:
            duration = None
        return Episode(f"podwise:{row['seq']}", title[:1000], link, show[:300],
                       duration_seconds=duration, published_at=when,
                       metadata={'discovery_origin': 'podwise', 'podwise_seq': row['seq'],
                                 'podwise_podcast_seq': row.get('podcastSeq'),
                                 'source_priority': 'B'}), None

    def discover(self, now: datetime, lookback_hours: int) -> DiscoveryResult:
        now = now.astimezone(UTC)
        cutoff = now - timedelta(hours=lookback_hours)
        rows, catalogs, details, errors, capped = [], set(), set(), [], []
        watch = self.store.discovery_watch_catalogs(now) if self.store else []
        catalogs.update(watch)
        request_count = 1
        # Popular entries have no publication date. Never substitute list position
        # or discovery time; read their dated catalog (or episode info) instead.
        data, error = self._read(('/episodes/popular', {'limit': self.popular_limit}))
        popular = (data or {}).get('result', [])
        if error or not isinstance(popular, list):
            errors.append('热门榜')
        else:
            for row in popular:
                if not isinstance(row, dict):
                    continue
                seq, show = row.get('seq'), row.get('podcastSeq')
                if type(seq) is int and seq > 0:
                    if type(show) is int and show > 0:
                        catalogs.add(show)
                    else:
                        details.add(seq)
        for topic in self.topics:
            for page in range(self.pages):
                request_count += 1
                data, error = self._read(('/episodes/search',
                    {'q': topic, 'page': page, 'hitsPerPage': 30}))
                entries = (data or {}).get('result', [])
                if error or not isinstance(entries, list):
                    errors.append('主题搜索')
                    break
                rows.extend(entries)
                if len(entries) < 30:
                    break
                total = _number(data.get('estimatedTotalHits'))
                if total is not None and total <= (page + 1) * 30:
                    break
                if page == self.pages - 1:
                    capped.append(topic)
        for topic in self.podcast_topics:
            request_count += 1
            data, error = self._read(('/podcasts/search', {'q': topic, 'hitsPerPage': 10}))
            entries = (data or {}).get('result', [])
            if error or not isinstance(entries, list):
                errors.append('频道搜索')
                continue
            catalogs.update(r['seq'] for r in entries if isinstance(r, dict)
                            and type(r.get('seq')) is int and r['seq'] > 0)
        # Stable daily rotation prevents the same tail of catalog candidates from
        # being permanently excluded by the request budget. No follow API calls.
        catalogs = sorted(catalogs)
        if len(catalogs) > self.catalog_limit:
            offset = now.date().toordinal() % len(catalogs)
            catalogs = (catalogs[offset:] + catalogs[:offset])[:self.catalog_limit]
            capped.append('频道目录')
        end = (now + timedelta(days=1)).date().isoformat()
        days = min(365, math.ceil(lookback_hours / 24) + 2)
        requests = [(f'/podcasts/{seq}/episodes', {'date': end, 'days': days}) for seq in catalogs]
        requests += [(f'/episodes/{seq}', None) for seq in sorted(details)]
        request_count += len(requests)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for request, (data, error) in zip(requests, pool.map(self._read, requests), strict=True):
                if error:
                    errors.append('日期目录/元数据')
                    continue
                value = data['result']
                if request[0].startswith('/podcasts/') and not isinstance(value, list):
                    errors.append('日期目录/元数据')
                    continue
                if request[0].startswith('/podcasts/'):
                    seq = int(request[0].split('/')[2])
                    value = [dict(r, podcastSeq=r.get('podcastSeq') or seq) if isinstance(r, dict) else r
                             for r in value]
                rows.extend(value if isinstance(value, list) else [value])
        episodes, invalid = {}, 0
        for row in rows:
            episode, reason = self._episode(row, now, cutoff)
            invalid += reason == 'invalid'
            if episode:
                episodes.setdefault(episode.id, episode)
        selected = sorted(episodes.values(), key=lambda e: e.published_at, reverse=True)
        if len(selected) > self.candidate_limit:
            capped.append('新集候选')
        selected = selected[:self.candidate_limit]
        notice = (f'Podwise 扩展发现：热门榜前 {self.popular_limit} 条、{len(self.topics)} 组主题搜索'
                  f'和 {len(catalogs)} 个频道日期目录，找到 {len(selected)} 期时间窗内候选'
                  '（去重和全文筛选前，不等于新增推荐数）。不自动订阅新频道。')
        if watch:
            notice += f' 包含 {len(watch)} 个近期多次产出高价值内容的观察频道。'
        if capped:
            notice += f' {len(capped)} 项达到扫描上限；搜索并非按最新时间完整排序，不保证覆盖所有新集。'
        if errors:
            notice += f' {len(errors)} 个读取请求失败，本次发现不完整；不能据此判断没有新节目。'
        if invalid:
            notice += f' {invalid} 条结果缺少有效日期或元数据，未作为新集。'
        return DiscoveryResult(selected, notice, request_count - len(errors), len(errors))
