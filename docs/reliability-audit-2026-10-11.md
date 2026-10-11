# Reliability audit — 2026-10-11

Scope: scheduled discovery and backlog, durable workers, transcript/document
publication, Feishu receipts, process health, and restart recovery. No group
messages or model calls were made for fault-injection tests.

## Confirmed defects addressed

| Failure mode | Protection added |
| --- | --- |
| Daily/document worker hangs while chat-only health remains green | Background jobs participate in health: preparation 15 minutes, daily 45 minutes, document 60 minutes. Sustained failure invokes the existing process shutdown/restart path, not unsafe concurrent retries of a still-running thread. |
| Repeated process crashes immediately reclaim the same job | First restart preserves immediate recovery; subsequent background interruptions back off. Five attempts enter the existing one-hour failed-job cooldown. Frozen output and caches remain intact. |
| Discovered but unprocessed episodes disappear outside the next 24-hour window | Persist candidates before provider/model work. Retain source metadata, existing retry deadlines and completed-state records. |
| Failed document stops being revisited after the six-day discovery window | Continue rescheduling existing unfinished document work beyond that window; retain subscription, source revision and original delivery checks. |
| Temporary Feishu document network/5xx failure aborts immediately | Bounded retry for reads and appends with the same server idempotency key. Uncertain creates, deletes and message edits return to existing reconciliation instead of being replayed blindly. HTTP 401 refreshes authentication once. |
| API response without a message ID is marked delivered | Require an actual remote message ID before the production messenger reports success. Error logs retain operation/code, not full remote response bodies. |

## Validation and limits

Regression cases cover safe retries versus uncertain writes, lost receipts,
background watchdog thresholds and cleanup, repeated crash backoff, candidate
metadata/cooldown preservation, summary-cap overflow, and aged document retry.
The existing complete suite must pass, followed by main-branch CI and a live
release/health readback. Offline failure simulation is not proof that external
providers will remain available tomorrow.

Read-only production inspection found an intact database, no pending outbox,
and approximately 259 MiB free on the persistent volume. The intentionally held
daily group backfill and an old terminal chat failure were not replayed: this
audit does not authorize sending stale replies or changing recipients.

Remaining architectural limits: single replica and persistent volume; existing
backups on that same volume are not offsite disaster recovery. Provider outages
and missing complete transcripts can still delay content. Platform message-size
limits remain enforced rather than silently dropping episodes or splitting a
requested single-message digest. These require an explicit fallback/product
decision if encountered; do not report them as eliminated by this audit.
