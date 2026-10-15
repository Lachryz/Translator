"""Fallback translation transport with script-owned abbreviation occurrences.

The model translates the N+1 prose gaps surrounding N source abbreviations or
whole URLs and returns supported Chinese expansions separately. It never writes
an abbreviation, its display parentheses, or a URL. Source order is retained, so
the prompt must ask for natural event-first wording when ordinary Chinese would
reverse abbreviations.
This codec checks structure and transport, not the truth of Chinese translations.
"""

from __future__ import annotations

import re

from jsonschema import Draft202012Validator

from .translation_contract import KNOWN, MARKERS, TOKENS, source_url_spans

_FIELDS = {"title_zh": "title", "abstract_zh": "abstract"}
_PAIRS = {"（": "）", "(": ")"}
_QUOTES = {"": "", '"': '"', "'": "'", "“": "”", "‘": "’"}


def _defined(source: str, tokens: list[str]) -> bool:
    return any(
        re.search(rf"\(\s*[\"'“‘]?{re.escape(token)}[\"'”’]?\s*\)", source)
        for token in tokens
    )


def prepare_slots(article: dict, fields) -> tuple[dict, dict]:
    """Return trusted field plans and a JSON Schema for only requested fields."""
    prepared, properties = {}, {}
    for field in fields:
        if field not in _FIELDS or field in prepared:
            raise ValueError(f"Unsupported or repeated translation field: {field}")
        source = article.get(_FIELDS[field], "")
        if not isinstance(source, str):
            raise TypeError(f"Source {_FIELDS[field]} must be a string")
        if MARKERS.search(source):
            raise ValueError(
                "Source contains reserved translation abbreviation markers"
            )
        urls = source_url_spans(source)
        matches = [
            match
            for match in TOKENS.finditer(source)
            if not any(start <= match.start() < end for start, end, _ in urls)
        ]
        prose_source = source
        for start, end, _ in reversed(urls):
            prose_source = (
                prose_source[:start] + " " * (end - start) + prose_source[end:]
            )
        families = {}
        for match in matches:
            token = match.group()
            families.setdefault(token.removesuffix("s"), []).append(token)
        gaps, slots, required, position, seen = [], [], {}, 0, set()
        events = sorted(
            [
                (match.start(), match.end(), match.group(), "abbreviation")
                for match in matches
            ]
            + [(start, end, url, "url") for start, end, url in urls]
        )
        for index, (start, end, token, kind) in enumerate(events):
            family = token.removesuffix("s") if kind == "abbreviation" else None
            gaps.append(source[position:start])
            position = end
            first = kind == "abbreviation" and family not in seen
            if kind == "abbreviation":
                seen.add(family)
            explicit = kind == "abbreviation" and _defined(
                prose_source, families[family]
            )
            needs_definition = (
                kind == "abbreviation"
                and field == "abstract_zh"
                and (family in KNOWN or explicit)
            )
            if first and needs_definition:
                required[token] = {
                    "source_token": token,
                    "family": family,
                    "source_explicitly_defined": explicit,
                }
            slots.append(
                {
                    "index": index,
                    "kind": kind,
                    "source_token": token,
                    "family": family,
                    "first_occurrence": first,
                    "definition_key": token if first and needs_definition else None,
                }
            )
        gaps.append(source[position:])
        prepared[field] = {
            "source_field": _FIELDS[field],
            "source_text": source,
            "gaps_en": gaps,
            "slots": slots,
            "required_definitions": required,
        }
        properties[field] = {
            "type": "object",
            "properties": {
                "gaps_zh": {
                    "type": "array",
                    "minItems": len(gaps),
                    "maxItems": len(gaps),
                    "items": {"type": "string"},
                },
                "definitions_zh": {
                    "type": "object",
                    "properties": {
                        key: {
                            "type": "string",
                            "minLength": 1,
                            "pattern": "[\u3400-\u9fff]",
                        }
                        for key in required
                    },
                    "required": list(required),
                    "additionalProperties": False,
                },
            },
            "required": ["gaps_zh", "definitions_zh"],
            "additionalProperties": False,
        }
    schema = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    return prepared, schema


def _definition_prefix(text: str, expansion: str) -> int:
    """Length of an exact leading (expansion), allowing either bracket style."""
    match = re.match(r"\s*([（(])\s*" + re.escape(expansion) + r"\s*([）)])", text)
    if match and _PAIRS[match[1]] == match[2]:
        return match.end()
    return 0


def _clean_boundaries(left: str, right: str, expansion: str) -> tuple[str, str]:
    """Remove only exact supplied names and paired wrappers beside one slot."""
    # Whole reverse or nested definition: expansion ( SLOT [(expansion)] ).
    wrapper = re.search(re.escape(expansion) + r"\s*([（(])\s*([\"'“‘]?)\s*$", left)
    if wrapper:
        remaining = right
        inner_end = _definition_prefix(remaining, expansion)
        if inner_end:
            remaining = remaining[inner_end:]
        closing = re.match(
            r"\s*"
            + re.escape(_QUOTES[wrapper[2]])
            + r"\s*"
            + re.escape(_PAIRS[wrapper[1]]),
            remaining,
        )
        if closing:
            left = left[: wrapper.start()]
            right = remaining[closing.end() :]
    # Exact forward definitions are rendered once by the script, including later
    # repeated definitions of the same family. Unrelated parentheses are kept.
    while end := _definition_prefix(right, expansion):
        right = right[end:]
    left = left.removesuffix(expansion)
    return left, right


