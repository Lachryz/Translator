from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from paragraph_translator import (
    ParagraphTranslator,
    Settings,
    TemplateRepository,
    StreamingGateway,
    citation_contract_errors,
    decode_utf8_input,
    extract_inline_markdown_links,
    extract_quoted_passages,
    format_markdown_sentence_list,
    format_preserved_paragraphs,
    format_citation,
    is_english_phrase,
    make_utf8_safe,
    markdown_link_contract_errors,
    normalize_translation,
    normalize_markdown_link_misclassifications,
    normalize_non_citation_unresolved,
    normalize_quoted_prose_misclassifications,
    phrase_max_tokens,
    quoted_prose_contract_errors,
    repair_utf8_surrogates,
    replace_citation_markers,
    single_paragraph_max_tokens,
    split_paragraphs,
    strip_footnote_markers,
)

ROOT = Path(__file__).resolve().parents[1]


class FakeGateway:
    def __init__(self) -> None:
        self.calls = []

    def run(self, requests, schema):
        self.calls.append((requests, schema))
        if requests[0]["custom_id"] == "CONTEXT":
            payload = {
                "document_summary": "测试上下文",
                "style_guidance": "学术中文",
                "glossary": [{"source": "capital market", "target": "资本市场"}],
            }
            return [{"custom_id": "CONTEXT", "response": json.dumps(payload)}]
        rows = []
        for request in reversed(requests):
            payload = {
                "paragraph_id": request["custom_id"],
                "translation": f"译文 {request['custom_id']}\n",
            }
            if schema is not None and schema.get("x-citation-contract"):
                payload.update({"citations": [], "unresolved_citations": []})
            rows.append({
                "custom_id": request["custom_id"],
                "response": json.dumps(payload),
            })
        return rows


class RecordingStatus:
    def __init__(self):
        self.updates = []

    def update(self, percent, message, mode=None):
        self.updates.append({"percent": percent, "message": message, "mode": mode})


