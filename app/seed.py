import datetime as dt

from sqlalchemy.orm import Session

from app.models import Account, BudgetItem, Category, FlowType, Frequency, UpcomingExpense

DEFAULT_ACCOUNTS = [
    # name, current_balance, low_balance_threshold
    ("Personal HSBC", 0.0, None),
    ("Joint Monzo", 0.0, None),
    ("Personal Monzo", 0.0, None),
    ("Personal Amex", 0.0, None),
    ("Unverified / Other", 0.0, None),
]

DEFAULT_CATEGORIES = [
    "Income",
    "Housing",
    "Utilities",
    "Insurance",
    "Transport",
    "Subscriptions",
    "Personal",
]

# Carried over from the household budget spreadsheet, cross-checked against
# ~3-12 months of real HSBC/Monzo/Amex statement history. Items whose source
# account couldn't be confirmed are parked on "Unverified / Other".
SEED_BUDGET_ITEMS = [
    # description, amount, category, account, frequency, effective_from, notes
    (
        "Rent / Mortgage",
        2800.00,
        "Housing",
        "Joint Monzo",
        Frequency.MONTHLY,
        dt.date(2025, 8, 17),
        "Confirmed 12/12 months via TLP RE Client Acc (letting agent)",
    ),
    (
        "Electricity",
        68.76,
        "Utilities",
        "Joint Monzo",
        Frequency.MONTHLY,
        dt.date(2025, 8, 1),
        "E.ON Next DD — avg £68.76 (range £62-76)",
    ),
    (
        "WiFi",
        20.56,
        "Utilities",
        "Joint Monzo",
        Frequency.MONTHLY,
        dt.date(2025, 8, 5),
        "Community Fibre DD — confirmed, very stable",
    ),
    (
        "Home insurance",
        23.00,
        "Insurance",
        "Unverified / Other",
        Frequency.MONTHLY,
        dt.date(2025, 8, 1),
        "Not found in reviewed statements — verify still active / correct amount",
    ),
    (
        "Vet insurance",
        23.02,
        "Insurance",
        "Joint Monzo",
        Frequency.MONTHLY,
        dt.date(2026, 4, 9),
        "Urban Jungle DD — renewed from £15.02 on this date",
    ),
    (
        "Car tax — B355RHC (DVLA)",
        31.50,
        "Transport",
        "Personal HSBC",
        Frequency.MONTHLY,
        dt.date(2025, 8, 1),
        None,
    ),
    (
        "Car tax — C800BWR (DVLA)",
        32.81,
        "Transport",
        "Personal HSBC",
        Frequency.MONTHLY,
        dt.date(2025, 8, 1),
        None,
    ),
    (
        "Car insurance (Elephant)",
        77.53,
        "Transport",
        "Personal HSBC",
        Frequency.MONTHLY,
        dt.date(2025, 8, 28),
        None,
    ),
    (
        "Fuel",
        102.54,
        "Transport",
        "Joint Monzo",
        Frequency.MONTHLY,
        dt.date(2025, 8, 1),
        "Variable card spend — budgeted estimate, not a fixed DD",
    ),
    ("Spotify", 17.66, "Subscriptions", "Personal Monzo", Frequency.MONTHLY, dt.date(2025, 8, 1), None),
    ("Netflix", 5.99, "Subscriptions", "Joint Monzo", Frequency.MONTHLY, dt.date(2025, 8, 5), None),
    ("Amazon Prime", 8.99, "Subscriptions", "Personal Amex", Frequency.MONTHLY, dt.date(2025, 8, 16), None),
    (
        "Apple",
        3.00,
        "Subscriptions",
        "Unverified / Other",
        Frequency.MONTHLY,
        dt.date(2025, 8, 1),
        "Not confirmed monthly — only a one-off £53.99 Amex charge seen",
    ),
    (
        "Google",
        2.00,
        "Subscriptions",
        "Unverified / Other",
        Frequency.MONTHLY,
        dt.date(2025, 8, 1),
        "Not found in reviewed statements",
    ),
    (
        "TV licence",
        36.00,
        "Subscriptions",
        "Joint Monzo",
        Frequency.MONTHLY,
        dt.date(2025, 8, 1),
        "Actual is £36, was budgeted £15; only seen Jun & Jul (may be 10-month DD scheme)",
    ),
    (
        "AWS",
        23.53,
        "Subscriptions",
        "Personal Monzo",
        Frequency.MONTHLY,
        dt.date(2025, 8, 1),
        "Usage-based — avg £23.53 (range ~£14-33)",
    ),
    (
        "Claude.ai",
        18.00,
        "Subscriptions",
        "Personal Amex",
        Frequency.MONTHLY,
        dt.date(2025, 8, 28),
        "Confirmed 3/3 months",
    ),
    (
        "Voxi (Vodafone) mobile",
        10.00,
        "Subscriptions",
        "Personal HSBC",
        Frequency.MONTHLY,
        dt.date(2025, 8, 8),
        "Variable £4.80-16",
    ),
    (
        "Nathan",
        150.00,
        "Personal",
        "Unverified / Other",
        Frequency.MONTHLY,
        dt.date(2025, 8, 1),
        "Not found in reviewed statements — verify (may be cash/another account)",
    ),
    (
        "Swimming",
        45.00,
        "Personal",
        "Unverified / Other",
        Frequency.MONTHLY,
        dt.date(2025, 8, 1),
        "Not found in reviewed statements — verify (may be cash/another account)",
    ),
    (
        "The Riders Hub",
        15.00,
        "Personal",
        "Unverified / Other",
        Frequency.MONTHLY,
        dt.date(2025, 8, 1),
        "Low confidence — appears sporadically, possibly a lesson/hobby subscription",
    ),
]

