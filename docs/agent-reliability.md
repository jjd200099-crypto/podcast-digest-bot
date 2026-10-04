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

## Observed acceptance status — 2026-09-18

Production application release: `0fcb9db`, running on Railway with Agents SDK,
four interactive workers, one replica, no sleep, ALWAYS restart and `/healthz`.
The deployed runtime SHA-256 matched the release checkout. The daily schedule
remains 08:30 Asia/Shanghai, a 24-hour window, with one active group recipient
and no active private daily recipients.

| Gate | Observed evidence | Boundary |
| --- | --- | --- |
| Ordinary conversations | Nine real-model cloud replays passed; actual answers reviewed | Isolated state, no Feishu sends |
| Podcast research and follow-ups | Six extended replays passed; Noam Brown detailed answer read all 40 source chunks; final detailed/source cases rerun after output-schema fix | Not a live colleague conversation |
| Runtime concurrency and delivery retry | Fast sender finished in about 3 seconds while another request ran; same-sender follow-up stayed ordered; simulated lost send receipt reused delivery UUID and committed only one answer | Fake outbound transport, not Feishu's service |
| Cloud deployment | Railway SUCCESS; live `/healthz` reported connected, zero active/stalled requests; database integrity OK | A healthy snapshot does not prove future uptime |
| Controlled cloud restart | No unfinished jobs before restart; platform restart accepted; PID 1 start time changed; health returned OK; 20 stored research turns and the sole subscription persisted | Idle restart, not an in-flight real user request |
| SDK ingress contract | Real SDK normalization/policy admitted two different colleagues' mentions and DM without mention; preserved quoted-message/thread IDs; deduplicated repeats and blocked unrelated mentions/bot loops | Synthetic wire events; cannot prove tenant scopes or Feishu event delivery |
| Real group/private acceptance | Pending authorization or user/colleague test | Do not report this as passed |

Deployment acceptance caught a real lifecycle incompatibility: the SDK's
foreground `connect()` can stay blocked before readiness is marked. The first
health-checked deployment did not pass. Release `0fcb9db` uses the public
`connect_until_ready()` lifecycle and passed the live health check; an actual
SDK/background-thread regression covers the readiness transition.

Remaining gates are real Feishu ingress and visible complete replies from both
the user and a colleague, plus ongoing operational observation. Do not mark the
full product complete based only on local checks or synthetic replays. No finite
test suite can guarantee zero future outages, semantic errors, or legitimate
permission/safety constraints.
