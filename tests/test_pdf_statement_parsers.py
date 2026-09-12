import datetime as dt

from app.pdf_statement_parsers import (
    detect_kind,
    extract_amex_period,
    extract_amex_summary,
    extract_hsbc_period,
    extract_hsbc_summary,
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
