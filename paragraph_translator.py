#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from translation_policy import (
    contract_issues, prepare_paragraph, preview_text, prose_errors,
    render_slot_payload, restore_payload, slot_material,
)
from translation_policy.translation_contract import TOKENS
from translator_runtime import DEFAULT_DEEPSEEK_URL, resolve_config_path, resolve_runtime_paths

ROOT = Path(__file__).resolve().parent
_SURROGATE_CHARACTER = re.compile(r"[\ud800-\udfff]")
_INLINE_MARKDOWN_LINK = re.compile(
    r"(?<!!)\[(?P<label>[^\]\n]+)\]\(\s*"
    r"(?P<destination><[^>\n]+>|(?>[^()\s]+|\([^()\n]*\))+?)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?\s*\)"
)
_QUOTED_PASSAGE = re.compile(
    r"“[^”\n]+”|\"[^\"\n]+\"|「[^」\n]+」|『[^』\n]+』"
)


def repair_utf8_surrogates(text: str) -> str:
    """Preserve valid UTF-16 pairs and replace isolated surrogates deterministically."""
    if _SURROGATE_CHARACTER.search(text) is None:
        return text
    return text.encode("utf-16", errors="surrogatepass").decode(
        "utf-16", errors="replace"
    )


def make_utf8_safe(value: Any) -> Any:
    """Recursively make every string safe for UTF-8 JSON and HTTP transport."""
    if isinstance(value, str):
        return repair_utf8_surrogates(value)
    if isinstance(value, dict):
        return {make_utf8_safe(key): make_utf8_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [make_utf8_safe(item) for item in value]
    if isinstance(value, tuple):
        return tuple(make_utf8_safe(item) for item in value)
    return value


def decode_utf8_input(data: bytes) -> str:
    """Decode clipboard/selection bytes without aborting on a malformed character."""
    return repair_utf8_surrogates(data.decode("utf-8", errors="replace"))


def streamed_translation(content: str) -> str:
    """Read only a top-level JSON translation string, even before it closes."""
    decoder = json.JSONDecoder()
    text = content.lstrip()
    if not text.startswith("{"):
        return ""
    index = 1
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        try:
            key, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            return ""
        if not isinstance(key, str):
            return ""
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] != ":":
            return ""
        index += 1
        while index < len(text) and text[index].isspace():
            index += 1
        if key == "translation":
            if index >= len(text) or text[index] != '"':
                return ""
            index += 1
            decoded: list[str] = []
            closed = False
            escapes = {'"': '"', "\\": "\\", "/": "/", "b": "\b",
                       "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
            while index < len(text):
                character = text[index]
                if character == '"':
                    closed = True
                    break
                if character == "\\":
                    if index + 1 >= len(text):
                        break
                    escaped = text[index + 1]
                    if escaped == "u":
                        digits = text[index + 2:index + 6]
                        if len(digits) != 4 or re.fullmatch(r"[0-9a-fA-F]{4}", digits) is None:
                            break
                        decoded.append(chr(int(digits, 16)))
                        index += 6
                        continue
                    if escaped not in escapes:
                        break
                    decoded.append(escapes[escaped])
                    index += 2
                    continue
                if ord(character) < 32:
                    break
                decoded.append(character)
                index += 1
            if not closed and decoded and 0xD800 <= ord(decoded[-1]) <= 0xDBFF:
                decoded.pop()  # Wait for the low half of a split Unicode pair.
            return repair_utf8_surrogates("".join(decoded))
        try:
            _, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            return ""
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] != ",":
            return ""
        index += 1
    return ""


