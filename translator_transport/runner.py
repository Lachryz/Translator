from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import time
from typing import Callable

import httpx
from jinja2 import Environment, StrictUndefined
from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from .token_guard import PromptTokenLimitError, enforce_prompt_token_limit
from .validation import OutputValidator

_STREAM_KEYS = {"model", "messages", "temperature", "max_tokens", "seed", "top_p", "stop",
                "response_format"}
_DEFAULT_TIMEOUT_SECONDS = 120.0
_RETRY_ENV = Environment(autoescape=False, undefined=StrictUndefined)
_RETRY_ENV.filters["dumps"] = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
_RETRY_TEMPLATE = _RETRY_ENV.from_string("{{ payload | dumps }}")


def _make_client(api_cfg: dict) -> AsyncOpenAI:
    timeout = float(api_cfg.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
    return AsyncOpenAI(api_key=api_cfg["key"], base_url=api_cfg["url"], max_retries=0,
                       timeout=timeout, http_client=httpx.AsyncClient(trust_env=False, timeout=timeout))


def _stream_params(body: dict) -> dict:
    params = {key: value for key, value in body.items() if key in _STREAM_KEYS and value is not None}
    params["stream"] = True
    if body.get("stream_options") is not None:
        params["stream_options"] = body["stream_options"]
    # Translator always requests non-thinking completions.
    params["extra_body"] = {"thinking": {"type": "disabled"}}
    return params


def _retry_body(body: dict, previous_response: str, validation_errors: tuple[dict, ...], attempt: int) -> dict:
    retry = copy.deepcopy(body)
    retry["messages"].extend([
        {"role": "assistant", "content": previous_response},
        {"role": "user", "content": _RETRY_TEMPLATE.render(payload={
            "type": "schema_validation_retry", "attempt": attempt,
            "validation_errors": list(validation_errors),
            "instruction": "仅修正上述错误，并重新输出完整且符合 schema 的结果。不要输出解释或其他内容。",
        })},
    ])
    return retry


def _safe_error(exc: BaseException) -> str:
    # SDK exceptions can contain response bodies, URLs, and credentials. Never echo them.
    if isinstance(exc, APIStatusError):
        return f"{type(exc).__name__}: HTTP {exc.status_code}"
    return f"{type(exc).__name__}: request or stream interrupted"


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, APIStatusError):
        return exc.status_code in {408, 409, 429} or exc.status_code >= 500
    return isinstance(exc, (TimeoutError, httpx.TransportError, APIConnectionError))


