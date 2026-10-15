from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import APIStatusError

from translator_transport.runner import _make_client, _run, run_stream
from translator_transport.token_guard import PromptTokenLimitError
from translator_transport.validation import OutputValidator


SCHEMA = {
    "type": "object", "properties": {"translation": {"type": "string"}},
    "required": ["translation"], "additionalProperties": False,
}


def request(custom_id="P0001", content="Translate this paragraph."):
    return {"custom_id": custom_id, "body": {
        "model": "deepseek-flash", "messages": [{"role": "user", "content": content}],
    }}


def config(**task_overrides):
    return {
        "api": {"key": "test-placeholder", "url": "https://example.invalid/v1"},
        "model": {"prompt_token_limit": 64000},
        "task_table": {"concurrency": 8, "validation_retries": 2, "transport_retries": 2,
                       "transport_retry_delay_seconds": 0, **task_overrides},
    }


class Stream:
    def __init__(self, text, *, failure=None, delay=0, on_close=None):
        self.parts = iter(text[index:index + 3] for index in range(0, len(text), 3))
        self.failure = failure
        self.delay = delay
        self.on_close = on_close
        self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(self.delay)
        part = next(self.parts, None)
        if part is None:
            if self.failure is not None:
                failure, self.failure = self.failure, None
                raise failure
            raise StopAsyncIteration
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content=part))])

    async def close(self):
        self.closed += 1
        if self.on_close:
            self.on_close()


class Client:
    def __init__(self, create):
        self.create_response = create
        self.chat = SimpleNamespace(completions=self)
        self.calls = []
        self.closed = 0

    async def create(self, **body):
        self.calls.append(body)
        return await self.create_response(body)

    async def close(self):
        self.closed += 1


