"""Deterministic protected-abbreviation checks around the translation schema."""

import re

TOKEN = (
    r"(?:[A-Z](?:\.[A-Z])+\.?|[A-Z](?:[/&][A-Z])+s?|"
    r"\d+[A-Z]{2,}\d*s?|DiD|[A-Z]{2,}\d*(?:-(?:[A-Z]+\d*|\d+))*s?)"
)
TOKENS = re.compile(rf"(?<![A-Za-z0-9_]){TOKEN}(?![A-Za-z0-9_])")
KNOWN = {
    "AI",
    "SEC",
    "LLM",
    "ESG",
    "IFRS",
    "GAAP",
    "IPO",
    "CEO",
    "CFO",
    "OLS",
    "IV",
    "ROA",
    "ROE",
}
DEFINITION_PLACEHOLDERS = {
    "中文全称",
    "中文全名",
    "中文名称",
    "中文释义",
    "中文译文",
    "Chinese full name",
}
MARKERS = re.compile(r"⟦translator-abbr-(?:title|abstract)-\d+⟧")
TRANSPORT_EVENTS = re.compile(rf"{MARKERS.pattern}|{TOKENS.pattern}")
URLS = re.compile(r"https?://[^\s<>\"'\u3400-\u9fff，。；：！？]+", re.IGNORECASE)


def source_url_spans(text: str) -> list[tuple[int, int, str]]:
    """Recognize opaque URLs, excluding surrounding sentence/Markdown punctuation."""
    spans = []
    for match in URLS.finditer(text):
        value = match.group().rstrip(".,;!?")
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
            while value.endswith(closing) and value.count(closing) > value.count(
                opening
            ):
                value = value[:-1]
        if value:
            spans.append((match.start(), match.start() + len(value), value))
    return spans


def source_urls(text: str) -> list[str]:
    return [value for _, _, value in source_url_spans(text)]


def protect_plural_abbreviations(values: dict) -> tuple[dict, list[dict]]:
    """Protect plural acronyms and their co-occurring singular forms independently."""
    protected, bindings = dict(values), []
    for field in ("title", "abstract"):
        source = values.get(field, "")
        if MARKERS.search(source):
            raise ValueError(
                "Source text contains reserved translation abbreviation markers"
            )
        if values.get(f"cached_{field}_zh"):
            continue
        urls = source_url_spans(source)
        occurrences = [
            match
            for match in TOKENS.finditer(source)
            if not any(start <= match.start() < end for start, end, _ in urls)
        ]
        plural_bases = {
            match.group().removesuffix("s")
            for match in occurrences
            if match.group().endswith("s")
        }
        fragments, position, index = [], 0, 0
        for match in occurrences:
            if match.group().removesuffix("s") not in plural_bases:
                continue
            index += 1
            marker = f"⟦translator-abbr-{field}-{index:04d}⟧"
            fragments.extend((source[position : match.start()], marker))
            position = match.end()
            bindings.append(
                {
                    "field": f"{field}_zh",
                    "marker": marker,
                    "source_token": match.group(),
                    "source_context": source[
                        max(0, match.start() - 80) : match.end() + 80
                    ],
                }
            )
        fragments.append(source[position:])
        protected[field] = "".join(fragments)
        for binding in bindings:
            if binding["field"] != f"{field}_zh":
                continue
            start = protected[field].index(binding["marker"])
            end = start + len(binding["marker"])
            binding["transport_context"] = protected[field][
                max(0, start - 80) : end + 80
            ]
    return protected, bindings


