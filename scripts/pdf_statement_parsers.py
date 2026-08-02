"""Structured transaction parsers for the two PDF statement formats we get
regularly: HSBC Premier current account statements and Amex statements.

app/statements.py deliberately avoids structured PDF parsing (bank layouts
vary too much to parse generically), but these two specific layouts are
stable enough to be worth parsing precisely — every total here has been
cross-checked against the statement's own "Payments In/Out" (HSBC) or
"New Debits" (Amex) summary line.
"""

import re
from pathlib import Path

import pdfplumber

MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


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


def _in_range(x, rng):
    return rng[0] <= x < rng[1]


def parse_hsbc_premier(path: Path, account: str) -> list[dict]:
    transactions = []
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
                        transactions.append({
                            "date": current_date,
                            "description": full_desc,
                            "amount": -amt if paid_out is not None else amt,
                            "account": account,
                            "source_file": path.name,
                        })
                    desc_words = []
    return transactions


# ---------------------------------------------------------------------------
# Amex statement
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"^([A-Z][a-z]{2})(\d{1,2})\s+([A-Z][a-z]{2})(\d{1,2})\s+(.*)$")
_AMOUNT_RE = re.compile(r"([\d,]+\.\d{2})\s*$")
_EXCLUDE_DESC_SUBSTRINGS = ("PAYMENT RECEIVED",)


def parse_amex(path: Path, account: str, year: int) -> list[dict]:
    transactions = []
    with pdfplumber.open(path) as pdf:
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    lines = full_text.split("\n")

    in_table = False
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.startswith("Date Date Transaction Details"):
            in_table = True
            continue
        if line.startswith("Total new spend transactions") or line.startswith("How you can pay"):
            in_table = False
            continue
        if not in_table or not line:
            continue

        m = _DATE_RE.match(line)
        if not m:
            continue  # continuation line (GOODS/FUEL/CR marker, forex sub-line, address)
        _, _, mon2, day2, rest = m.groups()
        amt_m = _AMOUNT_RE.search(rest)
        if not amt_m:
            continue
        amount = float(amt_m.group(1).replace(",", ""))
        desc = rest[:amt_m.start()].strip()
        month_num = MONTHS.get(mon2)
        if not month_num:
            continue
        date = f"{year:04d}-{month_num:02d}-{int(day2):02d}"

        if any(s in desc for s in _EXCLUDE_DESC_SUBSTRINGS):
            continue

        # A "CR" marker anywhere in this transaction's continuation lines
        # (forex sub-lines etc.) up to the next dated line means it was a
        # credit/refund, not spend — keep it positive (inflow).
        is_credit = False
        for j in range(idx + 1, len(lines)):
            nxt = lines[j].strip()
            if _DATE_RE.match(nxt):
                break
            if nxt == "CR" or re.search(r"\bCR\b", nxt):
                is_credit = True
                break

        transactions.append({
            "date": date,
            "description": desc,
            "amount": amount if is_credit else -amount,
            "account": account,
            "source_file": path.name,
        })
    return transactions
