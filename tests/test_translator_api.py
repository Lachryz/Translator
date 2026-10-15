from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import translator_api


class FakeEngine:
    settings = SimpleNamespace(model="deepseek-flash")

    def __init__(self):
        self.calls = []

    def translate(self, text, *, status, preserve_paragraphs):
        self.calls.append((text, preserve_paragraphs))
        status.update(100, "完成", mode="测试模式")
        return "测试译文"


class TranslatorAPITests(unittest.TestCase):
    def setUp(self):
        translator_api.clear_translator_cache()
        self.addCleanup(translator_api.clear_translator_cache)

    def test_invalid_helper_input_fails_before_configuration_or_network_initialization(self):
        for method in (translator_api.translate, translator_api.translate_with_metadata):
            with patch("translator_api.create_translator") as create:
                for value in (None, 1, [], "", " \n\t"):
                    with self.subTest(method=method.__name__, value=value):
                        with self.assertRaises(translator_api.InvalidTranslationInput):
                            method(value)
                with self.assertRaises(translator_api.InvalidTranslationInput):
                    method("A sentence.", preserve_paragraphs=1)
                create.assert_not_called()

    def test_valid_calls_reuse_engine_and_return_metadata(self):
        engine = FakeEngine()
        with patch("translator_api.create_translator", return_value=engine) as create:
            self.assertEqual(translator_api.translate("First sentence."), "测试译文")
            result = translator_api.translate_with_metadata(
                "First paragraph.\n\nSecond paragraph.", preserve_paragraphs=True,
            )
        create.assert_called_once()
        self.assertEqual(result.to_dict(), {
            "translation": "测试译文", "mode": "测试模式",
            "paragraph_count": 2, "model": "deepseek-flash",
        })
        self.assertTrue(engine.calls[-1][1])

    def test_initialization_and_translation_failures_preserve_original_cause(self):
        for failure_at_init in (True, False):
            original = RuntimeError("offline simulated failure")
            engine = FakeEngine()
            with patch("translator_api.create_translator", return_value=engine) as create:
                if failure_at_init:
                    create.side_effect = original
                else:
                    def fail_translation(*args, **kwargs):
                        raise original

                    engine.translate = fail_translation
                with self.subTest(initialization=failure_at_init):
                    with self.assertRaises(translator_api.TranslationFailed) as caught:
                        translator_api.translate("First sentence.")
                    self.assertIs(caught.exception.__cause__, original)
            translator_api.clear_translator_cache()

    def test_default_configuration_is_resolved_on_each_call_and_cache_can_refresh(self):
        with patch("translator_api.create_translator", side_effect=lambda path: FakeEngine()) as create:
            with patch.dict(os.environ, {"TRANSLATOR_CONFIG": "/tmp/translator-first.json"}):
                first = translator_api.get_translator()
                self.assertIs(first, translator_api.get_translator())
            with patch.dict(os.environ, {"TRANSLATOR_CONFIG": "/tmp/translator-second.json"}):
                second = translator_api.get_translator()
                self.assertIsNot(first, second)
                translator_api.clear_translator_cache()
                self.assertIsNot(second, translator_api.get_translator())
        self.assertEqual(create.call_count, 3)
        self.assertEqual(first.config_path, Path("/tmp/translator-first.json").resolve())

    def test_concurrent_first_calls_initialize_one_cached_engine(self):
        ready = threading.Barrier(8)

        def create_engine(path):
            time.sleep(0.01)
            return FakeEngine()

        def get_engine(index):
            ready.wait(timeout=3)
            return translator_api.get_translator()

        with patch("translator_api.create_translator", side_effect=create_engine) as create:
            with ThreadPoolExecutor(max_workers=8) as pool:
                translators = list(pool.map(get_engine, range(8)))
        create.assert_called_once()
        self.assertTrue(all(item is translators[0] for item in translators))


if __name__ == "__main__":
    unittest.main()
