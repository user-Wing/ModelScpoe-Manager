import unittest

from modelscope_manager.styles import theme_qss


class ThemeStyleTests(unittest.TestCase):
    def test_both_themes_style_time_editors(self):
        self.assertIn("QTimeEdit", theme_qss(False))
        self.assertIn("QTimeEdit", theme_qss(True))
        self.assertNotEqual(theme_qss(False), theme_qss(True))

    def test_acrylic_theme_keeps_qt_top_level_opaque(self):
        self.assertNotIn("rgba(243,243,243,218)", theme_qss(False, True))
        self.assertNotIn("rgba(32,33,36,218)", theme_qss(True, True))
        self.assertIn("MainWindow, QMainWindow", theme_qss(False, True))
