from aqt.qt import (
    QGraphicsOpacityEffect,
    QLabel,
    QProgressBar,
    QPropertyAnimation,
    QVBoxLayout,
    QWidget,
    Qt,
    qtmajor,
)

FADE_IN_DURATION_MS = 200

if qtmajor > 5:
    QAlignCenter = Qt.AlignmentFlag.AlignCenter
else:
    QAlignCenter = Qt.AlignCenter  # type: ignore


class LoadingIndicator(QWidget):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)

        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.opacity_effect.setOpacity(0.0)
        self.setGraphicsEffect(self.opacity_effect)

        self.fade_in_animation = QPropertyAnimation(self.opacity_effect, b"opacity", self)
        self.fade_in_animation.setDuration(FADE_IN_DURATION_MS)
        self.fade_in_animation.setStartValue(0.0)
        self.fade_in_animation.setEndValue(1.0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.label = QLabel(text, self)
        self.label.setAlignment(QAlignCenter)
        self.label.setStyleSheet("font-weight: bold;")

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(260)

        layout.addWidget(self.label)
        layout.addWidget(self.progress, alignment=QAlignCenter)

        self.fade_in_animation.start()

    def set_text(self, text: str) -> None:
        self.label.setText(text)