def split_paragraphs(text: str) -> list[str]:
    """Treat every non-empty selected line as a paragraph and ignore blank lines."""
    normalized = repair_utf8_surrogates(text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    return [line.strip() for line in normalized.split("\n") if line.strip()]


def normalize_translation(text: str) -> str:
    """Force one model result to remain one output paragraph."""
    return re.sub(r"\s*\n+\s*", " ", repair_utf8_surrogates(text).strip())


def extract_inline_markdown_links(text: str) -> list[dict[str, str]]:
    """Extract standard inline Markdown web links without interpreting their labels."""
    return [
        {
            "source_text": match.group(0),
            "label": match.group("label"),
            "destination": match.group("destination"),
        }
        for match in _INLINE_MARKDOWN_LINK.finditer(repair_utf8_surrogates(text))
    ]


def extract_quoted_passages(text: str) -> list[str]:
    """Extract double-quoted prose spans that must remain translatable content."""
    text = repair_utf8_surrogates(text)
    passages = []
    for match in _QUOTED_PASSAGE.finditer(text):
        # Quoted acronyms in definition units follow the definition policy.
        if (TOKENS.fullmatch(match.group()[1:-1])
                and re.search(r"[（(]\s*$", text[:match.start()])
                and re.match(r"\s*[）)]", text[match.end():])):
            continue
        passages.append(match.group())
    return passages


def normalize_quoted_prose_misclassifications(
    payload: Any,
    source_text: str = "",
) -> Any:
    """Remove quoted prose from unresolved citations without changing the translation."""
    if not isinstance(payload, dict):
        return payload
    unresolved = payload.get("unresolved_citations")
    if not isinstance(unresolved, list):
        return payload

    source_quotes = set(extract_quoted_passages(source_text))
    normalized_unresolved: list[Any] = []
    for item in unresolved:
        candidate = item if isinstance(item, str) else (
            item.get("source_text", "") if isinstance(item, dict) else ""
        )
        candidate_text = str(candidate).strip()
        if candidate_text in source_quotes or extract_quoted_passages(candidate_text) == [candidate_text]:
            continue
        normalized_unresolved.append(item)

    normalized = dict(payload)
    normalized["unresolved_citations"] = normalized_unresolved
    return normalized


def _is_citation_like_source(source_text: str) -> bool:
    text = source_text.strip()
    if _is_footnote_source(text):
        return True
    if re.search(r"\b10\.[0-9]{4,9}/\S+", text, re.IGNORECASE):
        return True
    author = r"(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+|[A-Z]{2,})"
    author_group = (
        rf"(?:{author}(?:,\s*{author})*(?:,?\s*(?:and|&)\s*{author})?"
        rf"|{author}\s+et\s+al\.?)"
    )
    year = r"[12][0-9]{3}[a-z]?"
    return bool(re.search(
        rf"\b{author_group}\s*(?:\(\s*|,\s*|\s+){year}(?:\s*\))?",
        text,
    ))


def normalize_non_citation_unresolved(payload: Any) -> Any:
    """Keep unresolved entries only when their source text is citation-like."""
    if not isinstance(payload, dict):
        return payload
    unresolved = payload.get("unresolved_citations")
    if not isinstance(unresolved, list):
        return payload

    normalized_unresolved: list[Any] = []
    for item in unresolved:
        candidate = item if isinstance(item, str) else (
            item.get("source_text", "") if isinstance(item, dict) else ""
        )
        if _is_citation_like_source(str(candidate)):
            normalized_unresolved.append(item)
    normalized = dict(payload)
    normalized["unresolved_citations"] = normalized_unresolved
    return normalized


def normalize_markdown_link_misclassifications(
    payload: Any,
    source_text: str = "",
) -> Any:
    """Remove link-shaped unresolved citations; link preservation is checked separately."""
    if not isinstance(payload, dict):
        return payload
    unresolved = payload.get("unresolved_citations")
    if not isinstance(unresolved, list):
        return payload

    translation_destinations = {
        link["destination"]
        for link in extract_inline_markdown_links(str(payload.get("translation", "")))
    }
    source_destinations = {
        link["destination"] for link in extract_inline_markdown_links(source_text)
    }
    known_destinations = translation_destinations | source_destinations
    normalized_unresolved: list[Any] = []
    for item in unresolved:
        candidate = item if isinstance(item, str) else (
            item.get("source_text", "") if isinstance(item, dict) else ""
        )
        links = extract_inline_markdown_links(str(candidate))
        if links and all(
            link["destination"] in known_destinations for link in links
        ):
            continue
        normalized_unresolved.append(item)

    normalized = dict(payload)
    normalized["unresolved_citations"] = normalized_unresolved
    return normalized


def normalize_translation_payload(
    payload: Any,
    source_text: str = "",
) -> Any:
    """Apply deterministic result cleanup in one stable, reusable order."""
    normalized = normalize_markdown_link_misclassifications(payload, source_text)
    normalized = normalize_quoted_prose_misclassifications(normalized, source_text)
    return normalize_non_citation_unresolved(normalized)


def format_markdown_sentence_list(paragraphs: list[str]) -> str:
    """Emit one compact unordered Markdown list per translated paragraph."""
    groups: list[str] = []
    for paragraph in paragraphs:
        items: list[str] = []
        links = iter(_INLINE_MARKDOWN_LINK.finditer(paragraph))
        link = next(links, None)
        start = 0
        for end, character in enumerate(paragraph):
            while link is not None and end >= link.end():
                link = next(links, None)
            if character != "。" or (link is not None and link.start() <= end):
                continue
            sentence = paragraph[start:end + 1].strip()
            start = end + 1
            if not sentence.removesuffix("。").strip():
                continue
            items.append(f"- {sentence}")
        remainder = paragraph[start:].strip()
        if remainder:
            items.append(f"- {remainder}")
        if items:
            groups.append("\n".join(items))
    return "\n\n".join(groups)


def format_preserved_paragraphs(paragraphs: list[str]) -> str:
    """Keep translated paragraph boundaries without adding Markdown list markers."""
    return "\n\n".join(paragraph.strip() for paragraph in paragraphs if paragraph.strip())


def single_paragraph_max_tokens(source: str, ceiling: int) -> int:
    """Choose a safe single-paragraph output cap without reserving 4096 tokens by default."""
    estimated = math.ceil(len(source) * 1.1) + 256
    return min(ceiling, max(512, estimated))


_ENGLISH_PHRASE_TOKEN = re.compile(r"[A-Za-z]+(?:[-'’][A-Za-z]+)*")


def is_english_phrase(source: str, word_limit: int) -> bool:
    """Select a short word/phrase without misclassifying punctuated sentences."""
    text = source.strip()
    if not text or word_limit < 1 or re.search(r"[.!?,;:。！？；：]", text):
        return False
    words = _ENGLISH_PHRASE_TOKEN.findall(text)
    if not 1 <= len(words) <= word_limit:
        return False
    remainder = _ENGLISH_PHRASE_TOKEN.sub("", text)
    return not remainder.strip()


def phrase_max_tokens(ceiling: int) -> int:
    """Keep the ultra-fast phrase response small while retaining JSON headroom."""
    return min(ceiling, 128)


_CITATION_MARKER = re.compile(r"\[\[CITATION:(C[0-9]{3,})\]\]")
_TRAILING_NOTE = re.compile(
    r"(?:\[\^(?P<markdown>[0-9]{1,3})\]|\[(?P<bracket>[0-9]{1,3})\]|"
    r"(?P<superscript>[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,3}))\s*$"
)
_MARKDOWN_FOOTNOTE = re.compile(r"\[\^[0-9]{1,3}\]")
_UNICODE_SUPERSCRIPT = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,3}")
_AFTER_SENTENCE_NOTE = re.compile(
    r"(?:(?<=[!?。！？])|(?<=[A-Za-z\u3400-\u9fff）)\]\"'”’]\.))"
    r"\s*(?:\[[0-9]{1,3}\]|[0-9]{1,3})(?=\s|$)"
)


def strip_footnote_markers(source: str) -> str:
    """Remove footnote-like numeric markers before translation."""
    cleaned = _MARKDOWN_FOOTNOTE.sub("", source)
    cleaned = _UNICODE_SUPERSCRIPT.sub("", cleaned)
    cleaned = _AFTER_SENTENCE_NOTE.sub("", cleaned)
    trailing = _TRAILING_NOTE.search(cleaned)
    if trailing is not None:
        cleaned = cleaned[:trailing.start()]
    return cleaned.rstrip()


def _contract_issue(
    message: str,
    instance_path: str = "/translation",
    expected: str = "citation markers and metadata must correspond exactly without rendered duplicates",
    actual_type: str = "string",
) -> dict[str, str]:
    return {
        "code": "citation_contract",
        "message": message,
        "instance_path": instance_path,
        "schema_path": "/x-citation-contract",
        "validator": "citation_contract",
        "expected": expected,
        "actual_type": actual_type,
    }


def _request_current_paragraph(request: dict[str, Any]) -> dict[str, Any]:
    messages = request.get("body", {}).get("messages", [])
    if not isinstance(messages, list):
        return {}
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        try:
            content = json.loads(str(message.get("content", "")))
        except json.JSONDecodeError:
            continue
        current = content.get("current_paragraph") if isinstance(content, dict) else None
        if isinstance(current, dict):
            return current
    return {}


def _request_current_source(request: dict[str, Any]) -> str:
    return str(_request_current_paragraph(request).get("source", ""))


def academic_contract_errors(payload: dict[str, Any], paragraph: dict[str, Any]) -> tuple[dict, ...]:
    if "translation_source" not in paragraph:
        return ()  # Context analysis and the short-phrase path have separate contracts.
    citations = payload.get("citations", [])
    visible = payload["translation"]
    if citations or payload.get("unresolved_citations"):
        visible = replace_citation_markers(visible, citations, payload.get("unresolved_citations", []))
    references = [{**item, "rendered": format_citation(item)} for item in citations]
    errors = prose_errors(paragraph["source"], visible, references)
    if not re.search(r"[\u3400-\u9fff]", visible):
        errors.append({"path": ["translation"], "validator": "chinese_translation",
                       "message": "Return Chinese prose; do not echo the English source."})
    return contract_issues(errors)