def repair_plural_markers(
    translated: dict, bindings: list[dict]
) -> tuple[dict, list[dict]]:
    """Repair exact raw plural spellings only in a complete, ordered occurrence sequence."""
    repaired, repairs = dict(translated), []
    for field in ("title_zh", "abstract_zh"):
        expected = [item for item in bindings if item["field"] == field]
        if not expected or len({item["marker"] for item in expected}) != len(expected):
            continue
        output = translated[field]
        source_tokens = {item["source_token"] for item in expected}
        urls = source_url_spans(output)
        events = [
            match
            for match in TRANSPORT_EVENTS.finditer(output)
            if (MARKERS.fullmatch(match.group()) or match.group() in source_tokens)
            and not any(start <= match.start() < end for start, end, _ in urls)
        ]
        if len(events) != len(expected) or any(
            match.group() not in (item["marker"], item["source_token"])
            for match, item in zip(events, expected)
        ):
            continue
        fragments, position = [], 0
        for match, item in zip(events, expected):
            fragments.extend((output[position : match.start()], item["marker"]))
            position = match.end()
            if match.group() == item["source_token"]:
                repairs.append(
                    {
                        "field": field,
                        "marker": item["marker"],
                        "source_token": item["source_token"],
                        "output_start": match.start(),
                        "output_end": match.end(),
                    }
                )
        fragments.append(output[position:])
        repaired[field] = "".join(fragments)
    return repaired, repairs


def repair_duplicate_abbreviation_definition(
    article: dict, translated: dict
) -> tuple[dict, list[dict]]:
    """Remove only an exact duplicated, source-defined first abbreviation expansion."""
    repaired, repairs = dict(translated), []
    source = article.get("abstract", "")
    output = translated.get("abstract_zh", "")
    if (
        not isinstance(source, str)
        or not isinstance(output, str)
        or not source
        or not output
    ):
        return repaired, repairs
    balance = 0
    for character in output:
        if character == "（":
            balance += 1
        elif character == "）":
            balance -= 1
        if balance < 0:
            return repaired, repairs
    if balance:
        return repaired, repairs
    source_tokens = list(dict.fromkeys(TOKENS.findall(source)))
    for token in source_tokens:
        if not re.search(rf"\(\s*[\"'“‘]?{re.escape(token)}[\"'”’]?\s*\)", source):
            continue
        variants = {
            value
            for value in source_tokens
            if value.removesuffix("s") == token.removesuffix("s")
        }
        occurrence = next(
            (match for match in TOKENS.finditer(output) if match.group() in variants),
            None,
        )
        if not occurrence or occurrence.group() != token:
            continue
        inner = re.match(r"（([^（）()\n。！？；;]+)））", output[occurrence.end() :])
        if not inner or not re.search(r"[\u3400-\u9fff]", inner[1]):
            continue
        expansion = inner[1]
        start = occurrence.start() - 1 - len(expansion)
        if start < 0 or output[start : occurrence.start()] != expansion + "（":
            continue
        # A suffix alone cannot prove the preceding full expansion is identical.
        # Keep ambiguous prose prefixes for a fresh translation instead.
        clause_prefix = re.split(r"[。！？；;，,：:\n]", output[:start])[-1].strip()
        if clause_prefix not in {"", "我们将", "本研究将", "本文将", "将", "把"}:
            continue
        end = occurrence.end() + inner.end()
        replacement = token + "（" + expansion + "）"
        original = output[start:end]
        output = output[:start] + replacement + output[end:]
        repairs.append(
            {
                "field": "abstract_zh",
                "token": token,
                "expansion": expansion,
                "before": original,
                "after": replacement,
                "output_start": start,
                "output_end": end,
            }
        )
    repaired["abstract_zh"] = output
    return repaired, repairs


