QSS = r"""
* { font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 13px; color: #202020; }
QMainWindow, QWidget#root { background: #f3f3f3; }
QScrollArea#settingsScroll, QWidget#settingsContent { background: #f3f3f3; }
QFrame#sidebar { background: rgba(249,249,249,245); border-right: 1px solid #dedede; }
QFrame#navSidebar { background: #f7f7f7; border-right: 1px solid #dedede; }
QLabel#brand { font-size: 19px; font-weight: 650; color: #1f1f1f; padding: 4px 8px; }
QLabel#navFooter { color: #888888; font-size: 11px; padding: 8px; }
QPushButton#navToggle { font-size: 18px; padding: 2px; min-width: 28px; min-height: 28px; }
QPushButton#navButton { background: transparent; border: none; text-align: left; padding: 11px 13px; font-size: 14px; }
QPushButton#navButton:hover { background: #ededed; }
QPushButton#navButton:checked { background: #e5f1fb; color: #005a9e; font-weight: 600; border-left: 3px solid #0067c0; padding-left: 10px; }
QFrame#card { background: #ffffff; border: 1px solid #e5e5e5; border-radius: 10px; }
QLabel#title { font-size: 24px; font-weight: 600; }
QLabel#subtitle { color: #666666; }
QLabel#section { font-size: 15px; font-weight: 600; }
QLabel#pathPill { color: #555555; background: #f5f5f5; border: 1px solid #e0e0e0; border-radius: 5px; padding: 7px 10px; }
QLabel#dropHint { color: #466b86; background: #edf6fc; border-radius: 5px; padding: 8px 11px; }
QLabel#success { color: #0f7b0f; font-weight: 600; }
QLabel#error { color: #c42b1c; font-weight: 600; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit { background: #ffffff; border: 1px solid #c7c7c7; border-bottom: 2px solid #8a8a8a; border-radius: 5px; padding: 7px 9px; min-height: 20px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTimeEdit:focus { border-bottom-color: #0067c0; }
QPushButton { background: #ffffff; border: 1px solid #c7c7c7; border-radius: 5px; padding: 7px 14px; min-height: 20px; }
QPushButton:hover { background: #f6f6f6; }
QPushButton:pressed { background: #eeeeee; }
QPushButton#primary { background: #0067c0; border-color: #0067c0; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #1975c5; }
QPushButton#symbolButton { font-size: 22px; font-weight: 500; padding: 0; }
QPushButton:disabled { color: #999999; background: #eeeeee; border-color: #dddddd; }
QTreeWidget, QListWidget, QTableWidget, QTextEdit { background: #ffffff; border: 1px solid #e2e2e2; border-radius: 7px; selection-background-color: #cce8ff; selection-color: #202020; outline: none; }
QTreeWidget#repositoryTree::item { min-height: 32px; padding: 2px 4px; }
QTreeWidget#repositoryTree::item:selected { background: #cce8ff; border-radius: 3px; }
QHeaderView::section { background: #fafafa; border: none; border-right: 1px solid #d6d6d6; border-bottom: 1px solid #e5e5e5; padding: 7px; font-weight: 600; }
QTabWidget::pane { border: none; background: transparent; }
QTabBar::tab { background: transparent; padding: 8px 18px; margin-right: 4px; border-bottom: 3px solid transparent; }
QTabBar::tab:hover { background: #f3f3f3; border-radius: 5px; }
QTabBar::tab:selected { color: #0067c0; font-weight: 600; border-bottom-color: #0067c0; }
QMenu { background: #ffffff; border: 1px solid #d8d8d8; border-radius: 6px; padding: 5px; }
QMenu::item { padding: 7px 24px; border-radius: 4px; }
QMenu::item:selected { background: #e8f3fb; }
QFrame#dropArea { background: #fafcff; border: 2px dashed #8bbbe8; border-radius: 10px; }
QFrame#dropArea[dragging="true"] { background: #e8f3fb; border-color: #0067c0; }
QProgressBar { border: 1px solid #d7d7d7; background: #eeeeee; border-radius: 5px; min-height: 18px; max-height: 18px; text-align: center; color: #202020; font-weight: 600; }
QProgressBar::chunk { background: #60aee8; border-radius: 4px; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 17px; height: 17px; background: #ffffff; border: 1px solid #777777; border-radius: 3px; }
QCheckBox::indicator:hover { border-color: #0067c0; background: #f3f9fd; }
QCheckBox::indicator:checked { background: #0067c0; border-color: #0067c0; image: url(modelscope_manager/assets/check.svg); }
QCheckBox::indicator:checked:hover { background: #1975c5; border-color: #1975c5; }
QCheckBox::indicator:disabled { background: #eeeeee; border-color: #bdbdbd; }
QSplitter::handle { background: #d5d5d5; width: 1px; margin-left: 3px; margin-right: 3px; }
QSplitter::handle:hover { background: #0067c0; }
QToolTip { background: #ffffff; color: #202020; border: 1px solid #cccccc; padding: 6px; }
"""


