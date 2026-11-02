import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from evaluation_queue import W
from author_selftest_worker import build_command,configure_control_verifier


class SelftestHarnessTests(unittest.TestCase):
    def test_frozen_verifier_budget_survives_historical_template_caps(self):
        from types import SimpleNamespace
        from harbor.models.trial.config import VerifierConfig
        from harbor.trial.single_step import SingleStepTrial
        config={'timeout_multiplier':0.5,'verifier_timeout_multiplier':0.25,
                'verifier':{'override_timeout_sec':600,'max_timeout_sec':600,
                            'tests_materialization':'upload','environment_mode':'separate','environment':{}}}
        configure_control_verifier(config)
        trial=object.__new__(SingleStepTrial)
        trial.config=SimpleNamespace(verifier=VerifierConfig.model_validate(config['verifier']),
                                     verifier_timeout_multiplier=config['verifier_timeout_multiplier'],
                                     timeout_multiplier=config['timeout_multiplier'])
        for budget in (300,900,7200):
            trial.task=SimpleNamespace(config=SimpleNamespace(verifier=SimpleNamespace(timeout_sec=budget)))
            self.assertEqual(trial._compute_verifier_timeout_sec(),budget)
        self.assertEqual(config['verifier']['tests_materialization'],'upload')
        self.assertNotIn('environment_mode',config['verifier'])

    def test_build_uses_explicit_codex_binaries(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            directory=Path(raw)
            for name in ('codex','rg'):
                binary=directory/name;binary.write_text('test');binary.chmod(0o700)
            with patch.dict(os.environ,GEWU_CODEX_BINARY=str(directory/'codex'),GEWU_RG_BINARY=str(directory/'rg')):
                args=build_command(directory,'a'*40,'task',directory/'output')
            self.assertEqual(args[args.index('--harness')+1],'codex')
            self.assertEqual(args[args.index('--codex-binary')+1],str(directory/'codex'))
            self.assertNotIn('mini-swe-agent',args)

    def test_missing_runtime_fails_before_docker_build(self):
        with patch.dict(os.environ,GEWU_CODEX_BINARY=str(W/'.tmp/nonexistent-runtime-binary')):
            with self.assertRaises(ValueError): build_command(W,'a'*40,'task',W/'.tmp/output')


if __name__ == '__main__': unittest.main()
