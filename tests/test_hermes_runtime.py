import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

from news_officer.hermes_runtime import (
    ToolBroker,
    _read_events,
    child_environment,
    session_profile,
)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.executed = []
        self.state = SimpleNamespace(steps=[], validation_errors=[],
                                     execute=lambda n, a: self.executed.append((n, a)) or {"ok": True},
                                     render=lambda v: v["message"])
        self.broker = ToolBroker(self.state, [{"name": "read_document"}])

    def test_unadvertised_tool_cannot_execute(self):
        for name in ["terminal", "execute_code", "read_file", "send_message", "memory", "confirm_source"]:
            self.assertIn("error", self.broker.invoke(name, {}))
        self.assertEqual(self.executed, [])

    def test_tool_calls_reuse_domain_executor_and_audit(self):
        self.assertTrue(self.broker.invoke("read_document", {"document_id": "actual"})["ok"])
        self.assertEqual(self.executed[0][0], "read_document")
        self.assertEqual(self.state.steps[0]["status"], "ok")

    def test_no_side_effect_after_answer_is_accepted(self):
        self.assertTrue(self.broker.invoke("submit_answer", {
            "kind": "conversation", "message": "你好", "points": []})["accepted"])
        self.assertIn("error", self.broker.invoke("read_document", {}))
        self.assertEqual(self.executed, [])

    def test_validation_error_is_repairable_not_success(self):
        def reject(_):
            raise ValueError('Full reading incomplete; next_start=12')
        self.state.render = reject
        result = self.broker.invoke("submit_answer", {"kind": "answer", "message": "", "points": []})
        self.assertFalse(result["accepted"])
        self.assertIn("next_start=12", result["error"])
        self.assertIsNone(self.broker.answer)

    def test_provider_error_cannot_leak_secret(self):
        def fail(*_):
            raise RuntimeError("secret-key-must-not-leak")
        self.state.execute = fail
        self.assertNotIn("secret-key", json.dumps(self.broker.invoke("read_document", {})))
        self.assertEqual(self.state.steps[0]["status"], "external_error")

    def test_environment_does_not_inherit_secrets(self):
        with patch.dict(os.environ, {"FEISHU_APP_SECRET": "secret", "OPENAI_API_KEY": "key",
                                     "PODWISE_API_TOKEN": "token", "PYTHONPATH": "/personal/code"}):
            env = child_environment(Path("/tmp/isolated-profile"))
        self.assertFalse(set(env) & {"FEISHU_APP_SECRET", "OPENAI_API_KEY", "PODWISE_API_TOKEN", "PYTHONPATH"})
        self.assertEqual(env["HERMES_HOME"], "/tmp/isolated-profile")

    def test_profiles_separate_chats_senders_and_path_traversal(self):
        root = Path("/tmp/pilot/state.db")
        keys = ["group:g:alice", "group:g:bob", "group:other:alice", "p2p:alice", "../../alice"]
        paths = [session_profile(root, key) for key in keys]
        self.assertEqual(len(set(paths)), len(keys))
        self.assertTrue(all(p.parent == root.parent / "hermes-pilot" for p in paths))
        self.assertTrue(all(len(p.name) == 64 for p in paths))

    def test_invalid_protocol_and_end_of_stream(self):
        events = Queue()
        _read_events(StringIO('not-json\n'), events)
        self.assertEqual(events.get()["code"], "invalid_worker_protocol")
        events = Queue()
        _read_events(StringIO('{"type":"done"}\n'), events)
        self.assertEqual(events.get()["type"], "done")
        self.assertIsNone(events.get())


@unittest.skipUnless(os.environ.get("TEST_HERMES_PYTHON"), "Optional pinned Hermes environment")
class ActualHermesTests(unittest.TestCase):
    def test_real_runtime_starts_with_only_brokered_tools(self):
        """Real Hermes builds prompts/session/toolsets; fake only model inference."""
        import subprocess
        worker = Path(__file__).resolve().parents[1] / "src/news_officer/hermes_worker.py"
        with tempfile.TemporaryDirectory() as directory:
            # A zero-iteration runtime should still expose the exact registry,
            # but never make an API call. No real key is present in this test.
            payload = {"model": "gpt-5.6-terra", "api_key": "offline-test",
                       "tools": [], "answer_schema": {"type": "object", "properties": {}},
                       "max_iterations": 0, "timeout": 5, "session_id": "isolation-test", "probe_only": True,
                       "instructions": "Test", "messages": [{"role": "user", "content": "hello"}]}
            result = subprocess.run([os.environ["TEST_HERMES_PYTHON"], "-I", str(worker)],
                                    input=json.dumps(payload) + "\n", text=True, cwd=directory,
                                    env=child_environment(Path(directory)), capture_output=True, timeout=45, check=False)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertTrue(events, result.stderr)
            self.assertEqual(events[0]["type"], "ready", events)
            self.assertEqual(events[0]["tools"], ["submit_answer"])


if __name__ == "__main__":
    unittest.main()
