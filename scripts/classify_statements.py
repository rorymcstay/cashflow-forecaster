"""One-off (but reusable) pass: parse every statement in a folder, classify
every transaction with app.classify, and report category totals plus
anything the keyword bank couldn't classify.

Usage:
    uv run python3 scripts/classify_statements.py "~/Downloads/bank statements" \
        --out /path/to/classified_transactions.csv

Expects the same file naming this budget's statements have used so far:
  - Monzo CSV exports named "Monzo Data Export*.csv" (first one = Joint Monzo,
    "* copy.csv" = Personal Monzo — adjust ACCOUNT_HINTS below if that changes)
  - HSBC Premier PDFs named "*Premier Bank_Statement.pdf"
  - Amex PDFs named "<start>_<Mon>_<year>_-_<end>_<Mon>_<year>.pdf"
"""

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.classify import classify
from app.pdf_statement_parsers import extract_amex_period, parse_amex, parse_hsbc_premier
from app.statements import extract_csv_transactions


def load_all_transactions(folder: Path) -> list[dict]:
    txns: list[dict] = []

    monzo_files = sorted(folder.glob("Monzo Data Export*.csv"))
    for f in monzo_files:
        account = "Personal Monzo" if "copy" in f.stem.lower() else "Joint Monzo"
        for t in extract_csv_transactions(str(f)):
            txns.append(
                {
                    "date": t["date"],
                    "description": t["description"],
                    "amount": t["amount"],
                    "account": account,
                    "source_file": f.name,
                    "bank_category_hint": t.get("category_hint") or None,
                }
            )

    for f in sorted(folder.glob("*Premier Bank_Statement.pdf")):
        for t in parse_hsbc_premier(f):
            txns.append({**t, "account": "Personal HSBC", "source_file": f.name})

    for f in sorted(folder.glob("*_-_*.pdf")):
        with pdfplumber.open(f) as pdf:
            first_page_text = pdf.pages[0].extract_text() or ""
        period = extract_amex_period(first_page_text)
        for t in parse_amex(f, period=period):
            txns.append({**t, "account": "Personal Amex", "source_file": f.name})

    return [t for t in txns if t["date"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="write full classified CSV here")
    ap.add_argument(
        "--exclude-months",
        nargs="*",
        default=[],
        help="YYYY-MM months to exclude from the summary (e.g. a partial current month)",
    )
    args = ap.parse_args()

    # Monzo's own per-transaction category is a reasonable fallback when our
    # keyword bank misses — but only for the categories that map cleanly onto
    # ours; Monzo's "Bills"/"General"/"Finances" are too much of a catch-all
    # to trust blindly, so those are left for manual review.
    monzo_fallback_map = {
        "Eating out": "Eating out",
        "Groceries": "Groceries",
        "Entertainment": "Entertainment",
        "Shopping": "Shopping",
        "Holidays": "Holidays",
        "Transport": "Transport",
        "Transfers": "Transfers",
        "Savings": "Transfers",
        "Income": "Income",
    }

    folder = args.folder.expanduser()
    txns = load_all_transactions(folder)
    for t in txns:
        cat = classify(t["description"])
        source = "keyword"
        if cat is None:
            hint = t.get("bank_category_hint")
            mapped = monzo_fallback_map.get(hint)
            if mapped:
                cat, source = mapped, "bank_hint"
        t["category"] = cat
        t["category_source"] = source if cat else None

    if args.out:
        fields = ["date", "account", "description", "amount", "category", "category_source", "source_file"]
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for t in sorted(txns, key=lambda t: t["date"]):
                w.writerow({k: t.get(k) for k in fields})
        print(f"Wrote {len(txns)} classified transactions to {args.out}\n")

    spend = [t for t in txns if t["amount"] < 0 and t["date"][:7] not in args.exclude_months]
    total_spend = sum(-t["amount"] for t in spend)
    unclassified = [t for t in spend if t["category"] is None]

    by_cat = defaultdict(lambda: {"n": 0, "total": 0.0, "months": defaultdict(float)})
    for t in spend:
        cat = t["category"] or "(unclassified)"
        by_cat[cat]["n"] += 1
        by_cat[cat]["total"] += -t["amount"]
        by_cat[cat]["months"][t["date"][:7]] += -t["amount"]

    print(f"{len(txns)} total transactions, {len(spend)} spend transactions, £{total_spend:,.2f} total spend")
    print(
        f"{len(unclassified)} unclassified spend transactions (£{sum(-t['amount'] for t in unclassified):,.2f})\n"
    )

    print(f"{'Category':<20}{'n':>5}{'Total':>12}{'Avg/mo':>10}{'Median/mo':>12}")
    for cat, d in sorted(by_cat.items(), key=lambda kv: -kv[1]["total"]):
        months = list(d["months"].values())
        avg_mo = d["total"] / len(months) if months else 0
        med_mo = statistics.median(months) if months else 0
        print(f"{cat:<20}{d['n']:>5}{d['total']:>12,.2f}{avg_mo:>10,.2f}{med_mo:>12,.2f}")

    if unclassified:
        print("\nTop unclassified merchants (by spend):")
        by_desc = defaultdict(lambda: [0, 0.0])
        for t in unclassified:
            key = t["description"][:40]
            by_desc[key][0] += 1
            by_desc[key][1] += -t["amount"]
        for desc, (n, total) in sorted(by_desc.items(), key=lambda kv: -kv[1][1])[:40]:
            print(f"  {total:>8.2f}  n={n:<3} {desc}")


if __name__ == "__main__":
    main()
