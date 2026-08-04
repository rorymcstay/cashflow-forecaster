"""A single deliberate dark theme, applied via QSS + Qt's Fusion style.

Fusion is used explicitly (rather than the native macOS style) because the
native style largely ignores stylesheet colors for controls like buttons and
tabs — QSS only reliably applies with Fusion. Shipping one committed theme
(instead of relying on the OS light/dark setting) also avoids the contrast
bugs that showed up before: colors picked for a light background clashing
badly once macOS dark mode kicked in.
"""

BG = "#14161B"
SURFACE = "#1B1E25"
SURFACE_ALT = "#21252E"
ELEVATED = "#262B35"
BORDER = "#2C313C"

TEXT = "#EDEFF2"
TEXT_MUTED = "#8D95A3"

ACCENT = "#5B8DEF"
ACCENT_HOVER = "#6C99F2"
ACCENT_PRESSED = "#4A78D6"
ACCENT_TEXT = "#FFFFFF"

SECTION_BG = "#20283A"  # section header rows (INCOME / EXPENSE / etc.)
TOTAL_BG = "#243352"  # subtotal + net/summary rows

WARNING = "#F2555C"
WARNING_BG = "#3A2429"

SUCCESS = "#34D399"

ICON_PATH = __file__.rsplit("/app/ui/", 1)[0] + "/app/assets/icon.png"


def build_palette():
    """An explicit dark QPalette, applied alongside the QSS below.

    QSS `color:` rules don't reliably reach item-view internals (table cell
    text, combo box popups, etc.) — those read QPalette roles first. Setting
    both is what avoids ending up with dark-on-dark or light-on-light text
    in places the stylesheet doesn't cover.
    """
    from PySide6.QtGui import QColor, QPalette

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(SURFACE_ALT))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(ELEVATED))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(ELEVATED))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(WARNING))
    palette.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(ACCENT_TEXT))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_MUTED))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(TEXT_MUTED))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(TEXT_MUTED))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(TEXT_MUTED))
    return palette


STYLESHEET = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-size: 13px;
}}

#HeaderBar {{
    background-color: {SURFACE};
    border-bottom: 1px solid {BORDER};
}}
#HeaderTitle {{
    font-size: 16px;
    font-weight: 600;
    color: {TEXT};
}}
#HeaderSubtitle {{
    color: {TEXT_MUTED};
    font-size: 11px;
}}

QLabel {{
    background: transparent;
}}

/* Tabs */
QTabWidget::pane {{
    border: none;
    background-color: {BG};
}}
QTabBar {{
    background: transparent;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_MUTED};
    padding: 10px 18px;
    margin-right: 2px;
    font-weight: 600;
    border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{
    color: {TEXT};
    border-bottom: 2px solid {ACCENT};
}}
QTabBar::tab:hover:!selected {{
    color: {TEXT};
}}

/* Buttons */
QPushButton {{
    background-color: {ELEVATED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 16px;
    font-weight: 600;
}}
QPushButton:hover {{
    background-color: {SURFACE_ALT};
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: {BORDER};
}}
QPushButton:disabled {{
    color: {TEXT_MUTED};
}}
QPushButton#primaryButton {{
    background-color: {ACCENT};
    color: {ACCENT_TEXT};
    border: 1px solid {ACCENT};
}}
QPushButton#primaryButton:hover {{
    background-color: {ACCENT_HOVER};
}}
QPushButton#primaryButton:pressed {{
    background-color: {ACCENT_PRESSED};
}}

/* Inputs */
QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox, QSpinBox {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    color: {TEXT};
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    color: {TEXT};
    outline: none;
}}
QCheckBox {{
    spacing: 8px;
}}
QDateEdit::drop-down {{
    border: none;
    width: 20px;
}}
QCalendarWidget QWidget {{
    background-color: {SURFACE};
    color: {TEXT};
}}
QCalendarWidget QToolButton {{
    color: {TEXT};
    background: transparent;
}}
QCalendarWidget QAbstractItemView:enabled {{
    background-color: {SURFACE};
    color: {TEXT};
    selection-background-color: {ACCENT};
}}

/* Tables */
QTableView, QTableWidget {{
    background-color: {SURFACE};
    alternate-background-color: {SURFACE_ALT};
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    border-radius: 8px;
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_TEXT};
}}
QTableView::item, QTableWidget::item {{
    padding: 6px;
    border: none;
}}
QHeaderView::section {{
    background-color: {SURFACE_ALT};
    color: {TEXT_MUTED};
    padding: 8px;
    border: none;
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
    font-size: 11px;
}}
QTableCornerButton::section {{
    background-color: {SURFACE_ALT};
    border: none;
}}

/* Scrollbars */
QScrollBar:vertical {{
    background: transparent;
    width: 11px;
    margin: 0px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {ACCENT};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0px;
    border: none;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 11px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER};
    border-radius: 5px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {ACCENT};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
    border: none;
}}

/* Dialogs */
QDialog {{
    background-color: {SURFACE};
}}

QMessageBox {{
    background-color: {SURFACE};
}}

QToolTip {{
    background-color: {ELEVATED};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 6px;
}}
"""
