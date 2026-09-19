import datetime as dt
import enum

from sqlalchemy import Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class FlowType(enum.Enum):
    INCOME = "Income"
    EXPENSE = "Expense"
    TRANSFER = "Transfer"


class Frequency(enum.Enum):
    WEEKLY = "Weekly"
    FORTNIGHTLY = "Fortnightly"
    FOUR_WEEKLY = "4-Weekly"
    MONTHLY = "Monthly"
    QUARTERLY = "Quarterly"
    SIX_MONTHLY = "6-Monthly"
    ANNUALLY = "Annually"


# Number of occurrences per calendar year, used to normalise any frequency to
# a monthly-equivalent figure for the Budget summary screen.
OCCURRENCES_PER_YEAR = {
    Frequency.WEEKLY: 52,
    Frequency.FORTNIGHTLY: 26,
    Frequency.FOUR_WEEKLY: 13,
    Frequency.MONTHLY: 12,
    Frequency.QUARTERLY: 4,
    Frequency.SIX_MONTHLY: 2,
    Frequency.ANNUALLY: 1,
}


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    current_balance: Mapped[float] = mapped_column(Float, default=0.0)
    balance_as_of: Mapped[dt.date] = mapped_column(Date, default=dt.date.today)
    low_balance_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Un-invested cash sitting in an investment account, tracked separately
    # from its holdings. For an account with any holdings, current_balance is
    # no longer a free-standing figure — it's kept in sync (see
    # app/investments.py:refresh_investment_value) with
    # cash_position + the live market value of those holdings. Meaningless
    # (always 0) for a plain account with no holdings.
    cash_position: Mapped[float] = mapped_column(Float, default=0.0)
    # Annual rate as a percentage (e.g. 4.5 for 4.5% APY), compounded monthly
    # in the cashflow forecast for any account that has one set — independent
    # of how the account is otherwise used.
    growth_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Credit card autopay: on cc_payment_day each month, a direct debit is
    # projected from cc_payee_account into this account, sized either to
    # clear the outstanding balance (cc_pay_in_full) or to a fixed
    # cc_fixed_payment_amount. Balance here follows the same sign convention
    # as every other account — negative means money owed.
    is_credit_card: Mapped[bool] = mapped_column(Boolean, default=False)
    cc_payee_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    cc_payment_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cc_pay_in_full: Mapped[bool] = mapped_column(Boolean, default=False)
    cc_fixed_payment_amount: Mapped[float | None] = mapped_column(Float, nullable=True)

    budget_items: Mapped[list["BudgetItem"]] = relationship(
        back_populates="account", foreign_keys="BudgetItem.account_id"
    )
    upcoming_expenses: Mapped[list["UpcomingExpense"]] = relationship(
        back_populates="account", foreign_keys="UpcomingExpense.account_id"
    )
    holdings: Mapped[list["Holding"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    cc_payee_account: Mapped["Account | None"] = relationship(
        "Account", remote_side="Account.id", foreign_keys=[cc_payee_account_id]
    )

    def __repr__(self) -> str:
        return self.name

    @property
    def share_quantities(self) -> dict[str, float]:
        """{ticker: number of shares held} — presence of any holdings marks
        this an investment account, driving both the historical Monte Carlo
        simulation and (via the mean historical return) the deterministic
        cashflow forecast. This is the stored, price-independent quantity;
        for relative portfolio weights (which need a live price per ticker)
        see app/investments.py:portfolio_weights."""
        return {h.ticker: h.quantity for h in self.holdings}


class Holding(Base):
    """A ticker + number of shares held within an investment account."""

    __tablename__ = "holdings"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    ticker: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[float] = mapped_column(Float)
    # Cost basis per share (what you actually paid, on average) — optional,
    # since it's not something a live price feed can tell you. Lets
    # app/investments.py:holdings_detail report real unrealized gain/loss
    # against the live price, not just the market's historical return.
    average_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    account: Mapped["Account"] = relationship(back_populates="holdings")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)

    def __repr__(self) -> str:
        return self.name


class VendorGroup(Base):
    """A named collection of vendors (merchant keys) that can be reported on
    as a single unit — an alternative axis to Category for the Insights
    screen (see app/analytics.py), e.g. grouping "Uber"/"Uber Eats"/"Bolt"
    under "Rideshare & Delivery" regardless of which Category each
    transaction happened to classify into."""

    __tablename__ = "vendor_groups"
    __table_args__ = (UniqueConstraint("name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    # Comma-separated normalised merchant keys — same convention/format as
    # BudgetItem.vendors (see app/transactions.py merchant_key).
    vendors: Mapped[str | None] = mapped_column(String(4000), nullable=True)

    def __repr__(self) -> str:
        return self.name

    @property
    def vendor_list(self) -> list[str]:
        return self.vendors.split(",") if self.vendors else []

    @vendor_list.setter
    def vendor_list(self, keys: list[str]) -> None:
        self.vendors = ",".join(keys) if keys else None


class BudgetStatus(enum.Enum):
    ACTIVE = "Active"
    ARCHIVED = "Archived"


class Budget(Base):
    """A named, switchable set of BudgetItem lines. Exactly one budget is
    ever "active" at a time — tracked in the single-row GlobalOptions table
    — and every forecast/report reads only the active budget's items, so
    switching budgets swaps the household's whole recurring-payment plan in
    one action. See app/budgets.py."""

    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[BudgetStatus] = mapped_column(Enum(BudgetStatus), default=BudgetStatus.ACTIVE)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.now(dt.UTC))
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)

    budget_items: Mapped[list["BudgetItem"]] = relationship(
        back_populates="budget", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return self.name


class GlobalOptions(Base):
    """Single-row (id always 1) table of app-wide settings — currently just
    which Budget is active. A dedicated table (rather than e.g. a column on
    some other singleton) so future global settings have an obvious home."""

    __tablename__ = "global_options"

    id: Mapped[int] = mapped_column(primary_key=True)
    active_budget_id: Mapped[int | None] = mapped_column(ForeignKey("budgets.id"), nullable=True)


class BudgetItem(Base):
    """A committed, recurring payment (income or expense)."""

    __tablename__ = "budget_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id"))
    description: Mapped[str] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Float)
    flow_type: Mapped[FlowType] = mapped_column(Enum(FlowType), default=FlowType.EXPENSE)
    frequency: Mapped[Frequency] = mapped_column(Enum(Frequency), default=Frequency.MONTHLY)
    effective_from: Mapped[dt.date] = mapped_column(Date)
    effective_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Comma-separated normalised merchant keys (see app/transactions.py
    # merchant_key) this item covers, or None for every transaction in
    # `category`. Lets a category-level budget line scope itself to a subset
    # of vendors — e.g. "Holiday" covering only certain airlines/hotels,
    # rather than every transaction ever filed under that category — see
    # match_budget_item in app/statement_import.py, which only falls back to
    # description-overlap matching when this is unset. Built by the Budget
    # Builder screens; free of commas since normalize_description strips
    # anything that isn't A-Z0-9/space.
    vendors: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    # Destination account for flow_type == TRANSFER items — the recurring
    # amount leaves `account` and is projected as an inflow here.
    target_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)

    budget: Mapped["Budget"] = relationship(back_populates="budget_items")
    category: Mapped["Category"] = relationship()
    account: Mapped["Account"] = relationship(back_populates="budget_items", foreign_keys=[account_id])
    target_account: Mapped["Account | None"] = relationship(foreign_keys=[target_account_id])

    @property
    def monthly_equivalent(self) -> float:
        return self.amount * OCCURRENCES_PER_YEAR[self.frequency] / 12

    @property
    def vendor_list(self) -> list[str]:
        return self.vendors.split(",") if self.vendors else []

    @vendor_list.setter
    def vendor_list(self, keys: list[str]) -> None:
        self.vendors = ",".join(keys) if keys else None

    def is_active_on(self, as_of: dt.date) -> bool:
        if self.effective_from > as_of:
            return False
        if self.effective_until is not None and self.effective_until < as_of:
            return False
        return True


