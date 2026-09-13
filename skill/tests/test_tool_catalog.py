import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import claude_task as ct
import kimi_backend as kb
import opencode_backend as oc
import tool_catalog as catalog


class ToolCatalogTests(unittest.TestCase):
    def test_kimi_default_media_and_explicit_selection(self):
        self.assertIn('ReadMediaFile', catalog.select('kimi', None))
        self.assertEqual(catalog.select('kimi', ['Read']), ['Read'])
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / 'config.toml'
            config.write_text('[thinking]\nenabled=true\neffort="max"\n')
            with patch.dict(os.environ, {'KIMI_CODE_HOME': root}), patch.object(kb, 'require_hooks'), patch.object(kb.sys, 'platform', 'darwin'):
                prepared = kb.prepare('/bin/echo', None, [])
            job = dict(prepared, session_id=None)
            prompt = Path(root) / 'task'; prompt.write_text('Read the image')
            args = kb.argv(job, {'run_token': 'fixture'}, False, str(prompt), root)
            self.assertIn('--agent-file', args)
            profile = (Path(root) / 'agent.md').read_text()
            self.assertIn('ReadMediaFile', profile)
            self.assertIn('subagents: []', profile)
            self.assertNotIn('WebSearch', profile)

    def test_same_validation_for_mcp_backend_and_cli(self):
        for backend, prepare in [('kimi', kb.prepare), ('opencode', oc.prepare)]:
            for tools in [['MadeUp'], ['Agent'], ['mcp__unknown'], ['*'], 'Read', [None]]:
                with self.subTest(backend=backend, tools=tools), self.assertRaises(ct.CliError):
                    prepare('/bin/echo', tools, [])
        parser = ct.build_parser()
        for backend, names in [('kimi', catalog.KIMI_TOOLS), ('opencode', catalog.OPENCODE_TOOLS + tuple(catalog.OPENCODE_ALIASES))]:
            for name in names:
                args = parser.parse_args(['start', '--cwd', '/tmp', '--prompt-file', '/tmp/task', '--backend', backend, '--'+backend+'-tool', name])
                self.assertEqual(getattr(args, backend+'_tool'), [name])

    def test_opencode_write_alias_and_web_selection(self):
        selected = catalog.select('opencode', ['write', 'apply_patch', 'webfetch', 'todowrite'])
        self.assertEqual(selected, ['edit', 'todowrite', 'webfetch'])
        self.assertNotIn('bash', selected)
        with tempfile.TemporaryDirectory() as root:
            prompt = Path(root) / 'task'; prompt.write_text('scope')
            job = dict(opencode_tools=selected, opencode_bin='/bin/echo', cwd=root, model=oc.MODEL, effort=oc.EFFORT)
            with patch.object(oc, 'prepare', return_value={'opencode_version':'fixture','opencode_compatibility':{}}):
                _, env, _ = oc.setup(job, {'run_token':'fixture'}, False, str(prompt), root)
            permissions = json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']
            self.assertEqual(permissions['webfetch'], 'allow')
            self.assertEqual(permissions['*'], 'deny')
            self.assertNotIn('bash', permissions)
            self.assertEqual(json.loads(env['CODEX_OPENCODE_ROUTE'])['tools'], selected)

    def test_claude_optional_web_tools_and_notebook(self):
        base = ct.enabled_tools({})
        self.assertIn('NotebookEdit', base)
        self.assertNotIn('WebSearch', base)
        rules = ct.validate_allow_rules(['WebFetch(domain:example.com)', 'WebSearch'])
        job = {'claude_bin':'/bin/echo', 'allow_tools':rules, 'session_id':'fixture'}
        args = ct.build_claude_argv(job, 'token', False)
        selected = args[args.index('--tools')+1].split(',')
        self.assertIn('WebFetch', selected)
        self.assertIn('WebSearch', selected)
        self.assertNotIn('Agent', selected)
        from claude_events import hook_settings
        settings = hook_settings('script', '/tmp', dict(job, job_id='fixture', cwd='/tmp/work'), {'round':0,'run_token':'t'})
        self.assertIn('WebFetch(domain:example.com)', settings['permissions']['allow'])
        self.assertIn('Edit(//tmp/work/**)', settings['permissions']['allow'])

    def test_capabilities_command_without_model_or_owner(self):
        args = ct.build_parser().parse_args(['capabilities'])
        info = ct.HANDLERS[args.command](None, args)
        self.assertIn('ReadMediaFile', info['kimi']['supported'])
        self.assertIn('webfetch', info['opencode']['supported'])
