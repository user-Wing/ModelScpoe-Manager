import unittest
from datetime import datetime

from modelscope_manager.transfer_policy import MIB, SpeedRule, TransferPolicy


class TransferPolicyTests(unittest.TestCase):
    def test_disabled_policy_is_unlimited(self):
        policy = TransferPolicy(False, 1, 2, [SpeedRule("08:00", "09:00", 3, 4)])
        self.assertEqual(policy.limits(datetime(2026, 1, 1, 8, 30)), (0, 0))

    def test_daily_rule_overrides_defaults(self):
        policy = TransferPolicy(True, 10, 20, [SpeedRule("08:00", "09:00", 1.5, 2.5)])
        self.assertEqual(policy.limits(datetime(2026, 1, 1, 7, 59)), (10 * MIB, 20 * MIB))
        self.assertEqual(policy.limits(datetime(2026, 1, 1, 8, 30)), (int(1.5 * MIB), int(2.5 * MIB)))

    def test_cross_midnight_and_last_matching_rule_wins(self):
        policy = TransferPolicy(True, 10, 20, [
            SpeedRule("22:00", "08:00", 3, 4),
            SpeedRule("23:00", "01:00", 1, 2),
        ])
        self.assertEqual(policy.limits(datetime(2026, 1, 1, 23, 30)), (MIB, 2 * MIB))
        self.assertEqual(policy.limits(datetime(2026, 1, 2, 7, 30)), (3 * MIB, 4 * MIB))

    def test_round_trip(self):
        expected = TransferPolicy(True, 1.25, 2.5, [SpeedRule("12:00", "13:00", 4, 5)])
        restored = TransferPolicy.from_dict(expected.to_dict())
        self.assertEqual(restored.to_dict(), expected.to_dict())

    def test_equal_start_and_end_is_rejected(self):
        with self.assertRaises(ValueError):
            TransferPolicy(True, rules=[SpeedRule("08:00", "08:00", 1, 1)])


if __name__ == "__main__":
    unittest.main()
