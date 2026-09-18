# Conversation reliability acceptance

Goal: an always-on cloud Agent that completes ordinary questions and supported
requests, including group colleagues and follow-ups. Do not equate message
delivery, passing schema checks, or a single model replay with task completion.
External outages, missing permissions and unavailable sources must be explained
truthfully; never suppress safety rules or fabricate completion to avoid a refusal.

## Changes

- Complete ordinary conversation in `message`, including general questions,
  drafting and planning. Transcript evidence remains required for podcast claims.
- Reject unfinished introductions/headings/lists, empty promises and open code
  blocks before persistence or delivery. Ask the SDK to repair specific errors.
  These are useful regression checks, not semantic proof of a useful answer.
- `get_request_status` reads only the same sender/session's actual job, delivery
  and answer records. No audit means unknown. It cannot inspect secrets or code,
  and cannot claim to fix itself. `record_feature_request` stores an idempotent
  request receipt without pretending the feature has shipped.
- Four message workers by default. SQLite atomically claims one oldest turn per
  conversation while allowing other conversations to progress. Retrying an older
  turn keeps later turns ordered. One replica is required for startup recovery.
- After eight seconds a slow active request gets an idempotent progress notice.
  Model/provider failures use bounded automatic retry (3/10/30/60 seconds), not
  empty silence. Progress and failure notices never replace the final answer.
- Before each model call, persist SDK replay items and the authorized evidence
  snapshot under the exact session/event. Restart/retry restores those results.
  The final answer and checkpoint deletion are one transaction. Credentials and
  owner/client objects are excluded. Side-effect tools retain idempotency guards;
  a crash during a tool before its next checkpoint can still repeat that tool.
- A 16-call segment may continue automatically, up to 48 model calls for one
  event. The bound prevents runaway bills; exhaustion is not successful research.
- `/healthz` reports only connection readiness and stalled-request counts. A
  watchdog exits on prolonged disconnection or a stuck interactive worker, so
  Railway can restart and recover durable jobs. Shutdown has a 30-second
  process deadline because cancelling asyncio does not stop synchronous tool
  threads; a subprocess regression verifies exit with a deliberately stuck
  executor. No user content or credentials
  are exposed in health responses. Model API availability is not inferred from
  WebSocket health. `scripts/configure_cloud_health.py` previews only the existing
  service's healthcheck, ALWAYS restart, one replica and no-sleep settings; apply
  is explicit. A plan restriction never authorizes a paid upgrade. No restart
  policy is an unconditional guarantee of future 24/7 uptime.

## Verification

Local: `python -m unittest discover -s tests -q` and `ruff check src tests scripts`.
The new regressions cover actual half-answer repair, general questions without
transcript requirements, diagnostic grounding/isolation, feature-request receipts,
concurrent real-thread claiming, per-session order, restart recovery, slow/fast
member separation, durable progress, checkpoint resume after provider failure,
total model-call bounds, and health/watchdog failure detection.

Cloud opt-in replays use `scripts/remote_hermes_pilot.py run PYTHON agents_sdk
--conversations` and `--extended`. Despite the historical script name, the backend
is explicitly selected. They run actual inference over cloned production data,
save full answers for manual review, and do not send Feishu messages or change
production subscriptions. The first conversation replay covered nine scenarios:
greeting, capabilities, general explanation, rewriting, a long deliverable,
self-diagnosis, feature registration, its follow-up, and another group member.

Still separate acceptance gates: final-code replay, cloud deployment identity,
real Feishu ingress and visible complete replies, restart/reconnect integration,
and sustained cloud health. Do not mark the full product complete based only on
the local suite or the synthetic conversation replays.