class UpcomingExpenseStatus(enum.Enum):
    PENDING = "Pending"
    ARCHIVED = "Archived"
    NEEDS_REVIEW = "Needs Review"


class UpcomingExpense(Base):
    """A one-off, dated item that isn't part of a recurring schedule."""

    __tablename__ = "upcoming_expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Float)
    flow_type: Mapped[FlowType] = mapped_column(Enum(FlowType), default=FlowType.EXPENSE)
    status: Mapped[UpcomingExpenseStatus] = mapped_column(
        Enum(UpcomingExpenseStatus), default=UpcomingExpenseStatus.PENDING
    )

    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    # Destination account for flow_type == TRANSFER items — the amount leaves
    # `account` and is projected as an inflow here, same as BudgetItem.
    target_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    # Set when a statement covering this expense's date is imported: to the
    # real transaction that matched it (status -> ARCHIVED), or left None
    # with status -> NEEDS_REVIEW if nothing in that statement matched.
    matched_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    last_seen_statement_id: Mapped[int | None] = mapped_column(ForeignKey("statements.id"), nullable=True)

    category: Mapped["Category"] = relationship()
    account: Mapped["Account"] = relationship(back_populates="upcoming_expenses", foreign_keys=[account_id])
    target_account: Mapped["Account | None"] = relationship(foreign_keys=[target_account_id])
    matched_transaction: Mapped["Transaction | None"] = relationship()
    last_seen_statement: Mapped["Statement | None"] = relationship()


