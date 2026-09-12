"""Structured transaction parsers for the two PDF statement formats we get
regularly: HSBC Premier current account statements and Amex statements.

app/statements.py deliberately avoids structured PDF parsing in general
(bank layouts vary too much to parse generically), but these two specific
layouts are stable enough to be worth parsing precisely. `parse_pdf_statement`
is the entry point the app uses: it detects which of the two formats a file
is, extracts transactions + the billing period, and cross-checks the parsed
totals against the statement's own summary line (Payments In/Out for HSBC,
New Credits/Debits for Amex) so a template change that silently breaks
parsing gets flagged as a reconciliation mismatch instead of importing wrong
numbers.

Promoted from the one-off pass in scripts/classify_statements.py, where the
column positions and totals were originally validated against a year of real
statements.
"""

import datetime as dt
import re
from pathlib import Path

import pdfplumber

MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    )
}


def _month_from_name(name: str) -> int | None:
    return MONTHS.get(name[:3].title())


def _resolve_period(day1: str, month1: str, day2: str, month2: str, end_year: int) -> tuple[dt.date, dt.date]:
    """A period's header only states the year once, against the end date —
    infer the start date's year too, handling the Dec-to-Jan wraparound."""
    m1, m2 = _month_from_name(month1), _month_from_name(month2)
    if m1 is None or m2 is None:
        raise ValueError(f"Unrecognised month name in statement period: {month1!r} / {month2!r}")
    start_year = end_year - 1 if m1 > m2 else end_year
    return dt.date(start_year, m1, int(day1)), dt.date(end_year, m2, int(day2))


def _year_for_month(month: int, period: tuple[dt.date, dt.date]) -> int:
    start, end = period
    if start.year == end.year:
        return end.year
    return start.year if month == start.month else end.year


def _page_text(path: Path, limit: int | None = 1) -> str:
    with pdfplumber.open(path) as pdf:
        pages = pdf.pages if limit is None else pdf.pages[:limit]
        return "\n".join((p.extract_text() or "") for p in pages)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

ACCOUNT_HINTS = {"hsbc_premier": "HSBC", "amex": "Amex"}


def detect_kind(path: Path) -> str | None:
    text = _page_text(path, limit=1)
    if "HSBC Premier" in text or "Your Premier Bank Account" in text:
        return "hsbc_premier"
    if "americanexpress.co.uk" in text or "American Express" in text:
        return "amex"
    return None


# ---------------------------------------------------------------------------
# HSBC Premier current account statement
# ---------------------------------------------------------------------------
# Column x-ranges read off the statement's word positions (pdfplumber
# extract_words). The template is stable across statements/pages.
_PAID_OUT_X = (345, 400)
_PAID_IN_X = (425, 480)
_BALANCE_X = (500, 535)
_DATE_DAY_X = (48, 58)
_SKIP_DESC_PREFIXES = ("BALANCEBROUGHTFORWARD", "BALANCECARRIEDFORWARD")
_MARKER_TOKENS = {"DD", "VIS", "BP", "CR", "OBP", "ATM", "TFR", "SO", "CHG", ")))", "A", "."}
_HSBC_PERIOD_RE = re.compile(r"(\d{1,2})\s+(\w+)\s+to\s+(\d{1,2})\s+(\w+)\s+(\d{4})")
_HSBC_SUMMARY_RE = re.compile(r"Payments In\s*£([\d,]+\.\d{2})\s*Payments Out\s*£([\d,]+\.\d{2})")


def _in_range(x: float, rng: tuple[float, float]) -> bool:
    return rng[0] <= x < rng[1]


def extract_hsbc_period(text: str) -> tuple[dt.date, dt.date] | None:
    m = _HSBC_PERIOD_RE.search(text)
    if not m:
        return None
    day1, month1, day2, month2, year = m.groups()
    return _resolve_period(day1, month1, day2, month2, int(year))


