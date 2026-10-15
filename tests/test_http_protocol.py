from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from translator_api import Translator
from translator_runtime import DEFAULT_DEEPSEEK_URL


class HTTPProtocolTests(unittest.TestCase):
    def test_key_only_configuration_streams_through_sdk_and_retries_schema_errors(self):
        requests = []

        def handle(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), DEFAULT_DEEPSEEK_URL + "/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer test-only")
            body = json.loads(request.content)
            requests.append(body)
            self.assertTrue(body["stream"])
            self.assertEqual(body["model"], "deepseek-flash")
            self.assertEqual(body["thinking"], {"type": "disabled"})
            content = json.dumps({
                "paragraph_id": "P9999" if len(requests) == 1 else "P0001",
                "translation": "市场流动性影响价格发现。",
            }, ensure_ascii=False)
            events = []
            for index in range(0, len(content), 5):
                chunk = {"id": "test", "object": "chat.completion.chunk", "created": 0,
                         "model": "deepseek-flash", "choices": [{"index": 0,
                             "delta": {"content": content[index:index + 5]},
                             "finish_reason": None}]}
                events.append("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
            events.append("data: [DONE]\n\n")
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="".join(events).encode())

        def make_client(api_config):
            return AsyncOpenAI(
                api_key=api_config["key"], base_url=api_config["url"], max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False),
            )

        with patch.dict(os.environ, {"DEEPSEEK_KEY": "test-only"}, clear=True), \
                patch("dotenv.load_dotenv"), \
                patch("translator_transport.runner._make_client", side_effect=make_client):
            result = Translator().translate_text("Market liquidity affects price discovery.")

        self.assertEqual(result, "市场流动性影响价格发现。")
        self.assertEqual(len(requests), 2)
        feedback = json.loads(requests[1]["messages"][-1]["content"])
        self.assertEqual(feedback["type"], "schema_validation_retry")
        self.assertEqual(feedback["validation_errors"][0]["code"], "paragraph_id_mismatch")


if __name__ == "__main__":
    unittest.main()
