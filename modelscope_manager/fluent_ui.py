from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    ComboBox,
    FluentIcon,
    FluentIconBase,
    MenuAnimationType,
    SettingCard,
    SwitchButton,
    TransparentToolButton,
)


class FluentSwitchButton(SwitchButton):
    """SwitchButton with the QCheckBox-compatible signal used by the app."""

    toggled = Signal(bool)

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._sourceText = text
        self.setDisplayText(text)
        self.checkedChanged.connect(self.toggled)

    def sourceText(self) -> str:
        return self._sourceText

    def setDisplayText(self, text: str) -> None:
        self.setOnText(text)
        self.setOffText(text)


class ControlSettingCard(SettingCard):
    """Standard Fluent setting card that accepts an arbitrary trailing control."""

    def __init__(
        self,
        icon: FluentIconBase,
        title: str,
        content: str,
        control: QWidget,
        parent: QWidget | None = None,
        trailing_margin: int = 0,
    ):
        super().__init__(icon, title, content, parent)
        self.control = control
        self.control.setParent(self)
        self.hBoxLayout.addWidget(self.control, 0, Qt.AlignmentFlag.AlignRight)
        if trailing_margin:
            self.hBoxLayout.addSpacing(trailing_margin)


class PanelSettingCard(SettingCard):
    """Collapsible Fluent card used for complex settings panels."""

    def __init__(
        self,
        icon: FluentIconBase,
        title: str,
        content: str,
        panel: QWidget,
        parent: QWidget | None = None,
    ):
        super().__init__(icon, title, content, parent)
        self._translator = lambda source: source
        self.panel = panel
        self.panel.setObjectName("fluentSettingsPanel")
        self.panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        while self.hBoxLayout.count():
            self.hBoxLayout.takeAt(0)
        self.headerWidget = QWidget(self)
        self.headerWidget.setFixedHeight(70)
        header = QHBoxLayout(self.headerWidget)
        header.setContentsMargins(16, 0, 16, 0)
        header.setSpacing(0)
        header.addWidget(self.iconLabel, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addSpacing(16)
        header.addLayout(self.vBoxLayout, 1)
        self.expandButton = TransparentToolButton(FluentIcon.CHEVRON_DOWN_MED, self.headerWidget)
        self.expandButton.setFixedSize(32, 32)
        self.expandButton.setToolTip("展开详细设置")
        self.expandButton.clicked.connect(self.toggleExpanded)
        header.addWidget(self.expandButton, 0, Qt.AlignmentFlag.AlignVCenter)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.headerWidget)
        body.addSpacing(12)
        body.addWidget(self.panel, 1)
        body.addSpacing(14)
        self.hBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.hBoxLayout.addLayout(body, 1)
        self._expanded = False
        self.setExpanded(False, animated=False)

    def isExpanded(self) -> bool:
        return self._expanded

    def setTranslator(self, translator) -> None:
        self._translator = translator
        self.setExpanded(self._expanded, animated=False)

    def setExpanded(self, expanded: bool, animated: bool = False) -> None:
        self._expanded = expanded
        self.expandButton.setIcon(
            FluentIcon.CARE_UP_SOLID if expanded else FluentIcon.CHEVRON_DOWN_MED
        )
        self.expandButton.setToolTip(self._translator(
            "收起详细设置" if expanded else "展开详细设置"
        ))
        target = max(110, self.panel.sizeHint().height() + 96) if expanded else 70
        self.panel.setVisible(expanded)
        self.setMinimumHeight(target if expanded else 70)
        self.setMaximumHeight(16777215 if expanded else 70)
        self.updateGeometry()
        parent = self.parentWidget()
        if parent:
            parent.updateGeometry()
        window = self.window()
        window.update()
        window.repaint()

    def toggleExpanded(self) -> None:
        self.setExpanded(not self.isExpanded(), animated=False)


class CleanComboBox(ComboBox):
    """Fluent combo menu without the native shadow rectangle and with screen clamping."""

    def _showComboMenu(self):
        if not self.items:
            return

        menu = self._createComboMenu()
        menu.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        # Fluent menus paint rounded corners into a translucent top-level window.
        # Making that window opaque exposes its black backing rectangle in light mode.
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        for item in self.items:
            action = QAction(item.icon, item.text, menu)
            action.setEnabled(item.isEnabled)
            menu.addAction(action)

        menu.view.itemClicked.connect(
            lambda item: self._onItemClicked(self.findText(item.text().lstrip()))
        )
        if menu.view.width() < self.width():
            menu.view.setMinimumWidth(self.width())
            menu.adjustSize()

        menu.setMaxVisibleItems(self.maxVisibleItems())
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.closedSignal.connect(self._onDropMenuClosed)
        self.dropMenu = menu
        if self.currentIndex() >= 0:
            menu.setDefaultAction(menu.actions()[self.currentIndex()])

        x = -menu.width() // 2 + menu.layout().contentsMargins().left() + self.width() // 2
        below = self.mapToGlobal(QPoint(x, self.height()))
        above = self.mapToGlobal(QPoint(x, 0))
        screen = QGuiApplication.screenAt(self.mapToGlobal(self.rect().center()))
        available = screen.availableGeometry() if screen else QGuiApplication.primaryScreen().availableGeometry()
        below_height = menu.view.heightForAnimation(below, MenuAnimationType.DROP_DOWN)
        above_height = menu.view.heightForAnimation(above, MenuAnimationType.PULL_UP)
        animation = MenuAnimationType.DROP_DOWN if below_height >= above_height else MenuAnimationType.PULL_UP
        position = below if animation == MenuAnimationType.DROP_DOWN else above
        menu.view.adjustSize(position, animation)
        menu.adjustSize()
        position.setX(max(available.left(), min(position.x(), available.right() - menu.width() + 1)))
        if animation == MenuAnimationType.DROP_DOWN:
            position.setY(min(position.y(), available.bottom() - menu.height() + 1))
        else:
            position.setY(max(available.top() + menu.height(), position.y()))
        menu.closedSignal.connect(lambda: (self.window().update(), self.window().repaint()))
        menu.view.adjustSize(position, MenuAnimationType.NONE)
        menu.exec(position, ani=False, aniType=MenuAnimationType.NONE)