def markdown_link_contract_errors(
    payload: dict[str, Any],
    source_text: str,
) -> tuple[dict[str, str], ...]:
    expected = extract_inline_markdown_links(source_text)
    if not expected:
        return ()
    actual = extract_inline_markdown_links(str(payload.get("translation", "")))
    expected_destinations = [link["destination"] for link in expected]
    actual_destinations = [link["destination"] for link in actual]
    if actual_destinations == expected_destinations:
        return ()
    issue = _contract_issue(
        "Every inline Markdown link must remain [translated label](original URL), in source order; "
        f"expected URLs {expected_destinations!r}, got {actual_destinations!r}.",
        "/translation",
        "translated Markdown link labels with byte-for-byte identical URLs",
    )
    issue["validator"] = "markdown_link_contract"
    issue["schema_path"] = "/x-markdown-link-contract"
    return (issue,)


def quoted_prose_contract_errors(
    payload: dict[str, Any],
    source_text: str,
) -> tuple[dict[str, str], ...]:
    expected_count = len(extract_quoted_passages(source_text))
    if expected_count == 0:
        return ()
    actual_count = len(extract_quoted_passages(str(payload.get("translation", ""))))
    if actual_count >= expected_count:
        return ()
    issue = _contract_issue(
        "Quoted prose is translatable content, not a citation; translate it and preserve its quote boundaries. "
        f"Expected at least {expected_count} quoted passage(s), got {actual_count}.",
        "/translation",
        "translated quoted prose with preserved quote boundaries",
    )
    issue["validator"] = "quoted_prose_contract"
    issue["schema_path"] = "/x-quoted-prose-contract"
    return (issue,)


def _expected_author_form(work: dict[str, Any], source_text: str = "") -> str:
    authors = [str(author).strip() for author in work.get("authors", [])]
    if len(authors) >= 3:
        return "et_al"
    if len(authors) == 2:
        return "dual"
    if len(authors) == 1:
        if work.get("author_form") == "et_al":
            return "et_al"
        first_author = re.escape(authors[0])
        if re.search(first_author + r"\s+et\s+al\.?", source_text, re.IGNORECASE):
            return "et_al"
        return "single"
    return str(work.get("author_form", ""))


def citation_contract_errors(payload: dict[str, Any]) -> tuple[dict[str, str], ...]:
    translation = str(payload.get("translation", ""))
    citations = payload.get("citations", [])
    if not isinstance(citations, list):
        return ()

    marker_ids = _CITATION_MARKER.findall(translation)
    citation_ids = [
        str(citation.get("citation_id", ""))
        for citation in citations
        if isinstance(citation, dict)
    ]
    issues: list[dict[str, str]] = []
    if len(marker_ids) != len(set(marker_ids)):
        issues.append(_contract_issue("Each citation marker must occur exactly once."))
    if len(citation_ids) != len(set(citation_ids)):
        issues.append(_contract_issue("Each citations item must have a unique citation_id.", "/citations"))
    if set(marker_ids) != set(citation_ids):
        issues.append(_contract_issue(
            "The set of citation IDs in translation must exactly equal the set in citations."
        ))

    wrapped_marker = re.compile(
        r"[（(\[]\s*\[\[CITATION:C[0-9]{3,}\]\]\s*[）)\]]"
    )
    if wrapped_marker.search(translation):
        issues.append(_contract_issue(
            "Citation markers must be bare and must replace the original citation brackets entirely."
        ))
    text_without_markers = _CITATION_MARKER.sub("", translation)
    for citation_index, citation in enumerate(citations):
        if not isinstance(citation, dict):
            continue
        source_text = str(citation.get("source_text", ""))
        if source_text and not _is_citation_like_source(source_text):
            issues.append(_contract_issue(
                "Only genuine bibliographic citations may appear in citations; ordinary prose, "
                "quotations, titles, links, and statements must be translated as content.",
                f"/citations/{citation_index}/source_text",
                "source text with bibliographic citation features",
            ))
        for work_index, work in enumerate(citation.get("works", [])):
            authors = work.get("authors", []) if isinstance(work, dict) else []
            if not authors:
                continue
            expected_form = _expected_author_form(work, source_text)
            actual_form = str(work.get("author_form", ""))
            if actual_form != expected_form:
                issues.append(_contract_issue(
                    f"author_form must be {expected_form} for the supplied author list and source citation; got {actual_form}.",
                    f"/citations/{citation_index}/works/{work_index}/author_form",
                    expected_form,
                ))
            first_author = str(authors[0]).strip()
            year = str(work.get("year", ""))
            if not first_author or not year:
                continue
            duplicate_pattern = re.compile(
                re.escape(first_author) + r".{0,40}?" + re.escape(year),
                re.IGNORECASE,
            )
            if duplicate_pattern.search(text_without_markers):
                issues.append(_contract_issue(
                    f"Rendered author-year citation {first_author} {year} appears outside its marker."
                ))
    return tuple(issues)


def _author_label(authors: list[str], author_form: str | None = None) -> str:
    cleaned = [str(author).strip() for author in authors]
    if not cleaned or any(not author for author in cleaned):
        raise ValueError("引用作者姓氏列表不能为空。")
    form = author_form or ("single" if len(cleaned) == 1 else "dual" if len(cleaned) == 2 else "et_al")
    if form == "single":
        if len(cleaned) != 1:
            raise ValueError("single 引用必须恰好包含一个作者姓氏。")
        return cleaned[0]
    if form == "dual":
        if len(cleaned) != 2:
            raise ValueError("dual 引用必须恰好包含两个作者姓氏。")
        return f"{cleaned[0]}和{cleaned[1]}"
    if form == "et_al":
        return f"{cleaned[0]}等"
    raise ValueError(f"未知作者形式：{form}")


def _year_labels(works: list[dict[str, Any]]) -> list[str]:
    """Format years and deterministically assign a/b to same-year works."""
    parsed: list[tuple[int, str]] = []
    positions_by_year: dict[int, list[int]] = {}
    for position, work in enumerate(works):
        year = int(work["year"])
        suffix = str(work.get("year_suffix", "")).strip().lower()
        if suffix and (len(suffix) != 1 or not suffix.isascii() or not suffix.isalpha()):
            raise ValueError(f"非法年份后缀：{suffix}")
        parsed.append((year, suffix))
        positions_by_year.setdefault(year, []).append(position)

    resolved_suffixes = [suffix for _, suffix in parsed]
    for year, positions in positions_by_year.items():
        if len(positions) == 1:
            continue
        used = {resolved_suffixes[position] for position in positions if resolved_suffixes[position]}
        if len(used) != sum(bool(resolved_suffixes[position]) for position in positions):
            raise ValueError(f"同一作者的 {year} 年引用含重复年份后缀。")
        available = (chr(code) for code in range(ord("a"), ord("z") + 1))
        for position in positions:
            if resolved_suffixes[position]:
                continue
            suffix = next((candidate for candidate in available if candidate not in used), None)
            if suffix is None:
                raise ValueError(f"同一作者的 {year} 年引用超过 26 篇。")
            resolved_suffixes[position] = suffix
            used.add(suffix)

    return [
        f"{year}{resolved_suffixes[position]}"
        for position, (year, _) in enumerate(parsed)
    ]


