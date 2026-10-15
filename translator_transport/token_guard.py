from __future__ import annotations

import json
import math
from typing import Any, Iterable, Mapping

DEFAULT_PROMPT_TOKEN_LIMIT = 64_000


class PromptTokenLimitError(ValueError):
    """Raised before dispatch when an estimated prompt exceeds the safety limit."""


def prompt_token_limit(config: Mapping[str, Any]) -> int:
    task = config.get("task_table") or {}
    model = config.get("model") or {}
    value = task.get("prompt_token_limit", model.get("prompt_token_limit", DEFAULT_PROMPT_TOKEN_LIMIT))
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("prompt_token_limit must be a positive integer")
    return value


def _estimated_text_tokens(text: str) -> int:
    """Conservative UTF-8 estimate, not a provider tokenizer or billing count."""
    return math.ceil(len(text.encode("utf-8")) / 3)


def estimate_request_prompt_tokens(request: Mapping[str, Any]) -> tuple[int, dict[str, int]]:
    body = request.get("body", request)
    components: dict[str, int] = {}
    for message in body.get("messages", []):
        content = message.get("content", "")
        if not isinstance(content, str):
            raise ValueError("Translator transport only supports text message content")
        role = str(message.get("role", "message"))
        components[role] = components.get(role, 0) + _estimated_text_tokens(content) + 4
    response_format = body.get("response_format")
    if response_format is not None:
        serialized = json.dumps(response_format, ensure_ascii=False, separators=(",", ":"))
        components["response_format"] = _estimated_text_tokens(serialized)
    return sum(components.values()) + 3, components


def enforce_prompt_token_limit(
    requests: Iterable[Mapping[str, Any]], config: Mapping[str, Any], *, source: str,
) -> None:
    limit = prompt_token_limit(config)
    failures: list[str] = []
    for index, request in enumerate(requests, 1):
        estimated, _ = estimate_request_prompt_tokens(request)
        if estimated > limit:
            failures.append(f"custom_id={request.get('custom_id', index)}: estimated={estimated:,}")
    if failures:
        details = "; ".join(failures[:10])
        raise PromptTokenLimitError(
            f"Prompt token safety check blocked {len(failures):,} request(s) before {source}; "
            f"estimated limit={limit:,}. {details}. "
            "No request was sent. Shorten the prompt or adjust model.prompt_token_limit."
        )