def restore_plural_abbreviations(
    translated: dict, bindings: list[dict]
) -> tuple[dict, list[dict]]:
    """Restore only exact, unique markers in source order in their assigned field."""
    restored, errors = dict(translated), []
    for field in ("title_zh", "abstract_zh"):
        output = translated[field]
        expected = {item["marker"]: item for item in bindings if item["field"] == field}
        observed = MARKERS.findall(output)
        missing = [item for marker, item in expected.items() if marker not in observed]
        repeated = [marker for marker in expected if observed.count(marker) > 1]
        unexpected = sorted(set(observed) - set(expected))
        urls = source_url_spans(output)
        lexical_tokens = {
            match.group()
            for match in TOKENS.finditer(output)
            if not any(start <= match.start() < end for start, end, _ in urls)
        }
        unprotected = sorted(
            lexical_tokens & {item["source_token"] for item in expected.values()}
        )
        expected_order = list(expected)
        out_of_order = (
            not (missing or repeated or unexpected) and observed != expected_order
        )
        if missing or repeated or unexpected or unprotected or out_of_order:
            errors.append(
                {
                    "path": [field],
                    "validator": "protected_abbreviation_marker",
                    "message": (
                        "Copy each supplied abbreviation marker exactly once in its assigned "
                        "field at the corresponding source occurrence. Do not translate, "
                        "singularize, duplicate, reorder, move between fields or invent markers. "
                        "The program restores the original spelling after validation."
                    ),
                    "missing_markers": missing,
                    "repeated_markers": repeated,
                    "unexpected_markers": unexpected,
                    "unprotected_tokens": unprotected,
                    "expected_marker_order": expected_order,
                    "observed_marker_order": observed,
                }
            )
            continue
        for marker, item in expected.items():
            restored[field] = restored[field].replace(marker, item["source_token"])
    return restored, errors


