import csv
import datetime as dt
import re
import statistics
from collections import defaultdict
from pathlib import Path

import pdfplumber

MONZO_EXPECTED_COLUMNS = {"Transaction ID", "Date", "Amount"}

_DATE_FORMATS = (
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d %b %y",
)

_FREQUENCY_ANCHORS = (
    (7, "Weekly"),
    (14, "Fortnightly"),
    (28, "4-Weekly"),
    (30.4, "Monthly"),
    (91, "Quarterly"),
    (182, "6-Monthly"),
    (365, "Annually"),
)


def _resolve_path(file_path: str) -> Path:
    path = Path(file_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")
    return path


def read_pdf_text(file_path: str, first_page: int = 1, last_page: int | None = None) -> str:
    """Raw text extraction from a PDF statement, one page at a time.

    This is a deliberate fallback rather than a structured parser: bank PDF
    layouts vary a lot and transactions often span multiple lines, so the
    reliable approach is to hand the caller readable text and let them
    interpret it, rather than pretend to fully parse an arbitrary bank's
    table layout.
    """
    path = _resolve_path(file_path)
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        start = max(1, first_page)
        end = total if last_page is None else min(last_page, total)
        chunks = []
        for i in range(start - 1, end):
            text = pdf.pages[i].extract_text() or ""
            chunks.append(f"--- Page {i + 1}/{total} ---\n{text}")
    return "\n\n".join(chunks)


def _try_parse_date(value: str) -> dt.date | None:
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def extract_csv_transactions(file_path: str) -> list[dict]:
    """Parse a bank/card CSV export into transactions.

    Amount is signed: positive = money in, negative = money out. Recognises
    the Monzo export format specifically; otherwise falls back to sniffing
    for Date / Description / Amount style columns, which covers most UK
    bank CSV exports.
    """
    path = _resolve_path(file_path)
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if MONZO_EXPECTED_COLUMNS.issubset(set(fieldnames)):
        transactions = []
        for row in rows:
            date = _try_parse_date(row.get("Date", ""))
            if date is None:
                continue
            try:
                amount = float(row.get("Amount", "0") or 0)
            except ValueError:
                continue
            transactions.append(
                {
                    "date": date.isoformat(),
                    "description": row.get("Name") or row.get("Description") or "",
                    "amount": amount,
                    "category_hint": row.get("Category", ""),
                    "source_format": "monzo_csv",
                }
            )
        return transactions

    fields_lower = {fn.lower(): fn for fn in fieldnames}
    date_col = next((fields_lower[k] for k in fields_lower if "date" in k), None)
    amount_col = next((fields_lower[k] for k in fields_lower if "amount" in k), None)
    desc_col = next(
        (
            fields_lower[k]
            for k in fields_lower
            if k in ("description", "name", "payee", "merchant", "memo", "details")
        ),
        None,
    )
    if not (date_col and amount_col):
        raise ValueError(
            f"Couldn't recognise this CSV's columns (found: {fieldnames}). "
            "Expected a Date column and an Amount column."
        )

    transactions = []
    for row in rows:
        date = _try_parse_date(row.get(date_col, ""))
        if date is None:
            continue
        raw_amount = str(row.get(amount_col, "0")).replace(",", "").replace("£", "").strip()
        try:
            amount = float(raw_amount)
        except ValueError:
            continue
        transactions.append(
            {
                "date": date.isoformat(),
                "description": row.get(desc_col, "") if desc_col else "",
                "amount": amount,
                "category_hint": "",
                "source_format": "generic_csv",
            }
        )
    return transactions


def normalize_description(description: str) -> str:
    text = description.upper()
    text = re.sub(r"\d{4,}", "", text)  # strip long reference/card numbers
    text = re.sub(r"[^A-Z0-9 ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return " ".join(text.split()[:4])  # merchant name is usually the first few words


def _guess_frequency(gaps_days: list[int]) -> str:
    if not gaps_days:
        return "Unknown"
    median_gap = statistics.median(gaps_days)
    return min(_FREQUENCY_ANCHORS, key=lambda c: abs(c[0] - median_gap))[1]


def find_recurring_transactions(
    transactions: list[dict], min_occurrences: int = 2, amount_tolerance_pct: float = 0.15
) -> list[dict]:
    """Group transactions by normalised merchant name and flag candidates
    that look like a recurring bill.

    Each input transaction should be a dict with `date` (ISO string),
    `description`, and `amount` (signed: + in, - out). Feed this the output
    of extract_csv_transactions, or a list assembled by hand from
    read_pdf_text. With fewer than 4 occurrences, amounts must stay within
    amount_tolerance_pct of their average to count as "recurring" (otherwise
    two unrelated purchases at the same shop would look like a bill); with
    4+ occurrences the amount is allowed to vary more freely, since that's
    good evidence of a usage-based recurring charge (e.g. fuel, cloud
    hosting) even when we can't pin one figure to it.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in transactions:
        key = normalize_description(str(t.get("description", "")))
        if key:
            groups[key].append(t)

    results = []
    for key, items in groups.items():
        if len(items) < min_occurrences:
            continue
        amounts = [abs(float(i["amount"])) for i in items]
        avg = statistics.mean(amounts)
        if avg == 0:
            continue
        spread = (max(amounts) - min(amounts)) / avg
        if spread > amount_tolerance_pct and len(items) < 4:
            continue

        dates = sorted(dt.date.fromisoformat(i["date"]) for i in items)
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        days_of_month = [d.day for d in dates]

        results.append(
            {
                "description": key,
                "sample_descriptions": sorted({str(i["description"]) for i in items})[:3],
                "occurrences": len(items),
                "first_date": dates[0].isoformat(),
                "last_date": dates[-1].isoformat(),
                "avg_amount": round(avg, 2),
                "min_amount": round(min(amounts), 2),
                "max_amount": round(max(amounts), 2),
                "typical_day_of_month": statistics.mode(days_of_month),
                "guessed_frequency": _guess_frequency(gaps),
                "direction": "in" if statistics.mean([float(i["amount"]) for i in items]) > 0 else "out",
            }
        )

    results.sort(key=lambda r: -r["occurrences"])
    return results