def _citation_groups(
    works: list[dict[str, Any]],
    source_text: str = "",
) -> list[tuple[str, list[str]]]:
    grouped: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}
    for work in works:
        authors = tuple(str(author).strip() for author in work["authors"])
        if not authors or any(not author for author in authors):
            raise ValueError("引用作者姓氏列表不能为空。")
        author_form = _expected_author_form(work, source_text)
        grouped.setdefault((author_form, authors), []).append(work)
    return [
        (_author_label(list(authors), author_form), _year_labels(author_works))
        for (author_form, authors), author_works in grouped.items()
    ]


def _join_narrative_groups(groups: list[str]) -> str:
    if len(groups) == 1:
        return groups[0]
    if len(groups) == 2:
        return f"{groups[0]}和{groups[1]}"
    return "、".join(groups[:-1]) + "和" + groups[-1]


def format_citation(citation: dict[str, Any]) -> str:
    style = citation["citation_style"]
    if style == "superscript":
        return ""
    groups = _citation_groups(
        list(citation["works"]),
        str(citation.get("source_text", "")),
    )
    if not groups:
        raise ValueError("引用块不得为空。")
    if style == "narrative":
        rendered = [f"{authors}（{'，'.join(years)}）" for authors, years in groups]
        return _join_narrative_groups(rendered)
    if style == "parenthetical":
        rendered = [f"{authors}，{'，'.join(years)}" for authors, years in groups]
        return f"（{'；'.join(rendered)}）"
    raise ValueError(f"未知引用类型：{style}")


def _direct_citation_pattern(citation: dict[str, Any]) -> re.Pattern[str] | None:
    works = citation.get("works", [])
    if not works:
        return None
    work = works[0]
    authors = [str(author).strip() for author in work.get("authors", [])]
    if not authors:
        return None
    first_author = re.escape(authors[0])
    author_form = _expected_author_form(work, str(citation.get("source_text", "")))
    if author_form == "dual" and len(authors) >= 2:
        author_expression = (
            first_author + r"\s*(?:和|与|及|and|&)\s*" + re.escape(authors[1])
        )
    elif author_form == "et_al":
        author_expression = first_author + r"(?:\s*(?:等人?|et\s+al\.?))?"
    else:
        author_expression = first_author
    year = re.escape(str(work.get("year", "")))
    if not year:
        return None
    if citation.get("citation_style") == "parenthetical":
        expression = (
            r"[（(]\s*[^）)]{0,30}?" + author_expression
            + r"[^）)]{0,50}?" + year + r"[^）)]{0,80}?[）)]"
        )
    else:
        expression = (
            author_expression + r"\s*[（(][^）)]{0,50}?"
            + year + r"[^）)]{0,80}?[）)]"
        )
    return re.compile(expression, re.IGNORECASE)


def _normalize_citation_placeholder(
    translation: str,
    citation: dict[str, Any],
) -> str:
    citation_id = str(citation["citation_id"])
    marker = f"[[CITATION:{citation_id}]]"
    wrapped = re.compile(r"[（(\[]\s*" + re.escape(marker) + r"\s*[）)\]]")
    normalized = wrapped.sub(marker, translation)
    direct_pattern = _direct_citation_pattern(citation)
    if direct_pattern is not None and direct_pattern.search(normalized):
        normalized = normalized.replace(marker, "")
        normalized = direct_pattern.sub(marker, normalized, count=1)
        normalized = wrapped.sub(marker, normalized)
    return normalized


def _is_footnote_source(source_text: str) -> bool:
    value = source_text.strip()
    return bool(re.fullmatch(
        r"(?:\[\^?[0-9]{1,3}\]|[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,3}|[0-9]{1,3})",
        value,
    ))


def replace_citation_markers(
    translation: str,
    citations: list[dict[str, Any]],
    unresolved_citations: list[dict[str, Any]],
) -> str:
    footnote_unresolved = [
        item for item in unresolved_citations
        if _is_footnote_source(str(item.get("source_text", "")))
    ]
    unresolved_citations = [
        item for item in unresolved_citations
        if not _is_footnote_source(str(item.get("source_text", "")))
    ]
    if unresolved_citations:
        details = "；".join(
            f"{item['source_text']}：{item['reason']}" for item in unresolved_citations[:3]
        )
        raise ValueError(f"存在无法解析为作者—年份格式的引用：{details}")

    citations_by_id: dict[str, dict[str, Any]] = {}
    for citation in citations:
        citation_id = str(citation["citation_id"])
        if citation_id in citations_by_id:
            raise ValueError(f"引用标识符重复：{citation_id}")
        citations_by_id[citation_id] = citation

    for citation in citations:
        citation_id = str(citation["citation_id"])
        marker = f"[[CITATION:{citation_id}]]"
        if citation.get("citation_style") == "superscript":
            translation = translation.replace(marker, "")
            continue
        translation = _normalize_citation_placeholder(translation, citation)

    ignored_footnotes = len(footnote_unresolved)
    for marker_id in _CITATION_MARKER.findall(translation):
        if ignored_footnotes <= 0:
            break
        if marker_id not in citations_by_id:
            translation = translation.replace(f"[[CITATION:{marker_id}]]", "", 1)
            ignored_footnotes -= 1

    marker_ids = _CITATION_MARKER.findall(translation)
    marker_set = set(marker_ids)
    citation_set = {
        citation_id for citation_id, citation in citations_by_id.items()
        if citation.get("citation_style") != "superscript"
    }
    if marker_set != citation_set:
        missing_metadata = sorted(marker_set - citation_set)
        missing_markers = sorted(citation_set - marker_set)
        raise ValueError(
            f"引用标识符不匹配：缺少结构化数据={missing_metadata}，缺少译文标识符={missing_markers}"
        )

    rendered = _CITATION_MARKER.sub(
        lambda match: format_citation(citations_by_id[match.group(1)]),
        translation,
    )
    if "[[CITATION:" in rendered:
        raise ValueError("译文中仍有无法识别的引用标识符。")
    return rendered


