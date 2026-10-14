import json
import unittest
from unittest.mock import patch

from test_daily_v4 import V4
from test_summarizer import EPISODE, TRANSCRIPT, summarizer_for

from news_officer.daily_enrichment import normalized_content
from news_officer.daily_report import reader_episode_v4
from news_officer.document_transcript import validate_translation
from news_officer.feishu import daily_card_delivery_parts
from news_officer.glossary import preserve_english_terms
from news_officer.library import LibraryError
from news_officer.models import DailyItem


class InteractiveCardTests(unittest.TestCase):
    def test_card_has_exact_schema_and_nested_points_and_three_part_title(self):
        summary = V4.replace('Show｜AI 成本取决于任务', 'Show｜Mike｜AI 成本取决于任务')
        section = reader_episode_v4(DailyItem(EPISODE, 'summarized', summary), 1,
                                    'https://example.com/doc')
        kind, content, uuid = daily_card_delivery_parts(
            section + '\n\n━━━━━━━━━━━━━━━━━━━━\n\n' + section,
            'daily:test', title='🎧 播客精选 · 2026-10-15')[0]
        # A delivery is always a single card and its UUID stays stable on retry.
        self.assertEqual(kind, 'interactive')
        self.assertEqual(uuid, daily_card_delivery_parts(section, 'daily:test', title='x')[0][2])
        card = json.loads(content)
        self.assertEqual(set(card), {'schema', 'body'})
        self.assertEqual(card['schema'], '2.0')
        elements = card['body']['elements']
        self.assertEqual([e['tag'] for e in elements], ['markdown', 'markdown', 'hr', 'markdown'])
        self.assertEqual(elements[0]['content'], '**🎧 播客精选 · 2026-10-15**')
        text = elements[1]['content']
        self.assertIn('【Show｜Mike｜AI 成本取决于任务】', text)
        self.assertIn('- 要点总结：\n    1.', text)
        self.assertIn('模型。\n    2.', text)
        self.assertIn('- [精读和完整中文对谈](https://example.com/doc)', text)
        self.assertIn('- [收听节目](', text)
        self.assertNotIn('重做版', content)

    def test_opaque_readback_cannot_authorize_overwriting_human_edits(self):
        with self.assertRaises(LibraryError):
            normalized_content('interactive', json.dumps({'title': None, 'elements': []}))
        card = daily_card_delivery_parts('text', 'daily:test', title='x')[0][1]
        self.assertEqual(normalized_content('interactive', card), json.loads(card))

    def test_glossary_is_source_aware_and_preserves_other_claims(self):
        source = 'Jason Ma: Our world-action model predicts future video.'
        for term in ['世界—动作模型', '世界-动作模型', '世界动作模型', 'world-action model']:
            text = f'{term}，公司称数据超过100万小时，仍需验证。'
            self.assertEqual(preserve_english_terms(text, source),
                             'world action model，公司称数据超过100万小时，仍需验证。')
            self.assertEqual(preserve_english_terms(text, '无关原文'), text)
        summary = V4.replace('AI 成本', '世界—动作模型')
        rendered = reader_episode_v4(DailyItem(EPISODE, 'summarized', summary), 1, source_text=source)
        self.assertIn('world action model', rendered)
        self.assertNotIn('世界—动作模型', rendered)

    def test_generation_prompt_and_postprocessing_preserve_english(self):
        from dataclasses import replace
        source = replace(TRANSCRIPT, text=TRANSCRIPT.text + '\nworld action model')
        summary = V4.replace('AI 成本', '世界—动作模型')
        instance = summarizer_for(summary)
        with patch.dict('os.environ', {'NEWS_OFFICER_DAILY_LAYOUT': 'v4'}):
            generated = instance.summarize(EPISODE, source)
        self.assertIn('world action model', generated)
        instructions = instance.client.responses.calls[0]['instructions']
        self.assertIn('短节目名｜真实嘉宾姓名｜一句中文核心主题', instructions)
        self.assertIn('术语表', instructions)

    def test_translation_normalizes_alias_without_changing_segment_ids(self):
        source = [{'id': 1, 'text': 'world action model'}]
        translated = [{'id': 1, 'text': '世界—动作模型可以预测未来。'}]
        self.assertEqual(validate_translation(source, translated),
                         [{'id': 1, 'text': 'world action model可以预测未来。'}])
        self.assertEqual(translated[0]['text'], '世界—动作模型可以预测未来。')

    def test_deepread_glossary_reaches_core_chapters_and_dialogue(self):
        from test_deepread_new_style import record, sample

        from news_officer.deepread import source_evidence, validate_deepread
        original = record()
        original.transcript.text += "\nJohn Smith: Our world action model predicts future video."
        value = sample()
        value['core'][0]['text'] += '世界—动作模型。'
        value['chapters'][0]['title'] += '世界-动作模型'
        value['chapters'][0]['turns'][1]['paragraphs'][0] += '世界动作模型。'
        output = validate_deepread(value, source_evidence(original.transcript.text))
        self.assertIn('world action model', output['core'][0]['text'])
        self.assertIn('world action model', output['chapters'][0]['title'])
        self.assertIn('world action model', output['chapters'][0]['turns'][1]['paragraphs'][0])
        self.assertIn('世界—动作模型', value['core'][0]['text'])