class ParagraphRulesTests(unittest.TestCase):
    def test_utf16_surrogate_pair_is_preserved_as_unicode(self):
        repaired = repair_utf8_surrogates("policy \ud83d\ude00 response")
        self.assertEqual(repaired, "policy 😀 response")
        repaired.encode("utf-8")

    def test_isolated_surrogates_are_replaced_before_prompt_rendering(self):
        repaired = repair_utf8_surrogates("before \ud800 middle \udcff after")
        self.assertEqual(repaired, "before � middle � after")
        repaired.encode("utf-8")

    def test_malformed_utf8_bytes_replace_only_invalid_characters(self):
        repaired = decode_utf8_input(b"before \xed\xa0\x80 after")
        self.assertTrue(repaired.startswith("before "))
        self.assertTrue(repaired.endswith(" after"))
        repaired.encode("utf-8")

    def test_nested_prompt_values_are_utf8_safe(self):
        repaired = make_utf8_safe({"items": ["valid", "bad \ud800 value"]})
        self.assertEqual(repaired, {"items": ["valid", "bad � value"]})
        json.dumps(repaired, ensure_ascii=False).encode("utf-8")

    def test_blank_and_nonblank_separators_can_be_mixed(self):
        source = "First paragraph.\nSecond paragraph.\n\n\nThird paragraph."
        self.assertEqual(
            split_paragraphs(source),
            ["First paragraph.", "Second paragraph.", "Third paragraph."],
        )

    def test_single_newlines_are_paragraph_boundaries(self):
        self.assertEqual(split_paragraphs("One.\nTwo.\nThree."), ["One.", "Two.", "Three."])

    def test_translation_is_one_paragraph(self):
        self.assertEqual(normalize_translation("第一行\n 第二行"), "第一行 第二行")

    def test_each_paragraph_becomes_a_compact_unordered_markdown_list(self):
        self.assertEqual(
            format_markdown_sentence_list([
                "第一句。第二句。",
                "第三句。第四句没有句号",
            ]),
            "- 第一句。\n- 第二句。\n\n- 第三句。\n- 第四句没有句号",
        )

    def test_sentence_lists_keep_markdown_links_intact(self):
        link = '[报告。附录](https://example.com/report "报告。附录")'
        self.assertEqual(
            format_markdown_sentence_list([f"阅读{link}。下一句。"]),
            f"- 阅读{link}。\n- 下一句。",
        )

    def test_empty_sentences_do_not_create_empty_list_items(self):
        self.assertEqual(format_markdown_sentence_list(["。。第一句。。"]), "- 第一句。")

    def test_preserved_paragraphs_are_not_split_into_lists(self):
        self.assertEqual(
            format_preserved_paragraphs([
                "第一句。第二句。",
                "第三句。第四句。",
            ]),
            "第一句。第二句。\n\n第三句。第四句。",
        )

    def test_inline_markdown_link_title_and_destination_are_parsed(self):
        self.assertEqual(
            extract_inline_markdown_links(
                "Read [What's Behind Amazon's Review Problem](https://www.wsj.com/articles/review)."
            ),
            [{
                "source_text": "[What's Behind Amazon's Review Problem](https://www.wsj.com/articles/review)",
                "label": "What's Behind Amazon's Review Problem",
                "destination": "https://www.wsj.com/articles/review",
            }],
        )

    def test_inline_markdown_links_accept_relative_and_balanced_destinations(self):
        links = extract_inline_markdown_links(
            "[local](/docs/a) and [math](https://example.com/Function_(math))"
        )
        self.assertEqual(
            [link["destination"] for link in links],
            ["/docs/a", "https://example.com/Function_(math)"],
        )

    def test_markdown_link_is_removed_from_unresolved_when_url_is_preserved(self):
        link = "[What's Behind Amazon's Review Problem](https://www.wsj.com/articles/review)"
        payload = {
            "translation": "请参阅[亚马逊评论问题背后的原因](https://www.wsj.com/articles/review)。",
            "unresolved_citations": [link],
        }
        normalized = normalize_markdown_link_misclassifications(payload)
        self.assertEqual(normalized["unresolved_citations"], [])

    def test_markdown_link_contract_rejects_changed_or_missing_url(self):
        source = "Read [the report](https://example.com/report)."
        valid = {"translation": "阅读[该报告](https://example.com/report)。"}
        invalid = {"translation": "阅读该报告（https://example.com/changed）。"}
        self.assertEqual(markdown_link_contract_errors(valid, source), ())
        self.assertEqual(
            markdown_link_contract_errors(invalid, source)[0]["validator"],
            "markdown_link_contract",
        )

    def test_quoted_statement_is_content_not_an_unresolved_citation(self):
        quote = "“The appropriate enforcement actions were taken.”"
        payload = {
            "translation": "亚马逊发言人表示：“我们已采取适当的执法措施。”",
            "unresolved_citations": [{
                "source_text": quote,
                "reason": "No author-year metadata",
            }],
        }
        normalized = normalize_quoted_prose_misclassifications(payload, quote)
        self.assertEqual(normalized["unresolved_citations"], [])
        self.assertEqual(extract_quoted_passages(quote), [quote])
        self.assertEqual(quoted_prose_contract_errors(normalized, quote), ())

    def test_non_citation_prose_is_removed_from_unresolved_entries(self):
        payload = {
            "translation": "该公司采取了行动。",
            "unresolved_citations": [{
                "source_text": "The company took action.",
                "reason": "No author-year metadata",
            }],
        }
        self.assertEqual(
            normalize_non_citation_unresolved(payload)["unresolved_citations"],
            [],
        )

    def test_genuine_author_year_unresolved_entry_is_retained(self):
        payload = {
            "translation": "已有研究。",
            "unresolved_citations": [{
                "source_text": "Smith, 2021",
                "reason": "Missing work details",
            }],
        }
        self.assertEqual(
            normalize_non_citation_unresolved(payload)["unresolved_citations"],
            payload["unresolved_citations"],
        )

    def test_year_in_ordinary_prose_does_not_make_it_a_citation(self):
        payload = {
            "translation": "亚马逊于2021年采取了行动。",
            "unresolved_citations": [{
                "source_text": "Amazon took action in 2021.",
                "reason": "No author-year metadata",
            }],
        }
        self.assertEqual(
            normalize_non_citation_unresolved(payload)["unresolved_citations"],
            [],
        )

    def test_ordinary_statement_cannot_enter_structured_citations(self):
        payload = {
            "translation": "[[CITATION:C001]]",
            "citations": [{
                "citation_id": "C001",
                "source_text": "The appropriate enforcement actions were taken.",
                "citation_style": "parenthetical",
                "works": [{
                    "authors": ["Amazon"],
                    "author_form": "single",
                    "year": 2021,
                    "year_suffix": "",
                }],
            }],
        }
        errors = citation_contract_errors(payload)
        self.assertTrue(any(
            error["instance_path"] == "/citations/0/source_text"
            for error in errors
        ))


