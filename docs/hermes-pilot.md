# Hermes pilot (not a production migration)

The existing Feishu gateway, daily scheduler, verified transcript store and
idempotent outbox remain unchanged. Only interactive research execution has a
selectable alternative. Default: `agents_sdk`. No automatic production switch.

## Actual integration

Pinned upstream: NousResearch/hermes-agent commit
`d177b119e9c56c9ddc0b7379ffce52341ec06584` (0.21.3).

`hermes_worker.py` calls the actual `AIAgent.run_conversation` loop. Hermes owns
model/tool iteration, same-turn recovery, native session persistence and its
context compression. The application still owns source authorization, quoted
message resolution and evidence validation. This is not a renamed SDK runner.

The worker uses an independent Python environment because Hermes pins OpenAI
2.24.0 while the current bot uses a newer SDK. It registers only the podcast
domain tools plus `submit_answer` through Hermes' registry. Tool Search is
disabled to keep the exposed capabilities explicit; unexpected tools fail
startup. Domain execution goes over stdin/stdout to a parent-side allowlist.
An answer is accepted only after the same application evidence checks as the
existing backend. Incomplete coverage yields missing offsets for model repair.

Each chat+sender gets a separate hashed profile and native SessionDB next to the
application database, under `hermes-pilot/`. Native history is restored on the
next turn; current evidence IDs must still come from a fresh authorized read.
Global memory, automatic skill learning, project context injection, background
review, shell/file tools, arbitrary HTTP tools and direct message sending are
not enabled in this pilot. Those need separate team-access design before use.

The child inherits no Feishu, Podwise or GitHub credentials, no personal Python
path and no dotenv settings. The model API key is passed via the private pipe,
not command arguments. Upstream stderr is not forwarded. This is a capability
and credential boundary, **not an OS sandbox for malicious dependency code**.
Source revision is pinned and four core file hashes are checked at startup.

## Reproduce

Create a new isolated environment (Python 3.12 recommended):

```sh
python scripts/install_hermes_pilot.py /absolute/new/hermes-pilot-runtime
```

The installer fetches the pinned official source archive, uses an editable
install as required upstream, and records the resolved dependencies in
`installed.txt`. It does not install a daemon or modify the bot's environment.
Hermes' commit/core dependencies are pinned; transitive resolution is recorded,
not yet a cross-platform hash-locked release artifact.

Run standard tests and the optional real-Hermes startup contract:

```sh
PYTHONPATH=src TEST_HERMES_PYTHON=/absolute/new/hermes-pilot-runtime/venv/bin/python \
  python -m unittest discover -s tests -v
```

Live replays require the existing bot configuration and model API key on the
test host. `smoke_agent_continuity.py` first clones its database and transcript
files; it never runs a delivery worker. Set the following only for the replay
process, not production Railway variables:

```sh
NEWS_OFFICER_AGENT_BACKEND=hermes \
NEWS_OFFICER_HERMES_PYTHON=/absolute/new/hermes-pilot-runtime/venv/bin/python \
  python scripts/smoke_agent_continuity.py --extended
```

Run the same script with `NEWS_OFFICER_AGENT_BACKEND=agents_sdk` for baseline.
The production-specific `remote_hermes_pilot.py` helper uploads only a temporary
code overlay to Railway and keeps model credentials on the cloud host. It does
not deploy or change service variables. A launched PID is not a passed replay;
inspect the printed log and JSON proofs.

## Acceptance and promotion

Compare the same model, questions and verified corpus: quoted detailed request,
guest-only continuation, short follow-up, two-episode comparison, source proposal
and separate confirmation. Source changes apply only to a disposable cloned DB.
Collect answer artifacts, full-reading coverage, repair count, elapsed time and
provider token counters. Framework cost estimates are not audited billing; an
unknown price is not zero. Small replay samples are not a general intelligence
ranking. Review actual content and privacy boundaries before promotion.

No production migration or additional Feishu message is implied by a passing
pilot. A future rollout must keep a restricted canary and immediate rollback to
`agents_sdk`, with unchanged daily delivery targets and schedule.
