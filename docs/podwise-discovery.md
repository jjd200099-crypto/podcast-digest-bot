# Podwise exploration outside the subscribed list

`NEWS_OFFICER_PODWISE_DISCOVERY=true` adds a discovery lane to the existing daily
job. It requires `PODWISE_API_TOKEN` and the transcript-grounded editorial policy.
No second scheduler, user-account chat access, or subscription changes are needed.
Tracked RSS coverage and the original group delivery schedule stay unchanged.

## Discovery, not a claim of exhaustive coverage

The read-only scan combines:

- Podwise's popular list (up to 100 entries). That endpoint does **not** return
  publication dates, so the scanner reads the associated podcast's dated catalog
  or episode metadata; discovery time and list position are never used as dates.
- Twelve topic queries across the episode database: OpenAI, Anthropic, DeepMind,
  foundation models, AI research, founders, Fireworks, RL, reasoning models, agents,
  and Chinese AI/model terms. Each query reads at most three 30-result pages. It
  does not stop at an old result because search order is not publication order.
- Podcast search for AI/research/founder channels, followed by their recent episode
  catalogs. This finds new episodes even when a channel is not in `feeds.json`.

At most 100 dated channel catalogs and 200 current-window episode candidates are
considered per scan. Catalog overflow rotates daily; capped search/catalog/candidate
counts are disclosed. Podwise does not document an exhaustive all-new-episodes
endpoint or date-sorted episode search, so this is bounded discovery, not every
release on the platform. Discovery metadata is untrusted and cannot supply content
summaries. Unknown/future dates and invalid metadata do not enter the daily window.
Known private/upload asset types are excluded.

The same production 24-hour publication window applies. A healthy source with zero
matches is distinct from an API failure. Failed reads are reported and retryable,
while successfully scanned RSS episodes can still be delivered. Exploration status
is persisted with the daily results, including on private/group archive retrieval.

## Full text, selection and deduplication

The normal archive → official provider → Podwise resolver obtains the full text.
A discovered `podwise:<seq>` item uses the stable asset ID to re-read episode info
and transcripts, avoiding a second ambiguous title search. Identity, timing,
duration and full-text density checks are unchanged. The existing text-density
validator is primarily tuned for English; metadata discovery itself is multilingual.

Only complete transcripts reach the two-criterion editorial policy. The mandatory
model-lab/founder inclusion rule also applies to discovered material. Recommended
episodes join the same daily brief; exceptional compilation-tier episodes follow
the same individual Feishu-document workflow. The outward recommendation remains
值得编译 / 值得看全文 / 看摘要 / 无关, with no visible stars.

Low-value material from both exploratory and tracked-RSS lanes is archived but
omitted from the brief body, with separate counts in the coverage notice. Every
tracked source is still scanned: coverage is not the same as recommending all
episodes. Missing full text remains
in the durable backlog, including after the original publication window expires.
Tracked RSS retries precede exploratory retries; overdue work rotates by next-check
time so unready discoveries cannot monopolize every backlog batch.

Same-asset links and publisher/title/day identities deduplicate the combined scan,
preferring the subscribed publisher representation. Exact publisher metadata also
checks past delivery IDs; same titles from unrelated publishers are not merged by
the global discovery lane. An exploratory rejection cannot suppress the user's
later explicitly tracked RSS item. Alternate titles/dates are not guessed into a
match, so ambiguous cross-publisher syndications may still require manual review.

Each discovery summary is marked `发现渠道：Podwise 扩展发现`. Finding an episode
does not call Podwise follow/import/process APIs, modify `feeds.json`, or enroll
the whole channel. Discovery items never spend transcription credits automatically,
even if processing is enabled for the explicitly tracked RSS lane.

## Verification

`PYTHONPATH=src python scripts/smoke_podwise_discovery.py --hours 24` performs a
smaller live scan using existing environment credentials (20 popular entries, one
page/query, up to 30 catalogs). `--assess-limit 1` additionally obtains a complete
transcript and rates it using an isolated temporary store. It sends no Feishu
messages, creates no documents, changes no follows, and submits no paid processing.
No token or environment dump is printed.

Official API references: [popular episodes](https://docs.podwise.ai/open-api-v1/discovery/popular-episodes),
[episode search](https://docs.podwise.ai/open-api-v1/discovery/search-episodes),
[dated podcast catalog](https://docs.podwise.ai/open-api-v1/discovery/list-podcast-episodes),
[episode metadata](https://docs.podwise.ai/open-api-v1/episodes/get-episode-info).