def abbreviation_errors(article: dict, translated: dict) -> list[dict]:
    errors = []
    for field, source_name in (("title_zh", "title"), ("abstract_zh", "abstract")):
        source = article.get(source_name, "")
        output = translated.get(field, "")
        source_occurrence_tokens = TOKENS.findall(source)
        source_tokens = list(dict.fromkeys(source_occurrence_tokens))
        output_occurrence_tokens = TOKENS.findall(output)
        output_tokens = set(output_occurrence_tokens)
        source_links = source_url_spans(source)
        definition_source = source
        for start, end, _ in reversed(source_links):
            definition_source = (
                definition_source[:start]
                + " " * (end - start)
                + definition_source[end:]
            )
        if sorted(source_urls(source)) != sorted(source_urls(output)):
            errors.append(
                {
                    "path": [field],
                    "validator": "url_literals",
                    "message": "Preserve every source URL destination exactly; never translate URL components or insert abbreviation definitions inside URLs.",
                    "expected_urls": source_urls(source),
                    "observed_urls": source_urls(output),
                }
            )
        missing = sorted(set(source_tokens) - output_tokens)
        unexpected = sorted(output_tokens - set(source_tokens))
        if missing or unexpected:
            source_occurrences = [
                {
                    "token": match.group(),
                    "source_context": source[
                        max(0, match.start() - 80) : match.end() + 80
                    ],
                }
                for match in TOKENS.finditer(source)
                if match.group() in missing
            ]
            errors.append(
                {
                    "path": [field],
                    "validator": "protected_abbreviation",
                    "message": (
                        "Copy every missing_tokens spelling literally into the translation of its "
                        "source_context. Preserve the lowercase plural s even in Chinese prose; "
                        "never singularize tokens listed in collapsed_plural_tokens. "
                        "Do not merely repeat the previous "
                        "output or append unrelated tokens. Preserve source abbreviations exactly; "
                        "do not introduce abbreviations from the abstract into the title."
                    ),
                    "missing_tokens": missing,
                    "unexpected_tokens": unexpected,
                    "source_occurrences": source_occurrences,
                    "collapsed_plural_tokens": {
                        token: token.removesuffix("s")
                        for token in missing
                        if token.endswith("s")
                        and token.removesuffix("s") in output_tokens
                    },
                }
            )
        for token in source_tokens:
            expected_count = source_occurrence_tokens.count(token)
            actual_count = output_occurrence_tokens.count(token)
            if expected_count != actual_count:
                errors.append(
                    {
                        "path": [field],
                        "validator": "protected_abbreviation_count",
                        "message": (
                            "Preserve every occurrence of this exact abbreviation spelling. "
                            "Do not omit, duplicate, singularize or append occurrences."
                        ),
                        "token": token,
                        "expected_count": expected_count,
                        "actual_count": actual_count,
                    }
                )
        if field != "abstract_zh":
            continue
        lexical_source_tokens = list(
            dict.fromkeys(
                match.group()
                for match in TOKENS.finditer(source)
                if not any(
                    start <= match.start() < end for start, end, _ in source_links
                )
            )
        )
        output_links = source_url_spans(output)
        lexical_output_matches = [
            match
            for match in TOKENS.finditer(output)
            if not any(start <= match.start() < end for start, end, _ in output_links)
        ]
        checked = set()
        for token in lexical_source_tokens:
            base = token.removesuffix("s")
            if base in checked:
                continue
            checked.add(base)
            variants = [
                value
                for value in lexical_source_tokens
                if value.removesuffix("s") == base
            ]
            explicitly_defined = any(
                re.search(
                    rf"\(\s*[\"'“‘]?{re.escape(value)}[\"'”’]?\s*\)", definition_source
                )
                for value in variants
            )
            if base not in KNOWN and not explicitly_defined:
                continue  # An ambiguous undefined acronym must not be guessed.
            match = next(
                (item for item in lexical_output_matches if item.group() in variants),
                None,
            )
            if match and (
                not re.match(
                    r"\s*（[^（）\n]*[\u3400-\u9fff][^（）\n]*）", output[match.end() :]
                )
                or re.search(r"[（(]\s*[\"'“‘]?$", output[: match.start()])
            ):
                errors.append(
                    {
                        "path": [field],
                        "validator": "abbreviation_first_definition",
                        "message": (
                            f"First occurrence of {base} must use the form "
                            "ABBREVIATION（中文全称）; replace the entire original full-name "
                            "plus parenthesized (possibly quoted) abbreviation with this form. "
                            "Avoid a reversed or repeated definition; do not insert a definition "
                            "inside the original parentheses or repeat the Chinese full name "
                            "before the abbreviation."
                        ),
                        "token": base,
                        "required_form": f"{match.group()}（中文全称）",
                        "forbidden_form": f"中文全称（{match.group()}）",
                        "forbidden_nested_form": f"中文全称（“{match.group()}（中文全称）”）",
                        "observed_context": output[
                            max(0, match.start() - 35) : match.end() + 50
                        ],
                    }
                )
            if match:
                definition = re.match(
                    r"\s*（([^（）\n]*[\u3400-\u9fff][^（）\n]*)）",
                    output[match.end() :],
                )
                if definition:
                    if definition[1].strip() in DEFINITION_PLACEHOLDERS:
                        errors.append(
                            {
                                "path": [field],
                                "validator": "abbreviation_placeholder_definition",
                                "message": "Replace the instructional definition placeholder with the actual source-supported Chinese full name. Do not copy prompt examples or template braces.",
                                "token": token,
                                "required_form": f"{match.group()}（原文支持的实际中文全称）",
                            }
                        )
                    if (
                        re.search(r"\{\s*$", output[: match.start()])
                        and re.match(r"\s*\}", output[match.end() + definition.end() :])
                        and "{" not in source
                    ):
                        errors.append(
                            {
                                "path": [field],
                                "validator": "abbreviation_template_artifact",
                                "message": "Return ordinary Chinese prose without template braces around the abbreviation definition.",
                                "token": token,
                            }
                        )
                    for later in lexical_output_matches:
                        if (
                            later.start() > match.start()
                            and later.group() in variants
                            and re.match(
                                rf"\s*（{re.escape(definition[1])}）",
                                output[later.end() :],
                            )
                        ):
                            errors.append(
                                {
                                    "path": [field],
                                    "validator": "abbreviation_repeated_definition",
                                    "message": "Keep this Chinese expansion only at the first occurrence; later occurrences use the exact abbreviation alone.",
                                    "token": later.group(),
                                    "expansion": definition[1],
                                    "observed_context": output[
                                        max(0, later.start() - 35) : later.end() + 50
                                    ],
                                }
                            )
    return errors
