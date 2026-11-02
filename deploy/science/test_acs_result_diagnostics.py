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

    def test_invalid_rewards_never_become_model_failures(self):
        for reward in ({}, {'reward':None}, {'reward':True}, {'reward':'0'},
                       {'reward':float('nan')}, {'reward':float('inf')}, {'reward':.5}, {'reward':-1}):
            with self.subTest(reward=reward):
                got=classify_trial({'verifier_result':{'rewards':reward}})
                self.assertFalse(got['scored'])
                self.assertIsNone(got['reward'])

    def test_exceptions_cannot_be_hidden_by_a_reward(self):
        for key,value in [('exception_info',{'exception_type':'ValueError'}),
                          ('exception_info',{'exception_type':'AgentTimeoutError'}),
                          ('cleanup_exception_info',{'exception_type':'RuntimeError'})]:
            with self.subTest(key=key,value=value):
                got=classify_trial({'verifier_result':{'rewards':{'reward':0}},key:value})
                self.assertFalse(got['scored'])

    def test_malformed_trial_is_unscored(self):
        for value in (None,[],False,{'exception_info':['bad']}):
            with self.subTest(value=value):self.assertFalse(classify_trial(value)['scored'])

    def test_timeout_requires_diagnosis_before_scoring(self):
        got=classify_trial({'exception_info':{'exception_type':'AgentTimeoutError'},'verifier_result':None})
        self.assertEqual(got['status'],'incomplete')
        self.assertEqual(got['diagnostic'],'agent_timeout_requires_cause_review')
        self.assertFalse(got['scored'])


if __name__ == "__main__":
    unittest.main()
