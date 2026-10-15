from __future__ import annotations

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

from paragraph_translator import Settings, StreamingGateway
from translator_runtime import DEFAULT_CONFIG, DEFAULT_DEEPSEEK_URL


class GatewayConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.load(DEFAULT_CONFIG)

    def test_only_key_is_required_with_default_endpoint(self):
        for url in (None, "", "   "):
            environment = {"DEEPSEEK_KEY": "test-only"}
            if url is not None:
                environment["DEEPSEEK_URL"] = url
            with self.subTest(url=url), patch.dict(os.environ, environment, clear=True), \
                    patch("dotenv.load_dotenv"):
                gateway = StreamingGateway(self.settings)
            self.assertEqual(gateway._config["api"]["url"], DEFAULT_DEEPSEEK_URL)
            self.assertEqual(gateway._config["model"]["name"], "deepseek-flash")

    def test_optional_environment_endpoint_overrides_config(self):
        with patch.dict(os.environ, {
            "DEEPSEEK_KEY": "test-only", "DEEPSEEK_URL": "https://example.invalid/v1",
        }, clear=True), patch("dotenv.load_dotenv"):
            gateway = StreamingGateway(self.settings)
        self.assertEqual(gateway._config["api"]["url"], "https://example.invalid/v1")

    def test_optional_config_endpoint_is_used_without_environment_override(self):
        settings = replace(self.settings, deepseek_url="https://example.invalid/config")
        with patch.dict(os.environ, {"DEEPSEEK_KEY": "test-only"}, clear=True), \
                patch("dotenv.load_dotenv"):
            gateway = StreamingGateway(settings)
        self.assertEqual(gateway._config["api"]["url"], settings.deepseek_url)

    def test_missing_key_reports_only_the_required_variable(self):
        with patch.dict(os.environ, {}, clear=True), patch("dotenv.load_dotenv"), \
                self.assertRaisesRegex(RuntimeError, "DEEPSEEK_KEY") as raised:
            StreamingGateway(self.settings)
        self.assertNotIn("DEEPSEEK_URL", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
