# Daily editorial policy: ai-investment-ranked-v2

All configured sources are still scanned inside the existing lookback window.
Rating happens only after a complete transcript has been obtained and archived.
Every verified episode gets a daily summary, including low-star episodes. Delivery
and archived-day retrieval sort by stars descending. Already delivered messages are not rewritten.

| Dimension | Weight | Substantive relevance / strong evidence |
|---|---:|---|
| AI | 40 | Models, AI applications, infrastructure or AI science discussed in depth |
| Investment usefulness | 20 | Customers, economics, competition, capital allocation or moat analysis |
| Current company research | 5 | An active private watchlist company is discussed substantively |
| Information gain | 20 | Specific new detail, original framework or firsthand information |
| Evidence quality | 15 | Explicit causal reasoning, traceable data, examples and limitations |

Each dimension is model-assessed from 0–5, with a source block ID for
every nonzero score. Full transcripts are split losslessly into numbered blocks;
code resolves IDs to verbatim excerpts rather than asking the model to recopy text.
Code validates excerpts, active company identity and expiry,
then calculates points. This is editorial judgment, not objective fact validation;
information gain is judged within the episode, not against all past podcasts.
Ads, name-dropping, generic same-sector mentions and celebrity status earn no
company bonus. No company match is required for a useful general AI episode.

There is no relevance-based daily exclusion. The legacy `selected` audit field is
retained for compatibility but must not gate summarization. Stars: >=85 is 5; >=70 is 4; >=55 is 3;
>=40 is 2; otherwise 1. Five stars additionally requires information gain and
evidence >= 4. Without a company match the maximum total is 95, so excellent AI episodes can still earn five stars. Low relevance
should be described honestly in the rationale. Scores are not investment-return forecasts.

The recommendation line contains only a short rationale, not numeric subtotals.
The final rating contains only five star glyphs. Ratings override the
summary model's freely generated star count. Reviews are cached privately in
SQLite by episode, transcript hash, active-profile hash, model and policy version.
Changing the source text or active profile invalidates the review cache.
Legacy `not_recommended` records within the discovery window can be reviewed again.
Invalid model output or missing configured profile raises an error; it does not
silently turn a candidate into a low-quality episode.
Schema or excerpt validation failures get one bounded model correction attempt;
the second response must pass the same checks or the episode remains failed/retryable.

## Private research profile

Set `NEWS_OFFICER_RESEARCH_FOCUS_PATH` to a private JSON file on the persistent
volume, e.g. `/data/research-focus.json`. Do not add it to this repository or store
raw group messages there. Use only user-approved or clearly stated research
preferences, never infer investment positions or include deal-sensitive notes.
The profile is reloaded at each evaluation. Expired entries earn no bonus.

Example (fictional company):

```json
{"companies":[{"name":"ExampleCo","aliases":["Example Company"],"expires_on":"2026-10-18"}]}
```

Company names in the active profile are supplied to the main model for selection;
they are not sent to the tone advisor. Only a public-discussion-based rationale,
not the internal watchlist itself, belongs in the outward recommendation.

`NEWS_OFFICER_EDITORIAL_FILTER=false` disables the evidence-grounded rating pass,
not transcript validation. Normal discovery still uses the 24-hour production
window; unfinished transcript work is durably retained for later catch-up. Changing
focus does not resend already delivered episodes; a deliberate backfill is separate.
