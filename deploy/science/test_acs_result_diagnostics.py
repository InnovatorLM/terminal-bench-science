import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from acs_result_diagnostics import classify_trial


class TrialDiagnosticsTest(unittest.TestCase):
    def test_network_value_error_is_not_a_model_failure(self):
        got = classify_trial({"verifier_result": None, "exception_info": {
            "exception_type": "ValueError",
            "exception_message": "verifier phase network policy differs from baseline",
        }})
        self.assertEqual(got["diagnostic"], "platform_dynamic_network_policy_mismatch")
        self.assertFalse(got["scored"])
        self.assertIsNone(got["reward"])

    def test_missing_verifier_result_is_unscored(self):
        got = classify_trial({"verifier_result": None})
        self.assertEqual(got["diagnostic"], "verifier_result_missing")
        self.assertFalse(got["scored"])

    def test_valid_reward_is_scored(self):
        got = classify_trial({"verifier_result": {"rewards": {"reward": 1}}})
        self.assertEqual(got["diagnostic"], "ok")
        self.assertTrue(got["scored"])


if __name__ == "__main__":
    unittest.main()
