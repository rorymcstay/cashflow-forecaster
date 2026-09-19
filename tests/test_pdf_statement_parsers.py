import datetime as dt

from app.pdf_statement_parsers import (
    detect_holdings_kind,
    detect_kind,
    extract_amex_period,
    extract_amex_summary,
    extract_hsbc_period,
    extract_hsbc_summary,
    parse_trading212_holdings,
)


def test_extract_hsbc_period_infers_start_year_from_end_year():
    text = "some header text\n24 April to 23 May 2026\nmore text"
    assert extract_hsbc_period(text) == (dt.date(2026, 4, 24), dt.date(2026, 5, 23))


def test_extract_hsbc_period_handles_december_to_january_wraparound():
    text = "6 December to 5 January 2027"
    assert extract_hsbc_period(text) == (dt.date(2026, 12, 6), dt.date(2027, 1, 5))


def test_extract_hsbc_period_missing_returns_none():
    assert extract_hsbc_period("nothing relevant here") is None


def test_extract_hsbc_summary_parses_payments_in_and_out_and_closing_balance():
    text = (
        "Account Summary\nOpeningBalance £106.15\nPayments In £38,416.29\n"
        "Payments Out £37,471.68\nClosingBalance £1,050.76\n"
    )
    assert extract_hsbc_summary(text) == {
        "expected_in": 38416.29,
        "expected_out": 37471.68,
        "closing_balance": 1050.76,
    }


def test_extract_hsbc_summary_closing_balance_none_when_missing():
    text = "Account Summary\nPayments In £38,416.29\nPayments Out £37,471.68\n"
    summary = extract_hsbc_summary(text)
    assert summary["closing_balance"] is None


def test_extract_amex_period():
    text = "Statement Period From 6April to5May2026"
    assert extract_amex_period(text) == (dt.date(2026, 4, 6), dt.date(2026, 5, 5))


def test_extract_amex_summary_negates_closing_balance_for_credit_card_convention():
    text = (
        "Previous Closing Balance New Credits New Debits Closing Balance\n"
        "£5,172.93 - £5,503.69 + £2,960.04 = £2,629.28"
    )
    assert extract_amex_summary(text) == {
        "expected_in": 5503.69,
        "expected_out": 2960.04,
        "closing_balance": -2629.28,
    }


def test_detect_kind_returns_none_for_unrecognised_text(tmp_path, monkeypatch):
    import app.pdf_statement_parsers as mod

    monkeypatch.setattr(mod, "_page_text", lambda path, limit=1: "Some other bank entirely")
    assert detect_kind(tmp_path / "whatever.pdf") is None


def test_detect_holdings_kind_recognises_trading212_font(tmp_path, monkeypatch):
    import app.pdf_statement_parsers as mod

    monkeypatch.setattr(mod, "_uses_trading212_font", lambda path: True)
    assert detect_holdings_kind(tmp_path / "whatever.pdf") == "trading212"


def test_detect_holdings_kind_returns_none_for_other_fonts(tmp_path, monkeypatch):
    import app.pdf_statement_parsers as mod

    monkeypatch.setattr(mod, "_uses_trading212_font", lambda path: False)
    assert detect_holdings_kind(tmp_path / "whatever.pdf") is None


_T212_OCR_TEXT = """TR* D | N G ele TAX ID CUSTOMER ID CUSTOMER NAME
GB PA807342C 13783781 Rory Mcstay
Confirmation of holdings
as of 18/09/2026
Trading 212 Stocks ISA
Holdings value: 5,515.00 GBP
INSTRUMENT ISIN QUANTITY PRICE
Vanguard S&P 500 UCITS ETF IEOOBFMXxD54 50 GBP 110.3
This document is electronically generated and it doesn't require signing.
"""


def test_parse_trading212_holdings_extracts_snapshot(tmp_path, monkeypatch):
    import app.pdf_statement_parsers as mod

    path = tmp_path / "confirmation.pdf"
    path.write_bytes(b"fake pdf bytes")
    monkeypatch.setattr(mod, "_ocr_first_page", lambda p, resolution=300: _T212_OCR_TEXT)

    result = parse_trading212_holdings(path)

    assert result["kind"] == "trading212"
    assert result["as_of"] == "2026-09-18"
    assert result["account_hint"] == "Trading 212 Stocks ISA"
    assert result["currency"] == "GBP"
    assert result["holdings_value"] == 5515.0
    assert len(result["holdings"]) == 1
    holding = result["holdings"][0]
    assert holding["instrument"] == "Vanguard S&P 500 UCITS ETF"
    # OCR misreads the ISIN's "00" as "OO" — resolved via the O->0 fallback
    # in _resolve_isin_ticker since it turns an unknown ISIN into a known one.
    assert holding["isin"] == "IE00BFMXXD54"
    assert holding["ticker"] == "VUAG.L"
    assert holding["quantity"] == 50.0
    assert holding["price"] == 110.3
    assert holding["value"] == 5515.0
    assert result["reconciliation"] == {
        "expected_value": 5515.0,
        "computed_value": 5515.0,
        "ok": True,
    }


def test_parse_trading212_holdings_unmapped_isin_leaves_ticker_none(tmp_path, monkeypatch):
    import app.pdf_statement_parsers as mod

    text = _T212_OCR_TEXT.replace("IEOOBFMXxD54", "US0378331005")  # Apple — not in the lookup table
    path = tmp_path / "confirmation.pdf"
    path.write_bytes(b"fake pdf bytes")
    monkeypatch.setattr(mod, "_ocr_first_page", lambda p, resolution=300: text)

    result = parse_trading212_holdings(path)

    holding = result["holdings"][0]
    assert holding["isin"] == "US0378331005"
    assert holding["ticker"] is None


def test_parse_trading212_holdings_missing_file_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        parse_trading212_holdings(tmp_path / "does-not-exist.pdf")
