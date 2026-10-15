"""Narrow, source-derived guards for reaction roles and adaptation claims."""

import re

from .translation_contract import TOKENS

TITLE_REACTIONS = re.compile(
    r"(?:^|[:：]\s*)market\s+reactions?\s+to\s+"
    r"(?P<event>[^\n:：;!?]{1,80}?)\s+among\s+(?P<group>[^\n:：;!?]{1,300})$",
    re.IGNORECASE,
)
EXACT_EVENT_ABBREVIATION = re.compile(r"[A-Z][A-Z0-9]{1,30}")
ADAPTATION_CHALLENGE = re.compile(
    r"\bchallenging\s+(?P<group>[^.!?;\n]{1,120}?)\s+to\s+adapt\b"
    r"(?P<context>[^.!?;\n]*)",
    re.IGNORECASE,
)
ADAPTATION_DIFFICULTY = re.compile(
    r"\b(?:unable\s+to\s+adapt|incapable\s+of\s+adapting|"
    r"(?:cannot|can\s+not|can't|could\s+not|couldn't|not\s+able\s+to)\s+adapt|"
    r"(?:did|does|do)\s+not\s+adapt|"
    r"(?:fail(?:ed|s)?\s+to|failure\s+to)\s+adapt|"
    r"struggl(?:e[ds]?|ing)\s+(?:to\s+adapt|with\s+adapting)|"
    r"(?:difficulty|difficulties|trouble)\s+(?:in\s+)?adapting|"
    r"(?:difficult|hard|impossible)\s+(?:for\s+[^.!?;\n]{1,80}?\s+)?to\s+adapt|"
    r"(?:adaptation|adapting)\s+(?:is|was|remains?|became|can\s+be|may\s+be)\s+"
    r"(?:very\s+)?(?:difficult|hard|impossible))\b",
    re.IGNORECASE,
)
NEGATED_SOURCE_DIFFICULTY = re.compile(
    r"\b(?:not|never|no|without)\s+(?:[A-Za-z]+\s+){0,3}$", re.IGNORECASE
)
UNSUPPORTED_ADAPTATION = re.compile(r"(?:无法|不能)\s*适应|使其\s*难以\s*适应")
NEGATED_TARGET_DIFFICULTY = re.compile(
    r"(?:(?:并非|不是|并不是|并不意味着|并不表示|未必|不一定)"
    r"[^，。；：！？\n]{0,30}|(?:并未发现|未发现|没有发现|未能发现|没有证据表明|无证据表明)"
    r"[^，。；：！？\n]{0,30}|(?:没有|并未|不会|不至于|未)\s*)$"
)
CALENDAR_YEAR = re.compile(r"(?<![A-Za-z0-9_.])(?:18|19|20)\d{2}(?![A-Za-z0-9_]|\.\d)")
CHINESE_YEAR_DIGITS = str.maketrans("0123456789", "〇一二三四五六七八九")
CHINESE_NUMBER_BOUNDARY = r"〇零一二三四五六七八九十百千万亿\d"


def _calendar_years(text: str) -> list[str]:
    acronym_spans = [match.span() for match in TOKENS.finditer(text)]
    return list(
        dict.fromkeys(
            match.group()
            for match in CALENDAR_YEAR.finditer(text)
            if not any(
                start <= match.start() and match.end() <= end
                for start, end in acronym_spans
            )
        )
    )


def _has_chinese_year(text: str, year: str) -> bool:
    spelling = year.translate(CHINESE_YEAR_DIGITS)
    spellings = {spelling, spelling.replace("〇", "零")}
    return any(
        re.search(
            rf"(?<![{CHINESE_NUMBER_BOUNDARY}]){value}(?![{CHINESE_NUMBER_BOUNDARY}])",
            text,
        )
        for value in spellings
    )


def _supported_adaptation_difficulty(abstract: str) -> list[str]:
    support = []
    for match in ADAPTATION_DIFFICULTY.finditer(abstract):
        preceding = abstract[max(0, match.start() - 50) : match.start()]
        if not NEGATED_SOURCE_DIFFICULTY.search(preceding):
            support.append(match.group())
    return support


