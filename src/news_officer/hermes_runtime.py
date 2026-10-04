"""Optional Hermes pilot. Real Hermes loop in a dependency-isolated subprocess.

Only the parent owns domain capabilities/Feishu credentials. This is a tool
allowlist and credential boundary, NOT an OS sandbox for untrusted Python code.
Production keeps the existing SDK unless an operator explicitly selects Hermes.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from .agent_runtime import MAX_MODEL_TURNS, ResearchAnswer

HERMES_COMMIT = "d177b119e9c56c9ddc0b7379ffce52341ec06584"
MAX_EVENTS = 100
MAX_LINE = 2_000_000


def child_environment(home: Path) -> dict[str, str]:
    # Never inherit Feishu/Podwise/GitHub credentials, custom provider settings,
    # PYTHONPATH, personal profiles, plugins, or observability exporters.
    return {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHON_DOTENV_DISABLED": "1",
        "HERMES_HOME": str(home),
        "HERMES_ENABLE_PROJECT_PLUGINS": "false",
    }


def session_profile(store_path: Path, key: str) -> Path:
    return Path(store_path).parent / "hermes-pilot" / hashlib.sha256(key.encode()).hexdigest()


class ToolBroker:
    def __init__(self, state, definitions):
        self.state = state
        self.allowed = {d["name"] for d in definitions}
        self.answer = None
        self.calls = 0

    def invoke(self, name, args):
        if self.answer is not None:
            return {"error": "Answer already accepted; no more actions allowed"}
        self.calls += 1
        if self.calls > 40:
            raise RuntimeError("Hermes tool budget exceeded")
        if name == "submit_answer":
            try:
                value = ResearchAnswer.model_validate(args).model_dump()
                self.answer = self.state.render(value)
                return {"accepted": True}
            except (ValueError, TypeError, KeyError) as exc:
                # Only application-authored render errors can reach the model.
                reason = str(exc)[:1200] if type(exc) is ValueError else "Invalid answer shape"
                self.state.validation_errors.append(reason)
                return {"accepted": False, "error": reason,
                        "next": "Fix this error using existing evidence or read the missing chunks; then submit_answer again."}
        if name not in self.allowed:
            return {"error": "Tool is not authorized in this session"}
        started = time.monotonic()
        status = "ok"
        try:
            result = self.state.execute(name, args)
            if isinstance(result, dict) and "error" in result:
                status = "error"
            return result
        except (ValueError, TypeError, KeyError):
            status = "invalid_arguments"
            return {"error": "Invalid tool arguments; use actual returned IDs"}
        except Exception:  # noqa: BLE001 - sanitize external tool failures
            status = "external_error"
            return {"error": "Tool failed; this is not an empty search or a successful action"}
        finally:
            self.state.steps.append({"tool": name, "status": status,
                                     "elapsed_ms": round((time.monotonic() - started) * 1000)})


def _read_events(stream, events):
    try:
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                events.put(None)
                return
            if len(line) > MAX_LINE:
                raise ValueError("Worker protocol line too large")
            events.put(json.loads(line))
    except (ValueError, OSError, TypeError):
        events.put({"type": "error", "code": "invalid_worker_protocol"})


def run_hermes(owner, state, messages, instructions, definitions, *, timeout=480):
    python = Path(owner.hermes_python)
    if not python.is_absolute() or not python.is_file():
        raise RuntimeError("Hermes pilot requires an absolute, installed NEWS_OFFICER_HERMES_PYTHON")
    home = session_profile(owner.store.path, state.key)
    scope = home.name
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    worker = Path(__file__).with_name("hermes_worker.py")
    broker = ToolBroker(state, definitions)
    schema = ResearchAnswer.model_json_schema()
    payload = {"api_key": owner._api_key, "model": owner.model,
               "instructions": instructions, "messages": messages,
               "tools": definitions, "answer_schema": schema,
               "session_id": scope, "max_iterations": MAX_MODEL_TURNS,
               "timeout": timeout - 15}
    started = time.monotonic()
    events = queue.Queue()
    state.engine_metrics = {"backend": "hermes", "commit": HERMES_COMMIT}
    process = subprocess.Popen(
        [str(python), "-I", str(worker)], cwd=home,
        env=child_environment(home), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        # Upstream stderr may include request data. Never forward or persist it.
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
    )
    reader = threading.Thread(target=_read_events, args=(process.stdout, events), daemon=True)
    reader.start()
    try:
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        process.stdin.flush()
        for _ in range(MAX_EVENTS):
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Hermes pilot deadline exceeded")
            try:
                event = events.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("Hermes pilot deadline exceeded") from None
            if event is None:
                raise RuntimeError("Hermes worker exited before completion")
            if progress := getattr(owner, "pilot_progress", None):
                progress({k: event[k] for k in ("type", "name", "step", "stage", "code") if k in event})
            if event.get("type") == "ready":
                if set(event.get("tools", [])) != broker.allowed | {"submit_answer"}:
                    raise RuntimeError("Unexpected Hermes tool surface")
            elif event.get("type") == "tool":
                result = broker.invoke(event["name"], event["args"])
                process.stdin.write(json.dumps(result, ensure_ascii=False) + "\n")
                process.stdin.flush()
            elif event.get("type") == "progress":
                state.model_calls = int(event["step"])
            elif event.get("type") == "done":
                state.model_calls = int(event.get("api_calls", 0))
                state.engine_metrics.update(event.get("usage", {}))
                if broker.answer is None:
                    raise RuntimeError("Hermes returned without an evidence-validated answer")
                return broker.answer
            else:
                raise RuntimeError(f"Hermes worker failed at {event.get('stage', 'protocol')} ({event.get('code', 'unknown')}); private diagnostics suppressed")
        raise RuntimeError("Hermes protocol event budget exceeded")
    finally:
        state.engine_metrics["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        reader.join(timeout=2)
        process.stdout.close()
