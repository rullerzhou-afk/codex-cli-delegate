import copy, importlib.util, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(os.environ.get('DELEGATE_SCRIPT', 'work/skill-kimi-hooks/claude-delegate/scripts/claude_task.py')).resolve()
sys.path.insert(0, str(SCRIPT.parent))
import claude_task as ct
import kimi_backend as k
import kimi_hooks as h
spec = importlib.util.spec_from_file_location('fixtures', Path(__file__).resolve().parent.parent / 'skill-kimi/test_kimi.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.path = self.home / 'config.toml'
        self.base = b'# user comment\r\n[thinking]\r\nenabled = true\r\neffort = "max"\r\n\r\n[[hooks]]\r\nevent="Stop"\r\ncommand="node /path/kimi-hook.js"\r\n\r\n[user_section]\r\nkeep = "yes"'
        self.path.write_bytes(self.base)
        self.path.chmod(0o600)
    def tearDown(self): self.tmp.cleanup()
    def test_install_idempotent_and_remove_exact_original_bytes(self):
        self.assertFalse(h.check(self.home))
        self.assertTrue(h.configure(self.home, 'install')['changed'])
        self.assertEqual(self.path.read_bytes(), self.base + h.block())
        self.assertTrue(h.check(self.home))
        self.assertFalse(h.configure(self.home, 'install')['changed'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(h.configure(self.home, 'remove')['changed'])
        self.assertEqual(self.path.read_bytes(), self.base)
    def test_foreign_suffix_preserved(self):
        h.configure(self.home, 'install')
        suffix = b'\n[other_user_section]\nkeep=true\n'
        self.path.write_bytes(self.path.read_bytes() + suffix)
        h.configure(self.home, 'remove')
        self.assertEqual(self.path.read_bytes(), self.base + suffix)
    def test_ambiguous_or_edited_block_and_inline_array_not_rewritten(self):
        for extra in (h.block() + h.block(), h.BEGIN, h.block().replace(b'timeout = 5', b'timeout = 9'), b'\nhooks = []\n'):
            with self.subTest(extra=extra[:40]):
                before = self.base + extra
                self.path.write_bytes(before)
                with self.assertRaises(ValueError): h.configure(self.home, 'install')
                self.assertEqual(self.path.read_bytes(), before)
    def test_symlink_refused(self):
        target = self.home / 'real.toml'
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(ValueError): h.configure(self.home, 'install')
        self.assertEqual(target.read_bytes(), self.base)
    def test_concurrent_writer_keeps_its_change(self):
        fsync = os.fsync
        def competing(fd):
            fsync(fd)
            self.path.write_bytes(self.base + b'\n# external change\n')
        with patch.object(h.os, 'fsync', side_effect=competing):
            with self.assertRaises(ValueError): h.configure(self.home, 'install')
        self.assertTrue(self.path.read_bytes().endswith(b'# external change\n'))
    def test_ordinary_kimi_guard_does_not_start_receiver(self):
        env = dict(os.environ)
        env.pop(h.ROUTE_ENV, None)
        # A nonexistent receiver with shell metacharacters would fail if run.
        result = subprocess.run(['/bin/sh', '-c', h.command('/no such/$(not-a-command)/receiver.py')],
                                input=b'{}', stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))


class Routing(unittest.TestCase):
    save = fixtures.KimiContract.save
    def setUp(self):
        fixtures.KimiContract.setUp(self)
        self.ctx = ct.Context(str(self.root / 'state'), owner='test-owner')
        self.job.update(job_id='11111111-2222-4333-8444-555555555555', owner='test-owner', backend='kimi', kimi_hooks=True)
        self.record.update(round=0, finalized=False)
        self.job.update(rounds=[self.record], current_round=0)
        self.directory = Path(ct.round_dir(self.ctx.job_dir(self.job['job_id']), 0))
        self.directory.mkdir(parents=True)
        self.inbox = self.directory / 'hooks.ndjson'
        self.ctx.save(self.job)
        self.address = json.loads(h.route(self.ctx.state_dir, self.job, self.record))
        self.payload = dict(hook_event_name='PostToolUseFailure', session_id=self.sid, cwd=str(self.cwd),
                            tool_name='Read', tool_call_id='tool-a', error={'message': 'SECRET'}, tool_input={'SECRET': 'value'})
    def tearDown(self): fixtures.KimiContract.tearDown(self)
    def send(self, payload=None, address=None):
        h.receive(json.dumps(self.payload if payload is None else payload).encode(),
                  json.dumps(self.address if address is None else address))
    def test_main_event_identity_only_and_stop_cannot_finalize(self):
        for event in h.EVENTS:
            self.payload['hook_event_name'] = event
            self.send()
        rows = [json.loads(line) for line in self.inbox.read_text().splitlines()]
        self.assertEqual([r['hook'] for r in rows], list(h.EVENTS))
        self.assertNotIn('SECRET', self.inbox.read_text())
        self.assertFalse(self.ctx.load(self.job['job_id'])['rounds'][0]['finalized'])
        self.assertEqual(self.inbox.stat().st_mode & 0o777, 0o600)
    def test_foreign_session_cwd_subagent_and_unsupported_event_ignored(self):
        for key, value in [('session_id', 'session_foreign'), ('cwd', str(self.root)), ('agent_id', 'child'),
                           ('hook_event_name', 'SessionEnd'), ('hook_event_name', 'Notification')]:
            with self.subTest(key=key, value=value):
                self.send(dict(self.payload, **{key: value}))
                self.assertFalse(self.inbox.exists())
    def test_owner_round_token_wrong_backend_disabled_finalized_and_stopped_ignored(self):
        for key, value in [('owner', 'someone-else'), ('round', 1), ('round', False), ('token', 'old-token')]:
            self.send(address=dict(self.address, **{key: value}))
            self.assertFalse(self.inbox.exists())
        for change in ('backend', 'kimi_hooks', 'finalized', 'stop_requested'):
            job = copy.deepcopy(self.job)
            if change == 'finalized': job['rounds'][0]['finalized'] = True
            else: job[change] = {'backend': 'claude', 'kimi_hooks': False, 'stop_requested': True}[change]
            self.ctx.save(job)
            self.send()
            self.assertFalse(self.inbox.exists())
    def test_no_marker_or_newer_foreign_turn_cannot_adopt_session(self):
        self.native[1]['input'] = []
        self.save()
        self.send()
        self.assertFalse(self.inbox.exists())
        self.native[1]['input'] = [dict(type='text', text='[delegation-run: %s]\nTask' % self.record['run_token'])]
        self.native.append(dict(type='turn.prompt', agentId='main', input=[]))
        self.save()
        self.send()
        self.assertFalse(self.inbox.exists())
    def test_malformed_and_oversized_input_ignored(self):
        for raw, route in [(b'invalid', json.dumps(self.address)), (b'{}', 'invalid'),
                           (b'x' * 1_048_577, json.dumps(self.address)), (b'{}', '')]:
            h.receive(raw, route)
        self.assertFalse(self.inbox.exists())
    def test_receive_cli_is_silent_and_zero(self):
        env = dict(os.environ, **{h.ROUTE_ENV: json.dumps(self.address)})
        result = subprocess.run([sys.executable, str(SCRIPT.parent/'kimi_hooks.py'), 'receive'],
                                input=json.dumps(self.payload).encode(), capture_output=True, env=env)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))
        self.assertTrue(self.inbox.exists())


