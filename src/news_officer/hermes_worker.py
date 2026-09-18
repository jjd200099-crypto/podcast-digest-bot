"""Standalone worker run with Hermes' Python, never the application's venv.

stdin/stdout are a private broker protocol; all Hermes UI output is discarded.
No bot credentials, production database, source secrets, or shell tools here.
"""
import contextlib
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import sys
import threading
from pathlib import Path

PIN = "d177b119e9c56c9ddc0b7379ffce52341ec06584"
STAGE = "input"
CORE_HASHES = {
    "run_agent.py": "3e87bfc369d3328c4182f63af4cf27850090579740b553603786154e0e59079a",
    "agent/conversation_loop.py": "d905a62f62b24922d317483ec0a3b96678448f2b03f08d97f07db61ede04b6c1",
    "agent/agent_init.py": "9d83fbd7ed5b701bf4d6ca30d92229d49cc1088804da3e36fa59f042a76f079a",
    "tools/registry.py": "5a3e232b8b0b6ec64a994efe1dc7309d9c733c813a3284d6df44f67cc1cb24a2",
}


def main():
    global STAGE
    wire = sys.stdout
    request = json.loads(sys.stdin.readline())
    # Disable the generic deferred-tool bridge: this pilot exposes only our
    # named domain tools and checks the final surface before the first API call.
    profile = Path(os.environ["HERMES_HOME"])
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    (profile / "config.yaml").write_text(json.dumps({"tools": {"tool_search": {"enabled": "off"}}}))
    lock = threading.Lock()
    accepted = False

    def emit(value):
        wire.write(json.dumps(value, ensure_ascii=False) + "\n")
        wire.flush()

    def bridge(name):
        def invoke(args, **_kwargs):
            nonlocal accepted
            with lock:
                emit({"type": "tool", "name": name, "args": args})
                result = json.loads(sys.stdin.readline())
                if name == "submit_answer" and result.get("accepted"):
                    accepted = True
                return json.dumps(result, ensure_ascii=False)
        return invoke

    # A separate profile is supplied by the parent for each chat+sender session.
    # Profile isolation does not itself restrict OS-level filesystem access.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        STAGE = "source_verification"
        root = Path(importlib.util.find_spec("run_agent").origin).parent
        for name, expected_hash in CORE_HASHES.items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected_hash:
                raise RuntimeError("Hermes source revision mismatch")

        STAGE = "imports"
        from hermes_state import SessionDB
        from run_agent import AIAgent
        from tools.registry import registry

        STAGE = "tools"
        definitions = list(request["tools"]) + [{
            "name": "submit_answer", "description": "提交回答并校验原文证据；若返回错误，修正后再次提交。成功后结束任务。",
            "parameters": request["answer_schema"],
        }]
        for definition in definitions:
            name = definition["name"]
            registry.register(
                name=name, toolset="podcast_pilot", handler=bridge(name),
                schema={"name": name, "description": definition["description"],
                        "parameters": definition["parameters"]},
                # Preserve a whole page of transcript rather than silently truncate.
                max_result_size_chars=100_000,
            )
        db = SessionDB(Path(os.environ["HERMES_HOME"]) / "sessions.sqlite3")
        STAGE = "agent_initialization"
        agent = AIAgent(
            model=request["model"], provider="openai", api_mode="codex_responses",
            api_key=request["api_key"], base_url="https://api.openai.com/v1",
            enabled_toolsets=["podcast_pilot"],
            quiet_mode=True, verbose_logging=False, save_trajectories=False,
            skip_context_files=True, load_soul_identity=False, skip_memory=True,
            skip_background_review=True, checkpoints_enabled=False,
            session_id=request["session_id"], session_db=db,
            step_callback=lambda step, _: emit({"type": "progress", "step": step}),
            max_iterations=request["max_iterations"], max_tokens=7500,
            run_budget_seconds=request["timeout"],
            request_overrides={"store": False, "parallel_tool_calls": False},
        )
        expected = {d["name"] for d in definitions}
        STAGE = "allowlist"
        if agent.valid_tool_names != expected:
            emit({"type": "error", "code": "unexpected_tool_surface",
                  "tools": sorted(agent.valid_tool_names), "expected": sorted(expected)})
            raise RuntimeError("Hermes exposed an unapproved tool")
        emit({"type": "ready", "tools": sorted(expected),
              "version": importlib.metadata.version("hermes-agent")})
        if request.get("probe_only"):
            emit({"type": "done", "api_calls": 0, "accepted": False})
            agent.close()
            db.close()
            return
        prompt = request["instructions"] + (
            "\n当前由 Hermes 执行。最终必须通过 submit_answer 工具提交结构化回答，"
            "不要把 JSON 当普通文本输出。若校验失败，依据具体错误继续读原文或修正引用。"
            "submit_answer accepted=true 后结束，不再执行任何工具。"
        )
        history = db.get_messages_as_conversation(request["session_id"], repair_alternation=True)
        if not history:
            history = request["messages"][:-1]
        question = request["messages"][-1]["content"]
        STAGE = "conversation"
        result = agent.run_conversation(question, system_message=prompt,
                                        conversation_history=history)
        # One bounded nudge if the runtime stopped with text instead of delivery.
        if not accepted and result.get("api_calls", 0) < request["max_iterations"]:
            previous_calls = result.get("api_calls", 0)
            agent.max_iterations = request["max_iterations"] - previous_calls
            result = agent.run_conversation(
                "回答尚未提交。请用 submit_answer 提交；需要证据则继续使用工具。",
                system_message=prompt, conversation_history=result.get("messages", []))
        usage = {name: getattr(agent, name, None) for name in (
            "session_input_tokens", "session_output_tokens", "session_cache_read_tokens",
            "session_reasoning_tokens", "session_estimated_cost_usd", "session_cost_status")}
        emit({"type": "done", "accepted": accepted,
              "api_calls": agent.session_api_calls, "usage": usage})
        agent.close()
        db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - never expose provider error bodies
        # Never send provider exception text (may include credentials/source text).
        print(json.dumps({"type": "error", "code": type(exc).__name__, "stage": STAGE}), flush=True)
        sys.exit(1)
