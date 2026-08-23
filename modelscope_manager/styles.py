QSS = r"""
* { font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 13px; color: #202020; }
QMainWindow, QWidget#root { background: #f3f3f3; }
QFrame#sidebar { background: rgba(249,249,249,245); border-right: 1px solid #dedede; }
QFrame#navSidebar { background: #f7f7f7; border-right: 1px solid #dedede; }
QLabel#brand { font-size: 19px; font-weight: 650; color: #1f1f1f; padding: 4px 8px; }
QLabel#navFooter { color: #888888; font-size: 11px; padding: 8px; }
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
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: #ffffff; border: 1px solid #c7c7c7; border-bottom: 2px solid #8a8a8a; border-radius: 5px; padding: 7px 9px; min-height: 20px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-bottom-color: #0067c0; }
QPushButton { background: #ffffff; border: 1px solid #c7c7c7; border-radius: 5px; padding: 7px 14px; min-height: 20px; }
QPushButton:hover { background: #f6f6f6; }
QPushButton:pressed { background: #eeeeee; }
QPushButton#primary { background: #0067c0; border-color: #0067c0; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #1975c5; }
QPushButton#symbolButton { font-size: 22px; font-weight: 500; padding: 0; }
QPushButton:disabled { color: #999999; background: #eeeeee; border-color: #dddddd; }
QTreeWidget, QTableWidget, QTextEdit { background: #ffffff; border: 1px solid #e2e2e2; border-radius: 7px; selection-background-color: #cce8ff; selection-color: #202020; outline: none; }
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
