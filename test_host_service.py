"""Check host recovery and service preparation without changing Mac services."""
import os
import errno
from pathlib import Path
import plistlib
import signal
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import install_host_service as service
import start_host


class HostRecoveryTests(unittest.TestCase):
    def test_waits_through_starting(self):
        status = Mock(side_effect=[None, {'BackendState': 'Starting'}, {'BackendState': 'Running'}])
        with patch.object(start_host.time, 'sleep') as sleep:
            self.assertEqual(start_host.wait_until_running(status)['BackendState'], 'Running')
        self.assertEqual(status.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_does_not_wait_on_login_requirement(self):
        status = Mock(return_value={'BackendState': 'NeedsLogin'})
        with patch.object(start_host.time, 'sleep') as sleep:
            self.assertEqual(start_host.wait_until_running(status)['BackendState'], 'NeedsLogin')
        sleep.assert_not_called()

    def test_wait_is_bounded(self):
        status = Mock(return_value={'BackendState': 'Starting'})
        with patch.object(start_host.time, 'sleep'):
            self.assertEqual(start_host.wait_until_running(status, attempts=3)['BackendState'], 'Starting')
        self.assertEqual(status.call_count, 3)

    def test_stops_waiting_when_owned_daemon_exits(self):
        daemon = Mock()
        daemon.poll.return_value = 2
        status = Mock()
        self.assertIsNone(start_host.wait_until_running(status, daemon))
        status.assert_not_called()

    def test_owned_daemon_failure_signals_only_self(self):
        failed = threading.Event()
        with patch.object(start_host.os, 'kill') as kill:
            start_host.watch_owned_daemon(Mock(), threading.Event(), failed, threading.Lock())
        self.assertTrue(failed.is_set())
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)

    def test_normal_shutdown_never_requests_restart(self):
        stopping, failed = threading.Event(), threading.Event()
        stopping.set()
        with patch.object(start_host.os, 'kill') as kill:
            start_host.watch_owned_daemon(Mock(), stopping, failed, threading.Lock())
        self.assertFalse(failed.is_set())
        kill.assert_not_called()

    def test_owned_cleanup_does_not_touch_an_exited_process(self):
        process = Mock()
        process.poll.return_value = 0
        start_host.stop_owned_process(process, 3)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_owned_cleanup_escalates_only_its_process_after_timeout(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('owned', 3), 0]
        start_host.stop_owned_process(process, 3)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_term_uses_graceful_exit_and_restores_handler(self):
        original = signal.getsignal(signal.SIGTERM)
        def request_stop(_args):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        with patch.object(start_host.nutcracker, 'remote_settings', return_value=None), \
                patch.object(start_host.nutcracker, 'main', side_effect=request_stop):
            with self.assertRaises(SystemExit) as stopped:
                start_host.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(signal.getsignal(signal.SIGTERM), original)


class ServicePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.runtime = self.root / '.runtime'
        self.runtime.mkdir()
        self.python = self.root / 'bundled-python'
        self.python.write_text('#!/bin/sh\nexit 0\n')
        self.python.chmod(0o755)
        (self.runtime / 'python').symlink_to(self.python)
        (self.root / 'start_host.py').write_text('# host\n')
        for name in service.HOST_FILES:
            file = self.root / name
            if not file.exists():
                file.write_text('test host asset\n')
        for name in service.VENDOR_FILES:
            file = self.root / 'vendor' / 'novnc' / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('test viewer asset\n')
        dependency = self.runtime / 'python-deps' / 'test_package' / '__init__.py'
        dependency.parent.mkdir(parents=True)
        dependency.write_text('# dependency\n')
        connector = self.runtime / 'tailscale'
        connector.mkdir()
        for name in ('tailscale', 'tailscaled'):
            file = connector / name
            file.write_text('#!/bin/sh\nexit 0\n')
            file.chmod(0o700)
        state = connector / 'state'
        (state / 'certs').mkdir(parents=True)
        (state / 'tailscaled.state').write_text('test connector state')
        (state / 'certs' / 'test.crt').write_text('test certificate')
        (self.runtime / 'auth.json').write_text('test private account settings')
        (self.runtime / 'remote.json').write_text('test private remote settings')
        self.home = self.root / 'test-home'
        (self.home / 'Library' / 'Application Support').mkdir(parents=True)
        self.host = service.background_path(self.home)

    def tearDown(self):
        self.temp.cleanup()

    def test_prepares_exact_user_service_without_launchctl(self):
        with patch.object(service.subprocess, 'run') as run:
            path, config = service.prepare(self.root)
        run.assert_not_called()
        self.assertEqual(plistlib.loads(path.read_bytes()), config)
        self.assertEqual(config['Label'], 'com.nutcracker.emulator.host')
        self.assertEqual(config['ProgramArguments'], [str(self.python), '-B', str(self.root / 'start_host.py'), '--no-browser'])
        self.assertEqual(config['WorkingDirectory'], str(self.root))
        self.assertTrue(config['RunAtLoad'])
        self.assertEqual(config['KeepAlive'], {'SuccessfulExit': False})
        self.assertEqual(config['ThrottleInterval'], 30)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_logs_are_private_and_preserved(self):
        log = self.runtime / 'host.stdout.log'
        log.write_text('previous output\n')
        service.prepare(self.root)
        self.assertEqual(log.read_text(), 'previous output\n')
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o700)

    def test_rejects_log_symlink_without_changing_target(self):
        target = self.root / 'unrelated.txt'
        target.write_text('keep this')
        (self.runtime / 'host.stderr.log').symlink_to(target)
        with self.assertRaises(OSError):
            service.prepare(self.root)
        self.assertEqual(target.read_text(), 'keep this')

    def test_rejects_runtime_symlink(self):
        linked_root = self.root / 'linked-root'
        linked_root.mkdir()
        (linked_root / '.runtime').symlink_to(self.runtime)
        with self.assertRaises(service.ServiceError):
            service.prepare(linked_root)

    def test_rejects_host_writable_by_other_users(self):
        (self.root / 'start_host.py').chmod(0o666)
        with self.assertRaises(service.ServiceError):
            service.prepare(self.root)

    def test_missing_python_fails_without_installing(self):
        (self.runtime / 'python').unlink()
        with self.assertRaises(service.ServiceError):
            service.prepare(self.root)

    def test_install_bootstraps_only_current_user_exact_label(self):
        path, config = service.prepare(self.root)
        home = self.root / 'home'
        (home / 'Library').mkdir(parents=True)
        results = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]
        with patch.object(service.sys, 'platform', 'darwin'), \
                patch.object(service.Path, 'home', return_value=home), \
                patch.object(service.subprocess, 'run', side_effect=results) as run:
            service.install(path, config)
        destination = home / 'Library' / 'LaunchAgents' / (service.LABEL + '.plist')
        self.assertEqual(plistlib.loads(destination.read_bytes()), config)
        self.assertEqual(run.call_args_list[0].args[0], ['/bin/launchctl', 'print', 'gui/' + str(os.getuid()) + '/' + service.LABEL])
        self.assertEqual(run.call_args_list[1].args[0], ['/bin/launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(destination)])

    def test_existing_different_service_is_preserved(self):
        path, config = service.prepare(self.root)
        home = self.root / 'home'
        agents = home / 'Library' / 'LaunchAgents'
        agents.mkdir(parents=True)
        destination = agents / (service.LABEL + '.plist')
        existing = plistlib.dumps({'Label': 'other-service'})
        destination.write_bytes(existing)
        with patch.object(service.sys, 'platform', 'darwin'), \
                patch.object(service.Path, 'home', return_value=home), \
                patch.object(service.subprocess, 'run') as run:
            with self.assertRaises(service.ServiceError):
                service.install(path, config)
        run.assert_not_called()
        self.assertEqual(destination.read_bytes(), existing)

    def test_existing_matching_loaded_service_is_not_restarted(self):
        path, config = service.prepare(self.root)
        home = self.root / 'home'
        agents = home / 'Library' / 'LaunchAgents'
        agents.mkdir(parents=True)
        (agents / (service.LABEL + '.plist')).write_bytes(path.read_bytes())
        with patch.object(service.sys, 'platform', 'darwin'), \
                patch.object(service.Path, 'home', return_value=home), \
                patch.object(service.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            service.install(path, config)
        self.assertEqual(run.call_count, 1)

    def test_background_bundle_contains_only_explicit_host_inputs(self):
        (self.root / 'private-personal.txt').write_text('not a host input')
        (self.runtime / 'windows.qcow2').write_text('do not copy VM disks')
        (self.runtime / 'tailscale' / 'daemon.log').write_text('do not copy daemon logs')
        (self.root / 'vendor' / 'novnc' / 'unnecessary.iso').write_text('not a viewer file')
        service.prepare_background_bundle(self.root, self.host)
        found = {str(file.relative_to(self.host)) for file in self.host.rglob('*') if file.is_file()}
        expected = set(service.HOST_FILES)
        expected.update('vendor/novnc/' + name for name in service.VENDOR_FILES)
        expected.update({
            '.runtime/python', '.runtime/auth.json', '.runtime/remote.json',
            '.runtime/python-deps/test_package/__init__.py',
            '.runtime/tailscale/tailscale', '.runtime/tailscale/tailscaled',
            '.runtime/tailscale/state/tailscaled.state', '.runtime/tailscale/state/certs/test.crt',
        })
        self.assertEqual(found, expected)

    def test_background_service_uses_application_support_for_runner_and_logs(self):
        service.prepare_background_bundle(self.root, self.host)
        path, config = service.prepare(self.host)
        self.assertEqual(config['WorkingDirectory'], str(self.host))
        self.assertEqual(config['ProgramArguments'][2], str(self.host / 'start_host.py'))
        self.assertEqual(config['StandardOutPath'], str(self.host / '.runtime' / 'host.stdout.log'))
        self.assertEqual(config['StandardErrorPath'], str(self.host / '.runtime' / 'host.stderr.log'))
        self.assertTrue(path.is_relative_to(self.home / 'Library' / 'Application Support'))
        self.assertEqual(stat.S_IMODE((self.host / '.runtime').stat().st_mode), 0o700)

    def test_preview_builds_future_service_without_creating_background_bundle(self):
        with patch.object(service.subprocess, 'run') as run:
            path, config = service.prepare_preview(self.root, self.host)
        run.assert_not_called()
        self.assertFalse(self.host.exists())
        self.assertEqual(plistlib.loads(path.read_bytes()), config)
        self.assertEqual(config['WorkingDirectory'], str(self.host))
        self.assertEqual(config['ProgramArguments'][2], str(self.host / 'start_host.py'))

    def test_immutable_bundle_files_use_hardlinks_and_state_is_independent(self):
        service.prepare_background_bundle(self.root, self.host)
        self.assertTrue((self.root / 'console.js').samefile(self.host / 'console.js'))
        self.assertTrue((self.runtime / 'tailscale' / 'tailscaled').samefile(self.host / '.runtime' / 'tailscale' / 'tailscaled'))
        self.assertTrue((self.runtime / 'python-deps' / 'test_package' / '__init__.py').samefile(self.host / '.runtime' / 'python-deps' / 'test_package' / '__init__.py'))
        self.assertFalse((self.runtime / 'auth.json').samefile(self.host / '.runtime' / 'auth.json'))
        self.assertFalse((self.runtime / 'tailscale' / 'state' / 'tailscaled.state').samefile(self.host / '.runtime' / 'tailscale' / 'state' / 'tailscaled.state'))

    def test_reinstall_preserves_installed_private_state(self):
        service.prepare_background_bundle(self.root, self.host)
        (self.host / '.runtime' / 'auth.json').write_text('updated installed account')
        (self.host / '.runtime' / 'tailscale' / 'state' / 'tailscaled.state').write_text('updated installed state')
        service.prepare_background_bundle(self.root, self.host)
        self.assertEqual((self.host / '.runtime' / 'auth.json').read_text(), 'updated installed account')
        self.assertEqual((self.host / '.runtime' / 'tailscale' / 'state' / 'tailscaled.state').read_text(), 'updated installed state')
        self.assertEqual(stat.S_IMODE((self.host / '.runtime' / 'auth.json').stat().st_mode), 0o600)

    def test_cross_volume_copy_fallback_preserves_contents(self):
        destination = self.root / 'copied-asset'
        with patch.object(service.os, 'link', side_effect=OSError(errno.EXDEV, 'cross-volume')):
            service.copy_bundle_file(self.root / 'console.js', destination)
        self.assertEqual(destination.read_bytes(), (self.root / 'console.js').read_bytes())
        self.assertFalse(destination.samefile(self.root / 'console.js'))

    def test_dependency_symlink_is_rejected(self):
        (self.runtime / 'python-deps' / 'unexpected-link').symlink_to(self.root / 'console.js')
        with self.assertRaises(service.ServiceError):
            service.prepare_background_bundle(self.root, self.host)

    def test_migration_replaces_only_known_documents_service(self):
        service.prepare_background_bundle(self.root, self.host)
        path, config = service.prepare(self.host)
        agents = self.home / 'Library' / 'LaunchAgents'
        agents.mkdir()
        destination = agents / (service.LABEL + '.plist')
        old = service.old_source_configuration(config, self.root)
        destination.write_bytes(plistlib.dumps(old))
        unrelated = agents / 'unrelated.service.plist'
        unrelated.write_bytes(b'leave untouched')
        results = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)]
        with patch.object(service.sys, 'platform', 'darwin'), \
                patch.object(service.Path, 'home', return_value=self.home), \
                patch.object(service.subprocess, 'run', side_effect=results) as run:
            service.install(path, config, source_root=self.root)
        self.assertEqual(plistlib.loads(destination.read_bytes()), config)
        self.assertEqual(unrelated.read_bytes(), b'leave untouched')
        self.assertEqual(run.call_args_list[1].args[0], ['/bin/launchctl', 'bootout', 'gui/' + str(os.getuid()) + '/' + service.LABEL])
        self.assertEqual(run.call_args_list[2].args[0], ['/bin/launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(destination)])

    def test_migration_refuses_different_runner_even_with_our_label(self):
        service.prepare_background_bundle(self.root, self.host)
        path, config = service.prepare(self.host)
        agents = self.home / 'Library' / 'LaunchAgents'
        agents.mkdir()
        destination = agents / (service.LABEL + '.plist')
        old = service.old_source_configuration(config, self.root)
        old['ProgramArguments'][2] = str(self.root / 'unrelated-program.py')
        saved = plistlib.dumps(old)
        destination.write_bytes(saved)
        with patch.object(service.sys, 'platform', 'darwin'), \
                patch.object(service.Path, 'home', return_value=self.home), \
                patch.object(service.subprocess, 'run') as run:
            with self.assertRaises(service.ServiceError):
                service.install(path, config, source_root=self.root)
        run.assert_not_called()
        self.assertEqual(destination.read_bytes(), saved)


if __name__ == '__main__':
    unittest.main()
