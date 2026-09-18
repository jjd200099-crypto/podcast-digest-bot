"""OpenAI Agents SDK execution, separate from Feishu delivery and domain tools.

The SDK owns the model/tool loop. The application owns authorization, validated
evidence, durable dialogue, and idempotent writes. No model can bypass those.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
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
from openai import AsyncOpenAI
from pydantic import BaseModel

from .library import LibraryError

LOGGER = logging.getLogger(__name__)
MAX_MODEL_TURNS = 16
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

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        if self.calls >= MAX_MODEL_TURNS:
            raise BudgetExceeded()
        self.calls += 1


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
        if len(state.steps) >= 32:
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


async def run_research(owner, state, messages, instructions, definitions):
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
        config = RunConfig(
            tracing_disabled=True,
            trace_include_sensitive_data=False,
            workflow_name="podcast-research",
            tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1),
        )
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
                    return state.render(result.final_output.model_dump())
                except (ValueError, TypeError, KeyError) as error:
                    # Render errors are application-authored and contain no source
                    # text or credentials. Keep their specific cause for repair
                    # and audit, rather than silently treating every failure alike.
                    reason = str(error) if isinstance(error, ValueError) else type(error).__name__
                    state.validation_errors.append(reason[:1200])
                    LOGGER.info("Research answer validation: %s", reason[:1200])
                    messages = result.to_input_list()
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
        except (MaxTurnsExceeded, BudgetExceeded):
            state.outcome = "budget_exhausted"
        finally:
            state.model_calls = budget.calls
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
