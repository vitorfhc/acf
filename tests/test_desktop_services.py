"""Optional desktop lifecycle and acknowledged service controls, without Docker."""
from concurrent.futures import ThreadPoolExecutor
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

RUNTIME = Path(__file__).resolve().parents[1] / 'src/acf/runtime'
sys.path.insert(0, str(RUNTIME))


def module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    loader.exec_module(result)
    return result


supervisor = module('acf_desktop_supervisor', RUNTIME / 'supervisor.py')
desktop = module('acf_desktop_runtime', RUNTIME / 'desktop.py')
control = module('acf_desktop_service_cli', RUNTIME / 'bin/acf-services')
novnc = module('acf_desktop_novnc', RUNTIME / 'install_novnc.py')


def running_process(pid=123):
    process = Mock(pid=pid)
    process.poll.return_value = None
    return process


class DesktopServiceTests(unittest.TestCase):
    def test_desktop_is_stopped_by_default_and_uses_separate_http_and_local_vnc(self):
        managed = supervisor.services({})
        for name in supervisor.DESKTOP_SERVICES:
            self.assertFalse(managed[name].enabled)
        self.assertEqual(supervisor.group_state(managed, supervisor.DESKTOP_SERVICES), 'stopped')
        self.assertTrue(managed['novnc'].enabled)
        self.assertIn('0.0.0.0:6082', managed['desktop-http'].command)
        command = managed['desktop-http'].command
        self.assertEqual(command[command.index('--token-plugin') + 1], 'TokenFile')
        self.assertEqual(command[command.index('--token-source') + 1], '/run/acf/desktop-token')
        self.assertNotIn('127.0.0.1:5902', command)
        self.assertIn('-localhost', managed['desktop-vnc'].command)
        self.assertIn('-nopw', managed['desktop-vnc'].command)
        self.assertEqual(managed['desktop-http'].state()['endpoints'][0]['path'], '/')

    def test_desktop_autostart_enables_all_dependencies(self):
        managed = supervisor.services({'desktop': True})
        self.assertTrue(all(managed[name].enabled for name in supervisor.DESKTOP_SERVICES))
        self.assertTrue(all(managed[name].enabled for name in supervisor.BROWSER_SERVICES))

    def test_settings_missing_defaults_off_and_invalid_values_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'settings.json'
            with patch.object(supervisor, 'SETTINGS', path):
                self.assertEqual(supervisor.service_settings(), {'desktop': False})
                path.write_text('{"desktop":true}')
                self.assertEqual(supervisor.service_settings(), {'desktop': True})
                for payload in ('{"desktop":"false"}', '{"desktop":1}', '[]', '{'):
                    path.write_text(payload)
                    with self.assertRaises(ValueError):
                        supervisor.service_settings()

    def test_stopping_browser_group_preserves_desktop_and_other_services(self):
        managed = supervisor.services({'desktop': True})
        for service in managed.values():
            service.process = running_process()
        stopped = []
        def stop(service):
            stopped.append(service.name)
            service.process = None
        with patch.object(supervisor.Service, 'stop', stop):
            supervisor.control_service(managed, 'browser', 'stop')
        self.assertEqual(stopped, list(reversed(supervisor.BROWSER_SERVICES)))
        self.assertFalse(any(managed[name].enabled for name in supervisor.BROWSER_SERVICES))
        self.assertEqual(supervisor.group_state(managed, supervisor.DESKTOP_SERVICES), 'running')
        self.assertTrue(managed['files'].enabled)
        self.assertTrue(managed['caido'].enabled)

    def test_public_desktop_control_never_stops_legacy_browser_display(self):
        managed = supervisor.services({'desktop': True})
        with patch.object(supervisor.Service, 'stop') as stop:
            supervisor.control_service(managed, 'desktop', 'stop')
        self.assertEqual(stop.call_count, len(supervisor.DESKTOP_SERVICES))
        self.assertTrue(managed['desktop'].enabled)
        self.assertTrue(managed['novnc'].enabled)
        self.assertFalse(any(managed[name].enabled for name in supervisor.DESKTOP_SERVICES))

    def test_missing_desktop_packages_do_not_enable_any_dependency(self):
        managed = supervisor.services({})
        with patch.object(supervisor.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Build container images'):
                supervisor.control_service(managed, 'desktop', 'start')
        self.assertFalse(any(managed[name].enabled for name in supervisor.DESKTOP_SERVICES))

    def test_starting_profile_restores_browser_dependencies_only(self):
        managed = supervisor.services({})
        with patch.object(supervisor.Service, 'stop'):
            supervisor.control_service(managed, 'browser', 'stop')
        supervisor.control_service(managed, 'browser-secondary', 'start')
        self.assertTrue(all(managed[name].enabled for name in supervisor.BROWSER_SERVICES[:4]))
        self.assertTrue(managed['browser-secondary'].enabled)
        self.assertFalse(managed['browser-primary'].enabled)
        self.assertFalse(managed['desktop-http'].enabled)

    def test_concurrent_service_requests_are_acknowledged_after_status(self):
        managed = supervisor.services({})
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(supervisor.shutil, 'which', return_value='/usr/bin/fixture'):
            run = Path(temporary)
            with patch.object(supervisor, 'RUN', run):
                supervisor.write_snapshot(managed)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    requests = [pool.submit(control.control, 'start', 'desktop', run, 3),
                                pool.submit(control.control, 'stop', 'files', run, 3)]
                    deadline = time.monotonic() + 3
                    while not all(request.done() for request in requests) and time.monotonic() < deadline:
                        replies = supervisor.read_requests(managed)
                        for service in managed.values():
                            if service.enabled and service.process is None:
                                service.process = running_process()
                        supervisor.write_snapshot(managed, replies)
                        time.sleep(.01)
                    for request in requests:
                        request.result(timeout=1)
                status = json.loads((run / 'status.json').read_text())
                self.assertEqual(status['control_protocol'], 3)
                controls = {row['name']: row['state'] for row in status['service_controls']}
                self.assertEqual(controls['desktop'], 'running')
                self.assertEqual(controls['files'], 'stopped')
                self.assertFalse(list((run / 'control').glob('*.json')))
                self.assertFalse(list((run / 'control-results').glob('*.json')))

    def test_failed_group_request_returns_actionable_error_to_cli(self):
        managed = supervisor.services({})
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(supervisor.shutil, 'which', return_value=None):
            run = Path(temporary)
            with patch.object(supervisor, 'RUN', run):
                supervisor.write_snapshot(managed)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(control.control, 'start', 'desktop', run, 3)
                    deadline = time.monotonic() + 3
                    while not result.done() and time.monotonic() < deadline:
                        replies = supervisor.read_requests(managed)
                        supervisor.write_snapshot(managed, replies)
                        time.sleep(.01)
                    with self.assertRaisesRegex(RuntimeError, 'Build container images'):
                        result.result(timeout=1)

    def test_legacy_supervisor_refuses_new_group_instead_of_stopping_browser_xvfb(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            (run / 'status.json').write_text(json.dumps({'services': {'desktop': {'state': 'running'}}}))
            with self.assertRaisesRegex(RuntimeError, 'updated supervisor'):
                control.control('stop', 'desktop', run)
            self.assertFalse((run / 'control.json').exists())

    def test_invalid_action_does_not_mutate_a_service(self):
        managed = supervisor.services({})
        with self.assertRaises(ValueError):
            supervisor.control_service(managed, 'files', 'invalid')
        self.assertTrue(managed['files'].enabled)


class DesktopBrowserTests(unittest.TestCase):
    def test_browser_reuses_mandatory_caido_arguments_and_separate_profile(self):
        arguments = desktop.browser_arguments(['https://example.test/path?q=value'])
        self.assertTrue(set(desktop.PROXY_ARGUMENTS).issubset(arguments))
        self.assertIn('--user-data-dir=/root/.config/acf/chromium-desktop', arguments)
        self.assertEqual(arguments[-1], 'https://example.test/path?q=value')
        self.assertFalse(any('ignore-certificate-errors' in argument for argument in arguments))

    def test_browser_trust_failure_prevents_launch(self):
        with patch.object(desktop, 'ensure_caido_trust', side_effect=RuntimeError('Caido unavailable')), \
                patch.object(desktop.os, 'execvpe') as execute:
            with self.assertRaises(RuntimeError):
                desktop.launch_browser()
        execute.assert_not_called()

    def test_browser_uses_the_trusted_root_nss_location_outside_xfce_settings(self):
        with patch.object(desktop, 'ensure_caido_trust') as trust, \
                patch.object(desktop.os, 'execvpe') as execute, \
                patch.dict(os.environ, {'XDG_DATA_HOME': '/root/.local/share/acf/desktop'}):
            desktop.launch_browser()
        trust.assert_called_once_with()
        self.assertEqual(execute.call_args.args[2]['XDG_DATA_HOME'], '/root/.local/share')
        self.assertEqual(execute.call_args.args[2]['DISPLAY'], ':2')

    def test_desktop_settings_are_isolated_and_custom_helpers_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def redirect(value):
                return root if value == '/root' else Path(value)
            with patch.object(desktop, 'Path', side_effect=redirect), \
                    patch.dict(os.environ, {'DBUS_SESSION_BUS_ADDRESS': 'old', 'SESSION_MANAGER': 'old'}):
                env = desktop.desktop_environment()
                helpers = Path(env['XDG_CONFIG_HOME']) / 'xfce4/helpers.rc'
                self.assertIn('WebBrowser=acf-browser', helpers.read_text())
                helpers.write_text('custom helper\n')
                desktop.desktop_environment()
                self.assertEqual(helpers.read_text(), 'custom helper\n')
            self.assertEqual(env['DISPLAY'], ':2')
            self.assertNotIn('DBUS_SESSION_BUS_ADDRESS', env)
            self.assertNotIn('SESSION_MANAGER', env)


class DesktopNoVNCTests(unittest.TestCase):
    def test_separate_desktop_page_preserves_browser_toolbar_and_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser, desktop_root = root / 'browser', root / 'desktop'
            browser.mkdir()
            (browser / 'vnc.html').write_text('<html><head><title>noVNC</title></head><body>existing noVNC</body></html>')
            (browser / 'vnc_auto.html').symlink_to('vnc.html')
            (browser / 'app').mkdir()
            (browser / 'app/ui.js').write_text('noVNC assets')
            novnc.install(browser, desktop_root)
            novnc.install(browser, desktop_root)
            self.assertIn('acf-profiles.js', (browser / 'vnc.html').read_text())
            self.assertNotIn('acf-profiles', (desktop_root / 'vnc.html').read_text())
            self.assertIn('existing noVNC', (desktop_root / 'vnc.html').read_text())
            self.assertEqual((desktop_root / 'app/ui.js').read_text(), 'noVNC assets')
            self.assertIn('ACF Desktop', (desktop_root / 'index.html').read_text())
            self.assertFalse((desktop_root / 'acf-profiles.js').exists())


if __name__ == '__main__':
    unittest.main()