def translation_semantic_hints(article: dict) -> dict[str, list[dict]]:
    """Derive positive constraints from raw or marker-protected source fields."""
    hints = {"title_zh": [], "abstract_zh": []}
    title = article.get("title", "") or ""
    abstract = article.get("abstract", "") or ""
    reaction = TITLE_REACTIONS.search(title.strip())
    if reaction:
        event, group = reaction.group("event").strip(), reaction.group("group").strip()
        hints["title_zh"].append(
            {
                "source_pattern": "market reactions to EVENT among GROUP",
                "reaction_target": event,
                "reacting_group": group,
                "required_relationship": f"GROUP对{event}的市场反应",
                "instruction": (
                    "GROUP is the group in which the reaction is observed; EVENT is the "
                    "target of that reaction. Preserve GROUP's source entity type and wording "
                    "and retain its markers, then express its market reaction to EVENT. "
                    "An event-first construction may instead say EVENT引发的GROUP市场反应."
                ),
            }
        )
    difficulty_support = _supported_adaptation_difficulty(abstract)
    for challenge in ADAPTATION_CHALLENGE.finditer(abstract):
        preceding = abstract[max(0, challenge.start() - 50) : challenge.start()]
        if NEGATED_SOURCE_DIFFICULTY.search(preceding):
            continue
        hints["abstract_zh"].append(
            {
                "source_pattern": "challenging GROUP to adapt",
                "adapting_group": challenge.group("group").strip(),
                "adaptation_context": challenge.group("context").strip(),
                "required_claim": "对GROUP适应原文所述要求提出挑战",
                "instruction": (
                    "Express the adaptation challenge, preserving source uncertainty and "
                    "context. Only an explicit source-supported adaptation difficulty or "
                    "inability clause licenses that additional assertion."
                ),
                "explicit_difficulty_support": difficulty_support,
            }
        )
    for field, source in (("title_zh", title), ("abstract_zh", abstract)):
        years = _calendar_years(source)
        if years:
            hints[field].append(
                {
                    "source_pattern": "standalone calendar years",
                    "expected_years": years,
                    "instruction": (
                        "Keep every expected calendar year explicitly in this same output field, "
                        "attached to its original event or context. Preserve Arabic year digits "
                        "or use their Chinese digit-by-digit year spelling."
                    ),
                }
            )
    return hints


def translation_semantic_errors(article: dict, translated: dict) -> list[dict]:
    """Reject explicit reversals or strengthened claims only for recognized sources."""
    hints = translation_semantic_hints(article)
    errors = []
    title = translated.get("title_zh", "") or ""
    abstract = translated.get("abstract_zh", "") or ""
    for hint in hints["title_zh"]:
        if "reaction_target" not in hint:
            continue
        event = hint["reaction_target"]
        if not EXACT_EVENT_ABBREVIATION.fullmatch(event):
            continue  # A translated event name cannot be aligned by exact spelling.
        reversal = re.search(rf"(?<![A-Za-z0-9_]){re.escape(event)}\s*对(?!于)", title)
        if reversal:
            following = re.split(
                r"[，。；：！？]", title[reversal.end() :], maxsplit=1
            )[0]
            if "来说" in following or "而言" in following:
                continue  # EVENT对GROUP来说... can still make EVENT the stimulus.
            errors.append(
                {
                    "path": ["title_zh"],
                    "validator": "title_reaction_roles",
                    "message": "Preserve the source reaction target and affected-group roles.",
                    "reaction_target": event,
                    "required_relationship": hint["required_relationship"],
                }
            )
    challenge_hints = [
        hint for hint in hints["abstract_zh"] if "explicit_difficulty_support" in hint
    ]
    if challenge_hints and not challenge_hints[0]["explicit_difficulty_support"]:
        for match in UNSUPPORTED_ADAPTATION.finditer(abstract):
            preceding = abstract[max(0, match.start() - 50) : match.start()]
            if NEGATED_TARGET_DIFFICULTY.search(preceding):
                continue
            errors.append(
                {
                    "path": ["abstract_zh"],
                    "validator": "adaptation_claim_strength",
                    "message": "Translate the adaptation challenge at the source's stated strength.",
                    "required_claim": challenge_hints[0]["required_claim"],
                    "source_pattern": "challenging GROUP to adapt",
                }
            )
            break
    for field, source, output in (
        ("title_zh", article.get("title", "") or "", title),
        ("abstract_zh", article.get("abstract", "") or "", abstract),
    ):
        expected = _calendar_years(source)
        visible_years = set(_calendar_years(output))
        missing = [
            year
            for year in expected
            if year not in visible_years and not _has_chinese_year(output, year)
        ]
        if missing:
            errors.append(
                {
                    "path": [field],
                    "validator": "source_year_preservation",
                    "message": "Preserve each source calendar year explicitly in the same field.",
                    "expected_years": expected,
                    "missing_years": missing,
                }
            )
    return errors