def streamed_paragraph_translation(
    content: str, paragraph: dict[str, Any],
) -> tuple[str, bool]:
    """Preview prose in order, holding internal citation tokens until metadata arrives."""
    translation = preview_text(streamed_translation(content), paragraph)
    try:
        payload = normalize_translation_payload(json.loads(content), paragraph["source"])
        if "gaps_zh" in payload:
            payload, errors = render_slot_payload(payload, paragraph, citations=True)
        else:
            payload, errors = restore_payload(payload, paragraph)
        if errors:
            raise ValueError("Invalid abbreviation transport")
        if payload["paragraph_id"] == paragraph["paragraph_id"]:
            rendered = replace_citation_markers(
                payload["translation"], payload["citations"], payload["unresolved_citations"],
            )
            return normalize_translation(strip_footnote_markers(rendered)), True
    except (ValueError, KeyError, TypeError, AttributeError):
        # Partial JSON and failed attempts are previews only; the gateway still
        # validates the full response and supplies structured errors on retry.
        pass
    marker = "[[CITATION:"
    if marker in translation:
        translation = translation[:translation.index(marker)]
    else:
        for start in range(max(0, len(translation) - len(marker)), len(translation)):
            if marker.startswith(translation[start:]):
                translation = translation[:start]
                break
    return normalize_translation(strip_footnote_markers(translation)), False


