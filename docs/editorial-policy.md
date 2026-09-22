# Daily editorial policy: relevance-density-v3

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
Founder fame alone earns no bonus. Generic entrepreneurship stories, promotion,
and incidental AI mentions remain low relevance.

**Information density (0–5)** measures concrete, non-generic ideas, explanations,
experiments, operating details and useful causal reasoning across the episode.
A long transcript or a large count of numbers does not imply high density. Five
is exceptional: compressing it into a brief would lose important detail. There is
no daily quota; zero compilation-worthy episodes is a valid outcome.

| Rating | Code gate: relevance / density | Reader action |
|---|---|---|
| ★★★★★ | Both exactly 5 | 值得编译 — automatically create an individual Feishu document |
| ★★★★☆ | Both at least 4, or relevance ≥3 and density 5 | 值得看全文 |
| ★★★☆☆ | Both at least 3, below the above gates | 看摘要即可 |
| ★★☆☆☆ | Both at least 2, below the above gates | 可跳过 |
| ★☆☆☆☆ | Otherwise | 可跳过 |

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
v3 invalidates previous five-dimension reviews. Private company profiles are no
longer read or sent to the scoring model and cannot change the score. Existing
private configuration is not deleted. `NEWS_OFFICER_EDITORIAL_FILTER=false`
disables the grounded rating pass, not complete-transcript validation.

## Presentation and compilation

The brief retains coherent Chinese paragraphs and a short recommendation reason.
The archived summary ends with five star glyphs; the delivery renderer appends
the reader action, without numeric subscores or repeated evidence labels.

`NEWS_OFFICER_DAILY_DOCUMENT_MIN_STARS` defaults to **5**. Production should also
set it to 5 explicitly, since an older environment override of 3 wins over code
defaults. Automatic compilation happens after the brief is delivered. Frozen
older document batches cannot bypass the current threshold. A user's explicit
request for a detailed episode document still bypasses the star threshold.
