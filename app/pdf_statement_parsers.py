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
import logging
import re
import subprocess
import tempfile
from pathlib import Path

import pdfplumber

# Trading 212's embedded font (see the Trading 212 section below) has a
# descriptor pdfminer can't parse a FontBBox out of — harmless since we only
# ever use this font's rendered glyphs (for OCR), never its bbox metrics,
# but pdfminer logs a warning on every character for every page opened, which
# is enough to swamp real output. Quieten just that logger, not warnings in
# general.
logging.getLogger("pdfminer").setLevel(logging.ERROR)

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
_HSBC_CLOSING_RE = re.compile(r"ClosingBalance\s*£([\d,]+\.\d{2})")


def _in_range(x: float, rng: tuple[float, float]) -> bool:
    return rng[0] <= x < rng[1]


def extract_hsbc_period(text: str) -> tuple[dt.date, dt.date] | None:
    m = _HSBC_PERIOD_RE.search(text)
    if not m:
        return None
    day1, month1, day2, month2, year = m.groups()
    return _resolve_period(day1, month1, day2, month2, int(year))


def extract_hsbc_summary(text: str) -> dict | None:
    joined = text.replace("\n", " ")
    m = _HSBC_SUMMARY_RE.search(joined)
    if not m:
        return None
    closing_m = _HSBC_CLOSING_RE.search(joined)
    return {
        "expected_in": float(m.group(1).replace(",", "")),
        "expected_out": float(m.group(2).replace(",", "")),
        # A normal account's own "ClosingBalance" is already the figure this
        # app stores directly — positive means money present, same as ours.
        "closing_balance": float(closing_m.group(1).replace(",", "")) if closing_m else None,
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
    _, credits, debits, closing = m.groups()
    return {
        "expected_in": float(credits.replace(",", "")),
        "expected_out": float(debits.replace(",", "")),
        # Amex states this as a positive "amount owed" — this app stores
        # credit-card debt as negative, so flip the sign here once, at the
        # source, rather than leaving every caller to remember to.
        "closing_balance": -float(closing.replace(",", "")),
    }


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
# Trading 212 "Confirmation of holdings" — a point-in-time snapshot of an
# investment account's positions (instrument, ISIN, quantity, price), not a
# list of transactions like the two formats above. Kept as its own
# detect/parse pair (parse_holdings_statement) rather than folded into
# parse_pdf_statement, since callers there expect a `transactions` list and
# there simply isn't one in this document.
#
# Trading 212 embeds a subsetted variable font ("Aeonik212VF...") whose
# ToUnicode map is broken for digits and some letters (e.g. 'a'): the glyphs
# still render correctly on screen/in print, but pdfplumber's text
# extraction returns them as blank — silently dropping every number, the
# customer's ISIN, and even words like "Trading". Regular text extraction is
# therefore useless here; this renders the page to an image and OCRs it with
# tesseract instead, which sees exactly what a human would.
# ---------------------------------------------------------------------------

_TRADING212_FONT_HINT = "Aeonik212"

_T212_DATE_RE = re.compile(r"as of\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.IGNORECASE)
_T212_VALUE_RE = re.compile(r"Holdings value:?\s*([\d,]+\.\d+)\s*([A-Z]{3})", re.IGNORECASE)
_T212_ACCOUNT_RE = re.compile(r"^Trading\s*212\s+([A-Za-z][A-Za-z ]*[A-Za-z])$")
_T212_ROW_RE = re.compile(
    r"^(?P<instrument>.+?)\s+(?P<isin>[A-Za-z0-9]{12})\s+(?P<quantity>[\d,]+(?:\.\d+)?)\s+"
    r"(?:(?P<currency>[A-Z]{3})\s+)?(?P<price>[\d,]+\.\d+)\s*$"
)

# ISIN -> Yahoo Finance ticker for instruments we've actually held. There's
# no general ISIN->ticker mapping available offline, so this only resolves
# what's been added here — anything else comes back with ticker=None for a
# human to fill in before calling set_holdings, rather than being guessed at.
_T212_ISIN_TICKERS = {
    "IE00BFMXXD54": "VUAG.L",  # Vanguard S&P 500 UCITS ETF (Acc)
    "IE00B3XXRP09": "VUSA.L",  # Vanguard S&P 500 UCITS ETF (Dist)
}


def _uses_trading212_font(path: Path) -> bool:
    with pdfplumber.open(path) as pdf:
        return any(_TRADING212_FONT_HINT in (c.get("fontname") or "") for c in pdf.pages[0].chars)


def detect_holdings_kind(path: Path) -> str | None:
    if _uses_trading212_font(path):
        return "trading212"
    return None


def _ocr_first_page(path: Path, resolution: int = 300) -> str:
    """Render the first page to an image and OCR it. Raises a clear error if
    the `tesseract` binary isn't installed, rather than an opaque
    FileNotFoundError from subprocess."""
    with pdfplumber.open(path) as pdf:
        image = pdf.pages[0].to_image(resolution=resolution)
    with tempfile.TemporaryDirectory() as tmp_dir:
        png_path = Path(tmp_dir) / "page.png"
        image.save(str(png_path))
        try:
            result = subprocess.run(
                ["tesseract", str(png_path), "stdout"],
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Reading a Trading 212 holdings PDF requires the `tesseract` OCR binary "
                "(its embedded font's text layer is broken for digits) — install it, e.g. "
                "`brew install tesseract`."
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"OCR of the holdings PDF failed: {exc.stderr}") from exc
    return result.stdout


def _resolve_isin_ticker(raw_isin: str) -> tuple[str, str | None]:
    """Clean up an OCR'd ISIN and look up its ticker. OCR reliably confuses
    the digit 0 with the letter O in these documents (e.g. "IE00..." reads
    as "IEOO..."); rather than blanket-correcting that (which could just as
    easily mangle a genuine letter), only apply the swap when it turns an
    unrecognised ISIN into one we actually know — an ISIN that doesn't match
    either way is returned as-is with ticker=None instead of being guessed
    at, since a wrong ticker would silently corrupt the account's weights."""
    isin = raw_isin.upper()
    if isin in _T212_ISIN_TICKERS:
        return isin, _T212_ISIN_TICKERS[isin]
    swapped = isin.replace("O", "0")
    if swapped in _T212_ISIN_TICKERS:
        return swapped, _T212_ISIN_TICKERS[swapped]
    return isin, None


def parse_trading212_holdings(file_path: str | Path) -> dict:
    """Parse a Trading 212 "Confirmation of holdings" PDF. Returns:
      - as_of: ISO date the snapshot is valid as of (None if not found)
      - account_hint: the Trading 212 product name (e.g. "Trading 212 Stocks
        ISA") to match against list_accounts' names, like parse_pdf_statement's
        account_hint
      - currency, holdings_value: the document's own stated total
      - holdings: [{instrument, isin, ticker, quantity, price, value}] — value
        is quantity*price, computed rather than parsed, since the document
        doesn't state it per-row
      - reconciliation: holdings_value vs the sum of computed values, the
        same cross-check parse_pdf_statement does against a statement's
        summary line, to catch an OCR/parsing mistake instead of silently
        importing wrong numbers

    This only reads the file — apply the result with set_holdings (weights =
    each holding's value) and update_account (current_balance =
    holdings_value, balance_as_of = as_of) yourself.
    """
    path = Path(file_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")

    text = _ocr_first_page(path)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    date_m = _T212_DATE_RE.search(text)
    as_of = dt.date(int(date_m.group(3)), int(date_m.group(2)), int(date_m.group(1))) if date_m else None

    value_m = _T212_VALUE_RE.search(text)
    holdings_value = float(value_m.group(1).replace(",", "")) if value_m else None
    currency = value_m.group(2).upper() if value_m else None

    account_hint = None
    for ln in lines:
        m = _T212_ACCOUNT_RE.match(ln)
        if m:
            account_hint = f"Trading 212 {m.group(1).strip()}"
            break

    holdings: list[dict] = []
    header_idx = next(
        (i for i, ln in enumerate(lines) if "ISIN" in ln.upper() and "QUANTITY" in ln.upper()), None
    )
    if header_idx is not None:
        for ln in lines[header_idx + 1 :]:
            m = _T212_ROW_RE.match(ln)
            if not m:
                break  # holding rows are contiguous right after the header
            isin, ticker = _resolve_isin_ticker(m.group("isin"))
            quantity = float(m.group("quantity").replace(",", ""))
            price = float(m.group("price").replace(",", ""))
            holdings.append(
                {
                    "instrument": m.group("instrument").strip(),
                    "isin": isin,
                    "ticker": ticker,
                    "quantity": quantity,
                    "price": price,
                    "value": round(quantity * price, 2),
                }
            )

    reconciliation = None
    if holdings_value is not None:
        computed = round(sum(h["value"] for h in holdings), 2)
        reconciliation = {
            "expected_value": holdings_value,
            "computed_value": computed,
            "ok": abs(holdings_value - computed) < 0.01,
        }

    return {
        "kind": "trading212",
        "as_of": as_of.isoformat() if as_of else None,
        "account_hint": account_hint,
        "currency": currency,
        "holdings_value": holdings_value,
        "holdings": holdings,
        "reconciliation": reconciliation,
    }


def parse_holdings_statement(file_path: str) -> dict:
    """Detect a broker holdings-confirmation PDF's format and parse it.
    Currently recognises Trading 212's "Confirmation of holdings" export.
    Returns kind=None (empty holdings) for anything else, so callers can
    fall back to read_pdf_statement / manual interpretation."""
    path = Path(file_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")

    kind = detect_holdings_kind(path)
    if kind is None:
        return {
            "kind": None,
            "as_of": None,
            "account_hint": None,
            "currency": None,
            "holdings_value": None,
            "holdings": [],
            "reconciliation": None,
        }
    return parse_trading212_holdings(path)


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
    closing_balance = None
    if summary is not None:
        closing_balance = summary.get("closing_balance")
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
        # The statement's own stated closing balance, already sign-adjusted
        # to this app's convention — pass straight to import_statement's
        # closing_balance param so a re-import can correct current_balance
        # even if it had drifted from something other than this statement.
        "closing_balance": closing_balance,
    }