class Notification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.clock = [0]
        self.job = dict(kimi_home=str(self.directory), cwd=str(self.directory), kimi_hooks=True)
        self.record = dict(run_token='test-token', started_epoch=0)
        self.m = k.KimiMonitor(self.directory, self.job, self.record, ct.write_json, clock=lambda: self.clock[0])
    def tearDown(self): self.tmp.cleanup()
    def test_hooks_before_and_after_native_error_only_notify_once(self):
        for before in (True, False):
            self.clock[0] = 0
            self.m = k.KimiMonitor(self.directory, self.job, self.record, ct.write_json, clock=lambda: self.clock[0])
            with self.subTest(hook_first=before):
                hook = dict(hook='PostToolUseFailure', tool='Read', tool_call_id='a')
                if before: self.m.hook(hook)
                self.m.fallback_error(('tool', 'a'), 'tool_result', 'Read', 'Kimi tool failed; see private evidence')
                if not before: self.m.hook(hook)
                self.clock[0] = 2
                self.m.tick()
                self.assertEqual(len(self.m.events), 1)
                self.assertEqual(self.m.events[0]['error']['source'], 'PostToolUseFailure')
                self.assertEqual(self.m.snapshot()['latest_error']['source'], 'PostToolUseFailure')
    def test_missing_hook_falls_back_then_late_duplicate_stays_quiet(self):
        self.m.fallback_error(('tool', 'a'), 'tool_result', 'Read', 'Kimi tool failed; see private evidence')
        self.assertEqual(self.m.events, [])
        self.clock[0] = 2
        self.m.tick()
        self.assertEqual(len(self.m.events), 1)
        self.assertEqual(self.m.events[0]['error']['source'], 'tool_result')
        self.m.hook(dict(hook='PostToolUseFailure', tool='Read', tool_call_id='a'))
        self.assertEqual(len(self.m.events), 1)
    def test_fatal_hook_once_and_stop_hint_only(self):
        self.m.hook(dict(hook='Stop'))
        self.assertEqual(self.m.events, [])
        self.m.hook(dict(hook='StopFailure'))
        self.m.fallback_error(('turn', None), 'kimi_turn', message='Kimi turn failed; see private evidence', fatal=True)
        self.m.hook(dict(hook='StopFailure'))
        self.m.tick(complete=True)
        self.assertEqual(len(self.m.events), 1)
        self.assertTrue(self.m.events[0]['error']['fatal'])
        self.assertEqual(self.m.events[0]['error']['source'], 'StopFailure')


if __name__ == '__main__': unittest.main()