def extract_hsbc_summary(text: str) -> dict | None:
    m = _HSBC_SUMMARY_RE.search(text.replace("\n", " "))
    if not m:
        return None
    return {
        "expected_in": float(m.group(1).replace(",", "")),
        "expected_out": float(m.group(2).replace(",", "")),
    }


def parse_hsbc_premier(path: Path) -> list[dict]:
    transactions: list[dict] = []
    current_date = None  # persists across pages within this document
    desc_words: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            if not any(w["text"] == "Payment" for w in words):
                continue  # skip T&C / info pages without a transaction table

            rows: dict[float, list] = {}
            for w in words:
                rows.setdefault(round(w["top"], 0), []).append(w)

            header_passed = False
            for top in sorted(rows.keys()):
                row_words = sorted(rows[top], key=lambda w: w["x0"])
                if not header_passed:
                    if any(w["text"] == "£Paid" for w in row_words):
                        header_passed = True
                    continue

                day_word = next((w for w in row_words if _in_range(w["x0"], _DATE_DAY_X)), None)
                if day_word and day_word["text"].isdigit():
                    try:
                        day = int(day_word["text"])
                        month = MONTHS.get(row_words[1]["text"])
                        year = 2000 + int(row_words[2]["text"])
                        if month:
                            current_date = f"{year:04d}-{month:02d}-{day:02d}"
                            row_words = row_words[3:]
                    except (ValueError, IndexError, KeyError):
                        pass

                paid_out = paid_in = None
                text_words = []
                for w in row_words:
                    txt, x0 = w["text"], w["x0"]
                    if _in_range(x0, _BALANCE_X):
                        continue
                    if _in_range(x0, _PAID_OUT_X) and re.match(r"^[\d,]+\.\d{2}$", txt):
                        paid_out = float(txt.replace(",", ""))
                        continue
                    if _in_range(x0, _PAID_IN_X) and re.match(r"^[\d,]+\.\d{2}$", txt):
                        paid_in = float(txt.replace(",", ""))
                        continue
                    if txt not in _MARKER_TOKENS:
                        text_words.append(txt)

                if text_words:
                    desc_words.append(" ".join(text_words))

                full_desc = " ".join(desc_words).strip()
                if full_desc.startswith(_SKIP_DESC_PREFIXES):
                    desc_words = []
                    continue

                if paid_out is not None or paid_in is not None:
                    if full_desc:
                        amt = paid_out if paid_out is not None else paid_in
                        assert amt is not None
                        transactions.append(
                            {
                                "date": current_date,
                                "description": full_desc,
                                "amount": -amt if paid_out is not None else amt,
                                "category_hint": "",
                                "source_format": "hsbc_premier_pdf",
                            }
                        )
                    desc_words = []
    return transactions


# ---------------------------------------------------------------------------
# Amex statement
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"^([A-Z][a-z]{2})(\d{1,2})\s+([A-Z][a-z]{2})(\d{1,2})\s+(.*)$")
_AMOUNT_RE = re.compile(r"([\d,]+\.\d{2})\s*$")
_AMEX_PERIOD_RE = re.compile(
    r"Statement Period From\s*(\d{1,2})\s*([A-Za-z]+)\s*to\s*(\d{1,2})\s*([A-Za-z]+)\s*(\d{4})"
)
_AMEX_SUMMARY_RE = re.compile(
    r"£([\d,]+\.\d{2})\s*-\s*£([\d,]+\.\d{2})\s*\+\s*£([\d,]+\.\d{2})\s*=\s*£([\d,]+\.\d{2})"
)


def extract_amex_period(text: str) -> tuple[dt.date, dt.date] | None:
    m = _AMEX_PERIOD_RE.search(text)
    if not m:
        return None
    day1, month1, day2, month2, year = m.groups()
    return _resolve_period(day1, month1, day2, month2, int(year))


