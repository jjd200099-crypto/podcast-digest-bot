# Daily editorial policy: relevance-density-priority-v4

All configured sources are scanned inside the existing 24-hour window. Rating
happens only after a complete transcript has been obtained and archived. Every
verified episode continues to receive a summary, including low-star episodes,
ordered by stars descending. This preserves the all-updates coverage contract;
`selected` denotes the recommended tier, not a discovery or transcript filter.
Already delivered messages are not rewritten.

## Only two criteria

**Relevance (0–5)** prioritizes model frontier, AI research, and substantive
firsthand interviews with successful AI unicorn founders, especially Silicon
Valley companies such as the user-nominated Fireworks. These are topic priorities,
not assertions about a guest's valuation or automatic changes to subscribed feeds.
Within relevance, two user-mandated topics have an inclusion floor: substantive
discussion of OpenAI, Anthropic or another foundation-model lab; and an interview
with a major guest identified as a prominent AI unicorn founder, including the
user-nominated Fireworks. A substantial segment is enough; the topic need not
dominate the episode. Founder life stories may qualify through this guest rule.
Advertisements, API usage alone, previews, passing mentions and title-only claims
do not qualify. Do not invent popularity, valuations or founder identity.

The model must explicitly classify relevance.priority as none / model_lab /
ai_unicorn_founder, supported by its relevance source excerpts. Priority topics
require relevance ≥4 and have a minimum action of **值得看全文**, even when density
is low. Density remains honest and unchanged; this is not an automatic compilation
pass, a third score, or an expansion of subscribed feeds. Complete transcripts
remain mandatory before content summaries or ratings.

**Information density (0–5)** measures concrete, non-generic ideas, explanations,
experiments, operating details and useful causal reasoning across the episode.
A long transcript or a large count of numbers does not imply high density. Five
is exceptional: compressing it into a brief would lose important detail. There is
no daily quota; zero compilation-worthy episodes is a valid outcome.

| Internal rating (not displayed) | Code gate: relevance / density | Reader action |
|---|---|---|
| ★★★★★ | Both exactly 5 | 值得编译 — automatically create an individual Feishu document |
| ★★★★☆ | Priority topic; otherwise both at least 4, or relevance ≥3 and density 5 | 值得看全文 |
| ★★★☆☆ | Both at least 3, below the above gates | 看摘要 |
| ★★☆☆☆ | Both at least 2, below the above gates | 无关 |
| ★☆☆☆☆ | Otherwise | 无关 |

There are no separate investment, fame, company-watchlist, novelty or evidence
scores. The internal ordering total is `(relevance + density) * 10`; it is not
displayed. Strong relevance cannot compensate for thin content to obtain five
stars. Scores are editorial judgments, not factual or investment-return guarantees.

## Grounding and audit

Full transcripts are split losslessly into numbered blocks. Each nonzero dimension
must cite 1–3 distinct block IDs; code extracts exact source text and verifies it.
Density 5 additionally requires at least two distinct source excerpts. These
checks establish attribution, not independent confirmation of guest claims, and
are a validation boundary rather than a third score. Ads and previews must not
stand in for substantive discussion. Invalid schema or evidence gets one bounded
repair attempt, then remains failed/retryable instead of receiving fabricated stars.

Reviews are cached by episode, transcript hash, model and policy version. Version
v4 invalidates previous reviews without priority classification. Private company profiles are no
longer read or sent to the scoring model and cannot change the score. Existing
private configuration is not deleted. `NEWS_OFFICER_EDITORIAL_FILTER=false`
disables the grounded rating pass, not complete-transcript validation.

## Presentation and compilation

The brief retains coherent Chinese paragraphs and a short recommendation reason.
The archived summary retains its internal five-star encoding for ordering, audit
and compilation gates. Delivery replaces that line with only the reader action:
值得编译 / 值得看全文 / 看摘要 / 无关. No stars or numeric subscores are displayed,
including document-link notifications. Here 无关 means not recommended for this
research brief: relevance OR information density is insufficient and no priority rule applies. A missing or
unverified transcript remains pending/unrated, never automatically 无关.

`NEWS_OFFICER_DAILY_DOCUMENT_MIN_STARS` defaults to **4**: both worth-reading
and exceptional episodes get a document. Production should also set it to 4
explicitly, since older environment overrides win over code defaults. In reader
mode, compilation finishes before the single brief is frozen; document links
are included in that same message. Frozen
older document batches cannot bypass the current threshold. A user's explicit
request for a detailed episode document still bypasses the star threshold.

After the operator confirms full-text publication rights for the configured
audience, `NEWS_OFFICER_DAILY_DOCUMENT_FULLTEXT=true` appends the complete verified
archive to each selected/requested document. This is a lossless original-language
appendix, not a new translation. It uses no additional Podwise quota. A separate
content hash makes appends resumable and deduplicated; existing document edits
are never overwritten. No broader sharing permissions or chat attachments are added.
