"""Build transparent Tk-compatible PNGs from the copied native SVG icons."""
from pathlib import Path
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QGuiApplication, QImage, QPainter
from PyQt5.QtSvg import QSvgRenderer


def main():
    app = QGuiApplication([])
    directory = Path(__file__).resolve().parents[1] / "assets/action-icons"
    count = 0
    for path in directory.glob("*.svg"):
        renderer = QSvgRenderer(str(path))
        if not renderer.isValid():
            raise ValueError(f"Invalid SVG: {path.name}")
        for size, suffix in ((24, ""), (48, "@2x")):
            image = QImage(size, size, QImage.Format_ARGB32)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing)
            renderer.render(painter, QRectF(0, 0, size, size))
            painter.end()
            if not image.save(str(path.with_name(path.stem + suffix + ".png"))):
                raise OSError(f"Cannot save {path.name}")
        count += 1
    print(f"Rendered {count} native icons at 24 px and 48 px.")


if __name__ == "__main__":
    main()