def extract_amex_summary(text: str) -> dict | None:
    m = _AMEX_SUMMARY_RE.search(text)
    if not m:
        return None
    _, credits, debits, _ = m.groups()
    return {"expected_in": float(credits.replace(",", "")), "expected_out": float(debits.replace(",", ""))}


def parse_amex(path: Path, period: tuple[dt.date, dt.date] | None = None) -> list[dict]:
    """`period` (start, end) resolves the year for each transaction's month —
    pass the result of extract_amex_period so a statement spanning a
    Dec/Jan boundary still dates its transactions correctly. Falls back to
    the current year for every transaction if omitted (degrades gracefully
    when period extraction itself fails)."""
    with pdfplumber.open(path) as pdf:
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    lines = full_text.split("\n")
    transactions: list[dict] = []

    in_table = False
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.startswith("Date Date Transaction Details"):
            in_table = True
            continue
        if line.startswith(("Total new spend transactions", "How you can pay")):
            in_table = False
            continue
        if not in_table or not line:
            continue

        m = _DATE_RE.match(line)
        if not m:
            continue  # continuation line (GOODS/CR marker, forex sub-line, address)
        _, _, mon2, day2, rest = m.groups()
        amt_m = _AMOUNT_RE.search(rest)
        if not amt_m:
            continue
        amount = float(amt_m.group(1).replace(",", ""))
        desc = rest[: amt_m.start()].strip()
        month_num = MONTHS.get(mon2)
        if not month_num:
            continue
        year = _year_for_month(month_num, period) if period else dt.datetime.now(dt.UTC).year
        date = f"{year:04d}-{month_num:02d}-{int(day2):02d}"

        # A "CR" marker on a continuation line (up to the next dated line)
        # means this was a credit/refund, not spend — keep it positive.
        is_credit = False
        for j in range(idx + 1, len(lines)):
            nxt = lines[j].strip()
            if _DATE_RE.match(nxt):
                break
            if nxt == "CR" or re.search(r"\bCR\b", nxt):
                is_credit = True
                break

        transactions.append(
            {
                "date": date,
                "description": desc,
                "amount": amount if is_credit else -amount,
                "category_hint": "",
                "source_format": "amex_pdf",
            }
        )
    return transactions


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------


def parse_pdf_statement(file_path: str) -> dict:
    """Detect the PDF's format, extract transactions + billing period + an
    account-name hint, and reconcile parsed totals against the statement's
    own summary. Returns kind=None (empty transactions) for anything that
    isn't one of the two known formats, so callers can fall back to the
    manual chat-based import path."""
    path = Path(file_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")

    kind = detect_kind(path)
    if kind is None:
        return {
            "kind": None,
            "transactions": [],
            "period_start": None,
            "period_end": None,
            "account_hint": None,
            "reconciliation": None,
        }

    first_page_text = _page_text(path, limit=1)

    if kind == "hsbc_premier":
        period = extract_hsbc_period(first_page_text)
        summary = extract_hsbc_summary(first_page_text)
        transactions = parse_hsbc_premier(path)
    else:
        period = extract_amex_period(first_page_text)
        summary = extract_amex_summary(first_page_text)
        transactions = parse_amex(path, period=period)

    reconciliation = None
    if summary is not None:
        actual_in = round(sum(t["amount"] for t in transactions if t["amount"] > 0), 2)
        actual_out = round(sum(-t["amount"] for t in transactions if t["amount"] < 0), 2)
        reconciliation = {
            "expected_in": summary["expected_in"],
            "expected_out": summary["expected_out"],
            "actual_in": actual_in,
            "actual_out": actual_out,
            "ok": (
                abs(summary["expected_in"] - actual_in) < 0.01
                and abs(summary["expected_out"] - actual_out) < 0.01
            ),
        }

    return {
        "kind": kind,
        "transactions": transactions,
        "period_start": period[0].isoformat() if period else None,
        "period_end": period[1].isoformat() if period else None,
        "account_hint": ACCOUNT_HINTS.get(kind),
        "reconciliation": reconciliation,
    }
