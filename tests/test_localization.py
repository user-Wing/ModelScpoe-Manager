import unittest

from modelscope_manager.localization import LocaleManager


class LocalizationTests(unittest.TestCase):
    def test_english_locale_loads_from_json(self):
        locale = LocaleManager("en_US")
        self.assertEqual(locale.text("资源管理"), "Resources")
        self.assertEqual(locale.text("开始下载"), "Start download")

    def test_chinese_locale_uses_source_text(self):
        locale = LocaleManager("zh_CN")
        self.assertEqual(locale.text("资源管理"), "资源管理")

    def test_unknown_locale_falls_back_to_chinese(self):
        locale = LocaleManager("invalid")
        self.assertEqual(locale.language, "zh_CN")


if __name__ == "__main__":
    unittest.main()