DARK_QSS = r"""
* { font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 13px; color: #e8e8e8; }
QMainWindow, QWidget#root { background: #202124; }
QScrollArea#settingsScroll, QWidget#settingsContent { background: #202124; }
QFrame#sidebar, QFrame#navSidebar { background: #25272b; border-right: 1px solid #3b3e44; }
QLabel#brand { font-size: 19px; font-weight: 650; color: #f2f2f2; padding: 4px 8px; }
QLabel#navFooter, QLabel#subtitle { color: #b7bac0; }
QPushButton#navToggle { font-size: 18px; padding: 2px; min-width: 28px; min-height: 28px; }
QPushButton#navButton { background: transparent; border: none; text-align: left; padding: 11px 13px; font-size: 14px; }
QPushButton#navButton:hover { background: #34373d; }
QPushButton#navButton:checked { background: #173b58; color: #d9edff; font-weight: 600; border-left: 3px solid #4aa3df; padding-left: 10px; }
QFrame#card { background: #2a2d32; border: 1px solid #41454d; border-radius: 10px; }
QLabel#title { font-size: 24px; font-weight: 600; }
QLabel#section { font-size: 15px; font-weight: 600; }
QLabel#pathPill { color: #d6d8dc; background: #30343a; border: 1px solid #4a4f58; border-radius: 5px; padding: 7px 10px; }
QLabel#dropHint { color: #b7dbf4; background: #213647; border-radius: 5px; padding: 8px 11px; }
QLabel#success { color: #74d680; font-weight: 600; }
QLabel#error { color: #ff8e84; font-weight: 600; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit { color: #f2f2f2; background: #202226; border: 1px solid #5a5f69; border-bottom: 2px solid #a5aab3; border-radius: 5px; padding: 7px 9px; min-height: 20px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTimeEdit:focus { border-bottom-color: #4aa3df; }
QComboBox QAbstractItemView { color: #f2f2f2; background: #2a2d32; selection-background-color: #174d73; }
QPushButton { color: #ededed; background: #30333a; border: 1px solid #5a5f69; border-radius: 5px; padding: 7px 14px; min-height: 20px; }
QPushButton:hover { background: #3a3e46; }
QPushButton:pressed { background: #24272d; }
QPushButton#primary { background: #1676b5; border-color: #1676b5; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #2588c7; }
QPushButton#symbolButton { font-size: 22px; font-weight: 500; padding: 0; }
QPushButton:disabled { color: #858991; background: #292c31; border-color: #42464d; }
QTreeWidget, QListWidget, QTableWidget, QTextEdit { color: #eeeeee; background: #24272c; border: 1px solid #454a53; border-radius: 7px; selection-background-color: #174d73; selection-color: #ffffff; outline: none; }
QTreeWidget#repositoryTree::item { min-height: 32px; padding: 2px 4px; }
QTreeWidget#repositoryTree::item:selected { background: #174d73; border-radius: 3px; }
QHeaderView::section { color: #e8e8e8; background: #30333a; border: none; border-right: 1px solid #4a4f58; border-bottom: 1px solid #4a4f58; padding: 7px; font-weight: 600; }
QTabWidget::pane { border: none; background: transparent; }
QTabBar::tab { background: transparent; padding: 8px 18px; margin-right: 4px; border-bottom: 3px solid transparent; }
QTabBar::tab:hover { background: #34373d; border-radius: 5px; }
QTabBar::tab:selected { color: #72bbeb; font-weight: 600; border-bottom-color: #4aa3df; }
QMenu { color: #eeeeee; background: #2d3036; border: 1px solid #4b5059; border-radius: 6px; padding: 5px; }
QMenu::item { padding: 7px 24px; border-radius: 4px; }
QMenu::item:selected { background: #174d73; }
QFrame#dropArea { background: #223442; border: 2px dashed #4c8fbe; border-radius: 10px; }
QFrame#dropArea[dragging="true"] { background: #1a465f; border-color: #63b5ed; }
QProgressBar { border: 1px solid #555b65; background: #30333a; border-radius: 5px; min-height: 18px; max-height: 18px; text-align: center; color: #f2f2f2; font-weight: 600; }
QProgressBar::chunk { background: #277db8; border-radius: 4px; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 17px; height: 17px; background: #202226; border: 1px solid #a0a5ad; border-radius: 3px; }
QCheckBox::indicator:hover { border-color: #63b5ed; background: #293d4c; }
QCheckBox::indicator:checked { background: #1676b5; border-color: #1676b5; image: url(modelscope_manager/assets/check.svg); }
QCheckBox::indicator:checked:hover { background: #2588c7; border-color: #2588c7; }
QCheckBox::indicator:disabled { background: #292c31; border-color: #545963; }
QSplitter::handle { background: #4a4f58; width: 1px; margin-left: 3px; margin-right: 3px; }
QSplitter::handle:hover { background: #4aa3df; }
QToolTip { background: #30333a; color: #f2f2f2; border: 1px solid #606671; padding: 6px; }
"""


ACRYLIC_LIGHT_QSS = r"""
QMainWindow, QWidget#root, QScrollArea#settingsScroll, QWidget#settingsContent { background: rgba(243,243,243,218); }
QFrame#navSidebar { background: rgba(247,247,247,205); }
QFrame#card { background: rgba(255,255,255,225); }
"""

ACRYLIC_DARK_QSS = r"""
QMainWindow, QWidget#root, QScrollArea#settingsScroll, QWidget#settingsContent { background: rgba(32,33,36,218); }
QFrame#navSidebar { background: rgba(37,39,43,205); }
QFrame#card { background: rgba(42,45,50,225); }
"""


def theme_qss(dark: bool, acrylic: bool = False) -> str:
    base = DARK_QSS if dark else QSS
    if not acrylic:
        return base
    return base + (ACRYLIC_DARK_QSS if dark else ACRYLIC_LIGHT_QSS)
