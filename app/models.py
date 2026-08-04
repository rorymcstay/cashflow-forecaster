import datetime as dt
import enum

from sqlalchemy import Date, DateTime, Enum, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class FlowType(enum.Enum):
    INCOME = "Income"
    EXPENSE = "Expense"


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

    budget_items: Mapped[list["BudgetItem"]] = relationship(back_populates="account")
    upcoming_expenses: Mapped[list["UpcomingExpense"]] = relationship(back_populates="account")

    def __repr__(self) -> str:
        return self.name


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)

    def __repr__(self) -> str:
        return self.name


class BudgetItem(Base):
    """A committed, recurring payment (income or expense)."""

    __tablename__ = "budget_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Float)
    flow_type: Mapped[FlowType] = mapped_column(Enum(FlowType), default=FlowType.EXPENSE)
    frequency: Mapped[Frequency] = mapped_column(Enum(Frequency), default=Frequency.MONTHLY)
    effective_from: Mapped[dt.date] = mapped_column(Date)
    effective_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)

    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))

    category: Mapped["Category"] = relationship()
    account: Mapped["Account"] = relationship(back_populates="budget_items")

    @property
    def monthly_equivalent(self) -> float:
        return self.amount * OCCURRENCES_PER_YEAR[self.frequency] / 12

    def is_active_on(self, as_of: dt.date) -> bool:
        if self.effective_from > as_of:
            return False
        if self.effective_until is not None and self.effective_until < as_of:
            return False
        return True


class UpcomingExpense(Base):
    """A one-off, dated item that isn't part of a recurring schedule."""

    __tablename__ = "upcoming_expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Float)

    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))

    category: Mapped["Category"] = relationship()
    account: Mapped["Account"] = relationship(back_populates="upcoming_expenses")


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
