from PySide6.QtCharts import QAreaSeries, QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.forecast import HypotheticalItem
from app.models import Account, FlowType, Frequency
from app.scenario_sim import run_scenario
from app.ui import theme
from app.ui.widgets import AccountMultiSelect

_OFFSET_COLORS = ["#5B8DEF", "#34D399", "#F2555C", "#F2A65B", "#B98CE0", "#4FD1C5"]


def _money_item(value: float) -> QTableWidgetItem:
    item = QTableWidgetItem(f"£{value:,.2f}")
    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


class ScenarioScreen(QWidget):
    """Monte Carlo stress-testing on top of the deterministic cashflow
    forecast: random shocks, income growth, a one-time payment compared at
    several timings, hypothetical (never-saved) budget lines, and — for
    accounts with holdings — historical market-regime variation. See
    app/scenario_sim.py for the engine this drives."""

    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session
        self.extra_line_rows: list[dict] = []
        self.offset_series: list[QLineSeries] = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Scenarios</h2>"))
        note = QLabel(
            "Stress-test the cashflow forecast: random shocks, income growth, a one-time payment "
            "compared at different timings, hypothetical (never-saved) budget lines, and — for "
            "accounts with holdings — historical market-regime variation instead of a flat return."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(note)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Accounts:"))
        self.account_select = AccountMultiSelect(self)
        top_row.addWidget(self.account_select)
        top_row.addWidget(QLabel("Horizon (yrs):"))
        self.horizon_spin = QSpinBox()
        self.horizon_spin.setRange(1, 10)
        self.horizon_spin.setValue(5)
        top_row.addWidget(self.horizon_spin)
        top_row.addWidget(QLabel("Paths:"))
        self.paths_spin = QSpinBox()
        self.paths_spin.setRange(50, 5000)
        self.paths_spin.setSingleStep(50)
        self.paths_spin.setValue(500)
        top_row.addWidget(self.paths_spin)
        self.regimes_check = QCheckBox("Use market-regime variation")
        self.regimes_check.setChecked(True)
        top_row.addWidget(self.regimes_check)
        top_row.addStretch()
        layout.addLayout(top_row)

        layout.addWidget(QLabel("<b>Income growth</b>"))
        growth_row = QHBoxLayout()
        growth_row.addWidget(QLabel("Annual income growth (%):"))
        self.income_growth_spin = QDoubleSpinBox()
        self.income_growth_spin.setRange(-50.0, 100.0)
        self.income_growth_spin.setDecimals(2)
        growth_row.addWidget(self.income_growth_spin)
        growth_row.addStretch()
        layout.addLayout(growth_row)

        layout.addWidget(QLabel("<b>Random shock</b>"))
        shock_row = QHBoxLayout()
        shock_row.addWidget(QLabel("Probability per year (%):"))
        self.shock_prob_spin = QDoubleSpinBox()
        self.shock_prob_spin.setRange(0.0, 100.0)
        self.shock_prob_spin.setDecimals(1)
        shock_row.addWidget(self.shock_prob_spin)
        shock_row.addWidget(QLabel("Size (£):"))
        self.shock_amount_spin = QDoubleSpinBox()
        self.shock_amount_spin.setRange(0.0, 10_000_000.0)
        self.shock_amount_spin.setDecimals(2)
        shock_row.addWidget(self.shock_amount_spin)
        shock_row.addWidget(QLabel("Hits account:"))
        self.shock_account_combo = QComboBox()
        shock_row.addWidget(self.shock_account_combo)
        shock_row.addStretch()
        layout.addLayout(shock_row)

        layout.addWidget(QLabel("<b>One-time payment</b>"))
        payment_row = QHBoxLayout()
        payment_row.addWidget(QLabel("Amount (£):"))
        self.payment_amount_spin = QDoubleSpinBox()
        self.payment_amount_spin.setRange(0.0, 100_000_000.0)
        self.payment_amount_spin.setDecimals(2)
        payment_row.addWidget(self.payment_amount_spin)
        payment_row.addWidget(QLabel("From account:"))
        self.payment_account_combo = QComboBox()
        payment_row.addWidget(self.payment_account_combo)
        payment_row.addWidget(QLabel("Years from now:"))
        self.payment_offsets_edit = QLineEdit("1,2,3,4,5")
        payment_row.addWidget(self.payment_offsets_edit)
        payment_row.addStretch()
        layout.addLayout(payment_row)
        payment_note = QLabel("Leave amount at 0 to skip this comparison.")
        payment_note.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(payment_note)

        layout.addWidget(QLabel("<b>Hypothetical budget lines</b>"))
        lines_note = QLabel('Never saved — for exploring "what if I added this" only.')
        lines_note.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(lines_note)
        self.extra_lines_layout = QVBoxLayout()
        layout.addLayout(self.extra_lines_layout)
        add_line_row = QHBoxLayout()
        add_line_btn = QPushButton("+ Add line")
        add_line_btn.clicked.connect(self._add_extra_line)
        add_line_row.addWidget(add_line_btn)
        add_line_row.addStretch()
        layout.addLayout(add_line_row)

        run_btn = QPushButton("Run Simulation")
        run_btn.setObjectName("primaryButton")
        run_btn.clicked.connect(self.run)
        layout.addWidget(run_btn)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self.status_label)

        layout.addWidget(self._build_chart())

        layout.addWidget(QLabel("<b>Payment timing comparison</b>"))
        self.impact_table = QTableWidget()
        self.impact_table.verticalHeader().setVisible(False)
        layout.addWidget(self.impact_table)

        self.reload_accounts()

    # -- chart ---------------------------------------------------------------

    def _build_chart(self) -> QChartView:
        self.chart = QChart()
        self.chart.setTitle("Combined Balance — 5th/25th/50th/75th/95th percentile")
        self.chart.setTitleBrush(QColor(theme.TEXT))
        self.chart.setBackgroundBrush(QColor(theme.SURFACE))
        self.chart.setBackgroundPen(QColor(theme.BORDER))
        self.chart.setPlotAreaBackgroundVisible(False)
        self.chart.legend().setVisible(True)
        self.chart.legend().setLabelColor(QColor(theme.TEXT_MUTED))

        self.band_90_lower = QLineSeries()
        self.band_90_upper = QLineSeries()
        self.band_90 = QAreaSeries(self.band_90_lower, self.band_90_upper)
        self.band_90.setColor(QColor(theme.ACCENT).lighter(180))
        self.band_90.setBorderColor(Qt.GlobalColor.transparent)
        self.band_90.setName("5th–95th percentile")
        self.chart.addSeries(self.band_90)

        self.band_50_lower = QLineSeries()
        self.band_50_upper = QLineSeries()
        self.band_50 = QAreaSeries(self.band_50_lower, self.band_50_upper)
        self.band_50.setColor(QColor(theme.ACCENT).lighter(140))
        self.band_50.setBorderColor(Qt.GlobalColor.transparent)
        self.band_50.setName("25th–75th percentile")
        self.chart.addSeries(self.band_50)

        self.median_series = QLineSeries()
        self.median_series.setName("Median")
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
        chart_view.setMaximumHeight(340)
        chart_view.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 8px;"
        )
        return chart_view

    # -- hypothetical budget lines --------------------------------------------

    def _add_extra_line(self) -> None:
        row_widget = QWidget()
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)

        desc_edit = QLineEdit()
        desc_edit.setPlaceholderText("Description")
        amount_spin = QDoubleSpinBox()
        amount_spin.setRange(0.0, 1_000_000.0)
        amount_spin.setPrefix("£ ")
        flow_combo = QComboBox()
        flow_combo.addItem("Expense", FlowType.EXPENSE)
        flow_combo.addItem("Income", FlowType.INCOME)
        freq_combo = QComboBox()
        for f in Frequency:
            freq_combo.addItem(f.value, f)
        freq_combo.setCurrentIndex(freq_combo.findData(Frequency.MONTHLY))
        account_combo = QComboBox()
        remove_btn = QPushButton("×")
        remove_btn.setFixedWidth(28)
        remove_btn.setToolTip("Remove this line")

        for w in (desc_edit, amount_spin, flow_combo, freq_combo, account_combo, remove_btn):
            row.addWidget(w)

        self.extra_lines_layout.addWidget(row_widget)
        entry = {
            "row_widget": row_widget,
            "desc": desc_edit,
            "amount": amount_spin,
            "flow": flow_combo,
            "freq": freq_combo,
            "account": account_combo,
        }
        self._populate_account_combo(account_combo, allow_none=False)
        remove_btn.clicked.connect(lambda: self._remove_extra_line(entry))
        self.extra_line_rows.append(entry)

    def _remove_extra_line(self, entry: dict) -> None:
        self.extra_lines_layout.removeWidget(entry["row_widget"])
        entry["row_widget"].deleteLater()
        self.extra_line_rows.remove(entry)

    # -- data ------------------------------------------------------------------

    def _populate_account_combo(self, combo: QComboBox, allow_none: bool) -> None:
        current = combo.currentData() if combo.count() else None
        combo.blockSignals(True)
        combo.clear()
        if allow_none:
            combo.addItem("—", None)
        for account in self.session.query(Account).order_by(Account.name).all():
            combo.addItem(account.name, account.id)
        idx = combo.findData(current)
        combo.setCurrentIndex(max(idx, 0))
        combo.blockSignals(False)

    def reload_accounts(self) -> None:
        accounts = self.session.query(Account).order_by(Account.name).all()
        self.account_select.set_accounts(accounts)
        self._populate_account_combo(self.shock_account_combo, allow_none=True)
        self._populate_account_combo(self.payment_account_combo, allow_none=True)
        for entry in self.extra_line_rows:
            self._populate_account_combo(entry["account"], allow_none=False)

    def _parse_offsets(self, text: str) -> list[int] | None:
        offsets = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                offsets.append(int(part))
            except ValueError:
                return None
        return offsets

    def _build_extra_items(self) -> list[HypotheticalItem] | None:
        items = []
        for entry in self.extra_line_rows:
            description = entry["desc"].text().strip()
            amount = entry["amount"].value()
            account_id = entry["account"].currentData()
            if not description or not amount or account_id is None:
                continue
            items.append(
                HypotheticalItem(
                    description=description,
                    amount=amount,
                    flow_type=entry["flow"].currentData(),
                    frequency=entry["freq"].currentData(),
                    account_id=account_id,
                )
            )
        return items or None

    # -- run ---------------------------------------------------------------

    def run(self) -> None:
        account_ids = self.account_select.checked_ids()
        if not account_ids:
            QMessageBox.warning(self, "No accounts", "Choose at least one account.")
            return

        one_time_payment = None
        if self.payment_amount_spin.value():
            payment_account_id = self.payment_account_combo.currentData()
            if payment_account_id is None:
                QMessageBox.warning(self, "No account", "Choose an account for the one-time payment.")
                return
            offsets = self._parse_offsets(self.payment_offsets_edit.text())
            if offsets is None:
                QMessageBox.warning(
                    self, "Invalid input", "Year offsets must be whole numbers, comma-separated."
                )
                return
            if not offsets:
                QMessageBox.warning(
                    self, "Invalid input", "Enter at least one year offset for the one-time payment."
                )
                return
            one_time_payment = {
                "amount": self.payment_amount_spin.value(),
                "account_id": payment_account_id,
                "year_offsets": offsets,
            }

        try:
            result = run_scenario(
                self.session,
                horizon_years=self.horizon_spin.value(),
                account_ids=list(account_ids),
                n_paths=self.paths_spin.value(),
                income_growth_rate_pct=self.income_growth_spin.value(),
                shock_probability_per_year=self.shock_prob_spin.value(),
                shock_amount=self.shock_amount_spin.value(),
                shock_account_id=self.shock_account_combo.currentData(),
                extra_budget_items=self._build_extra_items(),
                one_time_payment=one_time_payment,
                use_market_regimes=self.regimes_check.isChecked(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Couldn't run scenario", str(exc))
            return

        self._render_result(result)

    def _render_result(self, result: dict) -> None:
        years = [i / 12 for i in range(len(result["dates"]))]
        bands = result["baseline"]

        self.band_90_lower.replace([QPointF(y, v) for y, v in zip(years, bands[5])])
        self.band_90_upper.replace([QPointF(y, v) for y, v in zip(years, bands[95])])
        self.band_50_lower.replace([QPointF(y, v) for y, v in zip(years, bands[25])])
        self.band_50_upper.replace([QPointF(y, v) for y, v in zip(years, bands[75])])
        self.median_series.replace([QPointF(y, v) for y, v in zip(years, bands[50])])
        all_values = list(bands[5]) + list(bands[95])

        for series in self.offset_series:
            self.chart.removeSeries(series)
        self.offset_series.clear()

        payment_scenarios = result.get("payment_scenarios")
        if payment_scenarios:
            for i, (offset, offset_bands) in enumerate(sorted(payment_scenarios.items())):
                series = QLineSeries()
                series.setName(f"Pay in year {offset}")
                pen = QPen(QColor(_OFFSET_COLORS[i % len(_OFFSET_COLORS)]))
                pen.setWidthF(2.0)
                pen.setStyle(Qt.PenStyle.DotLine)
                series.setPen(pen)
                series.replace([QPointF(y, v) for y, v in zip(years, offset_bands[50])])
                self.chart.addSeries(series)
                series.attachAxis(self.x_axis)
                series.attachAxis(self.y_axis)
                self.offset_series.append(series)
                all_values += offset_bands[50]

        self.x_axis.setRange(0, years[-1] if years else 1)
        if all_values:
            lo, hi = min(all_values), max(all_values)
            pad = (hi - lo) * 0.05 or max(abs(hi), 1.0) * 0.05
            self.y_axis.setRange(lo - pad, hi + pad)

        summary = result["summary"]
        regime_note = (
            f"Market-regime data used for: {', '.join(result['regime_accounts_used'])}."
            if result["regime_accounts_used"]
            else "No market-regime data applied (no in-scope holdings, or data unavailable)."
        )
        self.status_label.setText(
            f"Probability of dropping below £0 at some point: {summary['probability_below_zero'] * 100:.0f}%. "
            f"Median ending balance: £{summary['median_ending_balance']:,.2f}.\n{regime_note}"
        )

        payment_impact = summary.get("payment_impact")
        self.impact_table.setRowCount(0)
        if payment_impact:
            self.impact_table.setColumnCount(4)
            self.impact_table.setHorizontalHeaderLabels(
                ["Pay in year", "Median ending balance", "Vs. no payment", "Prob. below £0"]
            )
            for offset, impact in sorted(payment_impact.items()):
                row = self.impact_table.rowCount()
                self.impact_table.insertRow(row)
                self.impact_table.setItem(row, 0, QTableWidgetItem(str(offset)))
                self.impact_table.setItem(row, 1, _money_item(impact["median_ending_balance"]))
                self.impact_table.setItem(row, 2, _money_item(impact["vs_baseline_median"]))
                self.impact_table.setItem(
                    row, 3, QTableWidgetItem(f"{impact['probability_below_zero'] * 100:.0f}%")
                )
            for col in range(4):
                self.impact_table.resizeColumnToContents(col)
        else:
            self.impact_table.setColumnCount(0)