class TransportTests(unittest.TestCase):
    def test_transport_retry_and_schema_retry_have_separate_budgets_and_reset_previews(self):
        streams, updates = [], []
        original = request()
        unchanged = copy.deepcopy(original)

        async def create(body):
            attempt = len(client.calls)
            text = json.dumps({"translation": "old"} if attempt == 1 else
                              {"translation": 12} if attempt == 2 else {"translation": "final"})
            stream = Stream(text, failure=httpx.ReadError("do not echo test-placeholder") if attempt == 1 else None)
            streams.append(stream)
            return stream

        client = Client(create)
        with patch("translator_transport.runner._make_client", return_value=client):
            rows = run_stream([original], config(transport_retries=1, validation_retries=1),
                              OutputValidator(SCHEMA), on_content=lambda pid, text: updates.append((pid, text)))
        row = rows[0]
        self.assertEqual(json.loads(row["response"]), {"translation": "final"})
        self.assertEqual(row["validation_attempts"], 2)
        self.assertFalse(any(key.startswith("usage_") for key in row))
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(len(row["transport_errors"]), 1)
        self.assertNotIn("test-placeholder", row["transport_errors"][0])
        feedback = json.loads(client.calls[-1]["messages"][-1]["content"])
        self.assertEqual(feedback["type"], "schema_validation_retry")
        self.assertEqual(feedback["validation_errors"][0]["instance_path"], "/translation")
        self.assertEqual(feedback["validation_errors"][0]["validator"], "type")
        self.assertEqual(client.calls[-1]["messages"][-2]["content"], row["retry_responses"][0])
        self.assertEqual(updates.count(("P0001", "")), 3)
        self.assertEqual(updates[-1][1], row["response"])
        self.assertTrue(all(body["stream"] for body in client.calls))
        self.assertTrue(all(body["extra_body"] == {"thinking": {"type": "disabled"}} for body in client.calls))
        self.assertEqual([stream.closed for stream in streams], [1, 1, 1])
        self.assertEqual(client.closed, 1)
        self.assertEqual(original, unchanged)

    def test_terminal_http_errors_are_noninteractive_retained_and_never_echo_secrets(self):
        for status in (400, 401, 402, 403, 404, 422):
            with self.subTest(status=status):
                async def create(body):
                    response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid"))
                    raise APIStatusError("sensitive test-placeholder", response=response,
                                         body={"key": "test-placeholder"})
                client = Client(create)
                with patch("translator_transport.runner._make_client", return_value=client), \
                        patch("builtins.input", side_effect=AssertionError("GUI must never ask for console input")):
                    rows = run_stream([request()], config())
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(rows[0]["custom_id"], "P0001")
                self.assertEqual(rows[0]["response"], "")
                self.assertIn(f"HTTP {status}", rows[0]["error"])
                self.assertNotIn("test-placeholder", json.dumps(rows))
                self.assertEqual(client.closed, 1)

    def test_retryable_http_status_uses_only_explicit_retry_budget(self):
        for status in (408, 409, 429, 500, 503):
            with self.subTest(status=status):
                async def create(body):
                    response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid"))
                    raise APIStatusError("temporary", response=response, body=None)
                client = Client(create)
                with patch("translator_transport.runner._make_client", return_value=client):
                    row = run_stream([request()], config(transport_retries=2))[0]
                self.assertEqual(len(client.calls), 3)
                self.assertEqual(len(row["transport_errors"]), 3)
                self.assertEqual(row["validation_attempts"], 1)

    def test_prompt_limit_blocks_entire_initial_dispatch_before_client_creation(self):
        settings = config()
        settings["model"]["prompt_token_limit"] = 30
        with patch("translator_transport.runner._make_client") as make:
            with self.assertRaises(PromptTokenLimitError):
                run_stream([request("short", "ok"), request("oversized", "中" * 100)], settings)
        make.assert_not_called()

    def test_schema_retry_prompt_is_checked_before_second_network_request(self):
        async def create(body):
            return Stream(json.dumps({"unexpected": "x" * 500}))
        settings = config()
        settings["model"]["prompt_token_limit"] = 100
        client = Client(create)
        with patch("translator_transport.runner._make_client", return_value=client):
            row = run_stream([request(content="short")], settings, OutputValidator(SCHEMA))[0]
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(row["validation_error_details"][0]["code"], "prompt_token_limit")
        self.assertEqual(client.closed, 1)

    def test_concurrency_limit_and_original_order_preserve_full_callback_content(self):
        active = 0
        maximum = 0
        completed, updates = [], {}

        async def create(body):
            nonlocal active, maximum
            pid = body["messages"][0]["content"]
            active += 1
            maximum = max(active, maximum)

            def close():
                nonlocal active
                active -= 1
                completed.append(pid)

            return Stream(pid, delay=0.004 if pid == "P0001" else 0, on_close=close)

        client = Client(create)
        requests = [request(f"P{index:04}", f"P{index:04}") for index in range(1, 9)]
        with patch("translator_transport.runner._make_client", return_value=client):
            rows = run_stream(requests, config(concurrency=2),
                              on_content=lambda pid, text: updates.setdefault(pid, []).append(text))
        self.assertEqual(maximum, 2)
        self.assertEqual(active, 0)
        self.assertEqual([row["custom_id"] for row in rows], [req["custom_id"] for req in requests])
        self.assertEqual([row["response"] for row in rows], [req["custom_id"] for req in requests])
        self.assertNotEqual(completed, [req["custom_id"] for req in requests])
        for pid, texts in updates.items():
            self.assertEqual(texts, ["", pid[:3], pid])

    def test_stream_timeout_closes_stream_and_client(self):
        stream = Stream("a", delay=1)

        async def create(body):
            return stream

        settings = config(transport_retries=0)
        settings["api"]["timeout_seconds"] = 0.001
        client = Client(create)
        with patch("translator_transport.runner._make_client", return_value=client):
            row = run_stream([request()], settings)[0]
        self.assertIn("TimeoutError", row["error"])
        self.assertEqual(stream.closed, 1)
        self.assertEqual(client.closed, 1)

    def test_sdk_implicit_retries_disabled_and_timeout_is_finite(self):
        with patch("translator_transport.runner.AsyncOpenAI") as sdk, \
                patch("translator_transport.runner.httpx.AsyncClient"):
            _make_client(config()["api"])
        self.assertEqual(sdk.call_args.kwargs["max_retries"], 0)
        self.assertGreater(sdk.call_args.kwargs["timeout"], 0)

    def test_invalid_transport_configuration_fails_before_client_creation(self):
        cases = [("concurrency", 0), ("concurrency", True), ("validation_retries", -1),
                 ("transport_retries", -1), ("rounds", 2),
                 ("transport_retry_delay_seconds", float("nan")),
                 ("transport_retry_delay_seconds", -1)]
        for field, value in cases:
            with self.subTest(field=field, value=value), patch("translator_transport.runner._make_client") as make:
                with self.assertRaises(ValueError):
                    run_stream([request()], config(**{field: value}))
                make.assert_not_called()

    def test_empty_requests_and_invalid_request_shape_do_not_create_client(self):
        cases = [[request(), request()], [{"custom_id": "P0001"}],
                 [{"custom_id": "", "body": request()["body"]}],
                 [request(content=[{"type": "text", "text": "unsupported"}])]]
        with patch("translator_transport.runner._make_client") as make:
            self.assertEqual(run_stream([], config()), [])
            for requests in cases:
                with self.subTest(requests=requests), self.assertRaises(ValueError):
                    run_stream(requests, config())
            make.assert_not_called()

    def test_schema_failures_preserve_structured_errors_after_retry_budget_exhaustion(self):
        async def create(body):
            return Stream('{"translation": false}')
        client = Client(create)
        with patch("translator_transport.runner._make_client", return_value=client):
            row = run_stream([request()], config(validation_retries=1), OutputValidator(SCHEMA))[0]
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(row["validation_attempts"], 2)
        self.assertEqual(row["validation_error_details"][0]["code"], "json_schema_validation")
        self.assertEqual(row["retry_error_details"][0], row["validation_error_details"])

    def test_schema_validator_rejects_empty_malformed_and_nonstandard_json(self):
        validator = OutputValidator(SCHEMA)
        for content, expected in (("", "empty_response"), ("{", "invalid_json"),
                                  ('{"translation": NaN}', "invalid_json")):
            with self.subTest(content=content):
                result = validator.validate(content, request())
                self.assertFalse(result.ok)
                self.assertEqual(result.errors[0]["code"], expected)
        self.assertTrue(validator.validate('{"translation": "译文"}', request()).ok)


class TransportCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_failure_closes_all_streams_before_client_and_preserves_exception(self):
        peer_started = asyncio.Event()
        active, events = set(), []
        original = OSError("status write failed")

        async def chunks(pid):
            try:
                if pid == "P1":
                    await peer_started.wait()
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="partial"))])
                else:
                    peer_started.set()
                    await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                active.remove(pid)
                events.append(("stream", pid))

        async def create(body):
            pid = body["messages"][0]["content"]
            active.add(pid)
            return chunks(pid)

        class CheckedClient(Client):
            async def close(self):
                self.closed += 1
                events.append(("client", tuple(sorted(active))))

        def callback(pid, content):
            if pid == "P1" and content:
                raise original

        client = CheckedClient(create)
        with patch("translator_transport.runner._make_client", return_value=client):
            with self.assertRaises(OSError) as caught:
                await _run([request("P1", "P1"), request("P2", "P2")], config(),
                           None, 2, 0, 0, 0, 120, callback)
        self.assertIs(caught.exception, original)
        self.assertEqual(events[-1], ("client", ()))
        self.assertEqual(set(events[:-1]), {("stream", "P1"), ("stream", "P2")})
        self.assertEqual(client.closed, 1)

    async def test_cancellation_closes_parallel_streams_before_client(self):
        both_started = asyncio.Event()
        active, events = set(), []

        async def chunks(pid):
            try:
                await asyncio.Future()
                yield  # Keep this an async generator even while waiting for cancellation.
            finally:
                await asyncio.sleep(0)
                active.remove(pid)
                events.append(("stream", pid))

        async def create(body):
            pid = body["messages"][0]["content"]
            active.add(pid)
            if len(active) == 2:
                both_started.set()
            return chunks(pid)

        class CheckedClient(Client):
            async def close(self):
                self.closed += 1
                events.append(("client", tuple(sorted(active))))

        client = CheckedClient(create)
        with patch("translator_transport.runner._make_client", return_value=client):
            task = asyncio.create_task(_run([request("P1", "P1"), request("P2", "P2")],
                                           config(), None, 2, 0, 0, 0, 120, None))
            await asyncio.wait_for(both_started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(events[-1], ("client", ()))
        self.assertEqual(set(events[:-1]), {("stream", "P1"), ("stream", "P2")})
        self.assertEqual(client.closed, 1)

    async def test_stream_and_client_close_errors_do_not_replace_callback_failure(self):
        original = OSError("callback failed")

        class BrokenStream(Stream):
            async def close(self):
                await asyncio.sleep(0)
                await super().close()
                raise RuntimeError("stream close failed")

        stream = BrokenStream("partial")

        async def create(body):
            return stream

        class BrokenClient(Client):
            async def close(self):
                self.closed += 1
                raise RuntimeError("client close failed")

        def callback(pid, content):
            if content:
                raise original

        client = BrokenClient(create)
        with patch("translator_transport.runner._make_client", return_value=client):
            with self.assertRaises(OSError) as caught:
                await _run([request()], config(), None, 1, 0, 0, 0, 120, callback)
        self.assertIs(caught.exception, original)
        self.assertEqual(stream.closed, 1)
        self.assertEqual(client.closed, 1)


if __name__ == "__main__":
    unittest.main()
