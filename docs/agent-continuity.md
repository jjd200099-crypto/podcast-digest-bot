# Conversational research: context, task, evidence, delivery

This is an application-harness improvement, not a migration to a different SDK.
The OpenAI Agents SDK still runs the model/tool loop. Feishu remains the transport;
SQLite retains event deduplication, session isolation and outbox delivery.

## Reference designs

- [Pi agent core](https://github.com/badlogic/pi-mono/blob/main/packages/agent/README.md):
  explicit application-message to model-context transformation, stateful tool
  execution and continuation. Applied here by injecting resolved quoted-message
  context rather than sending only a bare text string to the model.
- [Hermes gateway sessions](https://github.com/NousResearch/hermes-agent/blob/main/gateway/session.py):
  separate transport identity, persisted conversation and runtime context. Applied
  here without merging different colleagues' conversation histories.
- [OpenAI SDK results and state](https://developers.openai.com/api/docs/guides/agents/results):
  application-managed conversation replay and continuation of the current run.

## Implemented behavior

1. Resolve a quoted bot message only after verifying its delivery audience.
   A daily episode message supplies its exact archive document ID. A quoted
   interactive reply supplies that single visible question/answer and its task;
   it never supplies the speaker's other session history or pending subscriptions.
2. `set_research_task` records the resolved objective, real document IDs and brief
   or detailed output mode. Persist the task and execution audit atomically with
   the committed answer. The next turn receives its own session's previous task.
   Current instructions supersede older objectives; metadata is not evidence.
3. Detailed notes require every page of every selected transcript to have been
   read in the current run. Reading coverage is tracked as unique chunk indices,
   not merely a tool-call count. Incomplete coverage returns the next missing
   offset to the model. The notes are organized by theme, as in the transcript
   skill, rather than forced into the daily digest's short format.
4. Model citations use server-issued IDs of verified evidence read this turn,
   not an extra model-retyped quotation. The first real replay read all 40 Noam
   Brown chunks but failed three times at exact-quote matching. Removing that
   redundant transcription step fixes this failure without admitting invented
   IDs or unread evidence. Legacy direct callers supplying a quotation still
   have it checked. Visible text must remain original summary, not copying.
5. Validation failures supply specific correction feedback and are stored in
   `research_run_state.audit_json`. Audit includes model calls, outcome and reading
   coverage, not raw tool outputs, secrets or private model reasoning. Exhaustion
   retains the task and does not fabricate successful delivery or subscription
   changes. Only a validated substantive answer has outcome `completed`.

## Acceptance tests

`python -m unittest discover -s tests -q` checks quote resolution, cross-chat
boundaries, per-user task continuity, reading completeness and targeted repair.

`python scripts/run_remote_continuity_smoke.py` is an opt-in real-model replay.
It launches a background replay, returning a PID and `/tmp/agent-replay-*.log`.
Read that log to observe completion; the script stores full answer artifacts in
the printed `/tmp/agent-replay-proof-*` directory. A launch receipt is not a pass.
It uploads a temporary code overlay to the configured Railway service, clones
the production database and archive, and replays these public-podcast cases:

- Reply to a delivered episode digest: “这篇很好，能不能为我做更完整详细的版本”.
- After a clarification about that request, send only “noam brown”.
- Follow with a question requesting three short sentences.

It asserts full reading, a substantial thematic detailed answer, correct episode,
source links and current-turn depth override. It sends **zero Feishu messages**
and makes **zero production database changes**. A successful replay is not proof
that a real Feishu client received a message; actual delivery needs separate
authorized verification.
