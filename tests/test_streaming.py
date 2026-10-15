from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from support import requires_two_batch

import httpx

from paragraph_translator import (
    ParagraphTranslator, Settings, StatusReporter, TemplateRepository,
    create_translator, streamed_translation, streamed_paragraph_translation,
)

ROOT = Path(__file__).resolve().parents[1]


class StreamTextTests(unittest.TestCase):
    def test_immediate_stream_events_preserve_every_fast_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            status_path = Path(temporary) / "status.json"
            stream_path = Path(temporary) / "stream.jsonl"
            reporter = StatusReporter(status_path, stream_path=stream_path)
            reporter.update(35, "正在翻译…", mode="单段落快速翻译")
            expected = ["", "中", "中文", "中文译", "中文译文"]
            for value in expected:
                reporter.update_translation(value)
            events = [json.loads(line) for line in stream_path.read_text().splitlines()]
            self.assertEqual([event["translation"] for event in events], expected)
            self.assertNotIn("translation", json.loads(status_path.read_text()))

    def test_all_partial_json_prefixes_decode_only_translation(self):
        expected = '中文“引用”\n😀 [链接](https://example.com/a?q="b") \\尾部'
        payload = json.dumps({'paragraph_id': 'P0001', 'translation': expected}, ensure_ascii=True)
        for end in range(len(payload) + 1):
            partial = streamed_translation(payload[:end])
            self.assertTrue(expected.startswith(partial), (end, partial))
            self.assertNotIn('�', partial)
        self.assertEqual(streamed_translation(payload), expected)

    def test_nested_keys_and_quoted_json_are_not_displayed(self):
        self.assertEqual(streamed_translation('{"metadata":{"translation":"wrong"},"translation":"right'), 'right')
        self.assertEqual(streamed_translation('{"note":"\\\"translation\\\":\\\"wrong"'), '')
        self.assertEqual(streamed_translation('JSON prefix {"translation":"wrong'), '')
        self.assertEqual(streamed_translation('{"translation":123'), '')

    def test_multi_paragraph_preview_holds_partial_citation_until_metadata_arrives(self):
        paragraph = {'paragraph_id': 'P0001', 'source': 'Evidence (Smith, 2020).'}
        payload = {
            'paragraph_id': 'P0001', 'translation': '证据[[CITATION:C001]]。',
            'citations': [{'citation_id': 'C001', 'source_text': '(Smith, 2020)',
                          'citation_style': 'parenthetical', 'works': [{
                              'authors': ['Smith'], 'author_form': 'single',
                              'year': 2020, 'year_suffix': '',
                          }]}],
            'unresolved_citations': [],
        }
        content = json.dumps(payload, ensure_ascii=False)
        for end in range(len(content)):
            text, complete = streamed_paragraph_translation(content[:end], paragraph)
            self.assertNotIn('[[', text)
            self.assertNotIn('CITATION', text)
            self.assertFalse(complete)
        self.assertEqual(streamed_paragraph_translation(content, paragraph), ('证据（Smith，2020）。', True))

    @requires_two_batch
    def test_preserved_multi_paragraph_stream_orders_parallel_responses_and_retries(self):
        calls = []
        attempts = {}
        previews = []
        first_started = asyncio.Event()
        second_finished = asyncio.Event()
        final = ['第一段正确译文。', '第二段译文。']
        with tempfile.TemporaryDirectory() as temporary:
            gesture = Path(temporary) / 'gesture'
            gesture.write_text('preserve')

            class Reporter(StatusReporter):
                def update_translation(self, text):
                    super().update_translation(text)
                    previews.append((text, self.mode))

            reporter = Reporter(Path(temporary) / 'status.json')

            class Client:
                def __init__(self):
                    self.chat = SimpleNamespace(completions=self)

                async def create(self, **body):
                    calls.append(body)
                    user = json.loads(body['messages'][1]['content'])
                    paragraph = user.get('current_paragraph')
                    if paragraph is None:
                        payload = {'document_summary': '测试语境', 'style_guidance': '学术中文', 'glossary': []}
                        return self.chunks(json.dumps(payload), None)
                    pid = paragraph['paragraph_id']
                    attempts[pid] = attempts.get(pid, 0) + 1
                    attempt = attempts[pid]
                    payload = {'paragraph_id': 'P9999' if pid == 'P0001' and attempt == 1 else pid,
                               'translation': '旧预览。' if pid == 'P0001' and attempt == 1 else final[int(pid[-1]) - 1],
                               'citations': [], 'unresolved_citations': []}
                    return self.chunks(json.dumps(payload, ensure_ascii=False), pid)

                async def chunks(self, response, pid):
                    if pid == 'P0002':
                        await first_started.wait()
                    for index in range(0, len(response), 3):
                        yield SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content=response[index:index+3]))])
                        if pid == 'P0001' and index == 0:
                            first_started.set()
                            await second_finished.wait()
                        await asyncio.sleep(0.001)
                    if pid == 'P0002':
                        # The later paragraph completes first but stays buffered.
                        assert not any(final[1] in text for text, _ in previews), previews
                        second_finished.set()
                    elif pid == 'P0001' and 'P9999' not in response:
                        assert reporter.translation == '\n\n'.join(final), reporter.translation

                async def close(self):
                    pass

            translator = create_translator(ROOT / 'translator_config.json')
            with patch('tobatch.runner._make_client', side_effect=lambda *args, **kwargs: Client()):
                output = translator.translate('First paragraph.\nSecond paragraph.', status=reporter, gesture_file=gesture)

        self.assertEqual(output, '\n\n'.join(final))
        self.assertEqual(len(calls), 4)  # context, two paragraphs, one schema retry
        feedback = json.loads(calls[-1]['messages'][-1]['content'])
        self.assertEqual(feedback['validation_errors'][0]['code'], 'paragraph_id_mismatch')
        self.assertTrue(any('旧预览' in text for text, _ in previews))
        self.assertTrue(any(text == '' for text, _ in previews))
        self.assertEqual(previews[-1], (output, '多段落综合翻译（保留原段落）'))
        self.assertTrue(all(not text.startswith(final[1]) for text, _ in previews))
        self.assertTrue(all(call['stream'] for call in calls))

    def test_double_press_does_not_display_multi_paragraph_stream(self):
        previews = []
        with tempfile.TemporaryDirectory() as temporary:
            gesture = Path(temporary) / 'gesture'
            gesture.write_text('')

            class Reporter(StatusReporter):
                def update_translation(self, text):
                    previews.append(text)

            def run(requests, schema, on_content=None):
                if requests[0]['custom_id'] == 'CONTEXT':
                    return [{'custom_id': 'CONTEXT', 'response': json.dumps({
                        'document_summary': '测试语境', 'style_guidance': '学术中文', 'glossary': [],
                    })}]
                rows = []
                for request in requests:
                    pid = request['custom_id']
                    payload = {'paragraph_id': pid, 'translation': f'译文{pid}。第二句。',
                               'citations': [], 'unresolved_citations': []}
                    content = json.dumps(payload, ensure_ascii=False)
                    if on_content:
                        on_content(pid, content[:40])  # unresolved gesture: no preview
                        gesture.write_text('normal')
                        on_content(pid, content)
                    rows.append({'custom_id': pid, 'response': content})
                return rows

            class Gateway:
                def run(self, requests, schema):
                    return run(requests, schema)

                def run_with_progress(self, requests, schema, on_content):
                    return run(requests, schema, on_content)

            translator = ParagraphTranslator(
                Settings.load(ROOT / 'translator_config.json'), Gateway(),
                TemplateRepository(ROOT / 'prompts'),
            )
            reporter = Reporter(Path(temporary) / 'status')
            output = translator.translate('First paragraph.\nSecond paragraph.', status=reporter, gesture_file=gesture)
        self.assertEqual(output, '- 译文P0001。\n- 第二句。\n\n- 译文P0002。\n- 第二句。')
        self.assertEqual(previews, [])
        self.assertEqual(reporter.mode, '多段落综合翻译')

    @requires_two_batch
    def test_stream_preview_precedes_completion_and_resets_on_both_retry_types(self):
        snapshots = []
        calls = []
        final = '正确的流式译文。'
        with tempfile.TemporaryDirectory() as temporary:
            status_path = Path(temporary) / 'status.json'

            class Reporter(StatusReporter):
                def update(self, *args, **kwargs):
                    super().update(*args, **kwargs)
                    snapshots.append(json.loads(status_path.read_text()))

            reporter = Reporter(status_path)

            class Client:
                def __init__(self):
                    self.chat = SimpleNamespace(completions=self)

                async def create(self, **body):
                    calls.append(body)
                    attempt = len(calls)
                    response = json.dumps({
                        'paragraph_id': 'P0002' if attempt == 2 else 'P0001',
                        'translation': '旧预览' if attempt <= 2 else final,
                    }, ensure_ascii=False)

                    async def chunks():
                        for index in range(0, len(response), 3):
                            yield SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content=response[index:index+3]))])
                            await asyncio.sleep(0.002)
                        self_during = reporter.translation
                        if attempt == 3:
                            assert self_during == final, self_during
                        if attempt == 1:
                            raise httpx.ReadError('simulated interrupted stream')
                    return chunks()

                async def close(self):
                    pass

            translator = create_translator(ROOT / 'translator_config.json')
            translator.gateway._config['task_table']['transport_retry_delay_seconds'] = 0.001
            with patch('tobatch.runner._make_client', return_value=Client()):
                output = translator.translate('Market liquidity affects price discovery.', status=reporter)

        self.assertEqual(output, final)
        self.assertEqual(len(calls), 3)
        feedback = json.loads(calls[-1]['messages'][-1]['content'])
        self.assertEqual(feedback['type'], 'schema_validation_retry')
        self.assertEqual(feedback['validation_errors'][0]['code'], 'paragraph_id_mismatch')
        previews = [item['translation'] for item in snapshots if 'translation' in item]
        self.assertTrue(any(value and value != final for value in previews))
        self.assertGreaterEqual(previews.count(''), 3)
        self.assertEqual(previews[-1], final)
        self.assertTrue(all('paragraph_id' not in value for value in previews))


if __name__ == '__main__':
    unittest.main()
