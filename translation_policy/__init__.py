"""Paragraph adapter for the DailyPaper translation contracts and slot codec."""

from __future__ import annotations

import re
from typing import Any

from .translation_contract import (
    TOKENS,
    abbreviation_errors,
    protect_plural_abbreviations,
    repair_plural_markers,
    restore_plural_abbreviations,
)
from .translation_semantics import (
    TITLE_REACTIONS,
    translation_semantic_errors,
    translation_semantic_hints,
)
from .translation_slots import prepare_slots, render_slots


def semantic_article(source: str) -> dict[str, str]:
    return {
        "abstract": source,
        "title": source if TITLE_REACTIONS.fullmatch(source.strip()) else "",
    }


def prepare_paragraph(paragraph: dict[str, Any]) -> dict[str, Any]:
    source = paragraph["source"]
    protected, bindings = protect_plural_abbreviations(
        {"title": "", "abstract": source}
    )
    return {
        **paragraph,
        "translation_source": protected["abstract"],
        "protected_abbreviations": bindings,
        "source_translation_constraints": translation_semantic_hints(
            semantic_article(source)
        ),
    }


def restore_payload(payload: dict, paragraph: dict) -> tuple[dict, list[dict]]:
    value = {"title_zh": "", "abstract_zh": payload["translation"]}
    bindings = paragraph.get("protected_abbreviations", [])
    value, _ = repair_plural_markers(value, bindings)
    restored, errors = restore_plural_abbreviations(value, bindings)
    return {**payload, "translation": restored["abstract_zh"]}, errors


def prose_errors(source: str, visible: str, citations: list[dict]) -> list[dict]:
    """Citation formatting is validated separately; do not count its acronym authors."""
    lexical_source, lexical_output = source, visible
    # The caller supplies rendered citation text to mask only bibliographic spans.
    for citation in citations:
        raw, rendered = citation.get("source_text", ""), citation.get("rendered", "")
        if raw:
            lexical_source = lexical_source.replace(raw, " ")
        if rendered:
            lexical_output = lexical_output.replace(rendered, " ")
    errors = abbreviation_errors(
        {"abstract": lexical_source},
        {"abstract_zh": lexical_output, "title_zh": ""},
    )
    article = semantic_article(source)
    # Paragraphs define first acronyms; title-specific role guards expect a bare
    # event token, as DailyPaper titles do. Keep definitions in the visible prose.
    role_title = re.sub(
        rf"({TOKENS.pattern})\s*（[^（）\n]*[\u3400-\u9fff][^（）\n]*）", r"\1", visible
    )
    errors += translation_semantic_errors(
        article,
        {"abstract_zh": visible, "title_zh": role_title if article["title"] else ""},
    )
    return errors


def preview_text(text: str, paragraph: dict) -> str:
    """Restore complete markers and withhold an unfinished or foreign marker."""
    bindings = {
        item["marker"]: item["source_token"]
        for item in paragraph.get("protected_abbreviations", [])
    }
    for marker, token in bindings.items():
        text = text.replace(marker, token)
    start = text.find("⟦")
    if start >= 0:
        text = text[:start]
    return text


def slot_material(paragraph: dict) -> tuple[dict, dict]:
    plan, schema = prepare_slots({"abstract": paragraph["source"]}, ["abstract_zh"])
    field = plan["abstract_zh"]
    keys = [f"gap_{index:04d}" for index in range(len(field["gaps_en"]))]
    literal = {
        key: gap
        for key, gap in zip(keys, field["gaps_en"], strict=True)
        if not gap.strip()
    }
    translatable = {
        key: gap
        for key, gap in zip(keys, field["gaps_en"], strict=True)
        if key not in literal
    }
    wire_schema = schema["properties"]["abstract_zh"]
    wire_schema["properties"]["gaps_zh"] = {
        "type": "object",
        "properties": {key: {"type": "string"} for key in translatable},
        "required": list(translatable),
        "additionalProperties": False,
    }
    return {
        **field,
        "gaps_en": translatable,
        "immutable_gaps_zh": literal,
        "output_skeleton": {
            "gaps_zh": dict.fromkeys(translatable, ""),
            "definitions_zh": dict.fromkeys(field["required_definitions"], "中文全称"),
        },
    }, wire_schema


def render_slot_payload(
    payload: dict, paragraph: dict, *, citations: bool
) -> tuple[dict, list[dict]]:
    from jsonschema import Draft202012Validator

    _, schema = slot_material(paragraph)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "paragraph_id": {"const": paragraph["paragraph_id"]},
            **schema["properties"],
        },
        "required": ["paragraph_id", *schema["required"]],
    }
    if citations:
        # The final translation schema performs the detailed citation validation.
        schema["properties"].update(
            {"citations": {"type": "array"}, "unresolved_citations": {"type": "array"}}
        )
        schema["required"] += ["citations", "unresolved_citations"]
    errors = [
        {
            "path": list(error.absolute_path),
            "validator": error.validator,
            "message": error.message,
            "expected_schema": error.schema,
        }
        for error in Draft202012Validator(schema).iter_errors(payload)
    ]
    if errors:
        return {}, errors
    plan, _ = prepare_slots({"abstract": paragraph["source"]}, ["abstract_zh"])
    field = plan["abstract_zh"]
    gaps = [
        gap if not gap.strip() else payload["gaps_zh"][f"gap_{index:04d}"]
        for index, gap in enumerate(field["gaps_en"])
    ]
    visible, _, errors = render_slots(
        {"abstract": paragraph["source"]},
        plan,
        {"abstract_zh": {"gaps_zh": gaps, "definitions_zh": payload["definitions_zh"]}},
        allow_citations=citations,
    )
    if errors:
        return {}, errors
    result = {
        "paragraph_id": payload["paragraph_id"],
        "translation": visible["abstract_zh"],
    }
    if citations:
        result.update(
            citations=payload["citations"],
            unresolved_citations=payload["unresolved_citations"],
        )
    return result, []


def contract_issues(errors: list[dict]) -> tuple[dict, ...]:
    """Use the shared machine-readable location format for validation errors."""
    return tuple(
        {
            **error,
            "code": error["validator"],
            "instance_path": "/translation"
            if error.get("path") in (["abstract_zh"], ["title_zh"])
            else "/" + "/".join(str(value) for value in error.get("path", [])),
            "schema_path": "/x-academic-translation-contract",
        }
        for error in errors
    )