class OpaqueCardAPI:
    def __init__(self, row):
        from types import SimpleNamespace
        self.messenger = SimpleNamespace(app_id='bot')
        self.message_id = row['remote_message_id']
        self.chat_id = row['target_id']
        self.updated = False
        self.revision = 1000
        self.patches = []
        self.lose_response = False

    def request(self, method, path, *, data=None):
        if method == 'PATCH':
            self.patches.append(data)
            self.updated = True
            self.revision += 1
            if self.lose_response:
                raise TimeoutError('lost response')
            return {}
        assert method == 'GET'
        return {'items': [{'message_id': self.message_id, 'chat_id': self.chat_id,
            'msg_type': 'interactive', 'updated': self.updated, 'update_time': str(self.revision),
            'sender': {'id': 'bot', 'id_type': 'app_id', 'sender_type': 'app'},
            'body': {'content': json.dumps({'title': None, 'elements': [[
                {'tag': 'text', 'text': '请升级至最新版本客户端，以查看内容'}]]})}}]}


import test_daily_enrichment as enrichment_fixtures


class CardEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    setUp = enrichment_fixtures.EnrichmentTests.setUp
    tearDown = enrichment_fixtures.EnrichmentTests.tearDown
    item = enrichment_fixtures.EnrichmentTests.item
    setup_job = enrichment_fixtures.EnrichmentTests.setup_job
    ready = enrichment_fixtures.EnrichmentTests.ready

    async def prepare_card(self):
        items = [self.item('one', 5), self.item('two', 5)]
        instance, job, _ = self.setup_job(items, key='daily:2026-10-15',
            payload={'scheduled_for': '2026-10-15T08:30:00+08:00'})
        instance.settings.daily_layout = 'v4'
        await instance._handle_daily_job(job)
        self.store.complete(job.key)
        with self.store._connect() as db:
            db.execute("UPDATE outbox SET remote_message_id='om_original' WHERE job_key=?", (job.key,))
            row = db.execute('SELECT * FROM outbox WHERE job_key=?', (job.key,)).fetchone()
        self.assertEqual(row['msg_type'], 'interactive')
        return job, OpaqueCardAPI(row)

    async def test_opaque_card_enriches_via_patch_and_revision_without_resending(self):
        from news_officer.daily_enrichment import update_daily_message
        job, api = await self.prepare_card()
        self.ready(job.key, 'one')
        update_daily_message(self.store, api, job.key)
        self.assertEqual(len(api.patches), 1)
        self.assertEqual(set(api.patches[0]), {'content'})
        self.assertIn('https://www.feishu.cn/docx/one', api.patches[0]['content'])
        self.ready(job.key, 'two')
        update_daily_message(self.store, api, job.key)
        update_daily_message(self.store, api, job.key)
        self.assertEqual(len(api.patches), 2)
        self.assertIn('https://www.feishu.cn/docx/two', api.patches[1]['content'])
        receipts = self.store.list_job_results(job.key, 'daily_edit_receipt')
        self.assertEqual(receipts[-1]['verification'], 'patch_ack_and_revision')
        self.assertEqual(len(self.messenger.attempts), 1)

    async def test_external_card_revision_stops_edits(self):
        from news_officer.daily_enrichment import update_daily_message
        job, api = await self.prepare_card()
        self.ready(job.key, 'one')
        api.updated = True
        update_daily_message(self.store, api, job.key)
        self.assertEqual(api.patches, [])
        self.assertEqual(len(self.messenger.attempts), 1)

    async def test_lost_patch_response_is_not_retried_over_unknown_revision(self):
        from news_officer.daily_enrichment import update_daily_message
        job, api = await self.prepare_card()
        self.ready(job.key, 'one')
        api.lose_response = True
        update_daily_message(self.store, api, job.key)
        api.lose_response = False
        update_daily_message(self.store, api, job.key)
        self.assertEqual(len(api.patches), 1)
        self.assertEqual(self.store.list_job_results(job.key, 'daily_edit_receipt'), [])
        self.assertEqual(len(self.messenger.attempts), 1)