SEED_UPCOMING_EXPENSES = [
    # date, description, amount, category, account, notes-in-description-only
    (dt.date(2026, 10, 14), "Thames Water (6-monthly)", 188.00, "Utilities", "Joint Monzo"),
    (dt.date(2027, 4, 27), "Council Tax (Wandsworth) — annual", 1020.35, "Housing", "Joint Monzo"),
]


def get_or_create_account(
    session: Session, name: str, balance: float = 0.0, threshold: float | None = None
) -> Account:
    account = session.query(Account).filter_by(name=name).one_or_none()
    if account is None:
        account = Account(
            name=name, current_balance=balance, balance_as_of=dt.date.today(), low_balance_threshold=threshold
        )
        session.add(account)
        session.flush()
    return account


def get_or_create_category(session: Session, name: str) -> Category:
    category = session.query(Category).filter_by(name=name).one_or_none()
    if category is None:
        category = Category(name=name)
        session.add(category)
        session.flush()
    return category


def seed_defaults(session: Session) -> None:
    for name, balance, threshold in DEFAULT_ACCOUNTS:
        get_or_create_account(session, name, balance, threshold)
    for name in DEFAULT_CATEGORIES:
        get_or_create_category(session, name)
    session.commit()

    # Only seed historical budget items / upcoming expenses once, on a
    # genuinely empty database — never re-insert on subsequent launches.
    if session.query(BudgetItem).count() == 0:
        for description, amount, cat_name, acc_name, frequency, eff_from, notes in SEED_BUDGET_ITEMS:
            session.add(
                BudgetItem(
                    description=description,
                    amount=amount,
                    flow_type=FlowType.EXPENSE,
                    frequency=frequency,
                    effective_from=eff_from,
                    effective_until=None,
                    notes=notes,
                    category=get_or_create_category(session, cat_name),
                    account=get_or_create_account(session, acc_name),
                )
            )
        session.commit()

    if session.query(UpcomingExpense).count() == 0:
        for date, description, amount, cat_name, acc_name in SEED_UPCOMING_EXPENSES:
            session.add(
                UpcomingExpense(
                    date=date,
                    description=description,
                    amount=amount,
                    category=get_or_create_category(session, cat_name),
                    account=get_or_create_account(session, acc_name),
                )
            )
        session.commit()
