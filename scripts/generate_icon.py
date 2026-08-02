"""Generates app/assets/icon.png — a rounded-square "£" monogram badge.

Run with: QT_QPA_PLATFORM=offscreen uv run scripts/generate_icon.py
Re-run after changing the palette in app/ui/theme to keep the logo in sync.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication

from app.ui import theme

OUT_DIR = Path(__file__).resolve().parent.parent / "app" / "assets"


def draw_icon(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    margin = size * 0.04
    rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    radius = rect.width() * 0.24

    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)

    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, QColor("#3E5C99"))
    gradient.setColorAt(1.0, QColor(theme.ACCENT))
    painter.fillPath(path, gradient)

    # subtle top-left highlight for a touch of depth
    highlight = QPainterPath()
    highlight.addRoundedRect(rect, radius, radius)
    painter.setClipPath(highlight)
    sheen = QLinearGradient(rect.topLeft(), rect.center())
    sheen.setColorAt(0.0, QColor(255, 255, 255, 40))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.fillRect(rect, sheen)
    painter.setClipping(False)

    # "£" monogram
    font = QFont(".AppleSystemUIFont" if sys.platform == "darwin" else "Segoe UI")
    font.setBold(True)
    font.setPixelSize(int(rect.height() * 0.56))
    painter.setFont(font)
    painter.setPen(QColor("#FFFFFF"))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "£")

    painter.end()
    return pixmap


def main():
    app = QApplication(sys.argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    icon = QIcon()
    for size in (16, 32, 64, 128, 256, 512):
        pm = draw_icon(size)
        icon.addPixmap(pm)
        if size == 512:
            pm.save(str(OUT_DIR / "icon.png"))
    print(f"Saved {OUT_DIR / 'icon.png'}")


if __name__ == "__main__":
    main()
