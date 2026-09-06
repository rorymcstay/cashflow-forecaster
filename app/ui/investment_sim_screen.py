import datetime as dt

from PySide6.QtCharts import QAreaSeries, QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import QDate, QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app import investment_sim, market_data
from app.investment_sim import grid_values as _grid_values
from app.models import Account
from app.ui import theme


class InvestmentSimScreen(QWidget):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Investment Simulation</h2>"))
        layout.addWidget(
            QLabel(
                "Historical-bootstrap Monte Carlo: resamples an account's actual historical "
                "monthly returns (via Yahoo Finance) rather than assuming a parametric distribution."
            )
        )

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Account:"))
        self.account_combo = QComboBox()
        controls.addWidget(self.account_combo)

        controls.addWidget(QLabel("As Of:"))
        self.as_of_edit = QDateEdit(QDate.currentDate())
        self.as_of_edit.setCalendarPopup(True)
        controls.addWidget(self.as_of_edit)

        controls.addWidget(QLabel("Lookback (yrs):"))
        self.lookback_spin = QSpinBox()
        self.lookback_spin.setRange(1, 50)
        self.lookback_spin.setValue(10)
        controls.addWidget(self.lookback_spin)

        controls.addWidget(QLabel("Horizon (yrs):"))
        self.horizon_spin = QSpinBox()
        self.horizon_spin.setRange(1, 60)
        self.horizon_spin.setValue(20)
        controls.addWidget(self.horizon_spin)

        controls.addWidget(QLabel("Monthly Contribution:"))
        self.contribution_spin = QDoubleSpinBox()
        self.contribution_spin.setRange(0, 1_000_000)
        self.contribution_spin.setPrefix("£ ")
        controls.addWidget(self.contribution_spin)

        self.force_refresh_check = QCheckBox("Force refresh market data")
        controls.addWidget(self.force_refresh_check)
        controls.addStretch()
        layout.addLayout(controls)

        run_row = QHBoxLayout()
        run_base_btn = QPushButton("Run Simulation")
        run_base_btn.clicked.connect(self.run_base_simulation)
        run_row.addWidget(run_base_btn)
        run_row.addStretch()
        layout.addLayout(run_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self.status_label)

        layout.addWidget(self._build_chart())

        layout.addWidget(self._build_grid_section())

        self.reload_accounts()

    # -- chart -------------------------------------------------------------

    def _build_chart(self) -> QChartView:
        self.chart = QChart()
        self.chart.setTitle("Projected Balance — 5th/25th/50th/75th/95th percentile")
        self.chart.setTitleBrush(QColor(theme.TEXT))
        self.chart.setBackgroundBrush(QColor(theme.SURFACE))
        self.chart.setBackgroundPen(QColor(theme.BORDER))
        self.chart.setPlotAreaBackgroundVisible(False)
        self.chart.legend().hide()

        self.band_90_lower = QLineSeries()
        self.band_90_upper = QLineSeries()
        self.band_90 = QAreaSeries(self.band_90_lower, self.band_90_upper)
        self.band_90.setColor(QColor(theme.ACCENT).lighter(180))
        self.band_90.setBorderColor(Qt.GlobalColor.transparent)
        self.chart.addSeries(self.band_90)

        self.band_50_lower = QLineSeries()
        self.band_50_upper = QLineSeries()
        self.band_50 = QAreaSeries(self.band_50_lower, self.band_50_upper)
        self.band_50.setColor(QColor(theme.ACCENT).lighter(140))
        self.band_50.setBorderColor(Qt.GlobalColor.transparent)
        self.chart.addSeries(self.band_50)

        self.median_series = QLineSeries()
        pen = QPen(QColor(theme.ACCENT))
        pen.setWidthF(2.5)
        self.median_series.setPen(pen)
        self.chart.addSeries(self.median_series)

        self.x_axis = QValueAxis()
        self.x_axis.setTitleText("Years")
        self.x_axis.setLabelsColor(QColor(theme.TEXT_MUTED))
        self.x_axis.setGridLineColor(QColor(theme.BORDER))
        self.chart.addAxis(self.x_axis, Qt.AlignmentFlag.AlignBottom)

        self.y_axis = QValueAxis()
        self.y_axis.setLabelFormat("£%.0f")
        self.y_axis.setLabelsColor(QColor(theme.TEXT_MUTED))
        self.y_axis.setGridLineColor(QColor(theme.BORDER))
        self.chart.addAxis(self.y_axis, Qt.AlignmentFlag.AlignLeft)

        for series in (self.band_90, self.band_50, self.median_series):
            series.attachAxis(self.x_axis)
            series.attachAxis(self.y_axis)

        chart_view = QChartView(self.chart)
        chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        chart_view.setMinimumHeight(280)
        chart_view.setMaximumHeight(320)
        chart_view.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 8px;"
        )
        return chart_view

    # -- grid section --------------------------------------------------------

    def _build_grid_section(self) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Scenario grid:"))
        self.grid_mode_combo = QComboBox()
        self.grid_mode_combo.addItem("Return & Volatility", "return_vol")
        self.grid_mode_combo.addItem("Contribution & Horizon", "contribution_horizon")
        self.grid_mode_combo.currentIndexChanged.connect(self._on_grid_mode_changed)
        mode_row.addWidget(self.grid_mode_combo)
        mode_row.addStretch()
        run_grid_btn = QPushButton("Run Grid")
        run_grid_btn.clicked.connect(self.run_grid)
        mode_row.addWidget(run_grid_btn)
        outer.addLayout(mode_row)

        self.return_vol_box = self._build_return_vol_inputs()
        self.contribution_horizon_box = self._build_contribution_horizon_inputs()
        outer.addWidget(self.return_vol_box)
        outer.addWidget(self.contribution_horizon_box)
        self.contribution_horizon_box.setVisible(False)

        grid_caption = QLabel("Each cell: median ending balance (median worst peak-to-trough drawdown)")
        grid_caption.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        outer.addWidget(grid_caption)

        self.grid_table = QTableWidget()
        self.grid_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        outer.addWidget(self.grid_table)

        return container

    def _spin(
        self, value: float, minimum: float, maximum: float, step: float, decimals: int = 2
    ) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setDecimals(decimals)
        box.setRange(minimum, maximum)
        box.setSingleStep(step)
        box.setValue(value)
        return box

    def _build_return_vol_inputs(self) -> QGroupBox:
        box = QGroupBox("Return shift (% pts) × Volatility scale (×)")
        form = QFormLayout(box)
        self.rs_min = self._spin(-4.0, -50.0, 50.0, 1.0)
        self.rs_max = self._spin(4.0, -50.0, 50.0, 1.0)
        self.rs_step = self._spin(2.0, 0.1, 50.0, 0.5)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("min"))
        row1.addWidget(self.rs_min)
        row1.addWidget(QLabel("max"))
        row1.addWidget(self.rs_max)
        row1.addWidget(QLabel("step"))
        row1.addWidget(self.rs_step)
        form.addRow("Return shift", row1)

        self.vs_min = self._spin(0.5, 0.1, 5.0, 0.1)
        self.vs_max = self._spin(1.5, 0.1, 5.0, 0.1)
        self.vs_step = self._spin(0.25, 0.05, 2.0, 0.05)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("min"))
        row2.addWidget(self.vs_min)
        row2.addWidget(QLabel("max"))
        row2.addWidget(self.vs_max)
        row2.addWidget(QLabel("step"))
        row2.addWidget(self.vs_step)
        form.addRow("Vol scale", row2)
        return box

    def _build_contribution_horizon_inputs(self) -> QGroupBox:
        box = QGroupBox("Monthly Contribution (£) × Horizon (years)")
        form = QFormLayout(box)
        self.contrib_min = self._spin(0, 0, 100_000, 100, decimals=0)
        self.contrib_max = self._spin(1000, 0, 100_000, 100, decimals=0)
        self.contrib_step = self._spin(200, 1, 100_000, 100, decimals=0)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("min"))
        row1.addWidget(self.contrib_min)
        row1.addWidget(QLabel("max"))
        row1.addWidget(self.contrib_max)
        row1.addWidget(QLabel("step"))
        row1.addWidget(self.contrib_step)
        form.addRow("Contribution", row1)

        self.horizon_min = self._spin(5, 1, 60, 1, decimals=0)
        self.horizon_max = self._spin(30, 1, 60, 1, decimals=0)
        self.horizon_step = self._spin(5, 1, 60, 1, decimals=0)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("min"))
        row2.addWidget(self.horizon_min)
        row2.addWidget(QLabel("max"))
        row2.addWidget(self.horizon_max)
        row2.addWidget(QLabel("step"))
        row2.addWidget(self.horizon_step)
        form.addRow("Horizon (yrs)", row2)
        return box

    def _on_grid_mode_changed(self) -> None:
        is_return_vol = self.grid_mode_combo.currentData() == "return_vol"
        self.return_vol_box.setVisible(is_return_vol)
        self.contribution_horizon_box.setVisible(not is_return_vol)

    # -- data ----------------------------------------------------------------

    def reload_accounts(self) -> None:
        current = self.account_combo.currentData() if self.account_combo.count() else None
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for account in self.session.query(Account).order_by(Account.name).all():
            if account.holdings:
                tickers = ", ".join(h.ticker for h in account.holdings)
                self.account_combo.addItem(f"{account.name} ({tickers})", account.id)
        idx = self.account_combo.findData(current)
        self.account_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.account_combo.blockSignals(False)

    def _selected_account(self) -> Account | None:
        account_id = self.account_combo.currentData()
        if account_id is None:
            return None
        return self.session.get(Account, account_id)

    def _historical_returns(self, account: Account, as_of: dt.date) -> list[float] | None:
        weights = account.portfolio_weights
        returns = market_data.fetch_portfolio_monthly_returns(
            weights, as_of, self.lookback_spin.value(), force_refresh=self.force_refresh_check.isChecked()
        )
        if not returns:
            QMessageBox.warning(
                self,
                "No market data",
                "Couldn't fetch historical price data for this portfolio "
                "(bad ticker, no network, or too little history).",
            )
            return None
        return returns

    def run_base_simulation(self) -> None:
        account = self._selected_account()
        if account is None:
            QMessageBox.information(
                self, "No investment accounts", "Add holdings to an account first, on the Accounts tab."
            )
            return

        as_of = dt.date(
            self.as_of_edit.date().year(), self.as_of_edit.date().month(), self.as_of_edit.date().day()
        )
        returns = self._historical_returns(account, as_of)
        if returns is None:
            return

        n_periods = self.horizon_spin.value() * 12
        paths = investment_sim.bootstrap_paths(
            returns,
            account.current_balance,
            n_periods,
            n_paths=2000,
            monthly_contribution=self.contribution_spin.value(),
        )
        bands = investment_sim.percentile_bands(paths)
        years = [i / 12 for i in range(n_periods + 1)]

        self.band_90_lower.replace([QPointF(y, v) for y, v in zip(years, bands[5])])
        self.band_90_upper.replace([QPointF(y, v) for y, v in zip(years, bands[95])])
        self.band_50_lower.replace([QPointF(y, v) for y, v in zip(years, bands[25])])
        self.band_50_upper.replace([QPointF(y, v) for y, v in zip(years, bands[75])])
        self.median_series.replace([QPointF(y, v) for y, v in zip(years, bands[50])])

        self.x_axis.setRange(0, years[-1] if years else 1)
        all_values = bands[5] + bands[95]
        self.y_axis.setRange(min(all_values) * 0.95, max(all_values) * 1.05)

        mean_return = sum(returns) / len(returns)
        dd = investment_sim.drawdown_stats(paths)
        self.status_label.setText(
            f"{len(returns)} historical monthly returns used (mean {mean_return * 100:.2f}%/mo, "
            f"~{((1 + mean_return) ** 12 - 1) * 100:.2f}%/yr). Median ending balance: "
            f"£{bands[50][-1]:,.0f} (90% range £{bands[5][-1]:,.0f} – £{bands[95][-1]:,.0f}).\n"
            f"Drawdown (worst peak-to-trough dip along the way): median {dd['percentiles'][50] * 100:.0f}%, "
            f"90% range {dd['percentiles'][5] * 100:.0f}% to {dd['percentiles'][95] * 100:.0f}%, "
            f"worst-case path {dd['worst'] * 100:.0f}%."
        )

    def run_grid(self) -> None:
        account = self._selected_account()
        if account is None:
            QMessageBox.information(
                self, "No investment accounts", "Add holdings to an account first, on the Accounts tab."
            )
            return
        as_of = dt.date(
            self.as_of_edit.date().year(), self.as_of_edit.date().month(), self.as_of_edit.date().day()
        )
        returns = self._historical_returns(account, as_of)
        if returns is None:
            return

        if self.grid_mode_combo.currentData() == "return_vol":
            return_shifts = [
                v / 100 for v in _grid_values(self.rs_min.value(), self.rs_max.value(), self.rs_step.value())
            ]
            vol_scales = _grid_values(self.vs_min.value(), self.vs_max.value(), self.vs_step.value())
            grid = investment_sim.return_vol_grid(
                returns,
                account.current_balance,
                self.horizon_spin.value() * 12,
                return_shifts,
                vol_scales,
                monthly_contribution=self.contribution_spin.value(),
            )
            row_values, col_values = return_shifts, vol_scales
            row_label_fmt = lambda v: f"{v * 100:+.1f}%"
            col_label_fmt = lambda v: f"{v:.2f}×"
            self._populate_grid(
                grid, row_values, col_values, "Return shift \\ Vol scale", row_label_fmt, col_label_fmt
            )
        else:
            contributions = _grid_values(
                self.contrib_min.value(), self.contrib_max.value(), self.contrib_step.value()
            )
            horizons = [
                int(v)
                for v in _grid_values(
                    self.horizon_min.value(), self.horizon_max.value(), self.horizon_step.value()
                )
            ]
            grid = investment_sim.contribution_horizon_grid(
                returns, account.current_balance, contributions, horizons
            )
            row_values, col_values = contributions, horizons
            row_label_fmt = lambda v: f"£{v:,.0f}/mo"
            col_label_fmt = lambda v: f"{v} yrs"
            self._populate_grid(
                grid, row_values, col_values, "Contribution \\ Horizon", row_label_fmt, col_label_fmt
            )

    def _populate_grid(self, grid, row_values, col_values, corner_label, row_fmt, col_fmt) -> None:
        self.grid_table.setRowCount(len(row_values))
        self.grid_table.setColumnCount(len(col_values) + 1)
        self.grid_table.setHorizontalHeaderLabels([corner_label] + [col_fmt(c) for c in col_values])
        self.grid_table.verticalHeader().setVisible(False)
        for r, rv in enumerate(row_values):
            row_header = QTableWidgetItem(row_fmt(rv))
            row_header.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.grid_table.setItem(r, 0, row_header)
            for c, cv in enumerate(col_values):
                stats = grid.get((rv, cv))
                if stats is None:
                    text = "—"
                else:
                    text = (
                        f"£{stats['median_ending_balance']:,.0f} ({stats['median_max_drawdown'] * 100:.0f}%)"
                    )
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.grid_table.setItem(r, c + 1, item)
        for col in range(self.grid_table.columnCount()):
            self.grid_table.resizeColumnToContents(col)