async def _close_resource(resource: object) -> None:
    close = getattr(resource, "close", None) or getattr(resource, "aclose", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        closing = asyncio.ensure_future(result)
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            # Finish closing before propagating cancellation to the caller.
            try:
                await closing
            except BaseException:
                pass
            raise


async def _collect_stream(client: AsyncOpenAI, body: dict, on_content: Callable[[str], None] | None) -> str:
    parts: list[str] = []
    if on_content is not None:
        on_content("")
    stream = await client.chat.completions.create(**_stream_params(body))
    failed = False
    try:
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                parts.append(chunk.choices[0].delta.content)
                if on_content is not None:
                    on_content("".join(parts))
    except BaseException:
        failed = True
        raise
    finally:
        try:
            await _close_resource(stream)
        except BaseException:
            if not failed:
                raise
    return "".join(parts)


async def _call(
    state: dict, client: AsyncOpenAI, sem: asyncio.Semaphore, config: dict,
    transport_retries: int, retry_delay: float, timeout: float,
    on_content: Callable[[str, str], None] | None,
) -> None:
    started = time.perf_counter()
    custom_id = state["row"]["custom_id"]
    callback = (lambda content: on_content(custom_id, content)) if on_content is not None else None
    try:
        enforce_prompt_token_limit([{"custom_id": custom_id, "body": state["body"]}],
                                   config, source="schema retry")
    except PromptTokenLimitError as exc:
        message = str(exc)
        state["row"].update(error=message, validation_error=message, validation_error_details=[{
            "code": "prompt_token_limit", "message": message, "instance_path": "/messages",
            "schema_path": "/prompt_token_limit", "validator": "prompt_token_limit",
        }])
        state["finished"] = True
        return
    try:
        for attempt in range(transport_retries + 1):
            try:
                async with sem, asyncio.timeout(timeout):
                    content = await _collect_stream(client, state["body"], callback)
                state["row"]["response"] = content
                return
            except (TimeoutError, httpx.TransportError, APIConnectionError, APIStatusError) as exc:
                message = _safe_error(exc)
                state["row"].setdefault("transport_errors", []).append(message)
                if not _retryable(exc) or attempt == transport_retries:
                    state["row"]["error"] = message
                    state["row"]["response"] = ""
                    state["finished"] = True
                    if callback is not None:
                        callback("")
                    return
                await asyncio.sleep(retry_delay * (2 ** attempt))
    finally:
        state["row"]["elapsed_seconds"] += time.perf_counter() - started


async def _run(
    requests: list[dict], config: dict, validator: OutputValidator | None,
    concurrency: int, validation_retries: int, transport_retries: int,
    retry_delay: float, timeout: float, on_content: Callable[[str, str], None] | None,
) -> list[dict]:
    states = [{"request": request, "body": request["body"], "finished": False, "row": {
        "custom_id": request["custom_id"], "response": "", "validation_attempts": 0, "round": 1,
        "elapsed_seconds": 0.0,
    }} for request in requests]
    client = _make_client(config["api"])
    sem = asyncio.Semaphore(concurrency)
    failed = False
    try:
        for attempt in range(1, validation_retries + 2):
            pending = [state for state in states if not state["finished"]]
            if not pending:
                break
            for state in pending:
                state["row"]["validation_attempts"] = attempt
            tasks = [asyncio.create_task(_call(state, client, sem, config, transport_retries,
                                               retry_delay, timeout, on_content)) for state in pending]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    if not task.done() and not task.cancelling():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            for state in pending:
                if state["finished"]:
                    continue
                row = state["row"]
                result = validator.validate(row["response"], state["request"]) if validator is not None else None
                if result is None or result.ok:
                    state["finished"] = True
                elif attempt > validation_retries:
                    row.update(error=result.error, validation_error=result.error,
                               validation_error_details=list(result.errors))
                    state["finished"] = True
                else:
                    row.setdefault("retry_errors", []).append(result.error)
                    row.setdefault("retry_error_details", []).append(list(result.errors))
                    row.setdefault("retry_responses", []).append(row["response"])
                    state["body"] = _retry_body(state["request"]["body"], row["response"], result.errors, attempt)
        for state in states:
            state["row"]["elapsed_seconds"] = round(state["row"]["elapsed_seconds"], 3)
        return [state["row"] for state in states]
    except BaseException:
        failed = True
        raise
    finally:
        try:
            await _close_resource(client)
        except BaseException:
            if not failed:
                raise


def _integer(value: object, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _validate_requests(requests: list[dict]) -> None:
    seen: set[str] = set()
    for index, request in enumerate(requests):
        if not isinstance(request, dict):
            raise ValueError(f"request {index} must be an object")
        custom_id = request.get("custom_id")
        if not isinstance(custom_id, str) or not custom_id:
            raise ValueError(f"request {index} must have a non-empty string custom_id")
        if custom_id in seen:
            raise ValueError(f"duplicate request custom_id: {custom_id}")
        seen.add(custom_id)
        body = request.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("model"), str) or not body["model"]:
            raise ValueError(f"request {index} must have a body with a model name")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError(f"request {index} must have a non-empty messages list")
        if any(not isinstance(message, dict) or not isinstance(message.get("content"), str)
               or not isinstance(message.get("role"), str) for message in messages):
            raise ValueError(f"request {index} messages must contain text content and a role")


def run_stream(
    requests: list[dict], config: dict, validator: OutputValidator | None = None, *,
    on_content: Callable[[str, str], None] | None = None,
) -> list[dict]:
    """Stream one text round, preserving request order and full-content callbacks.

    Each transport attempt starts its callback with an empty string. Transient
    failures retry independently of schema failures; terminal failures stay in
    their original result row. No interactive prompts or console output occur.
    """
    task = config.get("task_table") or {}
    concurrency = _integer(task.get("concurrency", 100), "concurrency", 1)
    validation_retries = _integer(task.get("validation_retries", 1), "validation_retries", 0)
    transport_retries = _integer(task.get("transport_retries", 3), "transport_retries", 0)
    if task.get("rounds", 1) != 1:
        raise ValueError("Translator transport supports exactly one round")
    retry_delay = float(task.get("transport_retry_delay_seconds", 1))
    timeout = float(config.get("api", {}).get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
    if not math.isfinite(retry_delay) or retry_delay < 0:
        raise ValueError("transport_retry_delay_seconds must be finite and non-negative")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("api.timeout_seconds must be finite and positive")
    _validate_requests(requests)
    enforce_prompt_token_limit(requests, config, source="dispatch")
    if not requests:
        return []
    return asyncio.run(_run(requests, config, validator, concurrency, validation_retries,
                            transport_retries, retry_delay, timeout, on_content))