class BatchGateway(Protocol):
    def run(
        self,
        requests: list[dict[str, Any]],
        schema: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        ...


class ProgressReporter(Protocol):
    def update(self, percent: int, message: str, mode: str | None = None) -> None:
        ...


@dataclass(frozen=True)
class Settings:
    model: str
    temperature: float
    max_tokens: int
    seed: int
    concurrency: int
    validation_retries: int
    prompt_token_limit: int
    phrase_word_limit: int
    env_file: Path
    deepseek_url: str = DEFAULT_DEEPSEEK_URL

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        path = resolve_config_path(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        runtime = resolve_runtime_paths(path, config=raw)
        return cls(
            model=str(raw["model"]),
            temperature=float(raw["temperature"]),
            max_tokens=int(raw["max_tokens"]),
            seed=int(raw["seed"]),
            concurrency=int(raw["concurrency"]),
            validation_retries=int(raw["validation_retries"]),
            prompt_token_limit=int(raw["prompt_token_limit"]),
            phrase_word_limit=int(raw.get("phrase_word_limit", 8)),
            env_file=runtime.env_file,
            deepseek_url=str(raw.get("deepseek_url", DEFAULT_DEEPSEEK_URL)).strip(),
        )


class StatusReporter:
    def __init__(self, path: Path | None, stream_path: Path | None = None) -> None:
        self.path = path
        self.stream_path = stream_path
        self.mode: str | None = None
        self.translation: str | None = None
        self._last_stream_write = 0.0

    def update(self, percent: int, message: str, mode: str | None = None) -> None:
        if self.path is None:
            return
        if mode is not None:
            self.mode = mode
        status = {
            "percent": max(0, min(100, int(percent))),
            "message": message,
        }
        if self.mode is not None:
            status["mode"] = self.mode
        if self.translation is not None and self.stream_path is None:
            status["translation"] = self.translation
        payload = json.dumps(
            status,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)

    def update_translation(self, translation: str) -> None:
        if self.path is None or translation == self.translation:
            return
        previous = self.translation
        self.translation = translation
        if self.stream_path is not None:
            # Every API content update is flushed as a separate event. Unlike
            # an overwritten snapshot, this cannot lose intermediate chunks.
            event = json.dumps({
                "translation": translation,
                "mode": self.mode,
                "message": f"{self.mode or '单段落快速翻译'}：正在接收译文…",
            }, ensure_ascii=False, separators=(",", ":"))
            with self.stream_path.open("ab", buffering=0) as stream:
                stream.write((event + "\n").encode("utf-8"))
            self.update(60, f"{self.mode or '单段落快速翻译'}：正在接收译文…")
            return
        now = time.monotonic()
        if translation and previous and now - self._last_stream_write < 0.03:
            return
        self._last_stream_write = now
        self.update(60, f"{self.mode or '单段落快速翻译'}：正在接收译文…")


class TemplateRepository:
    def __init__(self, directory: Path) -> None:
        from jinja2 import Environment, StrictUndefined

        env = Environment(undefined=StrictUndefined, autoescape=False)
        env.filters["tojson"] = lambda value: json.dumps(
            make_utf8_safe(value), ensure_ascii=False, separators=(",", ":")
        )
        self._templates = {
            path.name: env.from_string(path.read_text(encoding="utf-8"))
            for path in directory.glob("*.j2")
        }
        self.academic_policy = json.loads(self._templates["academic_policy.json.j2"].render())

    def render_json(self, name: str, **values: Any) -> dict[str, Any]:
        values.setdefault("academic_policy", self.academic_policy)
        return json.loads(self._templates[name].render(**values))

    def render_prompt(self, name: str, **values: Any) -> str:
        payload = self.render_json(name, **values)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class StreamingGateway:
    def __init__(self, settings: Settings) -> None:
        from dotenv import load_dotenv

        load_dotenv(settings.env_file)
        url = os.environ.get("DEEPSEEK_URL", "").strip() or settings.deepseek_url
        key = os.environ.get("DEEPSEEK_KEY", "").strip()
        if not key:
            raise RuntimeError(f"DEEPSEEK_KEY not found in {settings.env_file} or environment")
        if not url:
            raise RuntimeError("Set a non-empty DEEPSEEK_URL or deepseek_url in the configuration")

        self._config = {
            "api": {"id": "deepseek", "url": url, "key": key},
            "model": {
                "name": settings.model,
                "prompt_token_limit": settings.prompt_token_limit,
                "collect_thinking": False,
            },
            "task_table": {
                "mode": "stream",
                "quiet": True,
                "rounds": 1,
                "concurrency": settings.concurrency,
                "validation_retries": settings.validation_retries,
                "transport_retries": 3,
                "transport_retry_delay_seconds": 1,
                "progress_enabled": False,
            },
        }

    def run(
        self,
        requests: list[dict[str, Any]],
        schema: dict[str, Any] | None,
        *,
        on_content: Callable[[str, str], None] | None = None,
    ) -> list[dict[str, Any]]:
        from translator_transport.runner import run_stream
        from translator_transport.validation import OutputValidator, ValidationResult

        slots_mode = bool(schema and schema.get("x-slot-translation-contract"))
        final_schema = schema
        if slots_mode:
            name = "translation.schema.json" if schema.get("x-citation-contract") else "translation.single.schema.json"
            final_schema = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
        base_validator = OutputValidator(final_schema) if final_schema is not None else None

        def resolve(payload: Any, paragraph: dict) -> tuple[Any, tuple[dict, ...]]:
            if slots_mode:
                payload, errors = render_slot_payload(
                    payload, paragraph, citations=bool(schema.get("x-citation-contract")),
                )
                return payload, contract_issues(errors)
            if isinstance(payload, dict) and isinstance(payload.get("translation"), str):
                payload, errors = restore_payload(payload, paragraph)
                return payload, contract_issues(errors)
            return payload, ()

        class TranslationContractValidator(OutputValidator):
            def validate(self, content: str, req: dict[str, Any]) -> ValidationResult:
                try:
                    raw_payload = json.loads(content)
                except json.JSONDecodeError:
                    return super().validate(content, req)
                paragraph = _request_current_paragraph(req)
                source_text = str(paragraph.get("source", ""))
                payload = normalize_translation_payload(raw_payload, source_text)
                payload, transport_errors = resolve(payload, paragraph)
                if transport_errors:
                    return ValidationResult(False, "; ".join(error["message"] for error in transport_errors), transport_errors)
                normalized_content = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                assert base_validator is not None
                base_result = base_validator.validate(normalized_content, req)
                if not base_result.ok:
                    return base_result

                if self.schema_doc.get("x-paragraph-id-contract"):
                    expected_id = str(req.get("custom_id", ""))
                    actual_id = payload.get("paragraph_id")
                    if actual_id != expected_id:
                        message = (
                            "paragraph_id must equal request custom_id "
                            f"{expected_id}; got {actual_id!r}"
                        )
                        return ValidationResult(False, message, ({
                            "code": "paragraph_id_mismatch",
                            "message": message,
                            "instance_path": "/paragraph_id",
                            "schema_path": "/x-paragraph-id-contract",
                            "validator": "paragraph_id_contract",
                            "expected": expected_id,
                            "actual_type": type(actual_id).__name__,
                        },))

                errors: tuple[dict[str, str], ...] = ()
                if self.schema_doc.get("x-markdown-link-contract"):
                    errors += markdown_link_contract_errors(payload, source_text)
                if self.schema_doc.get("x-quoted-prose-contract"):
                    errors += quoted_prose_contract_errors(payload, source_text)
                if self.schema_doc.get("x-citation-contract"):
                    errors += citation_contract_errors(payload)
                if not errors:
                    errors += academic_contract_errors(payload, paragraph)
                if not errors:
                    return base_result
                message = "; ".join(error["message"] for error in errors)
                return ValidationResult(False, message, errors)

        with contextlib.redirect_stdout(io.StringIO()):
            rows = run_stream(
                requests,
                self._config,
                validator=TranslationContractValidator(schema) if schema is not None else None,
                **({"on_content": on_content} if on_content is not None else {}),
            )
        paragraphs = {request["custom_id"]: _request_current_paragraph(request) for request in requests}
        for row in rows:
            paragraph = paragraphs.get(row.get("custom_id"), {})
            if row.get("error") or not paragraph or not row.get("response"):
                continue
            payload, errors = resolve(json.loads(row["response"]), paragraph)
            if errors:
                raise RuntimeError("Translation transport changed after validation")
            payload = normalize_translation_payload(payload, paragraph.get("source", ""))
            row["response"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return rows

    def run_with_progress(
        self,
        requests: list[dict[str, Any]],
        schema: dict[str, Any] | None,
        on_content: Callable[[str, str], None],
    ) -> list[dict[str, Any]]:
        return self.run(requests, schema, on_content=on_content)


class ParagraphTranslator:
    def __init__(
        self,
        settings: Settings,
        gateway: BatchGateway,
        templates: TemplateRepository,
    ) -> None:
        self.settings = settings
        self.gateway = gateway
        self.templates = templates
        schema_dir = ROOT / "schemas"
        self.context_schema = self._load_schema(schema_dir / "context.schema.json")
        self.translation_schema = self._load_schema(
            schema_dir / "translation.schema.json"
        )
        self.single_translation_schema = self._load_schema(
            schema_dir / "translation.single.schema.json"
        )

    @staticmethod
    def _load_schema(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(f"schema must be a JSON object: {path}")
        return payload

    def _request(
        self,
        custom_id: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        return self.templates.render_json(
            "provider_request.json.j2",
            custom_id=custom_id,
            model=self.settings.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=self.settings.temperature,
            max_tokens=self.settings.max_tokens if max_tokens is None else max_tokens,
            seed=self.settings.seed,
        )

    @staticmethod
    def _payload_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        payloads: dict[str, dict[str, Any]] = {}
        for row in rows:
            custom_id = str(row.get("custom_id", ""))
            if custom_id in payloads:
                raise RuntimeError(f"Transport returned duplicate request ID: {custom_id}")
            if row.get("error"):
                details = row.get("validation_error_details") or []
                citation_only = bool(details) and all(
                    detail.get("validator") == "citation_contract" for detail in details
                )
                if not citation_only:
                    raise RuntimeError(f"Translation request {custom_id} failed: {row['error']}")
            try:
                payload = make_utf8_safe(json.loads(str(row["response"])))
            except (KeyError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Translation request {custom_id} returned unusable JSON") from exc
            if not isinstance(payload, dict):
                raise RuntimeError(f"Translation request {custom_id} did not return a JSON object")
            payloads[custom_id] = normalize_translation_payload(payload)
        return payloads

    @staticmethod
    def _build_paragraphs(sources: list[str]) -> list[dict[str, Any]]:
        single_paragraph = len(sources) == 1
        return [
            {
                "paragraph_id": f"P{index:04d}",
                "source": source if single_paragraph else strip_footnote_markers(source),
                "markdown_links": extract_inline_markdown_links(source),
                "quoted_passages": extract_quoted_passages(source),
            }
            for index, source in enumerate(sources, start=1)
        ]

    @staticmethod
    def _mode_label(
        *,
        single_paragraph: bool,
        phrase_mode: bool,
        preserve_paragraphs: bool,
    ) -> str:
        if phrase_mode:
            return "词组极速翻译"
        if single_paragraph:
            return "单段落快速翻译"
        if preserve_paragraphs:
            return "多段落综合翻译（保留原段落）"
        return "多段落综合翻译"

    def _document_context(
        self,
        paragraphs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        context_rows = self.gateway.run(
            [self._request(
                "CONTEXT",
                self.templates.render_prompt("context.system.json.j2"),
                self.templates.render_prompt(
                    "context.user.json.j2",
                    paragraphs=paragraphs,
                ),
            )],
            self.context_schema,
        )
        context = self._payload_by_id(context_rows).get("CONTEXT")
        if context is None:
            raise RuntimeError("Transport did not return the document context")
        return context

    def _translation_system_prompt(
        self,
        *,
        single_paragraph: bool,
        phrase_mode: bool,
        source: str = "",
    ) -> str:
        if phrase_mode:
            template = "translation.phrase.system.json.j2"
        elif single_paragraph:
            template = "translation.single.system.json.j2"
        else:
            template = "translation.system.json.j2"
        if phrase_mode:
            terminology = self.templates.academic_policy["terminology_policy"]
            glossary = [entry for entry in terminology["glossary"]
                        if re.search(r"(?<!\w)" + re.escape(entry["source"]) + r"(?!\w)", source, re.I)]
            return self.templates.render_prompt(template, academic_policy={
                "terminology_policy": {"rule": terminology["rule"], "glossary": glossary},
            })
        return self.templates.render_prompt(template)

    def _translation_requests(
        self,
        paragraphs: list[dict[str, Any]],
        *,
        single_paragraph: bool,
        phrase_mode: bool,
        document_context: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        system_prompt = self._translation_system_prompt(
            single_paragraph=single_paragraph,
            phrase_mode=phrase_mode,
            source=paragraphs[0]["source"] if phrase_mode else "",
        )
        requests: list[dict[str, Any]] = []
        for index, paragraph in enumerate(paragraphs):
            if phrase_mode:
                user_prompt = self.templates.render_prompt(
                    "translation.phrase.user.json.j2",
                    current_paragraph=paragraph,
                )
                max_tokens = phrase_max_tokens(self.settings.max_tokens)
            elif single_paragraph:
                user_prompt = self.templates.render_prompt(
                    "translation.single.user.json.j2",
                    current_paragraph=paragraph,
                )
                max_tokens = single_paragraph_max_tokens(
                    paragraph.get("translation_source", paragraph["source"]),
                    self.settings.max_tokens,
                )
            else:
                if document_context is None:
                    raise RuntimeError("document context is required for multiple paragraphs")
                user_prompt = self.templates.render_prompt(
                    "translation.user.json.j2",
                    document_context=document_context,
                    previous_paragraph=paragraphs[index - 1] if index > 0 else None,
                    current_paragraph=paragraph,
                    next_paragraph=(
                        paragraphs[index + 1] if index + 1 < len(paragraphs) else None
                    ),
                )
                max_tokens = self.settings.max_tokens
            requests.append(
                self._request(
                    paragraph["paragraph_id"],
                    system_prompt,
                    user_prompt,
                    max_tokens=max_tokens,
                )
            )
        return requests

    def _retry_slots(
        self, rows: list[dict[str, Any]], paragraphs: list[dict[str, Any]],
        *, single_paragraph: bool, document_context: dict[str, Any] | None,
        on_content: Callable[[str, str], None] | None,
    ) -> list[dict[str, Any]]:
        """Retry only exhausted acronym/URL failures; retain successful rows."""
        retry_rows = {
            row["custom_id"]: row for row in rows if row.get("error") and any(
                str(error.get("validator", "")).startswith(
                    ("protected_abbreviation", "abbreviation_", "url_literals", "markdown_link_contract")
                ) for error in row.get("validation_error_details", [])
            )
        }
        if not retry_rows:
            return rows
        schema = self.single_translation_schema if single_paragraph else self.translation_schema
        citation_prompt = json.loads(self._translation_system_prompt(
            single_paragraph=single_paragraph, phrase_mode=False,
        ))
        # Reuse the authoritative citation rules without changing final output fields.
        citation_policy = {"rules": [rule for rule in citation_prompt["rules"]
                                    if any(term in rule.lower() for term in ("citation", "author", "footnote"))]}
        system = self.templates.render_prompt(
            "translation.slots.system.json.j2", citation_policy=citation_policy,
        )
        requests = []
        for index, paragraph in enumerate(paragraphs):
            failed = retry_rows.get(paragraph["paragraph_id"])
            if failed is None:
                continue
            material, wire_schema = slot_material(paragraph)
            wire_schema = {**wire_schema, "properties": {**wire_schema["properties"],
                "paragraph_id": {"const": paragraph["paragraph_id"]}},
                "required": ["paragraph_id", *wire_schema["required"]]}
            if not single_paragraph:
                for field in ("citations", "unresolved_citations"):
                    wire_schema["properties"][field] = schema["properties"][field]
                    wire_schema["required"].append(field)
            errors = [{key: value for key, value in error.items()
                       if key not in {"observed_context", "forbidden_form", "forbidden_nested_form"}}
                      for error in failed["validation_error_details"]]
            user = self.templates.render_prompt(
                "translation.slots.user.json.j2", current_paragraph=paragraph,
                slot_material=material, output_schema=wire_schema,
                document_context=document_context,
                previous_paragraph=paragraphs[index - 1] if index else None,
                next_paragraph=paragraphs[index + 1] if index + 1 < len(paragraphs) else None,
                validation_retry_errors=errors,
            )
            requests.append(self._request(paragraph["paragraph_id"], system, user))
        slot_schema = {**schema, "x-slot-translation-contract": True}
        if on_content is not None:
            repaired = self.gateway.run_with_progress(requests, slot_schema, on_content)
        else:
            repaired = self.gateway.run(requests, slot_schema)
        by_id = {row["custom_id"]: row for row in repaired}
        missing = set(retry_rows) - set(by_id)
        if missing:
            raise RuntimeError(f"Structured slot translation did not return {sorted(missing)}")
        return [by_id.get(row["custom_id"], row) for row in rows]

    @staticmethod
    def _translated_paragraphs(
        paragraphs: list[dict[str, Any]],
        translated: dict[str, dict[str, Any]],
        *,
        single_paragraph: bool,
        mode_label: str,
        reporter: ProgressReporter,
    ) -> list[str]:
        results: list[str] = []
        for position, paragraph in enumerate(paragraphs, start=1):
            expected_id = paragraph["paragraph_id"]
            payload = translated.get(expected_id)
            if payload is None:
                raise RuntimeError(f"Transport did not return {expected_id}")
            if payload.get("paragraph_id") != expected_id:
                raise RuntimeError(
                    f"Translation ID mismatch: expected {expected_id}, "
                    f"got {payload.get('paragraph_id')}"
                )
            if "translation_source" in paragraph:
                payload, errors = restore_payload(payload, paragraph)
                issues = contract_issues(errors) or academic_contract_errors(payload, paragraph)
                if issues:
                    raise RuntimeError(f"Translation contract failed for {expected_id}: "
                                       + json.dumps(issues, ensure_ascii=False))
            if single_paragraph:
                result = repair_utf8_surrogates(str(payload["translation"])).strip()
            else:
                result = replace_citation_markers(
                    str(payload["translation"]),
                    list(payload["citations"]),
                    list(payload["unresolved_citations"]),
                )
                result = normalize_translation(strip_footnote_markers(result))
            results.append(result)
            reporter.update(
                90 + round(7 * position / len(paragraphs)),
                f"{mode_label}：正在整理第 {position}/{len(paragraphs)} 段…",
            )
        return results

    @staticmethod
    def _format_output(
        results: list[str],
        *,
        preserve_paragraphs: bool,
        mode_label: str,
        reporter: ProgressReporter,
    ) -> str:
        if len(results) == 1:
            output = results[0]
        elif preserve_paragraphs:
            reporter.update(99, f"{mode_label}：正在保留原始段落结构…")
            output = format_preserved_paragraphs(results)
        else:
            reporter.update(99, f"{mode_label}：正在按句子格式化 Markdown 列表…")
            output = format_markdown_sentence_list(results)
        reporter.update(100, f"{mode_label}：翻译完成，正在打开结果…")
        return output

    def translate(
        self,
        source_text: str,
        status: ProgressReporter | None = None,
        *,
        preserve_paragraphs: bool = False,
        gesture_file: Path | None = None,
    ) -> str:
        reporter = status or StatusReporter(None)
        reporter.update(5, "正在解析选中的文本…")
        sources = split_paragraphs(source_text)
        if not sources:
            raise ValueError("未收到可翻译的选中文本。")

        single_paragraph = len(sources) == 1
        paragraphs = self._build_paragraphs(sources)
        phrase_mode = single_paragraph and is_english_phrase(
            paragraphs[0]["source"], self.settings.phrase_word_limit
        )
        if not phrase_mode:
            paragraphs = [prepare_paragraph(paragraph) for paragraph in paragraphs]
        mode_label = self._mode_label(
            single_paragraph=single_paragraph,
            phrase_mode=phrase_mode,
            preserve_paragraphs=preserve_paragraphs,
        )
        reporter.update(
            8,
            f"{mode_label}：已识别 {len(paragraphs)} 个段落…",
            mode=mode_label,
        )

        document_context: dict[str, Any] | None = None
        if single_paragraph:
            preparation = "正在准备极速请求…" if phrase_mode else "正在准备一次直出请求…"
            reporter.update(20, f"{mode_label}：{preparation}")
            reporter.update(35, f"{mode_label}：正在翻译…")
        else:
            reporter.update(15, f"{mode_label}：正在建立全文语境和统一术语…")
            document_context = self._document_context(paragraphs)
            reporter.update(
                35,
                f"{mode_label}：正在并行翻译 {len(paragraphs)} 个段落…",
            )
        requests = self._translation_requests(
            paragraphs,
            single_paragraph=single_paragraph,
            phrase_mode=phrase_mode,
            document_context=document_context,
        )
        schema = self.single_translation_schema if single_paragraph else self.translation_schema
        stream_runner = getattr(self.gateway, "run_with_progress", None)
        update_translation = getattr(reporter, "update_translation", None)
        stream_paragraphs = preserve_paragraphs or gesture_file is not None
        on_content = None
        if (single_paragraph or stream_paragraphs) and callable(stream_runner) and callable(update_translation):
            previews = {paragraph["paragraph_id"]: ("", False) for paragraph in paragraphs}
            paragraph_by_id = {paragraph["paragraph_id"]: paragraph for paragraph in paragraphs}
            stream_mode_announced = False

            def on_content(custom_id: str, content: str) -> None:
                nonlocal stream_mode_announced
                if custom_id not in paragraph_by_id:
                    return
                if single_paragraph:
                    try:
                        payload = json.loads(content)
                        if isinstance(payload, dict) and "gaps_zh" in payload:
                            payload, errors = render_slot_payload(payload, paragraph_by_id[custom_id], citations=False)
                            text = payload.get("translation", "") if not errors else ""
                        else:
                            text = preview_text(streamed_translation(content), paragraph_by_id[custom_id])
                    except (ValueError, TypeError):
                        text = preview_text(streamed_translation(content), paragraph_by_id[custom_id])
                    update_translation(text)
                    return
                previews[custom_id] = streamed_paragraph_translation(content, paragraph_by_id[custom_id])
                # Network requests overlap gesture recognition. A double press
                # keeps the original progress-only UI and final sentence lists.
                if gesture_file is not None and gesture_file.read_text(encoding="ascii").strip() != "preserve":
                    return
                if not stream_mode_announced:
                    reporter.update(35, "多段落综合翻译（保留原段落）：正在接收译文…",
                                    mode="多段落综合翻译（保留原段落）")
                    stream_mode_announced = True
                ordered = []
                for paragraph in paragraphs:
                    text, complete = previews[paragraph["paragraph_id"]]
                    if text:
                        ordered.append(text)
                    if not complete:
                        break
                update_translation("\n\n".join(ordered))

            rows = stream_runner(requests, schema, on_content)
        else:
            rows = self.gateway.run(requests, schema)
        if not phrase_mode:
            rows = self._retry_slots(rows, paragraphs, single_paragraph=single_paragraph,
                                     document_context=document_context, on_content=on_content)
        translated = self._payload_by_id(rows)
        if single_paragraph:
            reporter.update(90, f"{mode_label}：正在读取结果…")
        else:
            reporter.update(90, f"{mode_label}：正在标准化并回填引用…")
        results = self._translated_paragraphs(
            paragraphs,
            translated,
            single_paragraph=single_paragraph,
            mode_label=mode_label,
            reporter=reporter,
        )
        if gesture_file is not None:
            # The gesture affects only final formatting, so network requests can
            # run while the double-press window is still open.
            deadline = time.monotonic() + 5
            while True:
                gesture = gesture_file.read_text(encoding="ascii").strip()
                if gesture in {"normal", "preserve"}:
                    preserve_paragraphs = gesture == "preserve"
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("快捷键格式识别未完成。")
                time.sleep(0.01)
            mode_label = self._mode_label(
                single_paragraph=single_paragraph,
                phrase_mode=phrase_mode,
                preserve_paragraphs=preserve_paragraphs,
            )
            reporter.update(98, f"{mode_label}：正在整理排版…", mode=mode_label)
        return self._format_output(
            results,
            preserve_paragraphs=preserve_paragraphs,
            mode_label=mode_label,
            reporter=reporter,
        )


def create_translator(config_path: str | Path | None = None) -> ParagraphTranslator:
    """Create the shared translation engine used by the CLI and Python API."""
    settings = Settings.load(config_path)
    templates = TemplateRepository(ROOT / "prompts")
    return ParagraphTranslator(settings, StreamingGateway(settings), templates)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Translate selected English paragraphs to Chinese")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="configuration path (defaults to TRANSLATOR_CONFIG or the bundled config)",
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        default=None,
        help="optional progress-status JSON file",
    )
    parser.add_argument(
        "--stream-file", type=Path,
        help="append and flush single or preserved multi-paragraph translation events",
    )
    parser.add_argument(
        "--gesture-file", type=Path,
        help="resolve shortcut output format after translation",
    )
    parser.add_argument(
        "--preserve-paragraphs",
        action="store_true",
        help="keep multi-paragraph output as paragraphs instead of sentence lists",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        translator = create_translator(args.config)
        # Load the transport/validators while stdin capture runs in parallel.
        from translator_transport import runner as _runner  # noqa: F401 - preload during capture

        source = decode_utf8_input(sys.stdin.buffer.read())
        status = StatusReporter(
            args.status_file.resolve(),
            stream_path=args.stream_file.resolve() if args.stream_file else None,
        ) if args.status_file else None
        print(
            translator.translate(
                source,
                status=status,
                preserve_paragraphs=args.preserve_paragraphs,
                gesture_file=args.gesture_file,
            ),
            end="",
        )
    except Exception as exc:
        print(f"翻译失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