class CitationFormattingTests(unittest.TestCase):
    def test_narrative_author_and_year_rules(self):
        citation = {
            "citation_style": "narrative",
            "works": [
                {"authors": ["Smith"], "year": 1999, "year_suffix": ""},
                {"authors": ["Smith"], "year": 2001, "year_suffix": ""},
                {"authors": ["Jones", "Lee"], "year": 2002, "year_suffix": ""},
                {"authors": ["Brown", "Chen", "Xu"], "year": 2003, "year_suffix": ""},
            ],
        }
        self.assertEqual(
            format_citation(citation),
            "Smith（1999，2001）、Jones和Lee（2002）和Brown等（2003）",
        )

    def test_parenthetical_author_and_separator_rules(self):
        citation = {
            "citation_style": "parenthetical",
            "works": [
                {"authors": ["Smith"], "year": 1999, "year_suffix": ""},
                {"authors": ["Jones", "Lee"], "year": 2002, "year_suffix": ""},
                {"authors": ["Brown", "Chen", "Xu"], "year": 2003, "year_suffix": ""},
            ],
        }
        self.assertEqual(
            format_citation(citation),
            "（Smith，1999；Jones和Lee，2002；Brown等，2003）",
        )

    def test_same_author_same_year_gets_a_b_suffixes(self):
        citation = {
            "citation_style": "narrative",
            "works": [
                {"authors": ["Smith"], "year": 1999, "year_suffix": ""},
                {"authors": ["Smith"], "year": 1999, "year_suffix": ""},
            ],
        }
        self.assertEqual(format_citation(citation), "Smith（1999a，1999b）")

    def test_et_al_uses_explicit_author_form_when_hidden_names_are_unknown(self):
        citation = {
            "citation_style": "parenthetical",
            "works": [{
                "authors": ["Brown"],
                "author_form": "et_al",
                "year": 2003,
                "year_suffix": "",
            }],
        }
        self.assertEqual(format_citation(citation), "（Brown等，2003）")

    def test_inconsistent_author_form_retries_then_falls_back_deterministically(self):
        citation = {
            "citation_id": "C001",
            "source_text": "Cohen et al. (2010)",
            "citation_style": "narrative",
            "works": [{
                "authors": ["Cohen"],
                "author_form": "dual",
                "year": 2010,
                "year_suffix": "",
            }],
        }
        payload = {
            "translation": "[[CITATION:C001]]发现这一结果。",
            "citations": [citation],
        }
        errors = citation_contract_errors(payload)
        self.assertTrue(any("author_form must be et_al" in error["message"] for error in errors))
        self.assertEqual(errors[0]["instance_path"], "/citations/0/works/0/author_form")
        self.assertEqual(errors[0]["expected"], "et_al")
        self.assertEqual(errors[0]["validator"], "citation_contract")
        self.assertEqual(format_citation(citation), "Cohen等（2010）")

    def test_superscript_note_citation_is_removed(self):
        citation = {
            "citation_style": "superscript",
            "note_number": 1,
        }
        self.assertEqual(format_citation(citation), "")
        self.assertEqual(
            replace_citation_markers(
                "相关结论[[CITATION:C001]]。",
                [{"citation_id": "C001", **citation}],
                [],
            ),
            "相关结论。",
        )

    def test_footnote_markers_are_removed_deterministically(self):
        self.assertEqual(strip_footnote_markers("First sentence.¹"), "First sentence.")
        self.assertEqual(strip_footnote_markers("First sentence.[^2]"), "First sentence.")
        self.assertEqual(strip_footnote_markers("First sentence. [3]"), "First sentence.")
        self.assertEqual(strip_footnote_markers("First sentence. 4"), "First sentence.")
        self.assertEqual(strip_footnote_markers("First sentence.5"), "First sentence.")

    def test_footnote_cleanup_preserves_plain_numbers_and_decimal_values(self):
        for source in (
            "The sample contains 100", "Revenue rose to 12", "Model 2",
            "The coefficient is 0.12", "p=.05", "p = .05", "增长至100", "100",
        ):
            with self.subTest(source=source):
                self.assertEqual(strip_footnote_markers(source), source)

    def test_contract_rejects_direct_rendering_plus_marker(self):
        payload = {
            "translation": "Brown等人（2003年）发现这一结果。[[CITATION:C001]]",
            "citations": [{
                "citation_id": "C001",
                "citation_style": "narrative",
                "works": [{
                    "authors": ["Brown"],
                    "author_form": "et_al",
                    "year": 2003,
                    "year_suffix": "",
                }],
            }],
        }
        errors = citation_contract_errors(payload)
        self.assertTrue(any("outside its marker" in error["message"] for error in errors))

    def test_contract_accepts_marker_only_translation(self):
        payload = {
            "translation": "[[CITATION:C001]]发现这一结果。",
            "citations": [{
                "citation_id": "C001",
                "citation_style": "narrative",
                "works": [{
                    "authors": ["Brown"],
                    "author_form": "et_al",
                    "year": 2003,
                    "year_suffix": "",
                }],
            }],
        }
        self.assertEqual(citation_contract_errors(payload), ())

    def test_contract_rejects_brackets_around_marker(self):
        payload = {
            "translation": "已有研究（[[CITATION:C001]]）。",
            "citations": [{
                "citation_id": "C001",
                "citation_style": "parenthetical",
                "works": [{
                    "authors": ["Smith"],
                    "author_form": "single",
                    "year": 1999,
                    "year_suffix": "",
                }],
            }],
        }
        errors = citation_contract_errors(payload)
        self.assertTrue(any("must be bare" in error["message"] for error in errors))

    def test_script_removes_outer_brackets_around_marker(self):
        citation = {
            "citation_id": "C001",
            "citation_style": "parenthetical",
            "works": [{
                "authors": ["Smith"],
                "author_form": "single",
                "year": 1999,
                "year_suffix": "",
            }],
        }
        self.assertEqual(
            replace_citation_markers("已有研究（[[CITATION:C001]]）。", [citation], []),
            "已有研究（Smith，1999）。",
        )

    def test_citation_replacement_preserves_preceding_text(self):
        citation = {
            "citation_id": "C001",
            "source_text": "(Dechow, Sloan, and Sweeney 1996)",
            "citation_style": "parenthetical",
            "works": [{
                "authors": ["Dechow", "Sloan", "Sweeney"],
                "author_form": "et_al",
                "year": 1996,
                "year_suffix": "",
            }],
        }
        payload = {
            "translation": "分析师可能自我审查负面意见[[CITATION:C001]]。",
            "citations": [citation],
        }
        self.assertEqual(citation_contract_errors(payload), ())
        self.assertEqual(
            replace_citation_markers(payload["translation"], [citation], []),
            "分析师可能自我审查负面意见（Dechow等，1996）。",
        )

    def test_script_relocates_marker_to_direct_rendered_citation(self):
        citation = {
            "citation_id": "C001",
            "citation_style": "narrative",
            "works": [{
                "authors": ["Cao"],
                "author_form": "et_al",
                "year": 2015,
                "year_suffix": "",
            }],
        }
        self.assertEqual(
            replace_citation_markers(
                "Cao等人（2015年）发现这一结果。[[CITATION:C001]]",
                [citation],
                [],
            ),
            "Cao等（2015）发现这一结果。",
        )

    def test_markers_are_replaced_and_must_match_metadata(self):
        citation = {
            "citation_id": "C001",
            "citation_style": "parenthetical",
            "works": [{"authors": ["Smith"], "year": 1999, "year_suffix": ""}],
        }
        self.assertEqual(
            replace_citation_markers("已有研究[[CITATION:C001]]。", [citation], []),
            "已有研究（Smith，1999）。",
        )
        with self.assertRaisesRegex(ValueError, "引用标识符不匹配"):
            replace_citation_markers("没有标识符。", [citation], [])

    def test_script_renders_repeated_marker_without_failing(self):
        citation = {
            "citation_id": "C001",
            "citation_style": "parenthetical",
            "works": [{"authors": ["Smith"], "year": 1999, "year_suffix": ""}],
        }
        self.assertEqual(
            replace_citation_markers(
                "已有研究[[CITATION:C001]]，后续研究也采用该结论[[CITATION:C001]]。",
                [citation],
                [],
            ),
            "已有研究（Smith，1999），后续研究也采用该结论（Smith，1999）。",
        )

    def test_unresolved_numeric_footnote_is_ignored(self):
        self.assertEqual(
            replace_citation_markers(
                "见[[CITATION:C001]]。",
                [],
                [{"source_text": "[12]", "reason": "未提供参考文献表"}],
            ),
            "见。",
        )


