"""OpenAI Agents SDK execution, separate from Feishu delivery and domain tools.

The SDK owns the model/tool loop. The application owns authorization, validated
evidence, durable dialogue, and idempotent writes. No model can bypass those.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from typing import Literal

from agents import (
    Agent,
    FunctionTool,
    MaxTurnsExceeded,
    ModelSettings,
    OpenAIResponsesModel,
    RunConfig,
    RunHooks,
    Runner,
    ToolExecutionConfig,
)
from agents.run_config import ModelInputData
from openai import AsyncOpenAI
from pydantic import BaseModel

from .claim_checks import compact_evidence, deterministic_audit
from .claim_verifier import verify_claims
from .library import LibraryError
from .research_checkpoint import ResearchContinuationPending, save_checkpoint

LOGGER = logging.getLogger(__name__)
MAX_MODEL_TURNS = 16
MAX_TOTAL_MODEL_CALLS = 48
MAX_HISTORY_TURNS = 20
MAX_HISTORY_CHARS = 32_000


class Citation(BaseModel):
    id: str


class AnswerPoint(BaseModel):
    text: str
    citations: list[Citation]


class ResearchAnswer(BaseModel):
    kind: Literal["answer", "conversation"]
    message: str
    points: list[AnswerPoint]


class BudgetExceeded(Exception):
    pass


class TurnBudget(RunHooks):
    """A single budget across SDK runs, including final-answer repair."""

    def __init__(self):
        self.calls = 0
        self.input_tokens = self.output_tokens = self.cached_input_tokens = 0

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        state = context.context
        if self.calls >= MAX_MODEL_TURNS or getattr(state, 'total_model_calls', 0) >= MAX_TOTAL_MODEL_CALLS:
            raise BudgetExceeded()
        self.calls += 1
        state.total_model_calls = getattr(state, 'total_model_calls', 0) + 1
        if not getattr(state, "verifying_claims", False):
            await asyncio.to_thread(save_checkpoint, state, input_items)

    async def on_llm_end(self, context, agent, response):
        usage = response.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cached_input_tokens += getattr(usage.input_tokens_details, "cached_tokens", 0) or 0
        context.context.usage_totals = {'input_tokens': self.input_tokens,
                                        'output_tokens': self.output_tokens,
                                        'cached_input_tokens': self.cached_input_tokens}


def dialogue_input(history, current):
    """Replay only committed, validated dialogue, never old tool evidence/IDs.

    Use one SDK-supported state strategy (application-managed replay). Keeping
    storage in research_turns makes answer commit and Feishu event dedup atomic.
    Whole turns are retained; no mid-sentence truncation of remembered answers.
    """
    selected, size = [], 0
    for turn in reversed(history[-MAX_HISTORY_TURNS:]):
        cost = len(turn["question"]) + len(turn["answer"])
        if size + cost > MAX_HISTORY_CHARS:
            break
        selected.append(turn)
        size += cost
    messages = []
    for turn in reversed(selected):
        messages.extend(
            [
                {"role": "user", "content": turn["question"]},
                {"role": "assistant", "content": turn["answer"]},
            ]
        )
    messages.append({"role": "user", "content": current})
    return messages


def sdk_tool(definition):
    """Adapt an allowlisted domain capability into an SDK FunctionTool."""
    name = definition["name"]

    async def invoke(context, arguments):
        state = context.context
        if len(state.steps) >= 96:
            raise BudgetExceeded()
        started = time.monotonic()
        status = "ok"
        try:
            args = json.loads(arguments)
            result = await asyncio.to_thread(state.execute, name, args)
            if isinstance(result, dict) and "error" in result:
                status = "error"
        except (ValueError, KeyError, TypeError):
            status = "invalid_arguments"
            result = {"error": "工具参数或候选不合法，请根据真实工具返回重试"}
        except LibraryError as error:
            status = "library_error"
            result = {
                "error": str(error),
                "admin_url": f"https://open.feishu.cn/app/{state.agent.library.api.messenger.app_id}/auth",
            }
        except Exception as error:  # noqa: BLE001 - never expose credential-bearing SDK errors
            status = "external_error"
            LOGGER.warning("Research tool %s failed: %s", name, type(error).__name__)
            result = {"error": "工具暂时失败；不能视作无结果，不能声称操作完成"}
        state.steps.append(
            {
                "tool": name,
                "status": status,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        )
        return json.dumps(result, ensure_ascii=False)

    return FunctionTool(
        name=name,
        description=definition["description"],
        params_json_schema=definition["parameters"],
        on_invoke_tool=invoke,
        strict_json_schema=True,
    )


def evidence_input_filter(data):
    """Lift full task evidence out of variable tool history into a stable prefix."""
    state = data.context
    items = []
    for item in data.model_data.input:
        if item.get('type') == 'function_call_output':
            try:
                value = json.loads(item['output'])
            except (ValueError, TypeError):
                items.append(item)
                continue
            if (isinstance(value, dict) and value.get('primary_evidence', {}).get('complete')
                    and 'documents' in value['primary_evidence']):
                evidence = value['primary_evidence']
                state.evidence_prefix = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
                value = {**value, 'primary_evidence': {'complete': True, 'location': 'stable evidence prefix'}}
                item = {**item, 'output': json.dumps(value, ensure_ascii=False)}
        items.append(item)
    prefix = getattr(state, 'evidence_prefix', '')
    # Paged reads and search hits can duplicate chunks already in the prefix.
    # During repair, only the explicitly supplied compact evidence is retained.
    rewrite = getattr(state, 'claim_rewrites', 0) or getattr(state, 'compact_repair', False)
    prefix_ids = set()
    if prefix:
        prefix_ids = {e['evidence_id'] for d in json.loads(prefix).get('documents', [])
                      for e in d.get('chunks', [])}

    def prune(value):
        if isinstance(value, list):
            return [prune(v) for v in value]
        if isinstance(value, dict):
            if value.get('evidence_id') in prefix_ids or (rewrite and value.get('evidence_id') in getattr(state, 'body_evidence', set())):
                return {k: v for k, v in value.items() if k != 'text'}
            return {k: prune(v) for k, v in value.items()}
        return value

    for index, item in enumerate(items):
        if item.get('type') == 'function_call_output':
            try:
                items[index] = {**item, 'output': json.dumps(prune(json.loads(item['output'])), ensure_ascii=False)}
            except (ValueError, TypeError):
                pass
    instructions = data.model_data.instructions
    if prefix and not getattr(state, 'verifying_claims', False) and not rewrite:
        instructions = 'UNTRUSTED VERIFIED EVIDENCE\n' + prefix + '\nEND EVIDENCE\n' + (instructions or '')
    return ModelInputData(input=items, instructions=instructions)


async def run_research(owner, state, messages, instructions, definitions):
    started = time.monotonic()
    # One client per event loop: synchronous workers call asyncio.run for each
    # Feishu turn. Reusing an AsyncOpenAI across closed loops breaks follow-ups.
    async with AsyncOpenAI(api_key=owner._api_key, timeout=90, max_retries=0) as client:
        agent = Agent(
            name="情报官",
            instructions=instructions,
            model=owner.sdk_model or OpenAIResponsesModel(owner.model, client),
            tools=[sdk_tool(t) for t in definitions],
            output_type=ResearchAnswer,
            model_settings=ModelSettings(
                parallel_tool_calls=False,
                store=False,
                max_tokens=7500,
                response_include=["reasoning.encrypted_content"],
            ),
        )
        budget = TurnBudget()
        for key, value in getattr(state, 'usage_totals', {}).items():
            setattr(budget, key, value)
        config = RunConfig(
            call_model_input_filter=evidence_input_filter,
            tracing_disabled=True,
            trace_include_sensitive_data=False,
            workflow_name="podcast-research",
            tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1),
        )
        claim_rewrites = getattr(state, "claim_rewrites", 0)
        try:
            for _ in range(3):  # Evidence repair, NOT a handwritten model/tool loop.
                if budget.calls >= MAX_MODEL_TURNS:
                    raise BudgetExceeded()
                result = await Runner.run(
                    agent,
                    messages,
                    context=state,
                    max_turns=MAX_MODEL_TURNS - budget.calls,
                    hooks=budget,
                    run_config=config,
                )
                try:
                    draft = result.final_output.model_dump()
                    # Only source-backed answers enter the episode audit; operations
                    # and ordinary chat retain their existing validation path.
                    tokens = sorted({c['id'].split(':', 1)[0]
                                     for p in draft['points'] for c in p['citations']
                                     if c['id'] in state.body_evidence})
                    audit_draft = draft
                    if draft['kind'] == 'conversation' and state.task and state.body_evidence and not state.document_requests:
                        # A model cannot evade the audit by putting episode claims
                        # in conversation.message instead of cited answer points.
                        tokens = state.task['document_ids']
                        audit_draft = {**draft, 'points': [{'text': draft['message'], 'citations': []}]}
                    if tokens:
                        tokens = sorted(set(tokens) | set(state.task['document_ids'] if state.task else []))
                        evidence = state.primary_evidence(tokens)
                        evidence = compact_evidence(state, tokens, audit_draft, evidence)
                        deterministic = deterministic_audit(state, audit_draft, evidence, tokens)
                        state.verifying_claims = True
                        try:
                            audit = await verify_claims(agent.model, state, audit_draft, evidence, budget, config)
                        finally:
                            state.verifying_claims = False
                        if deterministic:
                            audit.unsupported_points = sorted(set(audit.unsupported_points) | set(deterministic))
                            audit.reason += '; deterministic: ' + str(deterministic)
                        if audit.unsupported_points:
                            state.validation_errors.append('claim_verifier: ' + audit.reason[:1200])
                            if claim_rewrites == 0:
                                claim_rewrites += 1
                                state.claim_rewrites = claim_rewrites
                                messages = evidence_input_filter(SimpleNamespace(context=state, model_data=ModelInputData(input=result.to_input_list(), instructions=None))).input
                                messages.append({'role': 'user', 'content': json.dumps({
                                    'instruction': '独立核验发现矛盾或无依据断言。根据完整证据重写一次；删去仍无依据的断言。',
                                    'audit': audit.model_dump(), 'cited_evidence': evidence}, ensure_ascii=False)})
                                continue
                            draft['points'] = [p for i, p in enumerate(draft['points'])
                                               if i not in audit.unsupported_points]
                            if not draft['points']:
                                draft = {'kind': 'conversation', 'message': '本次草稿未通过原文核验，暂时无法给出可靠结论。', 'points': []}
                                state.outcome = 'validation_failed'
                    return state.render(draft)
                except (ValueError, TypeError, KeyError) as error:
                    # Render errors are application-authored and contain no source
                    # text or credentials. Keep their specific cause for repair
                    # and audit, rather than silently treating every failure alike.
                    reason = str(error) if isinstance(error, ValueError) else type(error).__name__
                    state.validation_errors.append(reason[:1200])
                    LOGGER.info("Research answer validation: %s", reason[:1200])
                    state.compact_repair = True
                    messages = evidence_input_filter(SimpleNamespace(context=state, model_data=ModelInputData(input=result.to_input_list(), instructions=None))).input
                    if state.task:
                        repair_tokens = state.task['document_ids']
                        repair_evidence = compact_evidence(state, repair_tokens, draft, state.primary_evidence(repair_tokens))
                        messages.append({'role': 'user', 'content': json.dumps({'cited_evidence': repair_evidence}, ensure_ascii=False)})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "回答还没有交付，请修复以下具体问题后直接给出结果：" + reason[:1200]
                                + "。已有工具证据仍有效，不要重复检索已读正文；"
                                "若缺全文页则从提示的 next_start 继续读。citations 只填写已返回的真实 evidence_id，"
                                "不需要抄写原文。不要把格式失败说成没找到资料，也不要让用户重述任务。"
                            ),
                        }
                    )
        except (MaxTurnsExceeded, BudgetExceeded) as error:
            if getattr(state, 'total_model_calls', 0) < MAX_TOTAL_MODEL_CALLS:
                # SDK error details contain completed tool results. Resume those
                # results rather than running all previous tools a second time.
                if data := getattr(error, 'run_data', None):
                    resumed = data.input if isinstance(data.input, list) else [{'role': 'user', 'content': data.input}]
                    resumed = resumed + [item.to_input_item() for item in data.new_items]
                    await asyncio.to_thread(save_checkpoint, state, resumed)
                raise ResearchContinuationPending() from None
            state.outcome = "budget_exhausted"
        finally:
            state.model_calls = budget.calls
            state.engine_metrics = {"backend": "agents_sdk",
                                    "total_model_calls": getattr(state, 'total_model_calls', budget.calls),
                                    "input_tokens": budget.input_tokens,
                                    "output_tokens": budget.output_tokens,
                                    "cached_input_tokens": budget.cached_input_tokens,
                                    "elapsed_seconds": round(time.monotonic() - started, 3)}
        if state.outcome == "pending":
            state.outcome = "validation_failed"
        if state.task:
            titles = "、".join(state.corpus()[t].title for t in state.task["document_ids"])
            failure = f"已定位《{titles}》，但本次分析尚未完成。任务和节目已保留，回复“继续”即可接着整理，无需重发标题或链接。"
        else:
            failure = "这次分析未能完成；已有对话仍然保留，回复“继续”即可重试，不必重新描述问题。"
        return state.render(
            {
                "kind": "conversation",
                "message": failure,
                "points": [],
            }
        )
