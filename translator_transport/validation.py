from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jsonschema import validators


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    error: str = ""
    errors: tuple[dict[str, str], ...] = ()


def _json_pointer(parts: Any) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _json_type(value: Any) -> str:
    for kind, name in ((type(None), "null"), (bool, "boolean"), (int, "integer"),
                       (float, "number"), (str, "string"), (list, "array"), (dict, "object")):
        if isinstance(value, kind):
            return name
    return type(value).__name__


def _failure(message: str, **details: str) -> ValidationResult:
    issue = {"message": message, "instance_path": "", "schema_path": "",
             "validator": "", "expected": "", "actual_type": "", **details}
    return ValidationResult(False, message, (issue,))


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


class OutputValidator:
    """Compile a JSON Schema once and return structured errors for model retries."""

    def __init__(self, schema_doc: dict[str, Any]) -> None:
        self.schema_doc = schema_doc
        validator_class = validators.validator_for(schema_doc)
        validator_class.check_schema(schema_doc)
        self._validator = validator_class(schema_doc)

    def validate(self, content: str, req: dict) -> ValidationResult:
        content = content.strip()
        if not content:
            return _failure("response is empty", code="empty_response", validator="non_empty",
                            expected="non-empty response", actual_type="empty")
        try:
            payload = json.loads(content, parse_constant=_reject_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            location = (f"line:{exc.lineno},column:{exc.colno}"
                        if isinstance(exc, json.JSONDecodeError) else "")
            message = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
            return _failure(f"response is not valid JSON: {message}", code="invalid_json",
                            instance_path=location, validator="json_parse", expected="valid JSON",
                            actual_type="string")
        error = next(self._validator.iter_errors(payload), None)
        if error is None:
            return ValidationResult(True)
        return _failure(error.message, code="json_schema_validation",
                        instance_path=_json_pointer(error.absolute_path),
                        schema_path=_json_pointer(error.absolute_schema_path),
                        validator=str(error.validator or ""),
                        expected=json.dumps(error.validator_value, ensure_ascii=False, sort_keys=True),
                        actual_type=_json_type(error.instance))
