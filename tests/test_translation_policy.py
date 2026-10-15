from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from paragraph_translator import (
    ROOT,
    ParagraphTranslator,
    Settings,
    TemplateRepository,
    StreamingGateway,
    academic_contract_errors,
    extract_inline_markdown_links,
    extract_quoted_passages,
    quoted_prose_contract_errors,
    streamed_paragraph_translation,
)
from translation_policy import (
    prepare_paragraph,
    preview_text,
    render_slot_payload,
    restore_payload,
    slot_material,
)


def paragraph(source, pid="P0001"):
    return prepare_paragraph(
        ParagraphTranslator._build_paragraphs([source])[0] | {"paragraph_id": pid}
    )


def translator(gateway=None):
    return ParagraphTranslator(
        Settings.load(ROOT / "translator_config.json"),
        gateway,
        TemplateRepository(ROOT / "prompts"),
    )


class PolicyTests(unittest.TestCase):
    def test_instructional_definition_placeholders_and_template_braces_fail(self):
        current = paragraph("We examine multinational corporations (MNCs).")
        for text in ("我们考察{MNCs（中文全称）}。", "我们考察{MNCs（跨国公司）}。"):
            errors = academic_contract_errors({"translation": text}, current)
            self.assertTrue(errors)
            self.assertTrue(
                any(error["validator"].startswith("abbreviation_") for error in errors)
            )
        self.assertFalse(
            academic_contract_errors(
                {"translation": "我们考察MNCs（跨国公司）。"}, current
            )
        )

    def test_protected_prose_occurrences_ignore_same_acronym_in_url(self):
        current = paragraph(
            "Key audit matters (KAMs) inform KAMs users. See [data](https://example.com/KAMs)."
        )
        a, b = [item["marker"] for item in current["protected_abbreviations"]]
        text = f"{a}（关键审计事项）向{b}用户提供信息。参见[数据](https://example.com/KAMs)。"
        restored, errors = restore_payload({"translation": text}, current)
        self.assertFalse(errors)
        self.assertFalse(academic_contract_errors(restored, current))
        self.assertFalse(restore_payload(restored, current)[1])

    def test_incomplete_markdown_url_does_not_backtrack_exponentially(self):
        import signal

        def timeout(signum, frame):
            raise TimeoutError("Markdown parser exceeded one second")

        previous = signal.signal(signal.SIGALRM, timeout)
        signal.setitimer(signal.ITIMER_REAL, 1)
        try:
            broken = "[数据](https://example.com/" + "x" * 2000
            self.assertEqual(extract_inline_markdown_links(broken), [])
            self.assertEqual(len(extract_inline_markdown_links(broken + ")")), 1)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def test_negated_finding_does_not_become_a_positive_inability_claim(self):
        current = paragraph(
            "The policy is challenging firms to adapt; we do not find that they cannot adapt."
        )
        self.assertFalse(
            academic_contract_errors(
                {"translation": "政策对企业适应提出挑战；我们并未发现企业无法适应。"},
                current,
            )
        )
        self.assertTrue(
            academic_contract_errors(
                {"translation": "政策对企业适应提出挑战；企业无法适应。"}, current
            )
        )

    def test_mixed_singular_plural_is_independently_protected_and_restored(self):
        current = paragraph("An MNC competes with MNCs. Another MNC adapts.")
        bindings = current["protected_abbreviations"]
        self.assertEqual(
            [item["source_token"] for item in bindings], ["MNC", "MNCs", "MNC"]
        )
        markers = [item["marker"] for item in bindings]
        payload = {
            "translation": f"一家{markers[0]}（跨国公司）与{markers[1]}竞争。另一家{markers[2]}调整。"
        }
        restored, errors = restore_payload(payload, current)
        self.assertEqual(errors, [])
        self.assertEqual(
            restored["translation"], "一家MNC（跨国公司）与MNCs竞争。另一家MNC调整。"
        )
        self.assertFalse(academic_contract_errors(restored, current))

    def test_missing_duplicate_foreign_reordered_and_singularized_tokens_fail(self):
        current = paragraph("MNCs and an MNC compete.")
        a, b = [item["marker"] for item in current["protected_abbreviations"]]
        for value in (
            a,
            a + a + b,
            b + a,
            a + b + "⟦translator-abbr-abstract-9999⟧",
            "MNC和MNC",
        ):
            with self.subTest(value=value):
                self.assertTrue(restore_payload({"translation": value}, current)[1])

    def test_complete_exact_raw_occurrences_can_be_repaired(self):
        current = paragraph("MNCs and an MNC compete.")
        result, errors = restore_payload(
            {"translation": "MNCs与一家MNC竞争。"}, current
        )
        self.assertFalse(errors)
        self.assertEqual(result["translation"], "MNCs与一家MNC竞争。")

    def test_url_acronyms_are_opaque_and_do_not_require_definitions(self):
        current = paragraph("See [data](https://example.com/US/KAMs?q=2020).")
        self.assertEqual(current["protected_abbreviations"], [])
        payload = {"translation": "参见[数据](https://example.com/US/KAMs?q=2020)。"}
        self.assertFalse(academic_contract_errors(payload, current))
        self.assertTrue(
            academic_contract_errors(
                {"translation": "参见[数据](https://example.com/US/KAM?q=2020)。"},
                current,
            )
        )

    def test_first_definition_and_repeated_definitions_are_validated(self):
        current = paragraph("Key audit matters (KAMs) inform KAMs users.")
        for value in (
            "关键审计事项（KAMs）向KAMs用户提供信息。",
            "KAMs（关键审计事项）向KAMs（关键审计事项）用户提供信息。",
        ):
            self.assertTrue(academic_contract_errors({"translation": value}, current))
        self.assertFalse(
            academic_contract_errors(
                {"translation": "KAMs（关键审计事项）向KAMs用户提供信息。"}, current
            )
        )

    def test_quoted_acronym_definition_does_not_conflict_with_ordinary_quotes(self):
        source = (
            "We examine key audit matters (“KAMs”). A manager says “quality matters”."
        )
        self.assertEqual(extract_quoted_passages(source), ["“quality matters”"])
        self.assertFalse(
            quoted_prose_contract_errors(
                {"translation": "我们研究KAMs（关键审计事项）。经理说“质量很重要”。"},
                source,
            )
        )
        self.assertTrue(
            quoted_prose_contract_errors(
                {"translation": "我们研究KAMs（关键审计事项）。经理说质量很重要。"},
                source,
            )
        )

    def test_year_claim_strength_and_reaction_roles_are_checked(self):
        cases = [
            (
                "The policy changed in 2021.",
                "政策发生了变化。",
                "source_year_preservation",
            ),
            (
                "The policy is challenging firms to adapt.",
                "政策使企业无法适应。",
                "adaptation_claim_strength",
            ),
            (
                "Market reactions to ESG among firms",
                "ESG（环境、社会和治理）对企业的市场反应",
                "title_reaction_roles",
            ),
        ]
        for source, output, code in cases:
            self.assertIn(
                code,
                [
                    error["validator"]
                    for error in academic_contract_errors(
                        {"translation": output}, paragraph(source)
                    )
                ],
            )
        self.assertFalse(
            academic_contract_errors(
                {"translation": "政策在二〇二一年发生了变化。"}, paragraph(cases[0][0])
            )
        )
        self.assertFalse(
            academic_contract_errors(
                {"translation": "政策对企业适应提出挑战。"}, paragraph(cases[1][0])
            )
        )

    def test_preview_never_exposes_partial_marker(self):
        current = paragraph("MNCs compete.")
        marker = current["protected_abbreviations"][0]["marker"]
        for index in range(1, len(marker)):
            self.assertEqual(preview_text("研究" + marker[:index], current), "研究")
        self.assertEqual(preview_text("研究" + marker + "。", current), "研究MNCs。")

    def test_slot_codec_owns_occurrences_urls_and_literal_spacing(self):
        current = paragraph("We use US GAAP. See https://example.com/KAMs.")
        material, _ = slot_material(current)
        self.assertEqual(material["immutable_gaps_zh"]["gap_0001"], " ")
        payload = {
            "paragraph_id": "P0001",
            "gaps_zh": {"gap_0000": "我们使用", "gap_0002": "。参见", "gap_0003": "。"},
            "definitions_zh": {"GAAP": "公认会计原则"},
        }
        output, errors = render_slot_payload(payload, current, citations=False)
        self.assertFalse(errors)
        self.assertEqual(
            output["translation"],
            "我们使用US GAAP（公认会计原则）。参见https://example.com/KAMs。",
        )
        self.assertFalse(academic_contract_errors(output, current))

    def test_slot_schema_rejects_shifted_keys_raw_tokens_and_invented_expansions(self):
        current = paragraph("We use ZZQ.")
        valid = {
            "paragraph_id": "P0001",
            "gaps_zh": {"gap_0000": "我们使用", "gap_0001": "。"},
            "definitions_zh": {},
        }
        for changes in (
            {"paragraph_id": "P0002"},
            {"gaps_zh": {"gap_0001": "我们使用", "gap_0002": "。"}},
            {"gaps_zh": {"gap_0000": "我们使用ZZQ", "gap_0001": "。"}},
            {"definitions_zh": {"ZZQ": "猜测全称"}},
        ):
            self.assertTrue(
                render_slot_payload(valid | changes, current, citations=False)[1]
            )
        self.assertFalse(render_slot_payload(valid, current, citations=False)[1])

    def test_complete_slot_stream_can_render_citations(self):
        current = paragraph("ESG attracts investors (Smith, 2020).")
        payload = {
            "paragraph_id": "P0001",
            "gaps_zh": {"gap_0001": "吸引投资者[[CITATION:C001]]。"},
            "definitions_zh": {"ESG": "环境、社会和治理"},
            "citations": [
                {
                    "citation_id": "C001",
                    "source_text": "(Smith, 2020)",
                    "citation_style": "parenthetical",
                    "works": [
                        {
                            "authors": ["Smith"],
                            "author_form": "single",
                            "year": 2020,
                            "year_suffix": "",
                        }
                    ],
                }
            ],
            "unresolved_citations": [],
        }
        preview, complete = streamed_paragraph_translation(json.dumps(payload), current)
        self.assertTrue(complete)
        self.assertEqual(preview, "ESG（环境、社会和治理）吸引投资者（Smith，2020）。")

    def test_phrase_only_receives_relevant_glossary_and_no_transport(self):
        engine = translator()
        requests = engine._translation_requests(
            ParagraphTranslator._build_paragraphs(["earnings management"]),
            single_paragraph=True,
            phrase_mode=True,
            document_context=None,
        )
        system = json.loads(requests[0]["body"]["messages"][0]["content"])
        self.assertEqual(
            [
                entry["source"]
                for entry in system["academic_policy"]["terminology_policy"]["glossary"]
            ],
            ["earnings"],
        )
        self.assertLess(len(requests[0]["body"]["messages"][0]["content"]), 2200)
        self.assertEqual(requests[0]["body"]["max_tokens"], 128)

    def test_custom_gateway_cannot_return_invalid_final_prose(self):
        class InvalidGateway:
            def run(self, requests, schema):
                return [
                    {
                        "custom_id": "P0001",
                        "response": json.dumps(
                            {"paragraph_id": "P0001", "translation": "政策改变。"}
                        ),
                    }
                ]

        with self.assertRaisesRegex(RuntimeError, "source_year_preservation"):
            translator(InvalidGateway()).translate("The policy changed in 2021.")

    def test_real_gateway_validator_restores_and_rejects_bad_transport(self):
        gateway = object.__new__(StreamingGateway)
        gateway._config = {}
        current = paragraph("Key audit matters (KAMs) inform users in 2020.")
        engine = translator(gateway)
        requests = engine._translation_requests(
            [current], single_paragraph=True, phrase_mode=False, document_context=None
        )
        marker = current["protected_abbreviations"][0]["marker"]
        good = {
            "paragraph_id": "P0001",
            "translation": marker + "（关键审计事项）在2020年向用户提供信息。",
        }

        def runner(requests, config, validator):
            self.assertFalse(
                validator.validate(
                    json.dumps(good | {"translation": "KAM向用户提供信息。"}),
                    requests[0],
                ).ok
            )
            self.assertTrue(validator.validate(json.dumps(good), requests[0]).ok)
            return [{"custom_id": "P0001", "response": json.dumps(good)}]

        with patch("translator_transport.runner.run_stream", side_effect=runner):
            rows = gateway.run(requests, engine.single_translation_schema)
        self.assertEqual(
            json.loads(rows[0]["response"])["translation"],
            "KAMs（关键审计事项）在2020年向用户提供信息。",
        )

    def test_slot_fallback_retries_only_failed_paragraph(self):
        gateway = object.__new__(StreamingGateway)
        gateway._config = {}
        calls = []

        def runner(requests, config, validator, **kwargs):
            calls.append([request["custom_id"] for request in requests])
            if calls[-1] == ["CONTEXT"]:
                return [
                    {
                        "custom_id": "CONTEXT",
                        "response": json.dumps(
                            {
                                "document_summary": "学术研究",
                                "style_guidance": "学术中文",
                                "glossary": [],
                            }
                        ),
                    }
                ]
            if len(calls) == 2:
                invalid = {
                    "paragraph_id": "P0001",
                    "translation": "KAM告诉用户。",
                    "citations": [],
                    "unresolved_citations": [],
                }
                result = validator.validate(json.dumps(invalid), requests[0])
                self.assertFalse(result.ok)
                accepted = {
                    "paragraph_id": "P0002",
                    "translation": "投资者进行学习。",
                    "citations": [],
                    "unresolved_citations": [],
                }
                self.assertTrue(
                    validator.validate(json.dumps(accepted), requests[1]).ok
                )
                return [
                    {
                        "custom_id": "P0001",
                        "error": result.error,
                        "validation_error_details": list(result.errors),
                        "response": json.dumps(invalid),
                    },
                    {"custom_id": "P0002", "response": json.dumps(accepted)},
                ]
            self.assertEqual(calls[-1], ["P0001"])
            request = json.loads(requests[0]["body"]["messages"][1]["content"])
            self.assertTrue(request["validation_retry_errors"])
            self.assertNotIn("previous_invalid_output", request)
            candidate = {
                "paragraph_id": "P0001",
                "gaps_zh": {
                    "gap_0000": "",
                    "gap_0001": "向",
                    "gap_0002": "用户提供信息。",
                },
                "definitions_zh": {"KAMs": "关键审计事项"},
                "citations": [],
                "unresolved_citations": [],
            }
            self.assertTrue(validator.validate(json.dumps(candidate), requests[0]).ok)
            return [{"custom_id": "P0001", "response": json.dumps(candidate)}]

        with patch("translator_transport.runner.run_stream", side_effect=runner):
            output = translator(gateway).translate(
                "Key audit matters (KAMs) inform KAMs users.\nInvestors learn.",
                preserve_paragraphs=True,
            )
        self.assertEqual(
            output, "KAMs（关键审计事项）向KAMs用户提供信息。\n\n投资者进行学习。"
        )
        self.assertEqual(calls, [["CONTEXT"], ["P0001", "P0002"], ["P0001"]])


if __name__ == "__main__":
    unittest.main()