class Statement(Base):
    """A single billing-period import of transactions for one account."""

    __tablename__ = "statements"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    period_start: Mapped[dt.date] = mapped_column(Date)
    period_end: Mapped[dt.date] = mapped_column(Date)
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.now(dt.UTC))
    source_note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    account: Mapped["Account"] = relationship()
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="statement", cascade="all, delete-orphan"
    )


class Transaction(Base):
    """A single classified transaction imported from a statement."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id"))
    date: Mapped[dt.date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(255))
    amount: Mapped[float] = mapped_column(Float)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    matched_budget_item_id: Mapped[int | None] = mapped_column(ForeignKey("budget_items.id"), nullable=True)

    statement: Mapped["Statement"] = relationship(back_populates="transactions")
    category: Mapped["Category | None"] = relationship()
    matched_budget_item: Mapped["BudgetItem | None"] = relationship()


class SuggestionType(enum.Enum):
    NEW_ITEM = "New Item"
    AMOUNT_CHANGE = "Amount Change"


class SuggestionStatus(enum.Enum):
    PENDING = "Pending"
    ACCEPTED = "Accepted"
    REJECTED = "Rejected"


class BudgetSuggestion(Base):
    """A proposed change to the budget, generated from imported transaction
    history, awaiting the user's accept/reject decision."""

    __tablename__ = "budget_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    suggestion_type: Mapped[SuggestionType] = mapped_column(Enum(SuggestionType))
    status: Mapped[SuggestionStatus] = mapped_column(Enum(SuggestionStatus), default=SuggestionStatus.PENDING)

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    description: Mapped[str] = mapped_column(String(120))
    proposed_amount: Mapped[float] = mapped_column(Float)
    proposed_frequency: Mapped[Frequency] = mapped_column(Enum(Frequency))

    budget_item_id: Mapped[int | None] = mapped_column(ForeignKey("budget_items.id"), nullable=True)
    current_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_statement_id: Mapped[int | None] = mapped_column(ForeignKey("statements.id"), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.now(dt.UTC))
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    account: Mapped["Account"] = relationship()
    category: Mapped["Category"] = relationship()
    budget_item: Mapped["BudgetItem | None"] = relationship()
