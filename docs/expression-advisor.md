# Expression advisor

The main Agent still researches, uses tools, and owns the final answer. An optional
DeepSeek Flash call first recommends the response's stance, opening, detail level,
and wording style. These are finite validated labels, not free-form instructions,
factual claims, tool permissions, or a second answer pasted over the first.
One advisor considers three lenses (situation, interpersonal response, expression)
in one call; this is not three autonomous agents debating every greeting.

The main model receives the plan as low-trust context and natural-expression
guidance. It writes and revises its own final response. Existing evidence,
completion, access-control and delivery validation remains in place. Explicit
user requests for detailed research take priority over a concise-style hint.

## Enable

Set `DEEPSEEK_API_KEY` in the existing cloud service's secret variables and set
`NEWS_OFFICER_TONE_ADVISOR=true`. Default model: `deepseek-flash`; endpoint fixed
to `https://api.deepseek.com`. No OpenAI key fallback. The feature is off by default.
If disabled, missing a key, timed out or invalid, the main Agent answers normally.

Each new conversation turn gets at most one advisory call per attempt, with a
four-second wall-clock deadline, no retries, non-thinking mode and 500 output
tokens. Completed-message dedup and restored research checkpoints skip new advice.
The advisor gets only the current question (up to 1600 characters) and the latest
two same-session turns (up to 500 question and 900 answer characters each).
It does not get the source archive, tool results, user IDs or service environment.
Recognizable credential strings are masked; this does not guarantee removal of
arbitrary sensitive prose. These excerpts are sent to DeepSeek when enabled.
No persistent personality/emotion profile is created. Failures log only their
exception class, never raw provider errors, prompts or credentials.

## Quality acceptance

### Reviewed conversational style reference

On 2026-09-18 the user authorized a one-time review of recent conversations in
three named Feishu groups through their own account. Recent visible samples were
read in the signed-in desktop client; this was **not** a complete seven-day export.
No bot membership or extra bot permissions were needed for that review.

Only manually reviewed, generalized style rules enter `COLLEAGUE_STYLE`, shared
by the advisor and main Agent: continue the actual context, state a concrete view
with reasons, disagree constructively, explain specific evidence gaps, and match
depth to the request rather than the length of the latest message. Illustrative
scenarios are synthetic, not quotes. Names, group identifiers, business details,
raw chat logs and personal profiles are not included in the reference or repo.
No message-fetching tool or personal-account credential is added to the bot.
This is prompt guidance, **not** model training or ongoing group monitoring.
The advisor's separate bounded current-conversation payload still applies as
described above; anonymizing the reference does not anonymize arbitrary live chat.

### Live comparison still required

Contract tests alone do not establish naturalness. After the cloud key is added,
compare actual main-model replies with the advisor off/on for: a greeting,
"怎么又没回答", "没看懂，讲人话", a short follow-up, excited feedback, a neutral
research question, a detailed memo request, and a request that genuinely requires
permission. Check that it acknowledges the specific concern without flattery or
invented emotions; answers the task without a generic menu; preserves citations,
numbers and actual completion state; and adds tolerable latency. Do not claim
the advisor is live or improves tone until this real-provider comparison passes.

Official API references (checked 2026-09-18):
- https://api-docs.deepseek.com/
- https://api-docs.deepseek.com/guides/json_mode
