"""Minimal-image trust setup and failure before starting an expensive solver."""
import asyncio
import importlib.util
import json
from pathlib import Path
import ssl
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from evaluation_queue import W
from runtime_trust import ca_bundle, configure_agent, IMPORT_PATH

spec=importlib.util.spec_from_file_location('universe_codex',Path(__file__).with_name('harbor-extensions')/'universe_codex.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class RuntimeTrustTests(unittest.TestCase):
    def test_real_harbor_factory_selects_adapter_for_legacy_and_new_profiles(self):
        from harbor.agents.factory import AgentFactory
        from harbor.models.trial.config import AgentConfig
        profiles = [dict(name='codex-preinstall'),
                    dict(name='codex-preinstall', import_path=IMPORT_PATH),
                    dict(name=None, import_path=IMPORT_PATH)]
        extension=str(Path(__file__).with_name('harbor-extensions'))
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw, \
             patch.object(sys, 'path', [extension, *sys.path]):
            for profile in profiles:
                configure_agent(profile)
                config=AgentConfig(**profile, model_name='openai/deepseek-v4.1-flash-1017')
                agent=AgentFactory.create_agent_from_config(config, logs_dir=Path(raw))
                self.assertEqual(type(agent).__name__, 'UniverseCodex')
                self.assertEqual(agent.extra_env['SSL_CERT_FILE'], module.REMOTE_CA)
            public=dict(name='codex-preinstall', import_path='public_codex:PublicCodex')
            configure_agent(public)
            self.assertEqual(public['import_path'], 'public_codex:PublicCodex')

    def test_workspace_bundle_contains_public_trust_roots(self):
        path=ca_bundle()
        self.assertTrue(path.is_relative_to(W))
        self.assertGreater(len(ssl.create_default_context(cafile=str(path)).get_ca_certs()),0)

    def test_adapter_uploads_ca_and_preflights_before_model(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            obj=object.__new__(module.UniverseCodex);obj.logs_dir=Path(raw)/'agent'
            env=SimpleNamespace(upload_file=AsyncMock())
            with patch.object(module.CodexPreinstall,'install',AsyncMock()), \
                 patch.object(obj,'exec_as_agent',AsyncMock(return_value=SimpleNamespace(return_code=0))) as execute:
                asyncio.run(obj.install(env))
            self.assertEqual(env.upload_file.await_args_list[0].args,(ca_bundle(),module.REMOTE_CA))
            self.assertEqual(env.upload_file.await_args_list[1].args,
                             (obj.logs_dir/'runtime-trust.json','/logs/agent/runtime-trust.json'))
            self.assertEqual(execute.call_args.kwargs['timeout_sec'],30)
            script=execute.call_args.kwargs['command']
            self.assertIn('create_default_context',script)
            self.assertNotIn('CERT_NONE',script)
            self.assertTrue(json.loads((obj.logs_dir/'runtime-trust.json').read_text())['tls_verified'])

    def test_tls_failure_does_not_mark_setup_ready(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            obj=object.__new__(module.UniverseCodex);obj.logs_dir=Path(raw)/'agent'
            with patch.object(module.CodexPreinstall,'install',AsyncMock()), \
                 patch.object(obj,'exec_as_agent',AsyncMock(return_value=SimpleNamespace(return_code=1))):
                with self.assertRaisesRegex(RuntimeError,'ModelEndpointTLSPreflightFailed'):
                    asyncio.run(obj.install(SimpleNamespace(upload_file=AsyncMock())))
            self.assertFalse((obj.logs_dir/'runtime-trust.json').exists())
