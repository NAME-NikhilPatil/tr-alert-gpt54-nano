"""Regression checks for financial-image eligibility guardrails."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

import financial_pipeline as financial_pipeline_module
import main as main_module
import run_regression_dry as regression_dry_module
import telegram_sender as telegram_sender_module

from bs_cf_image import _cash_flow_only_rows, build_bs_cf_rows, clean_variable_label
from db_manager import is_seen, reserve_seen
from financial_cell_model import annotate_extraction_with_cell_model, canonical_cell_issues
from financial_filing_classifier import FINANCIAL_RESULTS, SKIPPED_NON_FINANCIAL_DISCLOSURE, classify_pdf_filing
from financial_validation import validate_financial_payload
from gpt54_extractor import (
    GPT54_MAX_OUTPUT_TOKENS_DEFAULT,
    _apply_schema_defaults,
    _apply_verified_company_corrections,
    _call_responses_api,
    _financial_model_route,
    _extract_pdf_with_gpt54_llm_values_first,
    _llm_values_result_period,
    _normalize_gpt_payload,
    _normalize_llm_values_first_payload,
    _pdf_request_timeout_seconds,
    _safe_reasoning_effort,
    _should_retry_values_first_with_xhigh,
    _temporary_gpt_route,
    extraction_json_schema,
    llm_values_first_json_schema,
    validate_gpt54_json,
)
from image_generator import (
    GeneratedFinancialImages,
    _available_render_jobs,
    _period_caption_parts,
    _standalone_conflicts_with_consolidated_source,
    _statement_basis,
    generate_financial_images,
)
from image_validation import validate_financial_png
from main import _extraction_date_matches_live_run, _filter_live_announcements
from mistral_parser import (
    _payload_from_ocr_tables,
    _parse_segment_table,
    _select_financial_pages,
    _select_segment_tables,
    normalize_mistral_extraction,
    payload_from_ocr_markdown_tables,
)
from models import Announcement
from pdf_job_worker import PdfJobWorkerConfig
from pl_image import RenderBlockedError, build_pl_rows, change_for_row, format_display_cell, normalize_rows, render_pl_image, result_display_columns, row_has_value, rows_to_table
from segment_image import build_segment_rows
from table_repair_engine import repair_financial_payload
from unit_detector import normalize_extraction_units
from utils import normalize_date


def main() -> int:
    tests = [
        test_roman_index_labels_are_dropped,
        test_cash_flow_labels_do_not_match_balance_sheet_financial_words,
        test_cash_flow_only_rows_are_detected,
        test_false_segment_table_is_skipped,
        test_named_segment_table_is_allowed,
        test_generated_png_validation_rejects_blank_image,
        test_generated_png_validation_rejects_photo_contamination,
        test_generated_png_validation_rejects_transparency,
        test_pnl_without_current_revenue_is_skipped,
        test_pnl_direct_values_are_not_overwritten,
        test_pnl_missing_purchase_component_is_zero,
        test_pnl_missing_subtotal_components_are_zero,
        test_pnl_profit_before_exceptional_excludes_other_income,
        test_pnl_numbered_post_gross_expense_labels_are_classified_correctly,
        test_pnl_ocr_shifted_numeric_labels_are_dropped,
        test_dynamic_pnl_line_items_and_q4_year_repair,
        test_finance_style_pnl_preserves_source_line_items,
        test_renderer_drops_repeated_value_vectors,
        test_repeated_financial_value_vectors_are_dropped,
        test_standalone_conflict_is_detected,
        test_long_pdf_page_selection_prioritizes_consolidated_statement_anchor,
        test_legacy_lakh_segment_values_are_converted_to_crores,
        test_inr_million_values_are_converted_to_crores,
        test_month_first_financial_table_headers_are_extracted,
        test_exchange_boilerplate_company_name_does_not_drop_rows,
        test_segment_parser_keeps_metrics_separate_and_prefers_consolidated,
        test_ordinal_date_headers_and_shifted_labels_are_recovered,
        test_consolidated_block_and_eps_continuation_are_used,
        test_packet_level_lakh_unit_scales_pnl_without_page_unit,
        test_bs_cf_compact_rows_keep_both_fy_columns,
        test_bs_cf_filters_pnl_reclassification_note_rows,
        test_astral_million_unit_finance_and_segment_columns,
        test_table_repair_engine_repairs_aryaman_revenue_pbt_pat,
        test_baid_standalone_lakh_values_stay_q4_and_fy_separate,
        test_asahi_consolidated_only_and_q4_fy_collision_guard,
        test_validation_block_message_hides_repair_audit_details,
        test_single_standalone_statement_is_default_output,
        test_financial_validation_accepts_formula_chain_and_margins,
        test_financial_validation_accepts_revenue_minus_total_expenses_basis,
        test_financial_validation_blocks_repeated_value_vectors,
        test_gpt54_schema_defaults_allow_missing_metadata_fields,
        test_gpt54_schema_defaults_drop_blank_label_rows,
        test_gpt54_extraction_schema_is_returned,
        test_gpt54_schema_rows_allow_source_provenance,
        test_llm_values_first_mode_uses_renderer_payload_without_strict_formula_block,
        test_llm_values_first_maps_nine_month_and_year_columns_by_position,
        test_llm_values_first_generic_pnl_order_company_cleanup_and_bs_reconcile,
        test_llm_values_first_recomputes_gross_profit_ebitda_and_margin,
        test_llm_values_first_blocks_total_income_component_mismatch,
        test_llm_values_first_suppresses_insurance_manufacturing_metrics,
        test_llm_values_first_removes_segment_proxy_balance_sheet_rows,
        test_llm_values_first_revenue_order_total_income_note_and_liabilities_row,
        test_legacy_company_patches_disabled_by_default,
        test_canonical_cell_model_flags_eps_conversion,
        test_formula_mismatch_hard_blocks_all_rendering,
        test_renderer_refuses_unapproved_pnl_input,
        test_financial_auditor_failure_blocks_rendering,
        test_gpt54_date_period_headers_are_normalized_by_statement_type,
        test_gpt54_prefers_prescaled_ocr_table_over_collapsed_gpt_rows,
        test_thousand_unit_values_convert_to_crores,
        test_month_year_headers_eps_and_consolidated_cash_flow_are_parsed,
        test_missing_current_fy_cash_flow_blocks_rendering,
        test_cash_flow_dash_value_is_present_for_period_check,
        test_q3_year_ocr_does_not_shift_segment_quarters_to_future_year,
        test_lakh_unit_not_confused_by_plain_thousand_word,
        test_sadbhav_verified_tax_eps_exceptional_correction,
        test_dynacons_verified_segment_uses_quarter_columns,
        test_panacea_verified_consolidated_values_and_eps,
        test_ahlada_verified_standalone_tax_eps_and_no_segment,
        test_titagarh_verified_consolidated_jv_exceptional_discontinued_eps,
        test_fischer_verified_consolidated_associate_and_eps,
        test_rajesh_verified_consolidated_million_quarter_mapping_and_bs_cf,
        test_gradiente_verified_standalone_image_heavy_fallback,
        test_unclear_display_values_render_as_na,
        test_live_announcement_date_gate_skips_past_dates,
        test_exchange_timestamp_formats_are_parsed_without_date_corruption,
        test_dedupe_keeps_distinct_attachments_for_same_company_and_timestamp,
        test_exchange_discovery_failure_does_not_block_other_exchange,
        test_empty_pdf_url_does_not_dedupe_unrelated_announcements,
        test_image_only_pdf_is_sent_to_gpt_vision_instead_of_locally_skipped,
        test_exchange_today_uses_india_timezone_at_utc_midnight_boundary,
        test_queued_previous_day_job_is_stale_after_ist_midnight,
        test_manual_verification_warning_is_client_friendly,
        test_empty_financial_payload_is_silent_skip,
        test_display_cells_preserve_meaningful_small_values,
        test_llm_values_first_uses_first_quarter_date_as_result_period,
        test_llm_values_first_retries_truncated_json_with_full_pdf,
        test_responses_api_uses_dedicated_http_retries_for_503,
        test_responses_api_defaults_to_one_http_attempt,
        test_responses_api_background_mode_polls_same_job_to_completion,
        test_responses_api_can_resume_stored_job_without_duplicate_post,
        test_reasoning_effort_is_clamped_to_high_or_xhigh,
        test_complex_pdf_defaults_to_high_reasoning,
        test_pdf_timeout_scales_for_large_long_hybrid_and_complex_inputs,
        test_live_gpt_defaults_bound_latency_and_concurrency,
        test_regression_dry_uses_the_http_retry_setting_read_by_the_client,
        test_valid_values_first_payload_does_not_retry_with_xhigh_by_default,
        test_live_output_uses_only_generic_no_data_message_for_any_no_image_problem,
        test_live_worker_logs_transport_detail_but_sends_only_generic_message,
        test_live_worker_defers_generic_notice_when_transient_503_will_be_requeued,
        test_telegram_transport_errors_redact_bot_token,
        test_runtime_data_dir_moves_relative_state_under_persistent_root,
        test_no_telegram_status_is_not_reported_as_sent,
        test_regression_console_text_is_safe_for_windows_cp1252,
        test_akme_warrant_pdf_is_structured_non_financial_skip,
    ]
    for test in tests:
        test()
        print(f"OK {test.__name__}")
    return 0


def test_roman_index_labels_are_dropped() -> None:
    assert clean_variable_label("III") == ""
    extraction = {
        "currency_unit": "Rs in Cr",
        "balance_sheet_variables": [
            {"section": "Variables", "rows": [{"label": "III", "values": {"FY26": "19.5"}}]}
        ],
    }
    rows = build_bs_cf_rows(extraction)
    assert not any(row_has_value(row) for row in rows)


def test_cash_flow_labels_do_not_match_balance_sheet_financial_words() -> None:
    extraction = {
        "currency_unit": "Rs in Cr",
        "balance_sheet_variables": [
            {
                "section": "Assets",
                "rows": [
                    {"label": "Investment", "values": {"FY26": "10"}},
                    {"label": "Financial Liabilities", "values": {"FY26": "20"}},
                ],
            }
        ],
    }
    rows = build_bs_cf_rows(extraction)
    value_labels = [row["label"] for row in rows if row_has_value(row)]
    assert value_labels == ["Investment", "Financial Liabilities"]
    assert not _cash_flow_only_rows(rows)


def test_cash_flow_only_rows_are_detected() -> None:
    extraction = {
        "currency_unit": "Rs in Cr",
        "cash_flow_variables": [
            {"label": "Net cash inflow (outflow) from operating activities", "values": {"FY26": "10"}},
            {"label": "Net cash inflow (outflow) from investing activities", "values": {"FY26": "-5"}},
            {"label": "Net cash inflow (outflow) from financing activities", "values": {"FY26": "2"}},
        ],
    }
    rows = build_bs_cf_rows(extraction)
    assert _cash_flow_only_rows(rows)
    assert [row["label"] for row in rows if row_has_value(row)] == [
        "Net cash inflow (outflow) from operating activities",
        "Net cash inflow (outflow) from investing activities",
        "Net cash inflow (outflow) from financing activities",
    ]


def test_sadbhav_verified_tax_eps_exceptional_correction() -> None:
    extraction = _apply_verified_company_corrections({"company_name": "Sadbhav Engineering Limited"})
    normalized, _, _, _ = normalize_extraction_units(extraction, company="Sadbhav Engineering Limited", announcement_date="2026-05-30")
    rows = build_pl_rows(normalize_rows(normalized["financial_rows"]))
    by_label = {row["label"]: row["values"] for row in rows}
    assert by_label["Exceptional Items"]["Q4 FY26"] == "229.7"
    assert by_label["Total tax expense"]["Q4 FY26"] == "39.03"
    assert by_label["PAT"]["Q4 FY26"] == "122.3"
    assert by_label["EPS (Basic)"]["Q4 FY26"] == "4.73"
    assert "Profit/(Loss) before exceptional items and tax (3-4)" not in by_label


def test_dynacons_verified_segment_uses_quarter_columns() -> None:
    extraction = _apply_verified_company_corrections({"company_name": "Dynacons Systems & Solutions Limited"})
    normalized, _, _, _ = normalize_extraction_units(extraction, company="Dynacons Systems & Solutions Limited", announcement_date="2026-05-30")
    pl_rows = build_pl_rows(normalize_rows(normalized["financial_rows"]))
    pl_by_label = {row["label"]: row["values"] for row in pl_rows}
    assert pl_by_label["Revenue"]["Q4 FY26"] == "402.45"
    assert pl_by_label["Gross Profit"]["Q4 FY26"] == "56.77"
    segment_rows = build_segment_rows(normalized)
    data_rows = [row for row in segment_rows if row.get("type") == "data"]
    assert any(row["label"] == "Revenue" and row["values"].get("Q4 FY26") == "398.7" and row["values"].get("Q4 FY25") == "325.65" for row in data_rows)
    assert any(row["label"] == "Segment Profit" and row["values"].get("Q4 FY26") == "34.45" for row in data_rows)


def test_panacea_verified_consolidated_values_and_eps() -> None:
    extraction = _apply_verified_company_corrections({"company_name": "Panacea Biotec Limited"})
    normalized, _, _, _ = normalize_extraction_units(extraction, company="Panacea Biotec Limited", announcement_date="2026-05-30")
    rows = build_pl_rows(normalize_rows(normalized["financial_rows"]))
    by_label = {row["label"]: row["values"] for row in rows}
    assert normalized["statement_basis"] == "consolidated"
    assert by_label["Revenue"]["Q4 FY26"] == "166.75"
    assert by_label["Cost of raw and packing materials consumed"]["Q4 FY26"] == "80.78"
    assert by_label["Exceptional items"]["Q4 FY26"] == "2.71"
    assert by_label["Profit Before Tax"]["Q4 FY26"] == "-1.25"
    assert by_label["PAT"]["Q4 FY26"] == "-1"
    assert by_label["EPS (Basic)"]["Q4 FY26"] == "0.08"
    assert by_label["EPS (Basic)"]["Q4 FY25"] == "(0.31)"
    cf_rows = {row["label"]: row["values"] for row in normalize_rows(normalized["cash_flow_variables"])}
    assert cf_rows["Net cash inflow (outflow) from operating activities"]["FY26"] == "16.68"
    assert cf_rows["Net cash inflow (outflow) from operating activities"]["FY25"] == "-27.37"


def test_ahlada_verified_standalone_tax_eps_and_no_segment() -> None:
    extraction = _apply_verified_company_corrections(
        {
            "company_name": "Ahlada Engineers Limited",
            "balance_sheet_variables": [
                {
                    "section": "Liabilities",
                    "rows": [
                        {"label": "Total liabilities", "values": {"FY26": "8092", "FY25": "6425"}},
                    ],
                }
            ],
        }
    )
    normalized, _, _, _ = normalize_extraction_units(extraction, company="Ahlada Engineers Limited", announcement_date="2026-05-31")
    rows = build_pl_rows(normalize_rows(normalized["financial_rows"]))
    by_label = {row["label"]: row["values"] for row in rows}
    assert normalized["statement_basis"] == "standalone"
    assert normalized["only_standalone_found"] is True
    assert by_label["Gross Profit"]["Q4 FY26"] == "8.421"
    assert by_label["Gross Profit Margin %"]["Q4 FY26"] == "32.83%"
    assert by_label["Gross Profit Margin %"]["Q4 FY25"] == "32.59%"
    assert by_label["Total Expenses excluding"]["Q4 FY26"] == "23.86"
    assert by_label["EBITDA"]["Q4 FY26"] == "1.7968"
    assert by_label["EBITDA Margin %"]["Q4 FY26"] == "7.00%"
    assert by_label["EBITDA Margin %"]["Q3 FY26"] == "16.87%"
    assert by_label["EBITDA Margin %"]["Q4 FY25"] == "12.39%"
    assert by_label["Profit before exceptional items, Other Income"]["Q4 FY26"] == "-1.62"
    assert by_label["Total tax expense"]["Q4 FY26"] == "-0.49"
    assert by_label["PAT"]["Q4 FY26"] == "-1.0352"
    assert by_label["PAT"]["Q3 FY26"] == "0.196"
    assert change_for_row({"values": by_label["PAT"]}, "Q4 FY26", "Q3 FY26") == "-628.16%"
    columns = result_display_columns(rows, "Q4 FY26")
    rendered_rows = rows_to_table(rows, columns, skip_margin_changes=True)
    rendered_by_label = {row[0]: row for row in rendered_rows}
    q3_index = next(index + 1 for index, column in enumerate(columns) if column.get("period") == "Q3 FY26")
    assert rendered_by_label["PAT"][q3_index] == "0.2"
    assert by_label["EPS (Basic)"]["Q4 FY26"] == "(0.80)"
    bs_rows = build_bs_cf_rows(normalized)
    bs_labels = {row["label"] for row in bs_rows}
    assert "Total Current Liabilities" in bs_labels
    assert "Total liabilities" not in bs_labels
    assert build_segment_rows(normalized) == []


def test_false_segment_table_is_skipped() -> None:
    extraction = {
        "currency_unit": "Rs in Cr",
        "segment_tables": [
            {
                "title": "Segment Wise",
                "rows": [
                    {"label": "Revenue", "values": {"Q4 FY26": "100"}},
                    {"label": "PAT", "values": {"Q4 FY26": "10"}},
                ],
            }
        ],
    }
    assert build_segment_rows(extraction) == []


def test_named_segment_table_is_allowed() -> None:
    extraction = {
        "currency_unit": "Rs in Cr",
        "segment_tables": [
            {
                "title": "Segment Wise",
                "rows": [
                    {"label": "(a) Cables", "values": {"Q4 FY26": "100"}},
                    {"label": "(b) EPC", "values": {"Q4 FY26": "50"}},
                ],
            }
        ],
    }
    rows = build_segment_rows(extraction)
    assert any(row.get("label") == "(a) Cables" for row in rows)


def test_generated_png_validation_rejects_blank_image() -> None:
    path = Path("output/_test/blank_financial_image.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1920, 1080), "white").save(path)

    issue = validate_financial_png(path)

    assert issue.startswith("image_probably_blank") or issue.startswith("image_not_table_like")


def test_generated_png_validation_rejects_photo_contamination() -> None:
    path = Path("output/_test/photo_contaminated_financial_image.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1920, 1080), (20, 20, 25))
    pixels = image.load()
    for y in range(1080):
        for x in range(1920):
            pixels[x, y] = ((x * 3 + y) % 256, (x + y * 5) % 256, (x * 7 + y * 2) % 256)
    for y in range(80, 300):
        for x in range(80, 700):
            pixels[x, y] = (198, 239, 206) if y % 40 else (31, 56, 100)
    image.save(path)

    issue = validate_financial_png(path)

    assert issue.startswith("image_photo_like") or issue.startswith("image_not_table_like")


def test_generated_png_validation_rejects_transparency() -> None:
    path = Path("output/_test/transparent_financial_image.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (1920, 1080), (255, 255, 255, 0))
    for x in range(1920):
        for y in range(80, 160):
            image.putpixel((x, y), (31, 56, 100, 255))
    image.save(path)

    issue = validate_financial_png(path)

    assert issue.startswith("image_has_transparency")


def test_pnl_without_current_revenue_is_skipped() -> None:
    extraction = {
        "company_name": "Sparse PNL Ltd",
        "currency_unit": "Rs in Cr",
        "result_period": "Q4 FY26",
        "financial_rows": [
            {"label": "Revenue", "values": {"Q4 FY25": "100"}},
            {"label": "PAT", "values": {"Q4 FY26": "10", "Q4 FY25": "5"}},
            {"label": "EPS (Basic)", "values": {"Q4 FY26": "1"}},
        ],
    }
    normalized, _, display_unit, _ = normalize_extraction_units(extraction, company="Sparse PNL Ltd", announcement_date="")
    quarter, fy = _period_caption_parts(normalized)
    jobs = _available_render_jobs(
        normalized=normalized,
        announcement=None,
        output_dir=Path("output/_test"),
        display_unit=display_unit,
        standalone_tag=False,
        company="Sparse PNL Ltd",
        quarter_label=quarter,
        fy_label=fy,
    )
    assert not next(job for job in jobs if job["kind"] == "pnl")["available"]
    assert build_pl_rows(normalize_rows(normalized.get("financial_rows")))


def test_pnl_direct_values_are_not_overwritten() -> None:
    rows = normalize_rows(
        [
            {"label": "Revenue", "values": {"Q4 FY26": "100"}},
            {"label": "Cost of materials consumed", "values": {"Q4 FY26": "10"}},
            {"label": "Gross Profit", "values": {"Q4 FY26": "80"}},
            {"label": "Employee Benefit Expense", "values": {"Q4 FY26": "8"}},
            {"label": "Other expenses", "values": {"Q4 FY26": "7"}},
            {"label": "EBITDA", "values": {"Q4 FY26": "60"}},
        ]
    )
    built = build_pl_rows(rows)
    values = {row["label"]: row.get("values", {}) for row in built}

    assert values["Gross Profit"]["Q4 FY26"] == "80"
    assert values["EBITDA"]["Q4 FY26"] == "60"


def test_pnl_missing_purchase_component_is_zero() -> None:
    rows = normalize_rows(
        [
            {"label": "Revenue", "values": {"Q4 FY26": "100"}},
            {"label": "Cost of materials consumed", "values": {"Q4 FY26": "10"}},
        ]
    )
    built = build_pl_rows(rows)
    values = {row["label"]: row.get("values", {}) for row in built}

    assert values["Gross Profit"]["Q4 FY26"] == "90"
    assert values["Gross Profit Margin %"]["Q4 FY26"] == "90.00%"


def test_pnl_missing_subtotal_components_are_zero() -> None:
    rows = normalize_rows(
        [
            {"label": "Revenue", "values": {"Q4 FY26": "100"}},
            {"label": "Gross Profit", "values": {"Q4 FY26": "70"}},
            {"label": "Employee Benefit Expense", "values": {"Q4 FY26": "8"}},
        ]
    )
    built = build_pl_rows(rows)
    values = {row["label"]: row.get("values", {}) for row in built}

    assert values["EBITDA"]["Q4 FY26"] == "62"
    assert values["Profit before exceptional items, Other Income"]["Q4 FY26"] == "62"
    assert values["Profit Before Tax"]["Q4 FY26"] == "62"
    assert values["PAT"]["Q4 FY26"] == "62"


def test_pnl_profit_before_exceptional_excludes_other_income() -> None:
    rows = normalize_rows(
        [
            {"label": "Revenue", "values": {"Q4 FY26": "100"}},
            {"label": "Gross Profit", "values": {"Q4 FY26": "80"}},
            {"label": "Employee Benefit Expense", "values": {"Q4 FY26": "10"}},
            {"label": "Other expenses", "values": {"Q4 FY26": "5"}},
            {"label": "Depreciation and amortisation expense", "values": {"Q4 FY26": "3"}},
            {"label": "Finance costs", "values": {"Q4 FY26": "2"}},
            {"label": "Other income", "values": {"Q4 FY26": "7"}},
            {"label": "Exceptional items", "values": {"Q4 FY26": "-1"}},
            {"label": "Profit before exceptional items and tax", "values": {"Q4 FY26": "67"}},
        ]
    )
    built = build_pl_rows(rows)
    values = {row["label"]: row.get("values", {}) for row in built}

    assert values["EBITDA"]["Q4 FY26"] == "65"
    assert values["Profit before exceptional items, Other Income"]["Q4 FY26"] == "60"
    assert values["Profit Before Tax"]["Q4 FY26"] == "66"


def test_pnl_numbered_post_gross_expense_labels_are_classified_correctly() -> None:
    rows = normalize_rows(
        [
            {"label": "Revenue", "values": {"Q4 FY26": "100"}},
            {"label": "Expenses", "type": "section", "values": {}},
            {"label": "a. Cost of material consumed", "values": {"Q4 FY26": "45"}},
            {"label": "b. Purchase of stock -in-trade", "values": {"Q4 FY26": "5"}},
            {"label": "c. Change in inventories", "values": {"Q4 FY26": "-2"}},
            {"label": "d. Employees benefits expense", "values": {"Q4 FY26": "10"}},
            {"label": "e. Finance Cost", "values": {"Q4 FY26": "3"}},
            {"label": "f. Depreciation and amortisation expense", "values": {"Q4 FY26": "4"}},
            {"label": "g. Power and fuel", "values": {"Q4 FY26": "6"}},
            {"label": "h. Other expenses", "values": {"Q4 FY26": "8"}},
            {"label": "Total Expenses", "values": {"Q4 FY26": "79"}},
        ]
    )
    built = build_pl_rows(rows)
    values = {row["label"]: row.get("values", {}) for row in built}
    roles = {row["label"]: row.get("formula_role", "") for row in built}

    assert values["Gross Profit"]["Q4 FY26"] == "46"
    assert values["EBITDA"]["Q4 FY26"] == "28"
    assert values["Profit before exceptional items, Other Income"]["Q4 FY26"] == "21"
    assert roles["Employees benefits expense"] == "employee"
    assert roles["Finance Cost"] == "finance"
    assert roles["Other expenses"] == "operating_expense"


def test_pnl_ocr_shifted_numeric_labels_are_dropped() -> None:
    rows = normalize_rows(
        [
            {"label": "Total Income", "values": {"Q4 FY26": "1361.32", "Q3 FY26": "2157.87"}},
            {"label": "Expenses", "values": {}},
            {"label": ",678.56", "values": {"Q4 FY26": "1457.67", "Q3 FY26": "1050.20"}},
            {"label": ",958.08", "values": {"Q4 FY26": "-25.15", "Q3 FY26": "288.86"}},
            {"label": "PAT", "values": {"Q4 FY26": "-74.54", "Q3 FY26": "18.05"}},
            {"label": "Profit Before Tax", "values": {"Q4 FY26": "-103.86", "Q3 FY26": "24.43"}},
            {"label": "Total tax expenses", "values": {"Q4 FY26": "29.32", "Q3 FY26": "-6.39"}},
        ]
    )
    built = build_pl_rows(rows)
    labels = [row["label"] for row in built]

    assert ",678.56" not in labels
    assert ",958.08" not in labels
    assert labels.count("PAT") == 1
    assert "Gross Profit" not in labels
    assert "EBITDA" not in labels
    assert not any(row.get("formula_role") == "gross_component" for row in built)


def test_dynamic_pnl_line_items_and_q4_year_repair() -> None:
    markdown = """
    <table>
      <tr><td>Statement of Audited Consolidated Financial Results for the quarter and year ended March 31, 2026</td></tr>
      <tr><td>Rs. in Lakhs</td></tr>
      <tr><td>Sr. No.</td><td>Particulars</td><td>3 months ended</td><td>Preceding 3 months ended</td><td>Corresponding 3 months ended</td><td>Current Year Ended</td><td>Previous Year Ended</td></tr>
      <tr><td></td><td></td><td>31-03-2025</td><td>31-12-2025</td><td>31-03-2025</td><td>31-03-2026</td><td>31-03-2025</td></tr>
      <tr><td>1</td><td>Income</td><td></td><td></td><td></td><td></td><td></td></tr>
      <tr><td></td><td>a) Revenue from Operations</td><td>4,761.58</td><td>4,158.16</td><td>6,625.06</td><td>21,083.45</td><td>45,308.92</td></tr>
      <tr><td></td><td>b) Other Income</td><td>145.85</td><td>223.75</td><td>1,026.20</td><td>1,033.28</td><td>1,442.64</td></tr>
      <tr><td></td><td>Total Income</td><td>4,957.43</td><td>4,381.91</td><td>7,651.26</td><td>22,116.73</td><td>46,752.56</td></tr>
      <tr><td>2</td><td>Expenses</td><td></td><td></td><td></td><td></td><td></td></tr>
      <tr><td></td><td>a) Cost of Production / Acquisition and Telecast Fees</td><td>6,674.05</td><td>5,245.38</td><td>8,863.39</td><td>26,552.44</td><td>27,781.04</td></tr>
      <tr><td></td><td>b) Changes in Inventories</td><td>(2,393.32)</td><td>198.87</td><td>(3,696.43)</td><td>(7,447.19)</td><td>6,612.31</td></tr>
      <tr><td></td><td>c) Marketing and Distribution Expense</td><td>361.30</td><td>175.44</td><td>497.87</td><td>1,218.80</td><td>3,011.39</td></tr>
      <tr><td></td><td>d) Employee Benefits Expense</td><td>783.97</td><td>928.96</td><td>904.83</td><td>3,452.07</td><td>3,385.85</td></tr>
      <tr><td></td><td>e) Finance Costs</td><td>78.35</td><td>38.07</td><td>16.95</td><td>190.73</td><td>337.38</td></tr>
      <tr><td></td><td>f) Depreciation and amortisation expense</td><td>107.63</td><td>162.75</td><td>173.76</td><td>685.51</td><td>753.49</td></tr>
      <tr><td></td><td>g) Other Expenses</td><td>1,055.03</td><td>788.80</td><td>1,964.47</td><td>3,886.61</td><td>5,890.88</td></tr>
      <tr><td></td><td>Total Expenses</td><td>6,717.01</td><td>7,538.27</td><td>8,724.84</td><td>28,518.97</td><td>47,772.25</td></tr>
      <tr><td>5</td><td>Profit / (Loss) before tax (3+4)</td><td>(1,809.58)</td><td>(3,156.36)</td><td>(1,073.58)</td><td>(6,402.24)</td><td>(1,019.69)</td></tr>
      <tr><td>7</td><td>Profit / (Loss) after tax (5-6)</td><td>(1,274.11)</td><td>(2,456.53)</td><td>9,402.92</td><td>(4,964.83)</td><td>8,457.01</td></tr>
    </table>
    """
    payload = payload_from_ocr_markdown_tables(markdown)
    assert payload["result_period"] == "Q4 FY26"
    source_rows = normalize_rows(payload["financial_rows"])
    built = build_pl_rows(source_rows)
    values = {row["label"]: row.get("values", {}) for row in built}

    assert "Cost of Production / Acquisition and Telecast Fees" in values
    assert "Marketing and Distribution Expense" in values
    assert "Cost of materials consumed" not in values
    assert values["Revenue"]["Q4 FY26"] == "47.62"
    assert values["Gross Profit"]["Q4 FY26"] == "1.2"
    assert values["Profit Before Tax"]["Q4 FY26"] == "-18.1"


def test_finance_style_pnl_preserves_source_line_items() -> None:
    rows = normalize_rows(
        [
            {"label": "Interest earned", "values": {"Q4 FY26": "120", "Q4 FY25": "100"}},
            {"label": "Other Income", "values": {"Q4 FY26": "20", "Q4 FY25": "15"}},
            {"label": "Total Income", "values": {"Q4 FY26": "140", "Q4 FY25": "115"}},
            {"label": "Interest expended", "values": {"Q4 FY26": "55", "Q4 FY25": "45"}},
            {"label": "Operating expenses", "values": {"Q4 FY26": "30", "Q4 FY25": "25"}},
            {"label": "Provisions and contingencies", "values": {"Q4 FY26": "10", "Q4 FY25": "8"}},
            {"label": "Profit Before Tax", "values": {"Q4 FY26": "45", "Q4 FY25": "37"}},
            {"label": "Total tax expense", "values": {"Q4 FY26": "12", "Q4 FY25": "10"}},
            {"label": "PAT", "values": {"Q4 FY26": "33", "Q4 FY25": "27"}},
            {"label": "Other Comprehensive Income", "values": {"Q4 FY26": "-2", "Q4 FY25": "1"}},
            {"label": "Paid up equity share capital", "values": {"Q4 FY26": "31.5", "Q4 FY25": "34.1"}},
            {"label": "EPS (Basic)", "values": {"Q4 FY26": "3.3", "Q4 FY25": "2.7"}},
        ]
    )
    built = build_pl_rows(rows)
    labels = [row["label"] for row in built]
    values = {row["label"]: row.get("values", {}) for row in built}

    assert "Interest earned" in labels
    assert "Interest expended" in labels
    assert "Operating expenses" in labels
    assert "Provisions and contingencies" in labels
    assert "Gross Profit" not in labels
    assert "EBITDA" not in labels
    assert "Other Comprehensive Income" not in labels
    assert "Paid up equity share capital" not in labels
    assert "EPS (Basic)" in labels
    assert values["Total Income"]["Q4 FY26"] == "140"
    assert values["PAT Margin %"]["Q4 FY26"] == "23.57%"
    assert next(row for row in built if row["label"] == "Total Income")["formula_role"] == "revenue"


def test_renderer_drops_repeated_value_vectors() -> None:
    rows = normalize_rows(
        [{"label": "Revenue", "values": {"Q4 FY26": "9543.11", "Q3 FY26": "2775.17"}}]
        + [
            {"label": label, "values": {"Q4 FY26": "100.00", "Q3 FY26": "100.00", "Q4 FY25": "100.00"}}
            for label in [
                "Employee Benefit Expense",
                "Other expenses",
                "Total Expenses",
                "EBITDA",
                "Finance Cost",
                "Profit Before Tax",
                "PAT",
            ]
        ]
    )
    assert build_pl_rows(rows) == []


def test_repeated_financial_value_vectors_are_dropped() -> None:
    payload = {
        "company_name": "Repeated Values Ltd",
        "parser_status": "parsed_mistral",
        "confidence": 0.95,
        "financial_rows": [
            {"label": f"Line item {index}", "values": {"Q4 FY26": "100", "Q3 FY26": "90", "FY26": "400"}}
            for index in range(5)
        ],
    }
    normalized = normalize_mistral_extraction(payload)
    assert normalized["financial_rows"] == []


def test_standalone_conflict_is_detected() -> None:
    extraction = {
        "company_name": "Conflict Ltd",
        "currency_unit": "Rs in Cr",
        "statement_basis": "standalone",
        "parser_message": "Only standalone data found.",
        "ocr_markdown": "Standalone financial results\nConsolidated financial results",
    }
    assert _statement_basis(extraction) == "standalone"
    assert _standalone_conflicts_with_consolidated_source(extraction)


class _FakePdfPage:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text(self, _kind: str) -> str:
        return self._text


class _FakePdfDocument:
    def __init__(self, pages: list[str]) -> None:
        self._pages = [_FakePdfPage(page) for page in pages]
        self.page_count = len(self._pages)

    def __getitem__(self, index: int) -> _FakePdfPage:
        return self._pages[index]


def test_long_pdf_page_selection_prioritizes_consolidated_statement_anchor() -> None:
    pages = ["Outcome of Board Meeting. Audited Standalone and Consolidated Financial Results."]
    pages += ["Independent auditors report. Basis for opinion."] * 23
    pages += ["Standalone and Consolidated Financial Statements:"]
    pages += [""] * 6
    selected = [page + 1 for page in _select_financial_pages(_FakePdfDocument(pages), max_pages=7)]
    assert selected == [23, 24, 25, 26, 27, 28, 29]


def test_legacy_lakh_segment_values_are_converted_to_crores() -> None:
    extraction = {
        "company_name": "Legacy Segment Ltd",
        "source_currency_unit": "Rs in Lakhs",
        "currency_unit": "Rs in Cr",
        "values_display_unit_applied": True,
        "ocr_markdown": "Financial results Amount in INR Lacs Segment information",
        "segment_tables": [
            {
                "title": "Segment Wise",
                "rows": [
                    {"label": "(a) Cables", "values": {"Q4 FY26": "100502.02"}},
                    {"label": "EPS (Basic)", "values": {"Q4 FY26": "1.23"}},
                ],
            }
        ],
    }
    normalized, source_unit, display_unit, _ = normalize_extraction_units(extraction)
    assert source_unit == "Rs in Lakhs"
    assert display_unit == "Rs in Cr"
    rows = normalized["segment_tables"][0]["rows"]
    assert rows[0]["values"]["Q4 FY26"] == "1005.0202"
    assert rows[1]["values"]["Q4 FY26"] == "1.23"


def test_inr_million_values_are_converted_to_crores() -> None:
    extraction = {
        "company_name": "INR Million Ltd",
        "ocr_markdown": "Statement of consolidated financial results [All amounts are in \u015b Million unless otherwise stated]",
        "financial_rows": [
            {"label": "Revenue", "values": {"Q4 FY26": "123.40"}},
            {"label": "EPS (Basic)", "values": {"Q4 FY26": "2.50"}},
        ],
    }

    normalized, source_unit, display_unit, _ = normalize_extraction_units(extraction)

    assert source_unit == "Rs in Millions"
    assert display_unit == "Rs in Cr"
    rows = normalized["financial_rows"]
    assert rows[0]["values"]["Q4 FY26"] == "12.34"
    assert rows[1]["values"]["Q4 FY26"] == "2.50"


def test_month_first_financial_table_headers_are_extracted() -> None:
    markdown = """
    <table>
      <tr><td colspan="6">Statement of Audited Consolidated Financial Results for the quarter and year ended March 31, 2026</td></tr>
      <tr><td rowspan="2">Particulars</td><td colspan="3">Quarter ended</td><td colspan="2">Year ended</td></tr>
      <tr><td>March 31, 2026 (Audited)</td><td>December 31, 2025</td><td>March 31, 2025</td><td>March 31, 2026</td><td>March 31, 2025</td></tr>
      <tr><td>Revenue from operations</td><td>10,967.79</td><td>6,439.66</td><td>6,832.68</td><td>26,412.70</td><td>24,387.80</td></tr>
      <tr><td>Other income</td><td>201.53</td><td>101.82</td><td>73.66</td><td>461.08</td><td>234.22</td></tr>
      <tr><td>Total income</td><td>11,169.32</td><td>6,541.48</td><td>6,906.34</td><td>26,873.78</td><td>24,622.02</td></tr>
      <tr><td>Expenses</td><td></td><td></td><td></td><td></td><td></td></tr>
      <tr><td>Profit before tax</td><td>1,458.13</td><td>1,142.50</td><td>751.91</td><td>4,295.31</td><td>3,839.31</td></tr>
    </table>
    """
    payload = payload_from_ocr_markdown_tables(markdown)
    rows = {row["label"]: row["values"] for row in payload["financial_rows"] if row.get("values")}

    assert payload["result_period"] == "Q4 FY26"
    assert rows["Revenue"]["Q4 FY26"] == "10967.79"
    assert rows["Revenue"]["Q3 FY26"] == "6439.66"
    assert rows["Revenue"]["Q4 FY25"] == "6832.68"
    assert rows["Revenue"]["FY26"] == "26412.7"
    assert rows["Profit Before Tax"]["Q4 FY26"] == "1458.13"


def test_exchange_boilerplate_company_name_does_not_drop_rows() -> None:
    announcement = Announcement(
        source="NSE",
        company_name="Pace Digitek Limited",
        identifier="PACE",
        announcement_datetime="26-05-2026",
        subject="Outcome of Board Meeting",
        pdf_url="",
    )
    payload = {
        "company_name": "BSE Limited",
        "parser_status": "parsed_mistral",
        "confidence": 0.95,
        "financial_rows": [{"label": "Revenue", "values": {"Q4 FY26": "100"}}],
    }

    normalized = normalize_mistral_extraction(payload, announcement)

    assert normalized["parser_status"] == "parsed_mistral"
    assert normalized["company_name"] == "Pace Digitek Limited"
    assert normalized["financial_rows"][0]["values"]["Q4 FY26"] == "100"


def test_segment_parser_keeps_metrics_separate_and_prefers_consolidated() -> None:
    rows = [
        ["Sl. No.", "Particulars", "Quarter Ended", "Year Ended"],
        ["31.03.2026", "31.12.2025", "31.03.2025", "31.03.2026", "31.03.2025"],
        ["1", "Segment Revenue", "", "", "", "", ""],
        ["", "(a) Cables", "27955.91", "19845.32", "20714.31", "88410.05", "79221.67"],
        ["", "(b) EPC", "73977.13", "52174.83", "103361.34", "273591.00", "330543.26"],
        ["", "Total", "101933.04", "72020.15", "124075.65", "362001.05", "409764.93"],
        ["2", "Segment Results", "", "", "", "", ""],
        ["", "(a) Cables", "2019.53", "1152.94", "1214.45", "5561.89", "3253.54"],
        ["", "(b) EPC", "4522.36", "(19.21)", "7025.73", "14795.87", "20572.46"],
        ["", "Total", "6541.89", "1133.73", "8240.18", "20357.76", "23826.00"],
    ]
    standalone = _parse_segment_table(rows, "AUDITED STANDALONE SEGMENT-WISE Rs. in Lakhs")
    consolidated = _parse_segment_table(rows, "AUDITED CONSOLIDATED SEGMENT-WISE Rs. in Lakhs")
    selected = _select_segment_tables([standalone, consolidated])
    assert len(selected) == 1
    labels = [row["label"] for row in selected[0]["rows"]]
    assert "Cables - Revenue" in labels
    assert "Cables - Segment Profit" in labels
    assert selected[0]["rows"][0]["values"]["Q4 FY26"] == "279.56"
    grouped = build_segment_rows({"segment_tables": selected})
    assert any(row.get("label") == "Cables" and row.get("type") == "section" for row in grouped)
    assert any(row.get("label") == "Revenue" for row in grouped)
    assert any(row.get("label") == "Segment Profit Margin %" for row in grouped)

    split_header_rows = [
        ["Particulars", "Quarter Ended", "Year ended 31st March 2026 Audited", "Year ended 31st March 2025 Audited"],
        ["31-03-2026", "31-12-2025", "31-03-2025"],
        ["Audited", "(Unaudited)", "Audited"],
        ["Segment revenue & other income from operations"],
        ["Explosives Division", "108.62", "116.26", "109.41", "387.72", "377.55"],
        ["Segment results (Profit / (Loss) before interest, exceptional items and tax)", "", "", "", "", ""],
        ["Explosives Division", "0.46", "6.27", "(4.36)", "(24.17)", "(106.36)"],
    ]
    split_header = _parse_segment_table(split_header_rows, "AUDITED CONSOLIDATED SEGMENT Rs. in Lakhs")
    first = split_header["rows"][0]
    assert first["values"]["Q4 FY26"] == "1.09"
    assert first["values"]["Q3 FY26"] == "1.16"
    assert first["values"]["Q4 FY25"] == "1.09"
    assert first["values"]["FY26"] == "3.88"
    assert first["values"]["FY25"] == "3.78"


def test_ordinal_date_headers_and_shifted_labels_are_recovered() -> None:
    markdown = """
    <table>
      <tr><th>Sr. no.</th><th>PARTICULARS</th><th>QUARTER ENDED (AUDITED) 31st March, 2026</th><th>QUARTER ENDED (UNAUDITED) 31st December, 2025</th><th>QUARTER ENDED (AUDITED) 31st March, 2025</th><th>YEAR ENDED (AUDITED) 31st March, 2026</th><th>YEAR ENDED (AUDITED) 31st March, 2025</th></tr>
      <tr><td>1</td><td>Income</td><td></td><td></td><td></td><td></td><td></td></tr>
      <tr><td></td><td>a. Revenue from Operations</td><td>13,356.85</td><td>10,522.21</td><td>10,234.40</td><td>44,825.80</td><td>33,550.10</td></tr>
      <tr><td></td><td>Total Income</td><td>14,013.48</td><td>10,717.09</td><td>10,316.32</td><td>46,094.93</td><td>33,800.02</td></tr>
      <tr><td>2</td><td>Expenses</td><td></td><td></td><td></td><td></td><td></td></tr>
      <tr><td>3</td><td>Profit before Tax (1-2)</td><td>2,855.05</td><td>2,138.31</td><td>2,479.60</td><td>9,701.71</td><td>7,350.90</td></tr>
      <tr><td>5</td><td>Net Profit for the Period/Year (3-4)</td><td>2,144.76</td><td>1,489.85</td><td>1,625.28</td><td>6,907.01</td><td>5,481.64</td></tr>
    </table>
    <table>
      <tr><td>S.No.</td><td>Particulars</td><td>Consolidated</td></tr>
      <tr><td>Quarter ended</td><td>Year ended</td></tr>
      <tr><td>31 March 2026</td><td>31 December 2026</td><td>31 March 2025</td><td>31 March 2026</td><td>31 March 2025</td></tr>
      <tr><td>1</td><td>Income from operations :</td><td></td><td></td><td></td><td></td><td></td></tr>
      <tr><td>(a) Revenue from operations</td><td>29,053.75</td><td>16,049.86</td><td>30,037.52</td><td>73,618.52</td><td>73,518.76</td></tr>
      <tr><td></td><td>Total income</td><td>29,957.19</td><td>16,458.17</td><td>30,341.75</td><td>75,668.00</td><td>74,556.04</td></tr>
      <tr><td>2</td><td>Expenses:</td><td></td><td></td><td></td><td></td><td></td></tr>
      <tr><td></td><td>Total expenses</td><td>23,024.64</td><td>15,167.22</td><td>25,240.12</td><td>66,648.21</td><td>63,766.67</td></tr>
      <tr><td>3</td><td>Profit before tax (1-2)</td><td>6,933.55</td><td>1,290.95</td><td>5,090.53</td><td>9,008.91</td><td>10,763.76</td></tr>
    </table>
    """

    payload = payload_from_ocr_markdown_tables("Rs. in Lakhs " + markdown)
    rows = {row["label"]: row["values"] for row in payload["financial_rows"] if row.get("values")}

    assert payload["result_period"] == "Q4 FY26"
    assert rows["Revenue"]["Q4 FY26"] == "290.54"
    assert rows["Revenue"]["Q3 FY26"] == "160.5"
    assert "Q3 FY27" not in rows["Revenue"]
    assert rows["Profit Before Tax"]["FY26"] == "90.09"


def test_consolidated_block_and_eps_continuation_are_used() -> None:
    markdown = """
    <table>
      <tr><td></td><td></td><td>PARTICULARS</td><td>STANDALONE</td><td>CONSOLIDATED</td></tr>
      <tr><td>QUARTER ENDED</td><td>YEAR ENDED</td><td>QUARTER ENDED</td><td>YEAR ENDED</td></tr>
      <tr><td>31.03.2026</td><td>31.12.2025</td><td>31.03.2025</td><td>31.03.2026</td><td>31.03.2025</td><td>31.03.2026</td><td>31.12.2025</td><td>31.03.2025</td><td>31.03.2026</td><td>31.03.2025</td></tr>
      <tr><td>1</td><td></td><td>Revenue from operations</td><td>18,351.65</td><td>21,892.16</td><td>27,259.63</td><td>83,791.62</td><td>88,649.13</td><td>42,481.12</td><td>44,051.07</td><td>63,158.42</td><td>152,530.81</td><td>162,868.75</td></tr>
      <tr><td>3</td><td></td><td>Total income</td><td>19,881.84</td><td>22,468.85</td><td>28,153.29</td><td>87,286.93</td><td>90,710.66</td><td>44,872.34</td><td>44,319.09</td><td>64,277.25</td><td>175,210.04</td><td>164,384.94</td></tr>
      <tr><td>4</td><td></td><td>Expenses</td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
      <tr><td>9</td><td></td><td>Profit / (Loss) before tax from continuing operations</td><td>(1,663.62)</td><td>(1,726.93)</td><td>1,189.68</td><td>(5,409.22)</td><td>(12,343.05)</td><td>(526.27)</td><td>(2,757.47)</td><td>3,557.47</td><td>(3,364.82)</td><td>6,413.45</td></tr>
    </table>
    <table>
      <tr><td>No.</td><td>Particulars</td><td>Standalone (Rs.in lakh, except per share data)</td><td>Consolidated</td></tr>
      <tr><td>Quarter ended</td><td>Year ended</td><td>Quarter ended</td><td>Year ended</td></tr>
      <tr><td>31.03.2026</td><td>31.12.2025</td><td>31.03.2025</td><td>31.03.2026</td><td>31.03.2025</td><td>31.03.2026</td><td>31.12.2025</td><td>31.03.2025</td><td>31.03.2026</td><td>31.03.2025</td></tr>
      <tr><td></td><td>-Basic Rs.</td><td>(0.29)</td><td>(0.69)</td><td>0.86</td><td>(1.74)</td><td>(4.41)</td><td>4.59</td><td>(1.91)</td><td>(0.04)</td><td>1.46</td><td>(8.03)</td></tr>
      <tr><td></td><td>-Diluted Rs.</td><td>(0.29)</td><td>(0.69)</td><td>0.85</td><td>(1.74)</td><td>(4.41)</td><td>4.54</td><td>(1.91)</td><td>(0.04)</td><td>1.44</td><td>(8.03)</td></tr>
    </table>
    """

    payload = payload_from_ocr_markdown_tables("Rs. in Lakhs " + markdown)
    rows = {row["label"]: row["values"] for row in payload["financial_rows"] if row.get("values")}

    assert rows["Revenue"]["Q4 FY26"] == "424.81"
    assert rows["Revenue"]["FY26"] == "1525.31"
    assert rows["EPS (Basic)"]["Q4 FY26"] == "4.59"
    assert rows["EPS (Diluted)"]["FY26"] == "1.44"


def test_packet_level_lakh_unit_scales_pnl_without_page_unit() -> None:
    markdown = """
    <table>
      <tr><td colspan="7">Standalone Results</td></tr>
      <tr><td></td><td></td><td>31-Mar-26</td><td>31-Dec-25</td><td>31-Mar-25</td><td>31-Mar-26</td><td>31-Mar-25</td></tr>
      <tr><td>(I)</td><td>Revenue from Operations</td><td>268.59</td><td>1,344.12</td><td>2,269.36</td><td>5,908.11</td><td>7,429.11</td></tr>
      <tr><td>(II)</td><td>Other Income</td><td>37.92</td><td>67.94</td><td>143.22</td><td>248.61</td><td>274.31</td></tr>
      <tr><td>(III)</td><td>Total Income</td><td>806.51</td><td>1,612.07</td><td>2,712.88</td><td>6,156.78</td><td>7,733.62</td></tr>
    </table>
    <table>
      <tr><td colspan="3">(Rs in lacs), unless stated otherwise</td></tr>
      <tr><td>Particulars</td><td>31st March -2026</td><td>31st March -2025</td></tr>
      <tr><td>Cash and cash equivalents</td><td>2,510.00</td><td>5,006.69</td></tr>
    </table>
    """

    payload = payload_from_ocr_markdown_tables(markdown)
    rows = {row["label"]: row["values"] for row in payload["financial_rows"] if row.get("values")}

    assert payload["source_currency_unit"] == "Rs in Lakhs"
    assert rows["Revenue"]["Q4 FY26"] == "7.69"
    assert rows["Revenue"]["Q3 FY26"] == "15.44"
    assert rows["Revenue"]["FY26"] == "59.08"


def test_bs_cf_compact_rows_keep_both_fy_columns() -> None:
    markdown = """
    <table>
      <tr><td colspan="3">Standalone Statement of Assets and Liabilities</td></tr>
      <tr><td colspan="3">(Rs in lacs), unless stated otherwise</td></tr>
      <tr><td>Particulars</td><td>Audited</td><td>Audited</td></tr>
      <tr><td>31st March -2026</td><td>31st March -2025</td></tr>
      <tr><td>Cash and cash equivalents</td><td>2,510.00</td><td>5,006.69</td></tr>
      <tr><td>TOTAL ASSETS</td><td>11,813.19</td><td>11,610.98</td></tr>
    </table>
    <table>
      <tr><td colspan="4">Statement of cash flows</td></tr>
      <tr><td colspan="4">(Rs in lacs), unless stated otherwise</td></tr>
      <tr><td>Sr No</td><td>Particulars</td><td>For the Period ended 31st March 2026</td><td>For the Period ended 31st March 2025</td></tr>
      <tr><td>Net cash flow from operating activities</td><td>3,376.14</td><td>2,787.96</td></tr>
    </table>
    """

    payload = payload_from_ocr_markdown_tables(markdown)
    bs_rows = payload["balance_sheet_variables"][0]["rows"]
    cash = next(row for row in bs_rows if row["label"] == "Cash and cash equivalents")
    cf = payload["cash_flow_variables"][0]

    assert cash["values"] == {"FY26": "25.1", "FY25": "50.07"}
    assert cf["values"] == {"FY26": "33.76", "FY25": "27.88"}


def test_bs_cf_filters_pnl_reclassification_note_rows() -> None:
    extraction = {
        "balance_sheet_variables": [
            {
                "section": "Variables",
                "rows": [
                    {"label": "Cost of materials consumed", "values": {"FY24": "26253"}},
                    {"label": "Purchases of stock-in-trade", "values": {"FY24": "41326"}},
                    {"label": "Other direct costs", "values": {"FY24": "27388"}},
                    {"label": "Total", "values": {"FY24": "94967"}},
                ],
            }
        ],
        "cash_flow_variables": [
            {
                "label": "Net cash inflow (outflow) from operating activities",
                "values": {"FY26": "-5354", "FY24": "16547"},
            },
            {
                "label": "Net cash inflow (outflow) from investing activities",
                "values": {"FY26": "36881", "FY24": "-5052"},
            },
            {
                "label": "Net cash inflow (outflow) from financing activities",
                "values": {"FY26": "-33021", "FY24": "-5085"},
            },
        ],
    }

    rows = build_bs_cf_rows(extraction)
    labels = [str(row.get("label") or "") for row in rows]

    assert "Cost of materials consumed" not in labels
    assert "Purchases of stock-in-trade" not in labels
    assert "Other direct costs" not in labels
    assert "Variables" not in labels
    assert any("operating activities" in label.lower() for label in labels)


def test_astral_million_unit_finance_and_segment_columns() -> None:
    markdown = """
    CONSOLIDATED AUDITED FINANCIAL RESULTS FOR THE QUARTER AND YEAR ENDED MARCH 31, 2026
    (Rs. In Million)
    <table>
      <tr><td>Particulars</td><td colspan="3">Quarter ended</td><td colspan="2">Year ended</td></tr>
      <tr><td></td><td>March 31, 2026</td><td>December 31, 2025</td><td>March 31, 2025</td><td>March 31, 2026</td><td>March 31, 2025</td></tr>
      <tr><td>Revenue from Operations</td><td>20,885</td><td>15,415</td><td>16,814</td><td>65,686</td><td>58,324</td></tr>
      <tr><td>Other Income</td><td>173</td><td>95</td><td>88</td><td>473</td><td>413</td></tr>
      <tr><td>Total Income</td><td>21,058</td><td>15,510</td><td>16,902</td><td>66,159</td><td>58,737</td></tr>
      <tr><td>Expenses</td><td></td><td></td><td></td><td></td><td></td></tr>
      <tr><td>Cost of materials consumed</td><td>11,634</td><td>9,207</td><td>9,316</td><td>38,527</td><td>34,511</td></tr>
      <tr><td>b. Purchases of traded goods</td><td>381</td><td>277</td><td>288</td><td>1,155</td><td>959</td></tr>
      <tr><td>c. Changes in inventories of finished goods, work-in-progress and traded goods</td><td>467</td><td>-236</td><td>584</td><td>-171</td><td>-278</td></tr>
      <tr><td>Employee Benefit Expense</td><td>1,542</td><td>1,477</td><td>1,331</td><td>5,904</td><td>5,179</td></tr>
      <tr><td>i. Borrowing Cost</td><td>74</td><td>87</td><td>81</td><td>319</td><td>333</td></tr>
      <tr><td>ii. Exchange Fluctuation</td><td>161</td><td>39</td><td>15</td><td>325</td><td>80</td></tr>
      <tr><td>Depreciation</td><td>740</td><td>734</td><td>648</td><td>2,916</td><td>2,434</td></tr>
      <tr><td>Other expenses</td><td>3,032</td><td>2,317</td><td>2,276</td><td>9,652</td><td>8,494</td></tr>
      <tr><td>Total Expenses</td><td>18,031</td><td>13,902</td><td>14,539</td><td>58,627</td><td>51,712</td></tr>
      <tr><td>Profit before exceptional items and tax</td><td>3,027</td><td>1,608</td><td>2,364</td><td>7,532</td><td>7,025</td></tr>
      <tr><td>Exceptional Items</td><td>61</td><td>165</td><td>-</td><td>226</td><td>-</td></tr>
      <tr><td>Profit Before Tax</td><td>2,966</td><td>1,443</td><td>2,364</td><td>7,306</td><td>7,025</td></tr>
      <tr><td>Total tax expense</td><td>836</td><td>366</td><td>583</td><td>1,959</td><td>1,836</td></tr>
      <tr><td>PAT</td><td>2,130</td><td>1,077</td><td>1,781</td><td>5,347</td><td>5,189</td></tr>
      <tr><td>Basic</td><td>7.93</td><td>4.01</td><td>6.67</td><td>19.97</td><td>19.50</td></tr>
    </table>
    CONSOLIDATED CASH FLOW STATEMENT (Rs. In Million)
    <table>
      <tr><td>Particulars</td><td>Year ended March 31, 2026</td><td>Year ended March 31, 2025</td></tr>
      <tr><td>Net cash flow from operating activities</td><td>11,170</td><td>6,296</td></tr>
      <tr><td>Net cash flow from investing activities</td><td>-5,065</td><td>-5,126</td></tr>
      <tr><td>Net cash flow from financing activities</td><td>-3,282</td><td>-1,183</td></tr>
    </table>
    CONSOLIDATED AUDITED SEGMENTWISE REVENUE, RESULTS, ASSETS AND LIABILITIES FOR THE QUARTER AND YEAR ENDED MARCH 31, 2026
    (Rs. In Million)
    <table>
      <tr><th rowspan="3">Sr. No.</th><th rowspan="3">Segment Information</th><th colspan="3">Quarter ended</th><th colspan="2">Year ended</th></tr>
      <tr><th>March 31, 2026</th><th>December 31, 2025</th><th>March 31, 2025</th><th>March 31, 2026</th><th>March 31, 2025</th></tr>
      <tr><th>(Audited)</th><th>(Unaudited)</th><th>(Audited)</th><th>(Audited)</th><th>(Audited)</th></tr>
      <tr><td>1</td><td>Segment Revenue</td><td></td><td></td><td></td><td></td><td></td></tr>
      <tr><td>a</td><td>Plumbing</td><td>15,342</td><td>10,720</td><td>12,266</td><td>46,787</td><td>41,963</td></tr>
      <tr><td>b</td><td>Paints and Adhesives</td><td>5,543</td><td>4,695</td><td>4,548</td><td>18,899</td><td>16,361</td></tr>
      <tr><td></td><td>Income from Operations</td><td>20,885</td><td>15,415</td><td>16,814</td><td>65,686</td><td>58,324</td></tr>
      <tr><td>2</td><td>Segment Results</td><td></td><td></td><td></td><td></td><td></td></tr>
      <tr><td>a</td><td>Plumbing</td><td>2,923</td><td>1,386</td><td>1,998</td><td>6,869</td><td>6,126</td></tr>
      <tr><td>b</td><td>Paints and Adhesives</td><td>228</td><td>297</td><td>414</td><td>1,034</td><td>1,150</td></tr>
    </table>
    """

    payload = payload_from_ocr_markdown_tables(markdown)
    normalized, source_unit, display_unit, _warnings = normalize_extraction_units(
        payload,
        company="Astral Limited",
        announcement_date="18-05-2026",
    )
    pl_rows = {row["label"]: row["values"] for row in build_pl_rows(normalized["financial_rows"]) if row_has_value(row)}
    cash_rows = {row["label"]: row["values"] for row in build_bs_cf_rows(normalized) if row_has_value(row)}
    segment_rows = build_segment_rows(normalized)

    assert source_unit == "Rs in Millions"
    assert display_unit == "Rs in Cr"
    assert normalized["statement_basis"] == "consolidated"
    assert pl_rows["Revenue"]["Q4 FY26"] == "2088.5"
    assert pl_rows["Revenue"]["FY26"] == "6568.6"
    assert pl_rows["Gross Profit"]["Q4 FY26"] == "840.3"
    assert pl_rows["EBITDA"]["Q4 FY26"] == "382.9"
    assert pl_rows["Finance costs"]["Q4 FY26"] == "23.5"
    assert pl_rows["Profit Before Tax"]["Q3 FY26"] == "144.3"
    assert pl_rows["Total tax expense"]["Q3 FY26"] == "36.6"
    assert pl_rows["PAT"]["Q4 FY26"] == "213"
    assert pl_rows["EPS (Basic)"]["Q4 FY26"] == "7.93"
    assert cash_rows["Net cash inflow (outflow) from operating activities"]["FY26"] == "1117"
    assert cash_rows["Net cash inflow (outflow) from investing activities"]["FY26"] == "-506.5"
    assert cash_rows["Net cash inflow (outflow) from financing activities"]["FY26"] == "-328.2"
    assert _segment_metric_value(segment_rows, "Plumbing", "Revenue", "Q4 FY26") == "1534.2"
    assert _segment_metric_value(segment_rows, "Paints and Adhesives", "Revenue", "Q4 FY26") == "554.3"
    assert _segment_metric_value(segment_rows, "Plumbing", "Revenue", "FY26") == "4678.7"

    gpt_style_segments = build_segment_rows(
        {
            "segment_tables": [
                {
                    "rows": [
                        {"label": "Segment Revenue - Plumbing", "values": {"Q4 FY26": "1534.2", "FY26": "4678.7"}},
                        {
                            "label": "Segment Revenue - Paints and Adhesives",
                            "values": {"Q4 FY26": "554.3", "FY26": "1889.9"},
                        },
                        {"label": "Segment Results - Plumbing", "values": {"Q4 FY26": "292.3", "FY26": "686.9"}},
                    ]
                }
            ]
        }
    )
    assert _segment_metric_value(gpt_style_segments, "Plumbing", "Revenue", "Q4 FY26") == "1534.2"
    assert _segment_metric_value(gpt_style_segments, "Paints and Adhesives", "Revenue", "FY26") == "1889.9"


def _segment_metric_value(rows: list[dict[str, object]], segment: str, metric: str, period: str) -> str:
    current_segment = ""
    for row in rows:
        label = str(row.get("label") or "")
        values = row.get("values") if isinstance(row.get("values"), dict) else {}
        if not values:
            current_segment = label
            continue
        if current_segment == segment and label == metric:
            return str(values.get(period) or "")
    return ""


def test_table_repair_engine_repairs_aryaman_revenue_pbt_pat() -> None:
    payload = {
        "company_name": "Aryaman Capital Markets Limited",
        "currency_unit": "Rs in Cr",
        "source_currency_unit": "Rs in Lakhs",
        "result_period": "Q4 FY26",
        "statement_basis": "standalone",
        "financial_rows": [
            {"label": "Revenue from Operations", "values": {"Q4 FY26": "2.69"}},
            {"label": "Other Income", "values": {"Q4 FY26": "0.38"}},
            {"label": "Total Income", "values": {"Q4 FY26": "8.07"}},
            {"label": "Total Expenses", "values": {"Q4 FY26": "3.88"}},
            {"label": "Profit Before Tax", "values": {"Q4 FY26": "0"}},
            {"label": "Total tax expense", "values": {"Q4 FY26": "-0.13"}},
            {"label": "PAT", "values": {"Q4 FY26": "0"}},
        ],
        "values_display_unit_applied": True,
        "ocr_markdown": "Standalone audited financial results Rs in lacs",
    }

    repaired = repair_financial_payload(payload, source_pdf="downloads/BSE/Aryaman_Capital_Markets_Ltd_2026-05-18.pdf")
    values = {row["label"]: row["values"] for row in repaired["financial_rows"]}
    reasons = {item["repair_reason"] for item in repaired["table_repair_metadata"]["repairs"]}

    assert values["Revenue from Operations"]["Q4 FY26"] == "7.69"
    assert values["Profit Before Tax"]["Q4 FY26"] == "4.19"
    assert values["PAT"]["Q4 FY26"] == "4.32"
    assert {"revenue_from_total_income", "pbt_from_total_income_minus_total_expenses", "pat_from_pbt_minus_tax"} <= reasons


def test_baid_standalone_lakh_values_stay_q4_and_fy_separate() -> None:
    payload = {
        "company_name": "Baid Finserv Limited",
        "currency_unit": "Rs in Cr",
        "source_currency_unit": "Rs in Lakhs",
        "statement_basis": "standalone",
        "result_period": "Q4 FY26",
        "values_display_unit_applied": True,
        "ocr_markdown": "Statement of Standalone Audited Financial Results Rs. in Lakhs",
        "financial_rows": [
            {"label": "Revenue from Operations", "values": {"Q4 FY26": "25.01", "Q3 FY26": "24.63", "Q4 FY25": "22.11", "FY26": "97.27", "FY25": "81.98"}},
            {"label": "Other Income", "values": {"Q4 FY26": "0.56", "FY26": "1.92"}},
            {"label": "Total Income", "values": {"Q4 FY26": "25.57", "FY26": "99.19"}},
            {"label": "Total Expenses", "values": {"Q4 FY26": "23.41", "FY26": "79.11"}},
            {"label": "Profit Before Tax", "values": {"Q4 FY26": "2.17", "FY26": "20.07"}},
            {"label": "Total tax expense", "values": {"Q4 FY26": "0.51", "FY26": "5.10"}},
            {"label": "PAT", "values": {"Q4 FY26": "1.66", "FY26": "14.97"}},
            {"label": "EPS (Basic)", "values": {"Q4 FY26": "0.07", "FY26": "1.15"}},
        ],
    }

    repaired = repair_financial_payload(payload)
    result = validate_financial_payload(repaired)

    assert _statement_basis(repaired) == "standalone"
    assert result.allows_images
    assert repaired["financial_rows"][0]["values"]["Q4 FY26"] == "25.01"
    assert repaired["financial_rows"][0]["values"]["FY26"] == "97.27"
    assert not any("q4_equals_fy" in issue for issue in result.issues)


def test_asahi_consolidated_only_and_q4_fy_collision_guard() -> None:
    good = {
        "company_name": "Asahi Songwon Colors Limited",
        "currency_unit": "Rs in Cr",
        "source_currency_unit": "Rs in Lakhs",
        "statement_basis": "consolidated",
        "result_period": "Q4 FY26",
        "values_display_unit_applied": True,
        "ocr_markdown": "Standalone Financial Results Consolidated Financial Results Rupees in Lakhs",
        "financial_rows": [
            {"label": "Revenue from Operations", "values": {"Q4 FY26": "144.05", "Q3 FY26": "128.00", "Q4 FY25": "118.00", "FY26": "535.48", "FY25": "480.00"}},
            {"label": "Other Income", "values": {"Q4 FY26": "1.00", "FY26": "3.00"}},
            {"label": "Total Income", "values": {"Q4 FY26": "145.05", "FY26": "538.48"}},
            {"label": "Total Expenses", "values": {"Q4 FY26": "120.00", "FY26": "480.00"}},
            {"label": "Profit Before Tax", "values": {"Q4 FY26": "25.05", "FY26": "58.48"}},
            {"label": "Total tax expense", "values": {"Q4 FY26": "5.00", "FY26": "12.00"}},
            {"label": "PAT", "values": {"Q4 FY26": "20.05", "FY26": "46.48"}},
        ],
        "segment_tables": [
            {
                "title": "Segment Results",
                "rows": [
                    {"label": "Pigments - Revenue", "values": {"Q4 FY26": "90.00", "FY26": "340.00"}},
                    {"label": "API - Revenue", "values": {"Q4 FY26": "54.05", "FY26": "195.48"}},
                ],
            }
        ],
    }
    good_result = validate_financial_payload(repair_financial_payload(good))
    assert good_result.allows_images
    assert any(row.get("label") == "Pigments" for row in build_segment_rows(good))
    assert any(row.get("label") == "API" for row in build_segment_rows(good))

    standalone = dict(good)
    standalone["statement_basis"] = "standalone"
    standalone_result = validate_financial_payload(repair_financial_payload(standalone))
    assert not standalone_result.allows_images
    assert any("consolidated_available_but_standalone_selected" in issue for issue in standalone_result.issues)

    collision = dict(good)
    collision["financial_rows"] = [
        {"label": "Revenue from Operations", "values": {"Q4 FY26": "535.48", "Q3 FY26": "128.00", "Q4 FY25": "118.00", "FY26": "535.48", "FY25": "480.00"}},
        {"label": "Profit Before Tax", "values": {"Q4 FY26": "58.48", "Q3 FY26": "20.00", "Q4 FY25": "15.00", "FY26": "58.48", "FY25": "40.00"}},
        {"label": "PAT", "values": {"Q4 FY26": "46.48", "Q3 FY26": "15.00", "Q4 FY25": "10.00", "FY26": "46.48", "FY25": "30.00"}},
    ]
    collision_result = validate_financial_payload(repair_financial_payload(collision))
    assert not collision_result.allows_images
    assert any("q4_equals_fy_column_collision" in issue for issue in collision_result.issues)


def test_validation_block_message_hides_repair_audit_details() -> None:
    extraction = {
        "company_name": "Audit Hidden Limited",
        "currency_unit": "Rs in Cr",
        "statement_basis": "standalone",
        "validation_allows_images": False,
        "validation_errors": ["q4_equals_fy_column_collision:Revenue,PAT", "revenue_from_total_income"],
        "financial_rows": [
            {"label": "Revenue", "values": {"Q4 FY26": "100", "FY26": "100"}},
            {"label": "Profit Before Tax", "values": {"Q4 FY26": "10", "FY26": "10"}},
        ],
    }

    generated = generate_financial_images(extraction, None, Path("output/_test/manual_warning"))

    assert not generated.images
    assert generated.warnings
    assert "Correct period columns" in generated.warnings[0]
    assert "Reason:" not in generated.warnings[0]
    assert "revenue_from_total_income" not in generated.warnings[0]
    assert "Revenue,PAT" not in generated.warnings[0]


def test_single_standalone_statement_is_default_output() -> None:
    extraction = {
        "company_name": "Single Statement Ltd",
        "currency_unit": "Rs in Cr",
        "statement_basis": "standalone",
        "parser_message": "Only standalone data found.",
        "ocr_markdown": "STATEMENT OF AUDITED STANDALONE FINANCIAL RESULTS",
    }

    assert _statement_basis(extraction) == "standalone"
    assert not _standalone_conflicts_with_consolidated_source(extraction)


def test_financial_validation_accepts_formula_chain_and_margins() -> None:
    extraction = {
        "company_name": "Validator Formula Ltd",
        "currency_unit": "Rs in Cr",
        "statement_basis": "consolidated",
        "result_period": "Q4 FY26",
        "ocr_markdown": "Statement of consolidated financial results Rs in Cr",
        "financial_rows": [
            {"label": "Revenue", "values": {"Q4 FY26": "100"}},
            {"label": "Cost of Production", "values": {"Q4 FY26": "60"}},
            {"label": "Employee benefits expense", "values": {"Q4 FY26": "10"}},
            {"label": "Other expenses", "values": {"Q4 FY26": "5"}},
            {"label": "Depreciation and amortisation expense", "values": {"Q4 FY26": "2"}},
            {"label": "Finance costs", "values": {"Q4 FY26": "1"}},
            {"label": "Other income", "values": {"Q4 FY26": "1"}},
            {"label": "Total tax expense", "values": {"Q4 FY26": "4"}},
        ],
    }

    result = validate_financial_payload(extraction)

    assert result.status == "ok"
    assert result.allows_images
    assert not result.issues


def test_financial_validation_accepts_revenue_minus_total_expenses_basis() -> None:
    extraction = {
        "company_name": "Validator Statutory Ltd",
        "currency_unit": "Rs in Cr",
        "statement_basis": "consolidated",
        "result_period": "Q4 FY26",
        "ocr_markdown": "Statement of consolidated financial results Rs in Cr",
        "financial_rows": [
            {"label": "Revenue from operations", "values": {"Q4 FY26": "100"}},
            {"label": "Other income", "values": {"Q4 FY26": "7"}},
            {"label": "Total income", "values": {"Q4 FY26": "107"}},
            {"label": "Expenses", "values": {}},
            {"label": "a. Cost of materials consumed", "values": {"Q4 FY26": "45"}},
            {"label": "b. Employee benefits expense", "values": {"Q4 FY26": "10"}},
            {"label": "c. Finance costs", "values": {"Q4 FY26": "3"}},
            {"label": "d. Depreciation and amortisation expense", "values": {"Q4 FY26": "4"}},
            {"label": "e. Other expenses", "values": {"Q4 FY26": "8"}},
            {"label": "Total expenses", "values": {"Q4 FY26": "70"}},
            {"label": "Profit / (Loss) before exceptional items and tax ((III) - (IV))", "values": {"Q4 FY26": "37"}},
            {"label": "Profit Before Tax", "values": {"Q4 FY26": "37"}},
            {"label": "Total tax expense", "values": {"Q4 FY26": "10"}},
            {"label": "PAT", "values": {"Q4 FY26": "27"}},
        ],
    }

    built = build_pl_rows(normalize_rows(extraction["financial_rows"]))
    pbei = next(row for row in built if row["label"] == "Profit before exceptional items, Other Income")
    assert pbei["values"]["Q4 FY26"] == "30"
    assert pbei.get("formula_basis") == "revenue_minus_total_expenses"

    result = validate_financial_payload(extraction)

    assert result.status in {"ok", "needs_review"}
    assert result.allows_images
    assert not any(issue.startswith("formula_mismatch") for issue in result.issues)


def test_financial_validation_blocks_repeated_value_vectors() -> None:
    extraction = {
        "company_name": "Repeated Vector Ltd",
        "currency_unit": "Rs in Cr",
        "statement_basis": "consolidated",
        "result_period": "Q4 FY26",
        "ocr_markdown": "Statement of consolidated financial results Rs in Cr",
        "financial_rows": [
            {"label": "Revenue", "values": {"Q4 FY26": "100", "FY26": "400"}},
            {"label": "Cost of Production", "values": {"Q4 FY26": "100", "FY26": "400"}},
            {"label": "Employee benefits expense", "values": {"Q4 FY26": "100", "FY26": "400"}},
            {"label": "Other expenses", "values": {"Q4 FY26": "100", "FY26": "400"}},
            {"label": "Depreciation", "values": {"Q4 FY26": "100", "FY26": "400"}},
            {"label": "Finance costs", "values": {"Q4 FY26": "100", "FY26": "400"}},
        ],
    }

    result = validate_financial_payload(extraction)

    assert result.status == "failed"
    assert not result.allows_images
    assert "repeated_identical_value_vector_artifact" in result.issues


def test_gpt54_schema_defaults_allow_missing_metadata_fields() -> None:
    payload = {
        "company_name": "Metadata Defaults Ltd",
        "statement_basis": "consolidated",
        "currency_unit": "Rs in Cr",
        "result_period": "Q4 FY26",
        "period_columns": ["Q4 FY26"],
        "financial_rows": [{"label": "Revenue", "values": {"Q4 FY26": "100"}}],
        "balance_sheet_variables": [],
        "cash_flow_variables": [],
        "segment_tables": [],
    }

    repaired = _apply_schema_defaults(payload)

    assert validate_gpt54_json(repaired) == []
    assert repaired["confidence"] == 0.5
    assert isinstance(repaired["warnings"], list)


def test_gpt54_schema_defaults_drop_blank_label_rows() -> None:
    payload = {
        "company_name": "Blank Label Ltd",
        "statement_basis": "consolidated",
        "currency_unit": "Rs in Cr",
        "result_period": "Q4 FY26",
        "period_columns": ["Q4 FY26"],
        "financial_rows": [
            {"label": "Revenue", "values": {"Q4 FY26": "100"}},
            {"label": "", "values": {"Q4 FY26": "bad"}},
        ],
        "balance_sheet_variables": [],
        "cash_flow_variables": [],
        "segment_tables": [],
    }

    repaired = _apply_schema_defaults(payload)

    assert validate_gpt54_json(repaired) == []
    assert len(repaired["financial_rows"]) == 1
    assert repaired["financial_rows"][0]["label"] == "Revenue"


def test_gpt54_extraction_schema_is_returned() -> None:
    schema = extraction_json_schema()

    assert schema["type"] == "object"
    assert "financial_rows" in schema["properties"]
    assert "balance_sheet_variables" in schema["properties"]
    assert "segment_tables" in schema["properties"]


def test_gpt54_schema_rows_allow_source_provenance() -> None:
    schema = extraction_json_schema()
    row_schema = schema["properties"]["financial_rows"]["items"]

    for key in ("source_page", "table_title", "statement_basis", "unit", "raw_columns", "source_confidence"):
        assert key in row_schema["properties"]


def test_llm_values_first_mode_uses_renderer_payload_without_strict_formula_block() -> None:
    schema = llm_values_first_json_schema()
    assert "pnl_image" in schema["properties"]
    payload = {
        "company_name": "Values First Ltd",
        "selected_basis": "CONSOLIDATED",
        "basis_note": "CONSOLIDATED",
        "source_unit": "Rs in Lakhs",
        "display_unit": "Rs in Cr",
        "currency": "INR",
        "periods": ["Q4 FY26", "Q3 FY26"],
        "pnl_image": {
            "title": "Values First Ltd",
            "columns": ["Q4 FY26", "Q3 FY26", "QoQ Change %"],
            "rows": [
                {"label": "Revenue", "values": {"Q4 FY26": "10", "Q3 FY26": "20", "QoQ Change %": "-50%"}, "is_bold": True},
                {"label": "Gross Profit", "values": {"Q4 FY26": "999", "Q3 FY26": "1", "QoQ Change %": "99800%"}, "is_bold": True},
                {"label": "EPS Basic", "values": {"Q4 FY26": "0.12", "Q3 FY26": "0.10", "QoQ Change %": "20%"}, "is_bold": True},
            ],
            "warnings": ["formula uncertain but render values visible"],
        },
        "bs_cf_image": {"title": "", "columns": [], "balance_sheet_rows": [], "cash_flow_rows": [], "warnings": []},
        "segment_image": {"required": False, "title": "", "columns": [], "rows": [], "warnings": []},
        "global_warnings": [],
        "render_decision": {"should_render": True, "reason": ""},
    }
    extraction = _normalize_llm_values_first_payload(payload, Path("values_first.pdf"), None, {})
    result = validate_financial_payload(extraction)

    assert result.allows_images is True
    assert result.status == "needs_review"
    assert result.metadata["strict_validation"] is False
    assert result.metadata["approved_pnl_rows"][1]["values"]["Q4 FY26"] == "999"
    assert result.metadata["approved_pnl_rows"][2]["values"]["Q4 FY26"] == "0.12"


def test_llm_values_first_maps_nine_month_and_year_columns_by_position() -> None:
    payload = {
        "company_name": "Quarter Aggregate Ltd",
        "selected_basis": "STANDALONE",
        "basis_note": "ONLY STANDALONE FOUND",
        "source_unit": "Rs in Lakhs",
        "display_unit": "Rs in Cr",
        "currency": "INR",
        "periods": [
            "Quarter ended 31.03.2026",
            "Quarter ended 31.12.2025",
            "Quarter ended 31.03.2025",
            "Nine months ended 31.03.2026",
            "Year ended 31.03.2025",
            "Year ended 31.03.2026",
        ],
        "pnl_image": {
            "title": "Standalone Results",
            "columns": [
                "Quarter ended 31.03.2026",
                "Quarter ended 31.12.2025",
                "Quarter ended 31.03.2025",
                "Nine months ended 31.03.2026",
                "Year ended 31.03.2025",
                "Year ended 31.03.2026",
            ],
            "rows": [
                {
                    "label": "Revenue from operations",
                    "values": {
                        "Quarter ended 31.03.2026": "25.6529",
                        "Quarter ended 31.12.2025": "24.2006",
                        "Quarter ended 31.03.2025": "38.3669",
                        "Nine months ended 31.03.2026": "75.1924",
                        "Year ended 31.03.2025": "100.8453",
                        "Year ended 31.03.2026": "131.9951",
                    },
                    "is_bold": True,
                },
                {
                    "label": "Purchase of Trade Goods",
                    "values": {
                        "Quarter ended 31.03.2026": "0",
                        "Quarter ended 31.12.2025": "0",
                        "Quarter ended 31.03.2025": "0",
                        "Nine months ended 31.03.2026": "0",
                        "Year ended 31.03.2025": "0",
                        "Year ended 31.03.2026": "31.954",
                    },
                    "source_note": "The source PDF shows '-' for blank cells; treated as 0 by the model.",
                },
                {
                    "label": "Exceptional Items",
                    "values": {
                        "Quarter ended 31.03.2026": "0",
                        "Quarter ended 31.12.2025": "0",
                    },
                    "source_note": "Exceptional items shown as '-' in the PDF.",
                },
                {
                    "label": "EPS Basic",
                    "values": {
                        "Quarter ended 31.03.2026": "(0.80)",
                        "Quarter ended 31.12.2025": "0.15",
                        "Quarter ended 31.03.2025": "0.75",
                        "Nine months ended 31.03.2026": "0.94",
                        "Year ended 31.03.2025": "0.14",
                        "Year ended 31.03.2026": "2.87",
                    },
                    "is_bold": True,
                },
            ],
        },
        "bs_cf_image": {"title": "", "columns": [], "balance_sheet_rows": [], "cash_flow_rows": [], "warnings": []},
        "segment_image": {"required": False, "title": "", "columns": [], "rows": [], "warnings": []},
        "global_warnings": [],
        "render_decision": {"should_render": True, "reason": ""},
    }

    extraction = _normalize_llm_values_first_payload(payload, Path("quarter_aggregate.pdf"), None, {})
    columns = extraction["approved_pnl_columns"]
    labels = [column["label"] for column in columns]

    assert labels == ["Q4 FY26", "Q3 FY26", "Q4 FY25", "9M FY26", "FY26", "FY25"]
    assert labels.count("FY26") == 1
    rendered = rows_to_table(extraction["approved_pnl_rows"], columns, skip_margin_changes=True)
    by_label = {row[0]: row for row in rendered}
    index_by_label = {column["label"]: index + 1 for index, column in enumerate(columns)}
    assert by_label["Revenue from operations"][index_by_label["9M FY26"]] == "75"
    assert by_label["Revenue from operations"][index_by_label["FY26"]] == "100"
    assert by_label["Revenue from operations"][index_by_label["FY25"]] == "131"
    assert by_label["Purchase of Trade Goods"][index_by_label["Q4 FY26"]] == "-"
    assert by_label["Exceptional Items"][index_by_label["Q4 FY26"]] == "-"
    assert by_label["EPS Basic"][index_by_label["FY25"]] == "2.87"


def test_llm_values_first_generic_pnl_order_company_cleanup_and_bs_reconcile() -> None:
    payload = {
        "company_name": "02 Example Limited",
        "selected_basis": "CONSOLIDATED",
        "basis_note": "CONSOLIDATED",
        "source_unit": "Rs in Lakhs",
        "display_unit": "Rs in Cr",
        "currency": "INR",
        "periods": ["FY26", "FY25"],
        "pnl_image": {
            "title": "Example",
            "columns": ["FY26", "FY25"],
            "rows": [
                {"label": "Revenue from operations", "values": {"FY26": "126.55", "FY25": "157.89"}, "is_bold": True},
                {
                    "label": "Profit before exceptional items",
                    "values": {"FY26": "1.24", "FY25": "6.41"},
                    "source_note": "PDF row is Profit/(loss) before tax and Exceptional Items.",
                    "is_bold": True,
                },
                {"label": "Other Income", "values": {"FY26": "2.85", "FY25": "4.69"}},
                {"label": "Exceptional items", "values": {"FY26": "0.00", "FY25": "0.00"}},
                {"label": "Profit Before Tax", "values": {"FY26": "1.24", "FY25": "6.41"}, "is_bold": True},
                {"label": "PAT", "values": {"FY26": "1.24", "FY25": "6.41"}, "is_bold": True},
                {"label": "EPS Basic", "values": {"FY26": "0.35", "FY25": "3.18"}, "is_bold": True},
            ],
        },
        "bs_cf_image": {
            "title": "BS CF",
            "columns": ["FY26", "FY25"],
            "balance_sheet_rows": [
                {"label": "Total Assets", "values": {"FY26": "126.55", "FY25": "157.89"}, "is_bold": True},
                {"label": "Total Equity", "values": {"FY26": "111.72", "FY25": "112.37"}, "is_bold": True},
                {"label": "Total Liabilities", "values": {"FY26": "14.3", "FY25": "45.0"}, "is_bold": True},
                {"label": "Total Equity and Liabilities", "values": {"FY26": "126.55", "FY25": "157.89"}, "is_bold": True},
                {"label": "Total current assets", "values": {"FY26": "17 27.691"}, "is_bold": True},
            ],
            "cash_flow_rows": [],
            "warnings": [],
        },
        "segment_image": {"required": False, "title": "", "columns": [], "rows": [], "warnings": []},
        "global_warnings": [],
        "render_decision": {"should_render": True, "reason": ""},
    }

    extraction = _normalize_llm_values_first_payload(payload, Path("02_Example_Limited.pdf"), None, {})
    assert extraction["company_name"] == "Example Limited"

    pnl_labels = [row["label"] for row in extraction["approved_pnl_rows"] if row.get("type") != "section"]
    assert pnl_labels.index("Other Income") < pnl_labels.index("Profit before tax and exceptional items as per PDF")
    assert pnl_labels.index("Total Income") < pnl_labels.index("Profit before tax and exceptional items as per PDF")

    pnl_table = rows_to_table(extraction["approved_pnl_rows"], extraction["approved_pnl_columns"], skip_margin_changes=True)
    by_label = {row[0]: row for row in pnl_table}
    assert by_label["Total Income"][1] == "129"
    assert by_label["Profit before tax and exceptional items as per PDF"][1] == "1.24"

    bs_table = rows_to_table(extraction["approved_bs_cf_rows"], extraction["approved_bs_cf_columns"], skip_margin_changes=True)
    bs_by_label = {row[0]: row for row in bs_table}
    assert bs_by_label["Total Liabilities"][1] == "14"
    assert bs_by_label["Total Liabilities"][2] == "45"
    assert bs_by_label["Total current assets"][1] == "1727"
    assert format_display_cell("Total current assets", "17 27.691") == "1727"


def test_llm_values_first_recomputes_gross_profit_ebitda_and_margin() -> None:
    payload = {
        "company_name": "Formula Rows Ltd",
        "selected_basis": "STANDALONE",
        "basis_note": "ONLY STANDALONE FOUND",
        "source_unit": "Rs in Millions",
        "display_unit": "Rs in Cr",
        "currency": "INR",
        "periods": ["FY26"],
        "pnl_image": {
            "title": "Formula Rows",
            "columns": ["FY26"],
            "rows": [
                {"label": "Revenue from operations", "values": {"FY26": "9188.805"}, "is_bold": True},
                {"label": "Cost of materials consumed", "values": {"FY26": "9158.882"}},
                {"label": "Changes in inventories", "values": {"FY26": "(63.177)"}},
                {"label": "Project execution expenses", "values": {"FY26": "10"}},
                {"label": "Gross Profit", "values": {"FY26": "(107.43)"}, "is_bold": True},
                {"label": "Gross Profit Margin %", "values": {"FY26": "99%"}, "is_bold": True},
                {"label": "Employee benefits expense", "values": {"FY26": "2.48"}},
                {"label": "Other Expenses", "values": {"FY26": "3.16"}},
                {"label": "EBITDA", "values": {"FY26": "(113.07)"}, "is_bold": True},
                {"label": "EBITDA Margin", "values": {"FY26": "2.07%"}, "is_bold": True},
            ],
        },
        "bs_cf_image": {"title": "", "columns": [], "balance_sheet_rows": [], "cash_flow_rows": [], "warnings": []},
        "segment_image": {"required": False, "title": "", "columns": [], "rows": [], "warnings": []},
        "global_warnings": [],
        "render_decision": {"should_render": True, "reason": ""},
    }

    extraction = _normalize_llm_values_first_payload(payload, Path("formula_rows.pdf"), None, {})
    table = rows_to_table(extraction["approved_pnl_rows"], extraction["approved_pnl_columns"], skip_margin_changes=True)
    by_label = {row[0]: row for row in table}

    assert by_label["Gross Profit"][1] == "83"
    assert by_label["Gross Profit Margin %"][1] == "0.9%"
    assert by_label["EBITDA"][1] == "77"
    assert by_label["EBITDA Margin"][1] == "0.84%"


def test_llm_values_first_revenue_order_total_income_note_and_liabilities_row() -> None:
    payload = {
        "company_name": "03 Generic Components Ltd",
        "selected_basis": "CONSOLIDATED",
        "basis_note": "CONSOLIDATED",
        "source_unit": "Rs in Lakhs",
        "display_unit": "Rs in Cr",
        "currency": "INR",
        "periods": ["Q4 FY26", "FY26", "FY25"],
        "pnl_image": {
            "title": "Generic Components",
            "columns": ["Q4 FY26", "FY26", "FY25"],
            "rows": [
                {"label": "Other Income", "values": {"FY26": "2.85", "FY25": "4.69"}},
                {"label": "EPS Basic", "values": {"Q4 FY26": "0.08", "FY26": "0.35", "FY25": "3.18"}},
                {
                    "label": "Revenue (Net sales / income from operations)",
                    "values": {"Q4 FY26": "0.92", "FY26": "8.42", "FY25": "17.12"},
                    "is_bold": True,
                },
                {"label": "Total Income", "values": {"Q4 FY26": "3.03", "FY26": "11.27", "FY25": "21.81"}, "is_bold": True},
                {"label": "Cost of Goods Consumed", "values": {"FY26": "4.15", "FY25": "10.20"}},
                {"label": "Changes in Inventories", "values": {"FY26": "(0.65)", "FY25": "(1.64)"}},
                {"label": "Employee benefits expense", "values": {"FY26": "2.48", "FY25": "1.30"}},
                {"label": "Other expenses", "values": {"FY26": "3.83", "FY25": "5.19"}},
                {"label": "Gross Profit", "values": {"FY26": "4.92", "FY25": "8.56"}, "is_bold": True},
                {"label": "EBITDA", "values": {"FY26": "1.09", "FY25": "3.37"}, "is_bold": True},
            ],
        },
        "bs_cf_image": {
            "title": "BS CF",
            "columns": ["FY26", "FY25"],
            "balance_sheet_rows": [
                {"label": "Total Assets", "values": {"FY26": "126.55", "FY25": "127.38"}, "is_bold": True},
                {"label": "Total Equity", "values": {"FY26": "111.72", "FY25": "81.85"}, "is_bold": True},
                {"label": "Total Non-current liabilities", "values": {"FY26": "0.11", "FY25": "0.00"}},
                {"label": "Total Current liabilities", "values": {"FY26": "14.71", "FY25": "45.52"}},
                {"label": "Total Equity and Liabilities", "values": {"FY26": "126.55", "FY25": "127.38"}, "is_bold": True},
            ],
            "cash_flow_rows": [],
            "warnings": [],
        },
        "segment_image": {"required": False, "title": "", "columns": [], "rows": [], "warnings": []},
        "global_warnings": [],
        "render_decision": {"should_render": True, "reason": ""},
    }

    extraction = _normalize_llm_values_first_payload(payload, Path("03_Generic_Components.pdf"), None, {})
    labels = [row["label"] for row in extraction["approved_pnl_rows"] if row.get("type") != "section"]
    assert labels.index("Revenue") == 0
    assert labels.index("Other income / write-back included in Total Income") < labels.index("Total Income")
    assert labels.index("Cost of Goods Consumed") < labels.index("Employee benefits expense")
    assert labels.index("Revenue") < labels.index("EPS Basic")

    pnl_table = rows_to_table(extraction["approved_pnl_rows"], extraction["approved_pnl_columns"], skip_margin_changes=True)
    pnl_by_label = {row[0]: row for row in pnl_table}
    assert pnl_by_label["Other income/write-back"][1] == "2.11"
    assert pnl_by_label["Cost of Goods Consumed"][2] == "4.15"
    assert pnl_by_label["Changes in inventories"][3] == "(1.64)"

    bs_table = rows_to_table(extraction["approved_bs_cf_rows"], extraction["approved_bs_cf_columns"], skip_margin_changes=True)
    bs_by_label = {row[0]: row for row in bs_table}
    assert bs_by_label["Total Current liabilities"][1] == "14"
    assert bs_by_label["Total Liabilities"][1] == "14"
    assert bs_by_label["Total Liabilities"][2] == "45"


def test_legacy_company_patches_disabled_by_default() -> None:
    payload = {
        "company_name": "Panacea Biotec Limited",
        "board_meeting_date": "2026-05-31",
        "statement_basis": "consolidated",
        "currency_unit": "Rs in Lakhs",
        "source_currency_unit": "Rs in Lakhs",
        "result_period": "Q4 FY26",
        "period_columns": ["Q4 FY26"],
        "financial_rows": [
            {
                "label": "Revenue from operations",
                "values": {"Q4 FY26": "123.45"},
                "source_page": 9,
                "table_title": "Consolidated financial results",
                "statement_basis": "consolidated",
                "unit": "Rs in Lakhs",
            }
        ],
        "balance_sheet_variables": [],
        "cash_flow_variables": [],
        "segment_tables": [],
        "key_variables": [],
        "confidence": 0.95,
        "warnings": [],
        "parser_message": "test",
    }

    normalized = _normalize_gpt_payload(payload, None, {"source_currency_unit": "Rs in Lakhs"})

    assert normalized.get("legacy_company_patch_mode") is False
    assert not any("verified_correction_applied" in str(item) for item in normalized.get("warnings", []))
    assert normalized["financial_rows"][0]["values"]["Q4 FY26"] == "123.45"


def test_canonical_cell_model_flags_eps_conversion() -> None:
    extraction = {
        "company_name": "EPS Guard Ltd",
        "statement_basis": "consolidated",
        "currency_unit": "Rs in Cr",
        "financial_rows": [
            {
                "label": "EPS Basic",
                "values": {"Q4 FY26": "0.01"},
                "source_page": 4,
                "table_title": "Consolidated P&L",
                "statement_basis": "consolidated",
            }
        ],
    }
    annotated = annotate_extraction_with_cell_model(extraction)
    annotated["canonical_financial_cells"][0]["raw_value"] = "1.00"
    annotated["canonical_financial_cells"][0]["normalized_value"] = "0.01"

    issues = canonical_cell_issues(annotated)

    assert "eps_converted:Q4 FY26:EPS Basic" in issues


def test_formula_mismatch_hard_blocks_all_rendering() -> None:
    extraction = {
        "company_name": "Formula Block Ltd",
        "statement_basis": "consolidated",
        "currency_unit": "Rs in Cr",
        "source_currency_unit": "Rs in Cr",
        "result_period": "Q4 FY26",
        "period_columns": ["Q4 FY26"],
        "financial_rows": [
            {"label": "Revenue", "values": {"Q4 FY26": "100"}},
            {"label": "Cost of materials consumed", "values": {"Q4 FY26": "10"}},
            {"label": "Gross Profit", "values": {"Q4 FY26": "999"}},
            {"label": "Employee benefits expense", "values": {"Q4 FY26": "5"}},
            {"label": "Other expenses", "values": {"Q4 FY26": "5"}},
            {"label": "EBITDA", "values": {"Q4 FY26": "989"}},
            {"label": "Depreciation", "values": {"Q4 FY26": "1"}},
            {"label": "Finance Cost", "values": {"Q4 FY26": "1"}},
            {"label": "Profit before exceptional items", "values": {"Q4 FY26": "987"}},
            {"label": "Other income", "values": {"Q4 FY26": "1"}},
            {"label": "Profit Before Tax", "values": {"Q4 FY26": "988"}},
            {"label": "Total tax expense", "values": {"Q4 FY26": "2"}},
            {"label": "PAT", "values": {"Q4 FY26": "986"}},
        ],
        "balance_sheet_variables": [],
        "cash_flow_variables": [],
        "segment_tables": [],
    }

    result = validate_financial_payload(extraction)

    assert any(issue.startswith("formula_mismatch:") for issue in result.issues)
    assert result.allows_images is False
    assert result.metadata["renderable_sections"] == []


def test_renderer_refuses_unapproved_pnl_input() -> None:
    try:
        render_pl_image(
            {
                "company_name": "Renderer Gate Ltd",
                "result_period": "Q4 FY26",
                "financial_rows": [{"label": "Revenue", "values": {"Q4 FY26": "100"}}],
            },
            None,
            Path("output") / "renderer_gate_test",
            "Rs in Cr",
            approved_rows=[{"label": "Revenue", "values": {"Q4 FY26": "100"}, "style": "key"}],
        )
    except RenderBlockedError:
        return
    raise AssertionError("renderer accepted unapproved P&L input")


def test_financial_auditor_failure_blocks_rendering() -> None:
    extraction = {
        "company_name": "Auditor Block Ltd",
        "statement_basis": "consolidated",
        "currency_unit": "Rs in Cr",
        "result_period": "Q4 FY26",
        "period_columns": ["Q4 FY26"],
        "financial_rows": [
            {"label": "Revenue", "values": {"Q4 FY26": "100"}},
            {"label": "Profit Before Tax", "values": {"Q4 FY26": "10"}},
            {"label": "PAT", "values": {"Q4 FY26": "8"}},
        ],
        "balance_sheet_variables": [],
        "cash_flow_variables": [],
        "segment_tables": [],
        "financial_auditor": {
            "validation_status": "FAIL",
            "failed_checks": ["exceptional_item_visible_but_missing"],
            "correct_values": {},
            "source_pages": {},
            "repair_needed": ["rerun selected P&L page"],
        },
        "auditor_validation_status": "FAIL",
    }

    result = validate_financial_payload(extraction)

    assert not result.allows_images
    assert any("auditor_validation_failed:exceptional_item_visible_but_missing" == issue for issue in result.issues)


def test_gpt54_date_period_headers_are_normalized_by_statement_type() -> None:
    payload = {
        "company_name": "Date Header Ltd",
        "board_meeting_date": "2026-05-28",
        "statement_basis": "consolidated",
        "currency_unit": "Rs in Cr",
        "result_period": "31-Mar-26",
        "period_columns": ["31-Dec-25", "31-Mar-25", "31-Mar-25 (FY)", "31-Mar-26", "31-Mar-26 (FY)"],
        "financial_rows": [
            {
                "label": "Revenue",
                "values": {
                    "31-Dec-25": "90",
                    "31-Mar-25": "80",
                    "31-Mar-25 (FY)": "320",
                    "31-Mar-26": "100",
                    "31-Mar-26 (FY)": "400",
                },
            }
        ],
        "balance_sheet_variables": [
            {
                "section": "Assets",
                "rows": [{"label": "Trade receivables", "values": {"31-Mar-26": "50", "31-Mar-25": "40"}}],
            }
        ],
        "cash_flow_variables": [
            {"label": "Net cash inflow (outflow) from operating activities", "values": {"31-Mar-26": "8", "31-Mar-25": "5"}}
        ],
        "segment_tables": [
            {"title": "Segments", "rows": [{"label": "Revenue", "values": {"31-Dec-25": "20", "31-Mar-26": "25"}}]}
        ],
        "key_variables": [],
        "confidence": 0.9,
        "warnings": [],
        "parser_message": "ok",
    }

    normalized = _normalize_gpt_payload(payload, None, {"ocr_markdown": "Rs in Cr"})

    assert normalized["result_period"] == "Q4 FY26"
    assert normalized["period_columns"] == ["Q3 FY26", "Q4 FY25", "FY25", "Q4 FY26", "FY26"]
    assert normalized["financial_rows"][0]["values"] == {
        "Q3 FY26": "90",
        "Q4 FY25": "80",
        "FY25": "320",
        "Q4 FY26": "100",
        "FY26": "400",
    }
    assert normalized["balance_sheet_variables"][0]["rows"][0]["values"] == {"FY26": "50", "FY25": "40"}
    assert normalized["cash_flow_variables"][0]["values"] == {"FY26": "8", "FY25": "5"}
    assert normalized["segment_tables"][0]["rows"][0]["values"] == {"Q3 FY26": "20", "Q4 FY26": "25"}


def test_gpt54_prefers_prescaled_ocr_table_over_collapsed_gpt_rows() -> None:
    payload = {
        "company_name": "Collapsed GPT Ltd",
        "board_meeting_date": "2026-05-28",
        "statement_basis": "standalone",
        "currency_unit": "Rs. In Lakhs",
        "result_period": "FY26",
        "period_columns": ["Q4 FY26", "Q3 FY26", "Q4 FY25"],
        "financial_rows": [
            {"label": "Revenue from Operations", "values": {"Q4 FY26": "9,726.58", "Q3 FY26": "2,462.88", "Q4 FY25": "2,211.43"}},
            {"label": "Total tax expense", "values": {"Q4 FY26": "509.81", "Q3 FY26": "170.97", "Q4 FY25": "141.88"}},
        ],
        "balance_sheet_variables": [
            {"section": "Assets", "rows": [{"label": "Cash and cash equivalents", "values": {"31-Mar-26": "709.10", "31-Mar-25": "1131.45"}}]}
        ],
        "cash_flow_variables": [
            {"label": "Net cash flows from (used in) operating activities", "values": {"2026": "(3326.95)", "2025": "192.81"}}
        ],
        "segment_tables": [],
        "key_variables": [],
        "confidence": 0.9,
        "warnings": [],
        "parser_message": "standalone only",
    }
    ocr_payload = {
        "ocr_markdown": "Statement of Standalone Audited Financial Results Rs. In Lakhs",
        "table_payload": {
            "company_name": "Collapsed GPT Ltd",
            "currency_unit": "Rs in Cr",
            "source_currency_unit": "Rs in Lakhs",
            "statement_basis": "standalone",
            "result_period": "Q4 FY26",
            "values_display_unit_applied": True,
            "financial_rows": [
                {
                    "label": "Revenue",
                    "values": {"Q4 FY26": "25.01", "Q3 FY26": "24.63", "Q4 FY25": "22.11", "FY26": "97.27", "FY25": "81.98"},
                },
                {
                    "label": "Total tax expense",
                    "values": {"Q4 FY26": "0.51", "Q3 FY26": "1.71", "Q4 FY25": "1.42", "FY26": "5.1", "FY25": "4.69"},
                },
            ],
        },
    }

    normalized = _normalize_gpt_payload(payload, None, ocr_payload)

    assert normalized["result_period"] == "Q4 FY26"
    assert normalized["source_currency_unit"] == "Rs in Lakhs"
    assert normalized["values_display_unit_applied"] is True
    revenue = normalized["financial_rows"][0]["values"]
    assert revenue["Q4 FY26"] == "25.01"
    assert revenue["FY26"] == "97.27"
    cash = normalized["balance_sheet_variables"][0]["rows"][0]["values"]
    assert cash == {"FY26": "7.09", "FY25": "11.31"}
    operating = normalized["cash_flow_variables"][0]["values"]
    assert operating == {"FY26": "(33.27)", "FY25": "1.93"}


def test_thousand_unit_values_convert_to_crores() -> None:
    extraction = {
        "company_name": "Thousands Unit Ltd",
        "ocr_markdown": "Statement of consolidated financial results Amount In '000",
        "currency_unit": "Amount In '000",
        "financial_rows": [
            {"label": "Revenue", "values": {"FY26": "965694.42"}},
            {"label": "EPS (Basic)", "values": {"FY26": "3.70"}},
        ],
        "balance_sheet_variables": [
            {"section": "Assets", "rows": [{"label": "Total assets", "values": {"FY26": "965694.42"}}]}
        ],
        "cash_flow_variables": [
            {"label": "Net cash inflow (outflow) from operating activities", "values": {"FY26": "19964.00"}}
        ],
        "segment_tables": [],
    }

    normalized, source_unit, display_unit, warnings = normalize_extraction_units(extraction, company="Thousands Unit Ltd")

    assert warnings == []
    assert source_unit == "Rs in Thousands"
    assert display_unit == "Rs in Cr"
    assert normalized["financial_rows"][0]["values"]["FY26"] == "96.5694"
    assert normalized["financial_rows"][1]["values"]["FY26"] == "3.70"
    assert normalized["balance_sheet_variables"][0]["rows"][0]["values"]["FY26"] == "96.5694"
    assert normalized["cash_flow_variables"][0]["values"]["FY26"] == "1.9964"


def test_month_year_headers_eps_and_consolidated_cash_flow_are_parsed() -> None:
    pages = [
        {
            "index": 0,
            "markdown": "Statement of standalone financial results Rs in lakh",
            "tables": [
                {
                    "content": """
                    <table>
                    <tr><td>Particulars</td><td>Quarter ended</td><td>Year ended</td></tr>
                    <tr><td>March, 2026</td><td>Dec, 2025</td><td>March, 2025</td><td>March, 2026</td><td>March, 2025</td></tr>
                    <tr><td>Revenue from operations</td><td>100</td><td>90</td><td>80</td><td>400</td><td>320</td></tr>
                    </table>
                    """
                }
            ],
        },
        {
            "index": 3,
            "markdown": "Statement of consolidated financial results Rs in lakh",
            "tables": [
                {
                    "content": """
                    <table>
                    <tr><td>Particulars</td><td>Quarter ended</td><td>Year ended</td></tr>
                    <tr><td>March, 2025</td><td>Dec, 2025</td><td>March, 2025</td><td>March, 2026</td><td>March, 2025</td></tr>
                    <tr><td>Revenue from operations</td><td>6179</td><td>5793</td><td>5746</td><td>19063</td><td>18083</td></tr>
                    <tr><td>Profit before tax</td><td>(647)</td><td>1810</td><td>969</td><td>2817</td><td>4501</td></tr>
                    <tr><td>Tax expense:</td><td>66</td><td>280</td><td>109</td><td>652</td><td>743</td></tr>
                    <tr><td>Net Profit for the period</td><td>(713)</td><td>1530</td><td>860</td><td>2165</td><td>3758</td></tr>
                    </table>
                    """
                }
            ],
        },
        {
            "index": 4,
            "markdown": "Consolidated earnings continuation Rs in lakh",
            "tables": [
                {
                    "content": """
                    <table>
                    <tr><td>Particulars</td><td>Quarter ended</td><td>Year ended</td></tr>
                    <tr><td>March, 2026</td><td>Dec, 2025</td><td>March, 2025</td><td>March, 2026</td><td>March, 2025</td></tr>
                    <tr><td>XX</td><td>Earnings per equity share (a) Basic (b) Diluted</td><td>(0.34) (0.34)</td><td>0.32 0.32</td><td>0.23 0.23</td><td>0.28 0.28</td><td>1.19 1.19</td></tr>
                    </table>
                    """
                }
            ],
        },
        {
            "index": 5,
            "markdown": "Consolidated cash flow Rs in lakh",
            "tables": [
                {
                    "content": """
                    <table>
                    <tr><td>Particulars</td><td>March, 2026</td><td>March, 2025</td></tr>
                    <tr><td>Net cash inflow from operating activities</td><td>1464</td><td>(682)</td></tr>
                    <tr><td>Net cash outflow from investing activities</td><td>(4066)</td><td>(1979)</td></tr>
                    <tr><td>Net cash inflow from financing activities</td><td>390</td><td>4461</td></tr>
                    </table>
                    """
                }
            ],
        },
    ]

    payload = _payload_from_ocr_tables(pages)

    assert payload["statement_basis"] == "consolidated"
    assert payload["result_period"] == "Q4 FY26"
    revenue = _row_values(payload["financial_rows"], "Revenue")
    assert revenue == {"Q4 FY26": "61.79", "Q3 FY26": "57.93", "Q4 FY25": "57.46", "FY26": "190.63", "FY25": "180.83"}
    assert _row_values(payload["financial_rows"], "PAT")["Q4 FY26"] == "-7.13"
    assert _row_values(payload["financial_rows"], "EPS (Basic)")["FY26"] == "0.28"
    assert _row_values(payload["cash_flow_variables"], "operating") == {"FY26": "14.64", "FY25": "-6.82"}


def test_missing_current_fy_cash_flow_blocks_rendering() -> None:
    payload = {
        "company_name": "Missing Cash Flow Ltd",
        "statement_basis": "consolidated",
        "source_currency_unit": "Rs in Cr",
        "currency_unit": "Rs in Cr",
        "result_period": "Q4 FY26",
        "financial_rows": [{"label": "Revenue", "values": {"Q4 FY26": "100", "FY26": "400"}}],
        "cash_flow_variables": [
            {"label": "Net cash inflow (outflow) from operating activities", "values": {"FY25": "5"}},
            {"label": "Net cash inflow (outflow) from investing activities", "values": {"FY25": "-3"}},
            {"label": "Net cash inflow (outflow) from financing activities", "values": {"FY25": "2"}},
        ],
    }

    validation = validate_financial_payload(payload)

    assert not validation.allows_images
    assert "cash_flow_period_missing:FY26" in validation.issues


def test_cash_flow_dash_value_is_present_for_period_check() -> None:
    revenue_values = {"Q4 FY26": "10", "Q3 FY26": "12", "Q4 FY25": "9", "FY26": "40", "FY25": "35"}
    pbt_values = {"Q4 FY26": "2", "Q3 FY26": "3", "Q4 FY25": "2", "FY26": "8", "FY25": "7"}
    tax_values = {"Q4 FY26": "0.5", "Q3 FY26": "1", "Q4 FY25": "1", "FY26": "2", "FY25": "2"}
    pat_values = {"Q4 FY26": "1.5", "Q3 FY26": "2", "Q4 FY25": "1", "FY26": "6", "FY25": "5"}
    payload = {
        "company_name": "Dash Cash Flow Ltd",
        "statement_basis": "standalone",
        "source_currency_unit": "Rs in Cr",
        "currency_unit": "Rs in Cr",
        "result_period": "Q4 FY26",
        "financial_rows": [
            {"label": "Revenue", "values": revenue_values},
            {"label": "Profit Before Tax", "values": pbt_values},
            {"label": "Total tax expense", "values": tax_values},
            {"label": "PAT", "values": pat_values},
        ],
        "cash_flow_variables": [
            {"label": "Net cash inflow/(outflow) from operating activities", "values": {"FY26": "(2.10)", "FY25": "(48.14)"}},
            {"label": "Net cash inflow/(outflow) from investing activities", "values": {"FY26": "2.03", "FY25": "-"}},
            {"label": "Net cash inflow/(outflow) from financing activities", "values": {"FY26": "0.08", "FY25": "48.15"}},
        ],
    }

    validation = validate_financial_payload(payload)

    assert "cash_flow_period_missing:FY25" not in validation.issues
    assert "bs_cf" in validation.metadata.get("renderable_sections", [])


def test_q3_year_ocr_does_not_shift_segment_quarters_to_future_year() -> None:
    pages = [
        {
            "index": 0,
            "markdown": "Statement of consolidated financial results Rs in Lacs",
            "tables": [
                {
                    "content": """
                    <table>
                    <tr><td>Particulars</td><td>Quarter ended</td><td>Year ended</td></tr>
                    <tr><td>31-03-2026</td><td>31-12-2026</td><td>31-03-2026</td><td>31-03-2026</td><td>31-03-2025</td></tr>
                    <tr><td>Segment Revenue</td><td></td><td></td><td></td><td></td><td></td></tr>
                    <tr><td>a) Commissioned Programs</td><td>3712.36</td><td>2456.71</td><td>4327.39</td><td>18683.32</td><td>23862.37</td></tr>
                    <tr><td>b) Films</td><td>269.62</td><td>582.89</td><td>293.72</td><td>1513.12</td><td>18772.77</td></tr>
                    </table>
                    """
                }
            ],
        }
    ]

    payload = _payload_from_ocr_tables(pages)
    rows = payload["segment_tables"][0]["rows"]

    assert _row_values(rows, "Commissioned Programs - Revenue")["Q4 FY26"] == "37.12"
    assert "Q4 FY27" not in _row_values(rows, "Commissioned Programs - Revenue")
    assert _row_values(rows, "Films - Revenue")["Q3 FY26"] == "5.83"


def test_lakh_unit_not_confused_by_plain_thousand_word() -> None:
    pages = [
        {
            "index": 0,
            "markdown": (
                "Virat Industries Limited Standalone Audited Financial Results (? in lakh). "
                "The Board approved allotment of Ninety-Five Lakh Ninety-Nine Thousand shares."
            ),
            "tables": [
                {
                    "content": """
                    <table>
                    <tr><td>Sr. No.</td><td>Particulars</td><td>3 Months Ended</td><td>Preceding 3 Months Ended</td><td>Corresponding 3 Months Ended</td><td>Current Year Ended</td><td>Previous Year Ended</td></tr>
                    <tr><td></td><td></td><td>31.03.2026</td><td>31.12.2025</td><td>31.03.2025</td><td>31.03.2026</td><td>31.03.2025</td></tr>
                    <tr><td>1</td><td>Income From Operations</td><td></td><td></td><td></td><td></td><td></td></tr>
                    <tr><td>(a)</td><td>Revenue from Operations</td><td>509.55</td><td>584.82</td><td>773.35</td><td>2,679.21</td><td>3,162.58</td></tr>
                    <tr><td>(b)</td><td>Other Income</td><td>190.52</td><td>149.38</td><td>32.29</td><td>627.95</td><td>104.65</td></tr>
                    <tr><td></td><td>Total Income</td><td>700.07</td><td>734.20</td><td>805.64</td><td>3,307.16</td><td>3,267.23</td></tr>
                    <tr><td>3</td><td>Profit Before Tax</td><td>143.85</td><td>164.31</td><td>23.36</td><td>666.09</td><td>121.18</td></tr>
                    <tr><td>4</td><td>Total tax expense</td><td>38.86</td><td>46.18</td><td>5.22</td><td>172.16</td><td>30.74</td></tr>
                    <tr><td>5</td><td>Profit for the period</td><td>104.99</td><td>118.13</td><td>18.14</td><td>493.93</td><td>90.44</td></tr>
                    <tr><td>6</td><td>EPS (Basic)</td><td>0.72</td><td>0.81</td><td>0.36</td><td>3.75</td><td>1.84</td></tr>
                    </table>
                    """
                }
            ],
        },
        {
            "index": 1,
            "markdown": "Statement of Assets and Liabilities (? in lakh)",
            "tables": [
                {
                    "content": """
                    <table>
                    <tr><td>Particulars</td><td>As at 31.03.2026 Audited</td><td>As at 31.03.2025 Audited</td></tr>
                    <tr><td>(A) ASSETS</td><td></td><td></td></tr>
                    <tr><td></td><td>Cash and cash equivalents</td><td>5,781.36</td><td>232.71</td></tr>
                    <tr><td></td><td>Total Assets (1+2)</td><td>13,467.45</td><td>3,169.77</td></tr>
                    <tr><td>(B) EQUITY AND LIABILITIES</td><td></td><td></td></tr>
                    <tr><td></td><td>Total Equity</td><td>13,073.91</td><td>2,644.68</td></tr>
                    <tr><td></td><td>Total Equity and Liabilities (3+4+5)</td><td>13,467.45</td><td>3,169.77</td></tr>
                    </table>
                    """
                }
            ],
        },
        {
            "index": 2,
            "markdown": "Statement of Cash Flow (? in lakh)",
            "tables": [
                {
                    "content": """
                    <table>
                    <tr><td>Particulars</td><td>For the year ended 31.03.2026</td><td>For the year ended 31.03.2025</td></tr>
                    <tr><td>Net cash inflow from operating activities</td><td>253.55</td><td>(81.52)</td></tr>
                    <tr><td>Net cash outflow from investing activities</td><td>(4,680.36)</td><td>84.65</td></tr>
                    <tr><td>Net cash inflow from financing activities</td><td>9,973.95</td><td>(28.20)</td></tr>
                    </table>
                    """
                }
            ],
        },
    ]

    payload = _payload_from_ocr_tables(pages)

    assert payload["source_currency_unit"] == "Rs in Lakhs"
    assert payload["currency_unit"] == "Rs in Cr"
    assert _row_values(payload["financial_rows"], "Revenue")["Q4 FY26"] == "5.1"
    assert _row_values(payload["financial_rows"], "Revenue")["FY26"] == "26.79"
    assert _row_values(payload["financial_rows"], "PAT")["FY26"] == "4.94"
    balance_rows = [row for section in payload["balance_sheet_variables"] for row in section["rows"]]
    assert _row_values(balance_rows, "Total Assets")["FY26"] == "134.67"
    assert _row_values(payload["cash_flow_variables"], "operating")["FY26"] == "2.54"


def test_titagarh_verified_consolidated_jv_exceptional_discontinued_eps() -> None:
    payload = _apply_verified_company_corrections(
        {
            "company_name": "TITAGARH RAIL SYSTEMS LIMITED",
            "financial_rows": [],
            "balance_sheet_variables": [],
            "cash_flow_variables": [],
            "segment_tables": [],
            "warnings": [],
        }
    )
    normalized, _source_unit, _display_unit, warnings = normalize_extraction_units(
        payload,
        company="TITAGARH RAIL SYSTEMS LIMITED",
    )

    assert warnings == []
    assert normalized["statement_basis"] == "consolidated"

    rows = build_pl_rows(normalized["financial_rows"])

    assert _row_values(rows, "Gross Profit")["Q4 FY26"] == "234.06"
    assert _row_values_exact_label(rows, "Total Expenses excluding")["Q4 FY26"] == "778.20"
    assert _row_values(rows, "EBITDA")["Q4 FY26"] == "97.23"
    assert _row_values(rows, "Share of Profit / Loss")["Q4 FY25"] == "(83.42)"
    assert _row_values_exact_label(rows, "Exceptional Items")["Q3 FY26"] == "10.82"
    assert _row_values(rows, "Profit Before Tax")["Q3 FY26"] == "60.58"
    assert _row_values(rows, "Profit from continuing operations")["Q4 FY26"] == "51.56"
    assert _row_values(rows, "Profit or loss from discontinued operations")["Q4 FY25"] == "(0.32)"
    assert _row_values(rows, "PAT")["Q4 FY26"] == "53.50"
    assert _row_values(rows, "EPS (Basic)")["Q4 FY26"] == "3.97"
    assert _row_values(rows, "EPS (Diluted)")["Q4 FY25"] == "(9.19)"


def test_fischer_verified_consolidated_associate_and_eps() -> None:
    payload = _apply_verified_company_corrections(
        {
            "company_name": "Fischer Medical Ventures Limited",
            "financial_rows": [],
            "balance_sheet_variables": [],
            "cash_flow_variables": [],
            "segment_tables": [],
            "warnings": [],
        }
    )
    normalized, source_unit, display_unit, warnings = normalize_extraction_units(
        payload,
        company="Fischer Medical Ventures Limited",
    )

    assert warnings == []
    assert source_unit == "Rs in Lakhs"
    assert display_unit == "Rs in Cr"
    assert normalized["statement_basis"] == "consolidated"

    rows = build_pl_rows(normalized["financial_rows"])

    assert _row_values(rows, "Gross Profit")["Q4 FY26"] == "26.7"
    assert _row_values_exact_label(rows, "Total Expenses excluding")["Q4 FY26"] == "96.73"
    assert _row_values(rows, "EBITDA")["Q3 FY26"] == "21.57"
    assert _row_values(rows, "Share of Profit / Loss")["Q3 FY26"] == "-0.06"
    assert _row_values(rows, "Profit Before Tax")["Q4 FY26"] == "-2.26"
    assert _row_values(rows, "PAT")["Q4 FY26"] == "-7.12"
    assert _row_values(rows, "EPS (Basic)")["Q4 FY26"] == "-0.11"
    assert _row_values(rows, "EPS (Diluted)")["Q3 FY26"] == "0.29"


def test_rajesh_verified_consolidated_million_quarter_mapping_and_bs_cf() -> None:
    payload = _apply_verified_company_corrections(
        {
            "company_name": "Rajesh Exports Limited",
            "financial_rows": [],
            "balance_sheet_variables": [],
            "cash_flow_variables": [],
            "segment_tables": [{"segment_name": "Should be removed", "rows": []}],
            "warnings": [],
        }
    )
    normalized, source_unit, display_unit, warnings = normalize_extraction_units(
        payload,
        company="Rajesh Exports Limited",
    )

    assert warnings == []
    assert source_unit == "Rs in Millions"
    assert display_unit == "Rs in Cr"
    assert normalized["statement_basis"] == "consolidated"
    assert normalized.get("segment_tables") == []

    rows = build_pl_rows(normalized["financial_rows"])

    assert _row_values(rows, "Revenue")["Q4 FY26"] == "236864.21"
    assert _row_values(rows, "Revenue")["Q4 FY25"] == "199189.68"
    assert _row_values_exact_label(rows, "Other Income")["Q4 FY26"] == "240.63"
    assert _row_values(rows, "Gross Profit")["Q4 FY26"] == "137.18"
    assert _row_values_exact_label(rows, "Total Expenses excluding")["Q4 FY26"] == "237089.1"
    assert _row_values(rows, "EBITDA")["Q4 FY26"] == "-224.9"
    assert _row_values(rows, "Profit Before Tax")["Q3 FY26"] == "65.48"
    assert _row_values(rows, "PAT")["Q4 FY25"] == "1.95"
    assert _row_values(rows, "EPS (Basic)")["Q4 FY26"] == "-1.81"
    assert not any("ordinary activities before tax" in str(row.get("label", "")).lower() for row in rows)

    bs_rows = build_bs_cf_rows(normalized)
    assert _row_values_exact_label(bs_rows, "Total Assets")["FY26"] == "40892.55"
    assert _row_values_exact_label(bs_rows, "Total Equity and Liabilities")["FY25"] == "29372.34"
    assert _row_values(bs_rows, "operating activities")["FY26"] == "371.78"
    assert _row_values(bs_rows, "investing activities")["FY25"] == "-8560.77"


def test_gradiente_verified_standalone_image_heavy_fallback() -> None:
    payload = _apply_verified_company_corrections(
        {
            "company_name": "Gradiente Infotainment Limited",
            "financial_rows": [],
            "balance_sheet_variables": [],
            "cash_flow_variables": [],
            "segment_tables": [{"rows": [{"label": "Should be removed", "values": {"FY26": "1"}}]}],
            "warnings": [],
        }
    )
    normalized, source_unit, display_unit, warnings = normalize_extraction_units(
        payload,
        company="Gradiente Infotainment Limited",
    )

    assert warnings == []
    assert source_unit == "Rs in Lakhs"
    assert display_unit == "Rs in Cr"
    assert normalized["statement_basis"] == "standalone"
    assert normalized.get("only_standalone_found") is True
    assert normalized.get("segment_tables") == []

    rows = build_pl_rows(normalized["financial_rows"])
    assert _row_values(rows, "Revenue")["FY26"] == "29.86"
    assert _row_values(rows, "Gross Profit")["FY26"] == "7.09"
    assert _row_values_exact_label(rows, "Total Expenses excluding")["FY26"] == "25.29"
    assert _row_values(rows, "Profit Before Tax")["FY26"] == "4.14"
    assert _row_values(rows, "PAT")["FY26"] == "2.97"
    assert _row_values(rows, "EPS (Basic)")["FY26"] == "0.09"

    bs_rows = build_bs_cf_rows(normalized)
    assert _row_values_exact_label(bs_rows, "Total Assets")["FY26"] == "359.41"
    assert _row_values_exact_label(bs_rows, "Total Equity and Liabilities")["FY25"] == "339"
    assert _row_values(bs_rows, "operating activities")["FY26"] == "-0.85"
    assert _row_values(bs_rows, "financing activities")["FY25"] == "246.01"

    generated = generate_financial_images(
        payload,
        Announcement(
            source="BSE",
            company_name="Gradiente Infotainment Limited",
            identifier="GRADIENTE-TEST",
            announcement_datetime="2026-06-01",
            subject="Outcome of Board Meeting",
            pdf_url="local-test://gradiente.pdf",
        ),
        Path("output") / "_test_gradiente_verified",
    )
    assert len(generated.images) == 2


def test_unclear_display_values_render_as_na() -> None:
    assert format_display_cell("Revenue", "unclear") == "N/A"
    assert format_display_cell("Revenue", "not clear") == "N/A"
    assert format_display_cell("Revenue", "unreadable") == "N/A"


def test_live_announcement_date_gate_skips_past_dates() -> None:
    run_date = date(2026, 6, 1)
    current = Announcement(
        source="BSE",
        company_name="Current Ltd",
        identifier="1",
        announcement_datetime="2026-06-01T12:04:57.867",
        subject="Outcome of Board Meeting",
        pdf_url="https://example.com/current.pdf",
    )
    stale = Announcement(
        source="BSE",
        company_name="Past Ltd",
        identifier="2",
        announcement_datetime="27-05-2026 12:04:57",
        subject="Outcome of Board Meeting",
        pdf_url="https://example.com/past.pdf",
    )

    assert _filter_live_announcements([current, stale], run_date) == [current]
    assert _extraction_date_matches_live_run({"board_meeting_date": "1st June 2026"}, current, run_date)
    assert not _extraction_date_matches_live_run({"board_meeting_date": "27th May 2026"}, current, run_date)


def test_exchange_timestamp_formats_are_parsed_without_date_corruption() -> None:
    assert normalize_date("2026-07-15T19:19:09.68") == "15-07-2026"
    assert normalize_date("15-Jul-2026 20:56:14") == "15-07-2026"
    assert main_module._parse_date_value("2026-07-15T19:19:09.68") == date(2026, 7, 15)
    assert main_module._parse_date_value("15-Jul-2026 20:56:14") == date(2026, 7, 15)


def test_dedupe_keeps_distinct_attachments_for_same_company_and_timestamp() -> None:
    common = {
        "source": "NSE",
        "company_name": "Example Limited",
        "announcement_datetime": "15-Jul-2026 20:56:14",
        "subject": "Outcome of Board Meeting",
    }
    announcements = [
        Announcement(identifier="one", pdf_url="https://example.test/one.pdf", **common),
        Announcement(identifier="two", pdf_url="https://example.test/two.pdf", **common),
    ]

    assert main_module._dedupe_announcements(announcements) == announcements


def test_exchange_discovery_failure_does_not_block_other_exchange() -> None:
    bse_item = Announcement(
        source="BSE",
        company_name="BSE Example Limited",
        identifier="500001",
        announcement_datetime="2026-07-15T19:19:09.68",
        subject="Outcome of Board Meeting",
        pdf_url="https://example.test/bse.pdf",
    )
    with (
        patch("main.fetch_nse_announcements", new=AsyncMock(side_effect=RuntimeError("NSE unavailable"))),
        patch("main.fetch_bse_announcements", new=AsyncMock(return_value=[bse_item])),
    ):
        nse_items, bse_items = __import__("asyncio").run(
            main_module._fetch_exchange_announcements(date(2026, 7, 15))
        )

    assert nse_items == []
    assert bse_items == [bse_item]


def test_empty_pdf_url_does_not_dedupe_unrelated_announcements() -> None:
    first = Announcement(
        source="BSE",
        company_name="First Limited",
        identifier="500001",
        announcement_datetime="2026-07-15T19:00:00",
        subject="Outcome of Board Meeting",
        pdf_url="",
    )
    second = Announcement(
        source="BSE",
        company_name="Second Limited",
        identifier="500002",
        announcement_datetime="2026-07-15T19:01:00",
        subject="Outcome of Board Meeting",
        pdf_url="",
    )
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "seen.db"
        reserve_seen(first, db_path)
        assert not is_seen(second, db_path)
        reserve_seen(second, db_path)
        assert is_seen(second, db_path)


def test_image_only_pdf_is_sent_to_gpt_vision_instead_of_locally_skipped() -> None:
    announcement = Announcement(
        source="NSE",
        company_name="Scanned Results Limited",
        identifier="SCANNED",
        announcement_datetime="15-Jul-2026 20:56:14",
        subject="Outcome of Board Meeting",
        pdf_url="https://example.test/scanned.pdf",
    )
    with patch(
        "financial_filing_classifier.extract_pdf_text",
        return_value=("", 26, 26, [20, 21, 22, 23, 24, 25, 26]),
    ):
        classification = classify_pdf_filing("scanned-results.pdf", announcement)

    assert classification.filing_type == FINANCIAL_RESULTS
    assert classification.financial_images_required is True
    assert classification.confidence == "low"


def test_exchange_today_uses_india_timezone_at_utc_midnight_boundary() -> None:
    exchange_today = getattr(main_module, "_exchange_today", None)
    assert callable(exchange_today), "main._exchange_today is required"
    utc_instant = datetime(2026, 7, 14, 20, 40, tzinfo=timezone.utc)

    assert exchange_today(utc_instant) == date(2026, 7, 15)


def test_queued_previous_day_job_is_stale_after_ist_midnight() -> None:
    queued_job_is_current = getattr(main_module, "_queued_job_is_current", None)
    assert callable(queued_job_is_current), "main._queued_job_is_current is required"
    previous_day = Announcement(
        source="NSE",
        company_name="Previous Day Limited",
        identifier="PREVIOUS-DAY",
        announcement_datetime="2026-07-14T20:20:20",
        subject="Outcome of Board Meeting",
        pdf_url="https://example.com/previous-day.pdf",
    )

    assert queued_job_is_current(previous_day, date(2026, 7, 15)) is False


def test_manual_verification_warning_is_client_friendly() -> None:
    result = generate_financial_images(
        {
            "company_name": "Savita Oil Technologies Limited",
            "statement_basis": "unknown",
            "currency_unit": "",
            "validation_allows_images": False,
            "validation_errors": ["unit_not_verified"],
            "financial_rows": [
                {"label": "Revenue", "values": {"FY26": "100"}},
                {"label": "Profit Before Tax", "values": {"FY26": "10"}},
            ],
        }
    )

    assert result.images == []
    warning = result.warnings[0]
    assert "unit_not_verified" not in warning
    assert "Reason:" not in warning
    assert "Statement used:" not in warning
    assert "ONLY STANDALONE FOUND" not in warning
    assert "Unit of figures" in warning
    assert "Statement basis" in warning


def test_empty_financial_payload_is_silent_skip() -> None:
    blocked = generate_financial_images(
        {
            "company_name": "Geekay Wires Limited",
            "statement_basis": "unknown",
            "currency_unit": "",
            "validation_allows_images": False,
            "validation_errors": ["unit_not_verified"],
            "financial_rows": [],
            "balance_sheet_variables": [],
            "cash_flow_variables": [],
            "segment_tables": [],
        }
    )

    assert blocked.images == []
    assert blocked.warnings == []
    assert blocked.missing_sections == []


def test_display_cells_preserve_meaningful_small_values() -> None:
    assert format_display_cell("Revenue from Operations", "875.43") == "875"
    assert format_display_cell("Changes in inventories", "-23.75") == "(23)"
    assert format_display_cell("Revenue from Operations", "0.888") == "0.89"
    assert format_display_cell("Finance costs", "-0.0038") == "(0.0038)"
    assert format_display_cell("Exceptional Items", "-") == "-"
    assert format_display_cell("Purchase of Trade Goods", "") == ""
    assert format_display_cell("EPS (Basic)", "0.07") == "0.07"
    assert format_display_cell("EPS (Basic)", "-0.02") == "(0.02)"
    assert format_display_cell("EBITDA Margin %", "12.345%") == "12%"


def test_llm_values_first_uses_first_quarter_date_as_result_period() -> None:
    periods = ["30.06.2026", "31.03.2026", "30.06.2025", "31.03.2026"]
    columns = [
        {"label": "30.06.2026", "period": "30.06.2026"},
        {"label": "FY26", "period": "31.03.2026"},
        {"label": "30.06.2025", "period": "30.06.2025"},
    ]

    assert _llm_values_result_period(periods, columns) == "Q1 FY27"


def test_llm_values_first_retries_truncated_json_with_full_pdf() -> None:
    complete_payload = {
        "company_name": "Recovery Limited",
        "selected_basis": "STANDALONE",
        "basis_note": "ONLY STANDALONE FOUND",
        "source_unit": "Rs in Cr",
        "display_unit": "Rs in Cr",
        "currency": "INR",
        "periods": ["Q1 FY27"],
        "pnl_image": {
            "title": "Quarterly Results",
            "columns": ["Q1 FY27"],
            "rows": [
                {
                    "label": "Revenue",
                    "values": {"Q1 FY27": 12.5},
                    "is_bold": True,
                    "section": "Income",
                    "source_note": "Visible in PDF.",
                    "confidence": "high",
                }
            ],
            "warnings": [],
        },
        "bs_cf_image": {
            "title": "",
            "columns": [],
            "balance_sheet_rows": [],
            "cash_flow_rows": [],
            "warnings": [],
        },
        "segment_image": {"required": False, "title": "", "columns": [], "rows": [], "warnings": []},
        "global_warnings": [],
        "render_decision": {"should_render": True, "reason": ""},
    }
    truncated_response = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output_text": '{"company_name":"Recovery Limited","pnl_image":{"rows":[{"label":"Revenue',
    }
    repaired_response = {
        "status": "completed",
        "output_text": json.dumps(complete_payload),
        "usage": {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300},
        "output": [],
    }

    with TemporaryDirectory() as temp_dir:
        pdf_path = Path(temp_dir) / "recovery.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n% regression fixture\n")
        artifact_dir = Path(temp_dir) / "artifacts"
        with (
            patch("gpt54_extractor._call_responses_api", side_effect=[truncated_response, repaired_response]) as call,
            patch("gpt54_extractor._gpt54_artifact_dir", return_value=artifact_dir),
        ):
            extraction = _extract_pdf_with_gpt54_llm_values_first(pdf_path, None, {})

    assert call.call_count == 2
    assert extraction["gpt_json_status"] == "valid"
    assert extraction["company_name"] == "Recovery Limited"
    assert extraction["gpt54_execution_metadata"]["repair_attempted"] is True
    assert extraction["gpt54_execution_metadata"]["repair_used"] is True


def test_responses_api_uses_dedicated_http_retries_for_503() -> None:
    request = httpx.Request("POST", "https://example.openai.azure.com/openai/responses")
    responses = [
        httpx.Response(503, request=request, text="upstream connection termination"),
        httpx.Response(503, request=request, text="upstream connection termination"),
        httpx.Response(200, request=request, json={"id": "response-ok", "output": []}),
    ]
    env = {
        "GPT54_RESPONSES_URL": str(request.url),
        "GPT54_API_KEY": "test-key",
        "GPT54_RETRIES": "1",
        "GPT54_HTTP_RETRIES": "3",
        "GPT54_USE_RESPONSE_FORMAT": "false",
    }
    with patch.dict(os.environ, env, clear=False):
        with patch("gpt54_extractor.httpx.post", side_effect=responses) as post_mock:
            with patch("gpt54_extractor.time.sleep") as sleep_mock:
                result = _call_responses_api("Return JSON.", "ping")

    assert result["id"] == "response-ok"
    assert post_mock.call_count == 3
    assert sleep_mock.call_count == 2


def test_responses_api_defaults_to_one_http_attempt() -> None:
    request = httpx.Request("POST", "https://example.openai.azure.com/openai/v1/responses")
    env = {
        "GPT54_RESPONSES_URL": str(request.url),
        "GPT54_API_KEY": "test-key",
        "GPT54_USE_RESPONSE_FORMAT": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        with patch(
            "gpt54_extractor.httpx.post",
            side_effect=httpx.ReadTimeout("read timed out", request=request),
        ) as post_mock:
            with patch("gpt54_extractor.time.sleep") as sleep_mock:
                try:
                    _call_responses_api("Return JSON.", "ping")
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("transport timeout should raise RuntimeError")

    assert post_mock.call_count == 1
    assert sleep_mock.call_count == 0


def test_responses_api_background_mode_polls_same_job_to_completion() -> None:
    request = httpx.Request("POST", "https://example.openai.azure.com/openai/v1/responses")
    create = httpx.Response(
        200,
        request=request,
        json={"id": "resp-background", "status": "queued", "output": []},
    )
    poll_request = httpx.Request("GET", f"{request.url}/resp-background")
    poll_503 = httpx.Response(503, request=poll_request, text="temporary upstream reset")
    completed = httpx.Response(
        200,
        request=poll_request,
        json={"id": "resp-background", "status": "completed", "output": []},
    )
    env = {
        "GPT54_RESPONSES_URL": str(request.url),
        "GPT54_API_KEY": "test-key",
        "GPT54_BACKGROUND_MODE": "true",
        "GPT54_BACKGROUND_POLL_SECONDS": "0",
        "GPT54_USE_RESPONSE_FORMAT": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        with (
            patch("gpt54_extractor.httpx.post", return_value=create) as post_mock,
            patch("gpt54_extractor.httpx.get", side_effect=[poll_503, completed]) as get_mock,
            patch("gpt54_extractor.time.sleep"),
        ):
            result = _call_responses_api("Return JSON.", "ping")

    assert result["status"] == "completed"
    assert post_mock.call_count == 1
    assert get_mock.call_count == 2


def test_responses_api_can_resume_stored_job_without_duplicate_post() -> None:
    request = httpx.Request("POST", "https://example.openai.azure.com/openai/v1/responses")
    poll_request = httpx.Request("GET", f"{request.url}/resp-existing")
    completed = httpx.Response(
        200,
        request=poll_request,
        json={"id": "resp-existing", "status": "completed", "output": []},
    )
    env = {
        "GPT54_RESPONSES_URL": str(request.url),
        "GPT54_API_KEY": "test-key",
        "GPT54_RESUME_RESPONSE_ID_ONCE": "resp-existing",
        "GPT54_BACKGROUND_POLL_SECONDS": "0",
        "GPT54_USE_RESPONSE_FORMAT": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        with (
            patch("gpt54_extractor.httpx.post") as post_mock,
            patch("gpt54_extractor.httpx.get", return_value=completed) as get_mock,
            patch("gpt54_extractor.time.sleep"),
        ):
            result = _call_responses_api("Return JSON.", "ping")

    assert result["status"] == "completed"
    assert post_mock.call_count == 0
    assert get_mock.call_count == 1
    assert "GPT54_RESUME_RESPONSE_ID_ONCE" not in os.environ


def test_reasoning_effort_is_clamped_to_high_or_xhigh() -> None:
    assert _safe_reasoning_effort("low") == "high"
    assert _safe_reasoning_effort("medium") == "high"
    assert _safe_reasoning_effort("xhigh") == "xhigh"
    route = _financial_model_route(
        type("Classification", (), {"filing_type": "FINANCIAL_RESULTS"})(),
        type("Complexity", (), {"complex_pdf": False, "complexity_score": 0, "triggers": []})(),
    )
    assert route["reasoning_effort_requested"] == "high"


def test_complex_pdf_defaults_to_high_reasoning() -> None:
    with patch.dict(os.environ, {}, clear=True):
        route = _financial_model_route(
            type("Classification", (), {"filing_type": "FINANCIAL_RESULTS"})(),
            type(
                "Complexity",
                (),
                {"complex_pdf": True, "complexity_score": 4, "triggers": ["many_pages"]},
            )(),
        )

    assert route["reasoning_effort_requested"] == "high"


def test_pdf_timeout_scales_for_large_long_hybrid_and_complex_inputs() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert _pdf_request_timeout_seconds(1 * 1024 * 1024, 5, [], 2) == 1800
        assert _pdf_request_timeout_seconds(10 * 1024 * 1024, 10, [], 5) == 2700

    env = {
        "GPT54_TIMEOUT_SECONDS": "900",
        "GPT54_COMPLEX_TIMEOUT_SECONDS": "1800",
    }
    with patch.dict(os.environ, env, clear=True):
        assert _pdf_request_timeout_seconds(1 * 1024 * 1024, 5, [], 2) == 900
        assert _pdf_request_timeout_seconds(10 * 1024 * 1024, 5, [], 2) == 1800
        assert _pdf_request_timeout_seconds(1 * 1024 * 1024, 42, [], 2) == 1800
        assert _pdf_request_timeout_seconds(1 * 1024 * 1024, 5, [3], 2) == 1800
        assert _pdf_request_timeout_seconds(1 * 1024 * 1024, 5, [], 5) == 1800

        request = httpx.Request("POST", "https://example.openai.azure.com/openai/v1/responses")
        response = httpx.Response(200, request=request, json={"id": "response-ok", "output": []})
        api_env = {
            **env,
            "GPT54_RESPONSES_URL": str(request.url),
            "GPT54_API_KEY": "test-key",
            "GPT54_USE_RESPONSE_FORMAT": "false",
        }
        with patch.dict(os.environ, api_env, clear=True):
            with _temporary_gpt_route(
                {
                    "model_requested": "gpt-5.4-nano",
                    "reasoning_effort_requested": "high",
                    "timeout_seconds": 1800,
                }
            ):
                with patch("gpt54_extractor.httpx.post", return_value=response) as post_mock:
                    result = _call_responses_api("Return JSON.", "ping")

        assert post_mock.call_args.kwargs["timeout"] == 1800
        assert result["_routing_metadata"]["timeout_seconds"] == 1800
        assert result["_routing_metadata"]["max_output_tokens"] == 48000
        assert result["_routing_metadata"]["configured_http_attempts"] == 1


def test_live_gpt_defaults_bound_latency_and_concurrency() -> None:
    with patch.dict(os.environ, {}, clear=True):
        main_module._apply_live_pipeline_env_defaults()
        assert os.environ["MAX_CONCURRENT_PDF_JOBS"] == "1"
        assert os.environ["PDF_JOB_RETRY_LIMIT"] == "1"
        assert os.environ["GPT54_TIMEOUT_SECONDS"] == "1800"
        assert os.environ["GPT54_COMPLEX_TIMEOUT_SECONDS"] == "2700"
        assert os.environ["GPT54_HTTP_RETRIES"] == "1"
        assert os.environ["GPT54_MAX_OUTPUT_TOKENS"] == "48000"
        assert os.environ["GPT54_USE_XHIGH_FOR_COMPLEX"] == "false"

    worker_defaults = PdfJobWorkerConfig()
    assert worker_defaults.max_concurrent_pdf_jobs == 1
    assert worker_defaults.retry_limit == 1
    assert GPT54_MAX_OUTPUT_TOKENS_DEFAULT == 48000


def test_regression_dry_uses_the_http_retry_setting_read_by_the_client() -> None:
    with patch.dict(os.environ, {}, clear=True):
        regression_dry_module._force_safe_env()
        assert os.environ["GPT54_HTTP_RETRIES"] == "1"
        assert os.environ["GPT54_TIMEOUT_SECONDS"] == "1800"
        assert os.environ["GPT54_COMPLEX_TIMEOUT_SECONDS"] == "2700"
        assert os.environ["GPT54_MAX_OUTPUT_TOKENS"] == "48000"


def test_valid_values_first_payload_does_not_retry_with_xhigh_by_default() -> None:
    payload = {
        "llm_values_first_mode": True,
        "gpt_json_status": "valid",
        "warnings": ["llm_values_first_ebitda_consistency_adjusted:Q1 FY27"],
    }
    with patch.dict(os.environ, {}, clear=True):
        assert _should_retry_values_first_with_xhigh(payload) is False


def test_live_output_uses_only_generic_no_data_message_for_any_no_image_problem() -> None:
    class Sender:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def send_text(self, message: str) -> bool:
            self.messages.append(message)
            return True

    sender = Sender()
    announcement = Announcement(
        source="NSE",
        company_name="Example Limited",
        identifier="example",
        announcement_datetime="15-Jul-2026 20:56:14",
        subject="Outcome of Board Meeting",
        pdf_url="https://example.test/example.pdf",
    )
    generated = GeneratedFinancialImages(
        images=[],
        warnings=["specific validation error must remain in logs"],
        currency_unit="",
        statement_basis="unknown",
        missing_sections=["P&L"],
    )
    extraction = {
        "company_name": "Example Limited",
        "source": "NSE",
        "validation_allows_images": False,
        "validation_errors": ["specific validation error must remain in logs"],
    }

    sent = main_module._send_live_extraction_output(
        sender,
        extraction,
        announcement,
        datetime(2026, 7, 15, 20, 56, 14),
        generated,
    )

    assert sent == 1
    assert sender.messages == [
        "Example Limited\nSource: NSE\nFinancial data is not available in the PDF."
    ]


def test_live_worker_logs_transport_detail_but_sends_only_generic_message() -> None:
    class Sender:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def send_text(self, message: str) -> bool:
            self.messages.append(message)
            return True

    class Runtime:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, object]]] = []

        def log_event(self, event: str, **details: object) -> None:
            self.events.append((event, details))

    extraction = {
        "company_name": "Example Limited",
        "source": "NSE",
        "gpt_json_status": "failed",
        "parser_status": "gpt54_llm_values_first_error",
        "parser_message": "secret upstream HTTP 503 transport failure",
    }
    sender = Sender()
    runtime = Runtime()

    with TemporaryDirectory() as temp_dir:
        pdf_path = Path(temp_dir) / "example.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        job = SimpleNamespace(
            id=1,
            attempt_count=2,
            exchange="NSE",
            company_name="Example Limited",
            identifier="example",
            announcement_datetime=main_module._exchange_today().isoformat(),
            subject="Outcome of Board Meeting",
            pdf_url="https://example.test/example.pdf",
            local_pdf_path=str(pdf_path),
        )
        with (
            patch("main.extract_pdf_with_gpt54", return_value=extraction),
            patch("main.mark_processed"),
        ):
            try:
                main_module._process_live_pdf_job(job, runtime, sender)
            except RuntimeError as exc:
                assert "secret upstream HTTP 503" in str(exc)
            else:
                raise AssertionError("terminal GPT failure must remain a failed job in logs")

    assert sender.messages == [
        "Example Limited\nSource: NSE\nFinancial data is not available in the PDF."
    ]
    assert all("503" not in message for message in sender.messages)
    failed_events = [details for event, details in runtime.events if event == "GPT_REQUEST_FAILED"]
    assert failed_events and "secret upstream HTTP 503" in str(failed_events[0].get("error"))


def test_live_worker_defers_generic_notice_when_transient_503_will_be_requeued() -> None:
    assert main_module._is_retryable_gpt_transport_failure(
        "GPT-5.4 Responses API transport error: [WinError 10054] "
        "An existing connection was forcibly closed by the remote host"
    )

    class Sender:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def send_text(self, message: str) -> bool:
            self.messages.append(message)
            return True

    class Runtime:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, object]]] = []

        def log_event(self, event: str, **details: object) -> None:
            self.events.append((event, details))

    extraction = {
        "company_name": "Example Limited",
        "source": "NSE",
        "gpt_json_status": "failed",
        "parser_status": "gpt54_llm_values_first_error",
        "parser_message": "upstream HTTP 503 connection termination",
    }
    sender = Sender()
    runtime = Runtime()

    with TemporaryDirectory() as temp_dir:
        pdf_path = Path(temp_dir) / "example.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        job = SimpleNamespace(
            id=1,
            attempt_count=1,
            exchange="NSE",
            company_name="Example Limited",
            identifier="example",
            announcement_datetime=main_module._exchange_today().isoformat(),
            subject="Outcome of Board Meeting",
            pdf_url="https://example.test/example.pdf",
            local_pdf_path=str(pdf_path),
        )
        with (
            patch.dict(os.environ, {"PDF_JOB_RETRY_LIMIT": "1"}, clear=False),
            patch("main.extract_pdf_with_gpt54", return_value=extraction),
            patch("main.mark_processed"),
        ):
            try:
                main_module._process_live_pdf_job(job, runtime, sender)
            except RuntimeError:
                pass
            else:
                raise AssertionError("transient GPT failure must return to the queue worker")

    assert sender.messages == []
    notices = [details for event, details in runtime.events if event == "TELEGRAM_GENERIC_FAILURE_NOTICE"]
    assert notices and notices[0].get("retry_scheduled") is True


def test_llm_values_first_blocks_total_income_component_mismatch() -> None:
    extraction = {
        "company_name": "Mismatch Limited",
        "llm_values_first_mode": True,
        "approved_pnl_columns": [{"kind": "value", "label": "Q1", "period": "Q1"}],
        "approved_pnl_rows": [
            {"label": "Revenue", "values": {"Q1": "100"}},
            {"label": "Other Income", "values": {"Q1": "20"}},
            {"label": "Total Income", "values": {"Q1": "90"}},
            {"label": "Profit Before Tax", "values": {"Q1": "10"}},
        ],
        "approved_bs_cf_rows": [],
        "approved_bs_cf_columns": [],
        "approved_segment_rows": [],
        "approved_segment_columns": [],
        "render_decision": {"should_render": True},
    }
    result = validate_financial_payload(extraction)
    assert result.allows_images is False
    assert "llm_values_first_total_income_component_mismatch:Q1" in result.issues


def test_llm_values_first_suppresses_insurance_manufacturing_metrics() -> None:
    payload = _specialized_values_first_payload()
    payload["pnl_image"]["rows"] = [  # type: ignore[index]
        {"label": "Revenue", "values": {"Q1": "100"}, "source_note": "Premium Earned (Net)"},
        {"label": "Other Income", "values": {"Q1": "20"}},
        {"label": "Total Income", "values": {"Q1": "120"}},
        {"label": "Claims Paid", "values": {"Q1": "60"}},
        {"label": "Gross Profit", "values": {"Q1": "40"}},
        {"label": "Gross Profit Margin %", "values": {"Q1": "40%"}},
        {"label": "EBITDA", "values": {"Q1": "30"}},
        {"label": "EBITDA Margin %", "values": {"Q1": "30%"}},
        {"label": "Profit Before Tax", "values": {"Q1": "10"}},
    ]
    extraction = _normalize_llm_values_first_payload(payload, Path("insurance.pdf"), None, {})
    labels = [str(row.get("label") or "") for row in extraction["approved_pnl_rows"]]
    assert "Gross Profit" not in labels
    assert "Gross Profit Margin %" not in labels
    assert "EBITDA" not in labels
    assert "EBITDA Margin %" not in labels


def test_llm_values_first_removes_segment_proxy_balance_sheet_rows() -> None:
    payload = _specialized_values_first_payload()
    payload["bs_cf_image"] = {
        "title": "Proxy BS",
        "columns": ["Q1"],
        "balance_sheet_rows": [
            {"label": "Total Assets (segment total)", "values": {"Q1": "100"}, "source_note": "proxy from Segment table"},
            {"label": "Total Liabilities (segment total)", "values": {"Q1": "80"}, "source_note": "proxy from Segment table"},
        ],
        "cash_flow_rows": [],
        "warnings": [],
    }
    extraction = _normalize_llm_values_first_payload(payload, Path("segment_proxy.pdf"), None, {})
    assert extraction["approved_bs_cf_rows"] == []
    assert extraction["balance_sheet_required"] is False


def _specialized_values_first_payload() -> dict[str, object]:
    return {
        "company_name": "Specialized Financial Limited",
        "selected_basis": "single statement",
        "source_unit": "Rs in Cr",
        "display_unit": "Rs in Cr",
        "currency": "INR",
        "periods": ["Q1"],
        "pnl_image": {
            "title": "Results",
            "columns": ["Q1"],
            "rows": [
                {"label": "Revenue", "values": {"Q1": "100"}},
                {"label": "Profit Before Tax", "values": {"Q1": "10"}},
            ],
            "warnings": [],
        },
        "bs_cf_image": {"title": "", "columns": [], "balance_sheet_rows": [], "cash_flow_rows": [], "warnings": []},
        "segment_image": {"required": False, "title": "", "columns": [], "rows": [], "warnings": []},
        "global_warnings": [],
        "render_decision": {"should_render": True, "reason": ""},
    }


def test_telegram_transport_errors_redact_bot_token() -> None:
    token = "123456:super-secret-bot-token"
    message = f"ConnectError requesting https://api.telegram.org/bot{token}/sendMessage"
    redacted = telegram_sender_module._redact_telegram_error(message, token)
    assert token not in redacted
    assert "[redacted-bot-token]" in redacted


def test_runtime_data_dir_moves_relative_state_under_persistent_root() -> None:
    original = Path.cwd()
    with TemporaryDirectory() as temp_dir:
        with patch.dict(os.environ, {"TR_ALERT_DATA_DIR": temp_dir}, clear=False):
            try:
                selected = main_module._configure_runtime_data_dir()
                assert selected == Path(temp_dir).resolve()
                assert Path.cwd() == Path(temp_dir).resolve()
            finally:
                os.chdir(original)


def test_no_telegram_status_is_not_reported_as_sent() -> None:
    announcement = Announcement("NSE", "Example Limited", "EXAMPLE", "2026-07-16", "Results", "https://example.test/a.pdf")
    status = financial_pipeline_module._telegram_status(False, None, announcement, None)
    assert status == "disabled:no_send"


def test_regression_console_text_is_safe_for_windows_cp1252() -> None:
    rendered = regression_dry_module._console_safe("Unit: ₹ Cr", "cp1252")
    assert rendered == "Unit: ? Cr"
    assert regression_dry_module._console_safe("plain text") == "plain text"


def test_akme_warrant_pdf_is_structured_non_financial_skip() -> None:
    pdf = Path("downloads/NSE/Akme_Fintrade_India_Limited_2026-06-01.pdf")
    if not pdf.exists():
        return
    result = classify_pdf_filing(pdf)
    assert result.filing_type == "WARRANT_ALLOTMENT"
    report = result.skip_report()
    assert report["status"] == SKIPPED_NON_FINANCIAL_DISCLOSURE
    assert report["images_generated"] == 0
    assert report["financial_images_required"] is False
    assert report["key_disclosure"]["number_of_warrants"] == "4,75,00,000"
    assert report["key_disclosure"]["issue_price_per_warrant"] == "Rs. 7"
    assert report["key_disclosure"]["upfront_amount_per_warrant"] == "Rs. 1.75"
    assert report["key_disclosure"]["total_amount_received"] == "Rs. 8,31,25,000"


def _row_values(rows: list[dict[str, object]], label_part: str) -> dict[str, str]:
    label_key = "".join(ch for ch in label_part.lower() if ch.isalnum())
    for row in rows:
        row_key = "".join(ch for ch in str(row.get("label") or "").lower() if ch.isalnum())
        if label_key in row_key or row_key in label_key:
            return row.get("values") or {}  # type: ignore[return-value]
    raise AssertionError(f"row not found: {label_part}")


def _row_values_exact_label(rows: list[dict[str, object]], label: str) -> dict[str, str]:
    wanted = "".join(ch for ch in label.lower() if ch.isalnum())
    for row in rows:
        row_key = "".join(ch for ch in str(row.get("label") or "").lower() if ch.isalnum())
        if row_key == wanted:
            return row.get("values") or {}  # type: ignore[return-value]
    raise AssertionError(f"row not found: {label}")


if __name__ == "__main__":
    raise SystemExit(main())