class PipelineTests(unittest.TestCase):
    def test_duplicate_batch_ids_cannot_silently_replace_a_result(self):
        rows = [
            {"custom_id": "P0001", "response": json.dumps({
                "paragraph_id": "P0001", "translation": value,
            })}
            for value in ("第一份译文", "相互矛盾的重复译文")
        ]
        with self.assertRaisesRegex(RuntimeError, "duplicate request ID: P0001"):
            ParagraphTranslator._payload_by_id(rows)

    def test_project_uses_embedded_deepseek_flash_streaming(self):
        settings = Settings.load(ROOT / "translator_config.json")
        self.assertEqual(settings.model, "deepseek-flash")

        with patch("dotenv.load_dotenv"), patch.dict(
            os.environ,
            {"DEEPSEEK_URL": "https://example.invalid", "DEEPSEEK_KEY": "test-key"},
        ):
            gateway = StreamingGateway(settings)

        self.assertEqual(gateway._config["api"]["id"], "deepseek")
        self.assertEqual(gateway._config["model"]["name"], "deepseek-flash")
        self.assertEqual(gateway._config["task_table"]["mode"], "stream")

    def test_prompt_assigns_citation_signal_boundary_decision_to_llm(self):
        prompt = json.loads(
            TemplateRepository(ROOT / "prompts").render_prompt(
                "translation.system.json.j2"
            )
        )
        policy = prompt["global_citation_policy"]["citation_signal_boundary_policy"]
        self.assertEqual(policy["decision_owner"], "LLM")
        self.assertIn("e.g.", policy["signals"])
        self.assertIn("inside the detected citation-block boundary", policy["boundary_rule"])
        self.assertIn("Do not translate it", policy["output_rule"])

        single_prompt = json.loads(
            TemplateRepository(ROOT / "prompts").render_prompt(
                "translation.single.system.json.j2"
            )
        )
        single_rules = " ".join(single_prompt["rules"])
        self.assertIn("citation-block boundary cue", single_rules)
        self.assertIn("Do not translate it", single_rules)
        self.assertIn("Render author-year citations directly", single_rules)
        self.assertNotIn("[[CITATION:", single_rules)
        self.assertIn("translate only its label", single_rules)

        multi_rules = " ".join(prompt["rules"])
        self.assertIn("translate only its label", multi_rules)
        self.assertIn("never put it in citations or unresolved_citations", multi_rules)

    def test_single_paragraph_output_cap_is_dynamic_and_bounded(self):
        self.assertEqual(single_paragraph_max_tokens("short", 4096), 512)
        self.assertEqual(single_paragraph_max_tokens("x" * 10000, 4096), 4096)

    def test_phrase_mode_is_limited_to_short_unpunctuated_english(self):
        self.assertTrue(is_english_phrase("agency theory", 8))
        self.assertTrue(is_english_phrase("cost-effective", 8))
        self.assertFalse(is_english_phrase("We test this.", 8))
        self.assertFalse(is_english_phrase("one two three four five six seven eight nine", 8))
        self.assertFalse(is_english_phrase("公司治理", 8))
        self.assertEqual(phrase_max_tokens(4096), 128)

    def test_transport_console_warning_never_leaks_into_translation_stdout(self):
        gateway = object.__new__(StreamingGateway)
        gateway._config = {}

        def fake_run_stream(requests, config, validator):
            print("⚠ 1 request(s) still failed after validation retries; retained as empty results.")
            return [{"custom_id": "P0001", "response": "{}"}]

        captured = io.StringIO()
        with patch("translator_transport.runner.run_stream", side_effect=fake_run_stream):
            with contextlib.redirect_stdout(captured):
                rows = gateway.run(
                    [],
                    {"type": "object", "x-citation-contract": True},
                )

        self.assertEqual(captured.getvalue(), "")
        self.assertEqual(rows[0]["custom_id"], "P0001")

    def test_gateway_enables_structured_validation_for_single_paragraph_request(self):
        gateway = object.__new__(StreamingGateway)
        gateway._config = {}

        def fake_run_stream(requests, config, validator):
            invalid = validator.validate("not json", {"custom_id": "P0001"})
            self.assertFalse(invalid.ok)
            self.assertEqual(invalid.errors[0]["code"], "invalid_json")

            wrong_id = validator.validate(
                '{"paragraph_id":"P0002","translation":"直出译文"}',
                {"custom_id": "P0001"},
            )
            self.assertFalse(wrong_id.ok)
            self.assertEqual(wrong_id.errors[0]["code"], "paragraph_id_mismatch")

            valid = validator.validate(
                '{"paragraph_id":"P0001","translation":"直出译文"}',
                {"custom_id": "P0001"},
            )
            self.assertTrue(valid.ok)
            return [{
                "custom_id": "P0001",
                "response": '{"paragraph_id":"P0001","translation":"直出译文"}',
            }]

        with patch("translator_transport.runner.run_stream", side_effect=fake_run_stream):
            schema = json.loads(
                (ROOT / "schemas/translation.single.schema.json").read_text(encoding="utf-8")
            )
            rows = gateway.run([], schema)

        self.assertEqual(rows[0]["custom_id"], "P0001")

    def test_gateway_accepts_preserved_markdown_link_misreported_as_unresolved(self):
        gateway = object.__new__(StreamingGateway)
        gateway._config = {}
        source_link = (
            "[What's Behind Amazon's Review Problem]"
            "(https://www.wsj.com/articles/whats-behind-amazons-review-problem-2021-08-13)"
        )
        translated_link = (
            "[亚马逊评论问题背后的原因]"
            "(https://www.wsj.com/articles/whats-behind-amazons-review-problem-2021-08-13)"
        )
        raw_payload = {
            "paragraph_id": "P0001",
            "translation": translated_link,
            "citations": [],
            "unresolved_citations": [source_link],
        }
        request = {
            "custom_id": "P0001",
            "body": {"messages": [{
                "role": "user",
                "content": json.dumps({
                    "current_paragraph": {"source": f"Read {source_link}."},
                }),
            }]},
        }

        def fake_run_stream(requests, config, validator):
            result = validator.validate(json.dumps(raw_payload), request)
            self.assertTrue(result.ok)
            return [{
                "custom_id": "P0001",
                "response": json.dumps(raw_payload),
            }]

        with patch("translator_transport.runner.run_stream", side_effect=fake_run_stream):
            schema = json.loads(
                (ROOT / "schemas/translation.schema.json").read_text(encoding="utf-8")
            )
            rows = gateway.run([request], schema)

        payload = ParagraphTranslator._payload_by_id(rows)["P0001"]
        self.assertEqual(payload["translation"], translated_link)
        self.assertEqual(payload["unresolved_citations"], [])

    def test_gateway_accepts_quoted_statement_misreported_as_unresolved(self):
        gateway = object.__new__(StreamingGateway)
        gateway._config = {}
        source_quote = "“The appropriate enforcement actions were taken.”"
        raw_payload = {
            "paragraph_id": "P0001",
            "translation": "亚马逊发言人表示：“我们已采取适当的执法措施。”",
            "citations": [],
            "unresolved_citations": [{
                "source_text": source_quote,
                "reason": "该引语无法解析出作者与年份信息",
            }],
        }
        request = {
            "custom_id": "P0001",
            "body": {"messages": [{
                "role": "user",
                "content": json.dumps({
                    "current_paragraph": {
                        "source": f"An Amazon spokesperson said, {source_quote}",
                    },
                }),
            }]},
        }

        def fake_run_stream(requests, config, validator):
            result = validator.validate(json.dumps(raw_payload), request)
            self.assertTrue(result.ok)
            return [{
                "custom_id": "P0001",
                "response": json.dumps(raw_payload),
            }]

        with patch("translator_transport.runner.run_stream", side_effect=fake_run_stream):
            schema = json.loads(
                (ROOT / "schemas/translation.schema.json").read_text(encoding="utf-8")
            )
            rows = gateway.run([request], schema)

        payload = ParagraphTranslator._payload_by_id(rows)["P0001"]
        self.assertEqual(payload["unresolved_citations"], [])

    def test_context_then_parallel_paragraph_batch_and_ordered_output(self):
        settings = Settings.load(ROOT / "translator_config.json")
        gateway = FakeGateway()
        translator = ParagraphTranslator(
            settings,
            gateway,
            TemplateRepository(ROOT / "prompts"),
        )
        status = RecordingStatus()
        output = translator.translate("First paragraph.\nSecond paragraph.", status=status)
        self.assertEqual(output, "- 译文 P0001\n\n- 译文 P0002")
        self.assertTrue(any(
            update["mode"] == "多段落综合翻译"
            and "多段落综合翻译" in update["message"]
            for update in status.updates
        ))
        self.assertEqual(len(gateway.calls), 2)
        self.assertTrue(all(schema is not None for _, schema in gateway.calls))
        self.assertEqual(
            [request["custom_id"] for request in gateway.calls[1][0]],
            ["P0001", "P0002"],
        )
        for request in gateway.calls[1][0]:
            for message in request["body"]["messages"]:
                self.assertIsInstance(json.loads(message["content"]), dict)

    def test_preserve_mode_keeps_multi_paragraph_output_without_list_markers(self):
        settings = Settings.load(ROOT / "translator_config.json")
        gateway = FakeGateway()
        translator = ParagraphTranslator(
            settings,
            gateway,
            TemplateRepository(ROOT / "prompts"),
        )
        status = RecordingStatus()

        output = translator.translate(
            "First paragraph.\nSecond paragraph.",
            status=status,
            preserve_paragraphs=True,
        )

        self.assertEqual(output, "译文 P0001\n\n译文 P0002")
        self.assertNotIn("- ", output)
        self.assertTrue(any(
            update["mode"] == "多段落综合翻译（保留原段落）"
            for update in status.updates
        ))

    def test_gesture_decision_is_deferred_until_network_requests_finish(self):

        with tempfile.TemporaryDirectory() as temporary:
            gesture = Path(temporary) / "gesture"
            gesture.write_text("")

            class DeferredGateway(FakeGateway):
                def run(self, requests, schema):
                    if gesture.read_text():
                        raise AssertionError("Network was delayed until gesture resolution")
                    rows = super().run(requests, schema)
                    if requests[0]["custom_id"] != "CONTEXT":
                        gesture.write_text("preserve")
                    return rows

            gateway = DeferredGateway()
            status = RecordingStatus()
            translator = ParagraphTranslator(
                Settings.load(ROOT / "translator_config.json"),
                gateway,
                TemplateRepository(ROOT / "prompts"),
            )
            output = translator.translate(
                "First paragraph.\nSecond paragraph.",
                status=status,
                gesture_file=gesture,
            )
            self.assertEqual(output, "译文 P0001\n\n译文 P0002")
            self.assertEqual(len(gateway.calls), 2)
            self.assertTrue(status.updates[-1]["message"].startswith(
                "多段落综合翻译（保留原段落）"
            ))

    def test_single_paragraph_skips_context_request(self):
        settings = Settings.load(ROOT / "translator_config.json")
        gateway = FakeGateway()
        translator = ParagraphTranslator(
            settings,
            gateway,
            TemplateRepository(ROOT / "prompts"),
        )

        status = RecordingStatus()
        output = translator.translate("Only one paragraph.[^2]", status=status)

        self.assertEqual(output, "译文 P0001")
        self.assertEqual(len(gateway.calls), 1)
        request = gateway.calls[0][0][0]
        self.assertEqual(request["custom_id"], "P0001")
        self.assertEqual(request["body"]["max_tokens"], 512)
        self.assertEqual(
            set(gateway.calls[0][1]["required"]),
            {"paragraph_id", "translation"},
        )
        self.assertTrue(gateway.calls[0][1]["x-paragraph-id-contract"])
        messages = request["body"]["messages"]
        self.assertTrue(all(isinstance(json.loads(message["content"]), dict) for message in messages))
        system_payload = json.loads(messages[0]["content"])
        self.assertEqual(
            set(system_payload["output_example"]),
            {"paragraph_id", "translation"},
        )
        self.assertNotIn("citation_contract", messages[0]["content"])
        self.assertNotIn("citation_id", messages[0]["content"])
        user_payload = json.loads(messages[1]["content"])
        self.assertEqual(set(user_payload), {"mode", "current_paragraph"})
        self.assertEqual(
            user_payload["current_paragraph"]["source"],
            "Only one paragraph.[^2]",
        )
        full_system = TemplateRepository(ROOT / "prompts").render_prompt(
            "translation.system.json.j2"
        )
        self.assertLess(len(messages[0]["content"]), len(full_system))
        self.assertTrue(any(
            update["mode"] == "单段落快速翻译"
            and "单段落快速翻译" in update["message"]
            for update in status.updates
        ))

    def test_short_english_phrase_uses_ultrafast_prompt_and_schema_retry_path(self):
        settings = Settings.load(ROOT / "translator_config.json")
        gateway = FakeGateway()
        translator = ParagraphTranslator(
            settings,
            gateway,
            TemplateRepository(ROOT / "prompts"),
        )

        status = RecordingStatus()
        output = translator.translate("agency theory", status=status)

        self.assertEqual(output, "译文 P0001")
        self.assertEqual(len(gateway.calls), 1)
        requests, schema = gateway.calls[0]
        self.assertIsNotNone(schema)
        self.assertTrue(schema["x-paragraph-id-contract"])
        request = requests[0]
        self.assertEqual(request["body"]["max_tokens"], 128)
        system_payload = json.loads(request["body"]["messages"][0]["content"])
        user_payload = json.loads(request["body"]["messages"][1]["content"])
        self.assertEqual(system_payload["role"], "ultrafast_english_phrase_translator")
        self.assertEqual(user_payload["mode"], "phrase_ultrafast")
        self.assertTrue(any(
            update["mode"] == "词组极速翻译"
            for update in status.updates
        ))


if __name__ == "__main__":
    unittest.main()
