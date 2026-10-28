"""Real SDK/tool execution, deterministic inference, production incident evidence."""
import asyncio
import json
from pathlib import Path

from agents import ModelResponse, Usage
from openai.types.responses.response_usage import InputTokensDetails
from test_research_library import LibraryFixture, ScriptedModel, final_output, tool_call

from news_officer.agent_runtime import run_research
from news_officer.models import Episode, IncomingMessage, Transcript
from news_officer.podcast_archive import PodcastArchive
from news_officer.research_agent import (
    INSTRUCTIONS,
    TOOLS,
    PodcastResearchAgent,
    ResearchTools,
)


class AuditScript(ScriptedModel):
    async def get_response(self, **kwargs):
        self.inputs.append(kwargs['input'])
        self.systems = getattr(self, 'systems', []) + [kwargs.get('system_instructions', '')]
        return ModelResponse(next(self.outputs), Usage(requests=1, input_tokens=100, output_tokens=20,
                             input_tokens_details=InputTokensDetails(cached_tokens=60, cache_write_tokens=0)), None)


class EpisodeGroundingTests(LibraryFixture):
    def setUp(self):
        super().setUp()
        self.fixture = json.loads((Path(__file__).parent / 'fixtures/qa_cpu_latent_space.json').read_text())
        ep = self.fixture['episode']
        self.record = self.store.save_verified_transcript(
            Episode(ep['episode_id'], ep['title'], ep['url'], ep['show_name']),
            Transcript(self.fixture['transcript']['transcript_text'], 'verified', ep['url'], True))
        self.store.save_transcript_digest(
            self.record.episode.id, self.fixture['digest']['digest_markdown'],
            self.record.content_sha256, self.record.record_revision_sha256)
        self.library = PodcastArchive(self.store)
        self.agent = PodcastResearchAgent(self.store, self.registry, self.library, 'test', 'test')
        self.agent.initialize()
        self.question = self.fixture['turn']['question']
        self.state = ResearchTools(self.agent, 'test', IncomingMessage('test', 'test', self.question, 'p2p', 'test'))
        self.token = self.record.reference

    def task_call(self):
        return tool_call('set_research_task', {'goal': '澄清并没有 CPU 节省',
                                             'document_ids': [self.token], 'format': 'brief'})

    def answer(self, text):
        ids = [i for i, t in self.state.chunks(self.token)
               if 'messages API written in Rust' in t or '40 to 50 percent' in t]
        return final_output({'kind': 'answer', 'message': '', 'points': [
            {'text': text, 'citations': [{'id': i} for i in ids]}]})

    def run_script(self, outputs):
        model = self.agent.sdk_model = AuditScript(outputs)
        answer = asyncio.run(run_research(self.agent, self.state,
                             [{'role': 'user', 'content': self.question}], INSTRUCTIONS, TOOLS))
        return answer, model

    def test_full_evidence_before_draft_and_negative_claim_rewritten(self):
        bad = '访谈没有披露具体的 CPU 降本技术或节省比例。'
        good = 'Messages API 从 Python 迁移到 Rust，CPU 节省约 40%–50%；Katelyn Lesse 在 [56:08] 说 “messages API written in Rust”。'
        answer, model = self.run_script([
            self.task_call(), self.answer(bad),
            final_output({'unsupported_points': [0], 'reason': '[58:53] 40 to 50 percent contradicts 未披露比例'}),
            self.answer(good), final_output({'unsupported_points': [], 'reason': 'supported'})])
        drafting_input = json.dumps(model.inputs[1], ensure_ascii=False) + str(model.systems[1])
        self.assertIn('messages API written in Rust', drafting_input)
        self.assertIn('40 to 50 percent', drafting_input)
        self.assertIn('40%–50%', drafting_input)
        self.assertNotIn('messages API written in Rust', json.dumps(model.inputs[1], ensure_ascii=False))
        task_result = next(json.loads(item['output']) for item in model.inputs[1]
                           if item.get('type') == 'function_call_output')
        self.assertEqual(task_result['remaining_reading'], [])
        self.assertEqual(task_result['primary_evidence']['location'], 'stable evidence prefix')
        self.assertEqual(self.state.task['goal'], self.question)
        self.assertNotIn('澄清并没有', self.state.task['resolved_references'])
        audit_input = json.dumps(model.inputs[2], ensure_ascii=False)
        self.assertIn('40 to 50 percent', audit_input)
        self.assertIn('40%–50%', audit_input)
        self.assertNotIn('Let it rip', audit_input)
        self.assertNotIn('Let it rip', json.dumps(model.inputs[3], ensure_ascii=False))
        self.assertNotIn('Let it rip', str(model.systems[3]))
        self.assertIn('Rust', answer)
        self.assertIn('Katelyn Lesse', answer)
        self.assertIn('[56:08]', answer)
        self.assertIn('messages API written in Rust', answer)
        self.assertNotIn('没有披露', answer)
        self.assertTrue(any('claim_verifier:' in e for e in self.state.validation_errors))
        self.assertEqual(len(model.inputs), 5)
        self.assertEqual(self.state.engine_metrics['total_model_calls'], 5)
        self.assertEqual(self.state.engine_metrics['input_tokens'], 500)
        self.assertEqual(self.state.engine_metrics['cached_input_tokens'], 300)
        self.assertEqual(self.state.engine_metrics['output_tokens'], 100)
        self.assertEqual(self.state.incomplete_documents(), [])

    def test_still_unsupported_number_is_dropped_after_one_rewrite(self):
        answer, model = self.run_script([
            self.task_call(), self.answer('CPU 节省 99%。'),
            final_output({'unsupported_points': [], 'reason': 'model mistakenly approved'}),
            self.answer('CPU 节省 98%。'),
            final_output({'unsupported_points': [], 'reason': 'model mistakenly approved'})])
        self.assertNotIn('98%', answer)
        self.assertNotIn('99%', answer)
        self.assertEqual(self.state.outcome, 'validation_failed')
        self.assertEqual(len(model.inputs), 5)

    def test_oversize_requires_paging_even_for_brief(self):
        from news_officer.library import LibraryDocument
        self.state.documents = {'large': LibraryDocument('large', 'Large', '', 'word ' * 130000)}
        result = self.state.execute('set_research_task', {
            'goal': 'summary', 'document_ids': ['large'], 'format': 'brief'})
        self.assertTrue(result['primary_evidence']['requires_paging'])
        self.assertTrue(self.state.incomplete_documents())

    def test_task_goal_includes_recent_verbatim_conversation(self):
        from news_officer.research_agent import conversation_goal
        original = self.question
        self.state.message = IncomingMessage('test', 'test', '那TTFT呢', 'p2p', 'test')
        self.state.conversation_goal = conversation_goal([{'question': original, 'answer': 'Rust 迁移。'}], '那TTFT呢')
        result = self.state.execute('set_research_task', {'goal': '模型改写', 'document_ids': [self.token], 'format': 'brief'})
        self.assertIn(original, result['task']['goal'])
        self.assertTrue(result['task']['goal'].endswith('那TTFT呢'))
        self.assertNotIn('模型改写', result['task']['goal'])

    def test_stale_digest_is_not_primary_evidence(self):
        with self.store._connect() as db:
            db.execute("UPDATE episode_digests SET source_sha256='stale'")
        evidence = self.state.primary_evidence([self.token])
        self.assertEqual(evidence['documents'][0]['digest'], '')
        self.assertIn('40 to 50 percent', json.dumps(evidence))

    def test_checkpoint_preserves_evidence_prefix_and_usage(self):
        from news_officer.research_checkpoint import restore_checkpoint, save_checkpoint
        self.state.evidence_prefix = json.dumps(self.state.primary_evidence([self.token]), ensure_ascii=False, sort_keys=True)
        self.state.compact_repair = True
        self.state.usage_totals = {'input_tokens': 120, 'cached_input_tokens': 80, 'output_tokens': 20}
        messages = [{'role': 'user', 'content': self.question}]
        save_checkpoint(self.state, messages)
        resumed = ResearchTools(self.agent, self.state.key, self.state.message)
        self.assertEqual(restore_checkpoint(resumed), messages)
        self.assertEqual(resumed.evidence_prefix, self.state.evidence_prefix)
        self.assertEqual(resumed.usage_totals, self.state.usage_totals)
        self.assertTrue(resumed.compact_repair)

    def test_replay_uses_readonly_source_and_disposable_copy(self):
        import importlib.util
        import io
        import sys
        from contextlib import redirect_stdout
        from unittest.mock import patch

        spec = importlib.util.spec_from_file_location('replay_qa', Path(__file__).parents[1] / 'scripts/replay_qa.py')
        replay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(replay)
        original = self.store.path.read_bytes()

        async def research(owner, state, messages, instructions, definitions):
            self.assertNotEqual(owner.store.path, self.store.path)
            self.assertEqual({t['name'] for t in definitions}, replay.READ_TOOLS)
            self.assertIsNone(owner.podcast_service)
            state.execute('set_research_task', {'goal': self.question,
                          'document_ids': [self.token], 'format': 'brief'})
            return 'offline replay'

        output = io.StringIO()
        with patch.object(sys, 'argv', ['replay_qa', '--db', str(self.store.path), '--question', self.question]), \
                patch.dict('os.environ', {'OPENAI_API_KEY': 'offline'}), \
                patch.object(replay, 'run_research', research), redirect_stdout(output):
            replay.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result['answer'], 'offline replay')
        self.assertEqual(result['tools'][0]['tool'], 'set_research_task')
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_eval_reports_tokens_and_keeps_original_database_unchanged(self):
        import importlib.util
        import io
        import sys
        from contextlib import redirect_stdout
        from unittest.mock import patch

        spec = importlib.util.spec_from_file_location('replay_eval', Path(__file__).parents[1] / 'scripts/replay_eval.py')
        replay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(replay)
        original = self.store.path.read_bytes()
        seen = []

        async def research(owner, state, messages, instructions, definitions):
            self.assertNotEqual(owner.store.path, self.store.path)
            self.assertIsNone(owner.podcast_service)
            self.assertEqual({t['name'] for t in definitions}, replay.READ_TOOLS)
            seen.append(messages)
            if state.message.text == 'TTFT改善多少':
                self.assertIn(self.question, state.conversation_goal)
            state.engine_metrics = {'elapsed_seconds': 2, 'input_tokens': 100, 'cached_input_tokens': 60,
                                    'output_tokens': 20, 'total_model_calls': 3}
            return 'Rust Python 40–50% 10–20% Katelyn messages API Claude 实现 shadow 流量 CPU sandbox CI 凭证 proxy vault'

        output = io.StringIO()
        with patch.object(sys, 'argv', ['replay_eval', '--db', str(self.store.path), '--runs', '2', '--concurrency', '2']), \
                patch.dict('os.environ', {'OPENAI_API_KEY': 'offline'}), \
                patch.object(replay, 'run_research', research), redirect_stdout(output):
            replay.main()
        result = json.loads(output.getvalue())
        self.assertEqual(len(seen), 16)
        self.assertEqual(len(result['cases']), 7)
        self.assertTrue(all(c['pass_rate'] == 1 for c in result['cases']))
        self.assertEqual(result['cases'][0]['mean_input_tokens'], 100)
        self.assertEqual(result['cases'][0]['mean_cached_input_tokens'], 60)
        self.assertEqual(result['cases'][0]['p90_latency'], 2)
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_known_episode_doc_link_resolves_public_episode_only(self):
        from news_officer.research_context import linked_episode_context
        with self.store._connect() as db:
            db.execute("CREATE TABLE daily_documents(day TEXT, document_id TEXT)")
            db.execute("INSERT INTO daily_documents VALUES (?, ?)",
                       ('episode:' + self.record.episode.id, 'known_doc'))
            db.execute("INSERT INTO daily_documents VALUES ('2026-10-28', 'daily_doc')")
        resolved = linked_episode_context(self.store, '这篇 https://team.feishu.cn/docx/known_doc?x=1')
        self.assertEqual(resolved[0]['document_id'], self.token)
        self.assertEqual(linked_episode_context(self.store, 'https://team.feishu.cn/docx/daily_doc'), [])
        self.assertEqual(linked_episode_context(self.store, 'https://other.test/docx/known_doc'), [])

    def test_conversation_cannot_bypass_episode_claim_audit(self):
        bad = final_output({'kind': 'conversation', 'message': '访谈未披露 CPU 节省比例。', 'points': []})
        answer, _ = self.run_script([
            self.task_call(), bad,
            final_output({'unsupported_points': [0], 'reason': 'absence contradicted by 40–50%'}),
            bad, final_output({'unsupported_points': [0], 'reason': 'still contradicted'})])
        self.assertNotIn('未披露', answer)
        self.assertEqual(self.state.outcome, 'validation_failed')
