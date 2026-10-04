# Hermes pilot acceptance — 2026-09-18

## Decision

The actual Hermes runtime is integrated and passed the six selected functional
replays. Do **not** promote this pilot to the production Feishu bot yet. Both
backends completed the tasks; this small sample did not establish better answer
quality from migration. The current Hermes adapter used more calls and time.
The running bot and daily schedule were not redeployed or reconfigured.

## Method

- Model: `gpt-5.6-terra` for both backends, same configured model API account.
- Same questions and verified corpus cloned from the running service.
- Hermes: pinned commit `d177b119e9c56c9ddc0b7379ffce52341ec06584`.
- Application authorization, evidence validation and domain tools shared.
- Hermes used its own loop/session store; Agents SDK used its existing loop.
- No Feishu sends, no production subscription/database changes. Source additions
  and Hermes native session state lived only in disposable test copies.
- This was one successful sample per case, not a statistically powered benchmark.
  Prompt wrappers, cache warmth, native history and runtime settings differ;
  these are integration measurements, not an intrinsic framework ranking.

## Functional result and measured latency

| Case | SDK outcome | Hermes outcome | SDK calls / seconds | Hermes calls / seconds |
| --- | --- | --- | --- | --- |
| Reply to a quoted Noam Brown summary, request detailed notes | Pass | Pass | 6 / 47.534 | 8 / 82.994 |
| After clarification, supply only “noam brown” | Pass | Pass | 7 / 41.804 | 10 / 48.986 |
| Follow up with a three-sentence question | Pass | Pass | 3 / 13.404 | 5 / 13.585 |
| Compare two actual episode transcripts | Pass | Pass | 5 / 9.553 | 7 / 25.589 |
| Propose Practical AI RSS, awaiting separate confirmation | Pass | Pass | 2 / 4.301 | 6 / 12.926 |
| Confirm source addition on the next turn | Pass | Pass | 2 / 3.538 | 5 / 9.763 |

Totals: SDK 25 calls / 120.134 seconds; Hermes 41 calls / 193.843 seconds.
Each detailed answer covered all 40 chunks of the selected Noam Brown source.
Hermes detailed outputs were 3,591 and 2,982 characters; brief output 633.
All final successful samples had zero application answer-validation errors.

The first SDK source-confirmation test failed because the temporary code overlay
changed the inferred feeds.json location. The harness was fixed to explicitly
copy the service's feed configuration into the disposable clone; both source
turns were rerun successfully. That failure is a harness issue, not model quality.

## Cost and quality interpretation

SDK records input/output/cache token counters. Hermes records its native counters
and estimated cost status. **Do not compare `session_input_tokens` directly with
SDK `input_tokens`: Hermes' canonical input field excludes cache reads/writes.**
Framework dollar estimates are not audited invoice amounts. Differences in cache
warmth and full-history replay prevent a clean dollar-cost claim from this run.

The trace shows extra turns after Hermes' `submit_answer` has been accepted,
including occasional repeat submissions. The broker rejects further actions,
so this does not repeat source writes or deliveries, but it consumes tokens.
The next optimization is a native clean stop after accepted delivery while
preserving session persistence, then a repeated blind content comparison.

Detailed answer structure and content were inspected, including the distinction
between Brown's predictions and established results. Automated acceptance checks
validate document identity, coverage, citations and output depth; they do not
prove every synthesized claim is semantically entailed. Do not promote on length
or test-count alone. Longer-session compression and shared memory governance were
not evaluated, and global skill learning/memory remain disabled.

## Evidence locations

Temporary cloud evidence (may disappear on a future service restart):

- SDK research: `/tmp/agent-replay-proof-0lg2g8yj`
- SDK source workflow retry: `/tmp/agent-replay-proof-lgra0tiy`
- Hermes core dialogue: `/tmp/agent-replay-proof-3bci6i_3`
- Hermes comparison/source workflow: `/tmp/agent-replay-proof-hlgv7rc2`

Logs: `/tmp/hermes-pilot-4he9spx_.log`, `/tmp/hermes-pilot-qn0szlh9.log`,
`/tmp/hermes-pilot-m3crq3cw.log`, `/tmp/hermes-pilot-29_ddk2x.log`.

Local checks: 348 tests passed with the actual pinned Hermes startup contract
enabled; Ruff and compilation passed. CI separately provisions a fresh pinned
Hermes environment and runs the no-key/no-inference startup/tool-boundary tests.