def render_slots(
    article: dict, prepared: dict, output, *, allow_citations=False
) -> tuple[dict, list[dict], list[dict]]:
    """Validate transport then return visible fields, audited repairs and errors.

    Failure returns no visible fields. Neither source, plans nor model output are
    mutated. The caller must apply its usual final prose and semantic validators.
    """
    expected, schema = prepare_slots(article, prepared)
    if prepared != expected:
        return (
            {},
            [],
            [
                {
                    "path": [],
                    "validator": "immutable_slot_plan",
                    "message": "Slot plan differs from the current source; rebuild it before retrying.",
                }
            ],
        )
    errors = [
        {
            "path": list(error.absolute_path),
            "validator": error.validator,
            "message": error.message,
            "expected_schema": error.schema,
        }
        for error in sorted(
            Draft202012Validator(schema).iter_errors(output),
            key=lambda item: str(list(item.absolute_path)),
        )
    ]
    if errors:
        return {}, [], errors
    for field, plan in prepared.items():
        candidate = output[field]
        source_aliases = {
            alias
            for slot in plan["slots"]
            if slot["kind"] == "abbreviation"
            for alias in (slot["source_token"], slot["family"])
        }
        raw_pattern = (
            re.compile(
                r"(?<![A-Za-z0-9_])(?:"
                + "|".join(
                    re.escape(token)
                    for token in sorted(source_aliases, key=len, reverse=True)
                )
                + r")(?![A-Za-z0-9_])",
                re.IGNORECASE,
            )
            if source_aliases
            else None
        )
        texts = [
            ([field, "gaps_zh", index], text)
            for index, text in enumerate(candidate["gaps_zh"])
        ] + [
            ([field, "definitions_zh", key], text)
            for key, text in candidate["definitions_zh"].items()
        ]
        for path, text in texts:
            lexical_text = (
                re.sub(r"\[\[CITATION:C[0-9]{3,}\]\]", "", text)
                if allow_citations and path[1] == "gaps_zh"
                else text
            )
            tokens = list(
                dict.fromkeys(
                    TOKENS.findall(lexical_text)
                    + (raw_pattern.findall(lexical_text) if raw_pattern else [])
                )
            )
            markers = MARKERS.findall(text)
            urls = [url for _, _, url in source_url_spans(text)]
            if tokens or markers:
                errors.append(
                    {
                        "path": path,
                        "validator": "immutable_slot_raw_abbreviation",
                        "message": (
                            "Return Chinese prose only in gaps and expansions. The script "
                            "inserts every exact source abbreviation; do not copy tokens or markers."
                        ),
                        "observed_tokens": tokens,
                        "observed_markers": markers,
                    }
                )
            if urls:
                errors.append(
                    {
                        "path": path,
                        "validator": "immutable_slot_raw_url",
                        "message": "The script inserts each whole URL unchanged; do not put URLs in prose gaps or definitions.",
                        "observed_urls": urls,
                    }
                )
        for key, expansion in candidate["definitions_zh"].items():
            if re.search(r"[（）()\r\n]", expansion):
                errors.append(
                    {
                        "path": [field, "definitions_zh", key],
                        "validator": "immutable_slot_expansion",
                        "message": "Provide only the Chinese full name, without parentheses or newlines.",
                    }
                )
    if errors:
        return {}, [], errors
    rendered, repairs = {}, []
    for field, plan in prepared.items():
        candidate = output[field]
        gaps = list(candidate["gaps_zh"])
        expansions = {
            metadata["family"]: candidate["definitions_zh"][key]
            for key, metadata in plan["required_definitions"].items()
        }
        for slot in plan["slots"]:
            index = slot["index"]
            expansion = expansions.get(slot["family"])
            if not expansion:
                continue
            before = [gaps[index], gaps[index + 1]]
            gaps[index], gaps[index + 1] = _clean_boundaries(*before, expansion)
            after = [gaps[index], gaps[index + 1]]
            if after != before:
                repairs.append(
                    {
                        "field": field,
                        "slot_index": index,
                        "source_token": slot["source_token"],
                        "validator": "immutable_slot_exact_definition_cleanup",
                        "before": before,
                        "after": after,
                        "expansion": expansion,
                    }
                )
        for previous, following in zip(plan["slots"], plan["slots"][1:]):
            index = following["index"]
            gap = gaps[index]
            if gap.strip():
                continue
            previous_end = previous["source_token"][-1]
            following_start = following["source_token"][0]
            ascii_boundary = bool(re.fullmatch(r"[A-Za-z0-9_]", previous_end)) or (
                previous["kind"] == "abbreviation" and previous_end == "."
            )
            if (
                ascii_boundary
                and re.fullmatch(r"[A-Za-z0-9_]", following_start)
                and gap != " "
            ):
                gaps[index] = " "
                repairs.append(
                    {
                        "field": field,
                        "slot_index": index,
                        "source_token": following["source_token"],
                        "previous_source_token": previous["source_token"],
                        "validator": "immutable_slot_ascii_separator",
                        "before": gap,
                        "after": " ",
                    }
                )
        fragments = [gaps[0]]
        for slot in plan["slots"]:
            fragments.append(slot["source_token"])
            if slot["definition_key"]:
                fragments.append(
                    "（" + candidate["definitions_zh"][slot["definition_key"]] + "）"
                )
            fragments.append(gaps[slot["index"] + 1])
        rendered[field] = "".join(fragments)
    return rendered, repairs, []
