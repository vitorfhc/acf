"""Single-port browser switching without merging or replacing profile data."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

RUNTIME = Path(__file__).resolve().parents[1] / 'src/acf/runtime'
sys.path.insert(0, str(RUNTIME))


def load(name):
    module_name = 'acf_browser_test_' + name
    spec = importlib.util.spec_from_file_location(module_name, RUNTIME / f'{name}.py')
    result = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = result
    spec.loader.exec_module(result)
    return result


browser = load('browser')
supervisor = load('supervisor')
installer = load('install_novnc')


class BrowserTopologyTests(unittest.TestCase):
    def test_one_display_and_vnc_listener_serve_both_profiles(self):
        services = supervisor.services()
        self.assertEqual(set(services), {'files', 'caido', 'desktop', 'window-manager',
                                         'vnc', 'novnc', 'browser-primary', 'browser-secondary',
                                         'desktop-display', 'desktop-session', 'desktop-vnc', 'desktop-http'})
        self.assertIn(':1', services['desktop'].command)
        self.assertIn('0.0.0.0:6080', services['novnc'].command)
        # The VNC target comes from the token file the host writes during Sync.
        command = services['novnc'].command
        self.assertEqual(command[command.index('--token-plugin') + 1], 'TokenFile')
        self.assertEqual(command[command.index('--token-source') + 1], '/run/acf/novnc-token')
        self.assertNotIn('127.0.0.1:5901', command)
        self.assertIn('-localhost', services['vnc'].command)
        self.assertIn('-nopw', services['vnc'].command)
        self.assertNotIn('-rfbauth', services['vnc'].command)
        for name in ('primary', 'secondary'):
            self.assertIn(':1', services['browser-' + name].command)
            self.assertIn(name, services['browser-' + name].command)
        arguments = [arg for name, service in services.items() if service.enabled for arg in service.command]
        self.assertNotIn(':2', arguments)
        self.assertFalse(any('6081' in arg or '5902' in arg for arg in arguments))

    def test_supervisor_starts_and_shuts_down_without_endpoint_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'workspace').mkdir()

            def sandbox_path(value):
                return root / str(value).lstrip('/')

            def stop_on_registration(signum, handler):
                if signum == supervisor.signal.SIGTERM:
                    handler(signum, None)

            with patch.dict(supervisor.os.environ, {}, clear=True), \
                    patch.object(supervisor, 'RUN', root / 'run/acf'), \
                    patch.object(supervisor, 'LOG', root / 'var/log/acf'), \
                    patch.object(supervisor, 'Path', side_effect=sandbox_path), \
                    patch.object(supervisor.os, 'chown'), patch.object(supervisor.os, 'chmod'), \
                    patch.object(supervisor.signal, 'signal', side_effect=stop_on_registration), \
                    patch.object(supervisor.subprocess, 'run') as run:
                (root / 'tmp').mkdir()
                supervisor.main()
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ['tmux', 'new-session', '-d', '-s', 'main', '-c', '/workspace'],
                ['tmux', 'kill-server'],
            ])
            self.assertFalse((root / 'run/acf/vnc.pass').exists())

    def test_profiles_keep_existing_paths_and_distinct_classes_and_cdp(self):
        for name, port, window_class in (('primary', 9222, 'ACFBrowserPrimary'),
                                         ('secondary', 9224, 'ACFBrowserSecondary')):
            with self.subTest(profile=name):
                arguments = browser.launch_arguments(name)
                self.assertIn('--user-data-dir=/root/.config/acf/chromium-' + name, arguments)
                self.assertIn('--class=' + window_class, arguments)
                self.assertIn('--remote-debugging-port=' + str(port), arguments)
                self.assertIn('--remote-debugging-address=127.0.0.1', arguments)

    def test_openbox_shortcuts_start_and_focus_correct_desktop(self):
        xml = ET.parse(RUNTIME / 'openbox.xml')
        ns = {'o': 'http://openbox.org/3.4/rc'}
        self.assertEqual(xml.findtext('o:desktops/o:number', namespaces=ns), '2')
        for index, name in enumerate(('primary', 'secondary'), 1):
            rule = xml.find(f'.//o:application[@class="{browser.PROFILES[name]["class"]}"]', ns)
            self.assertIsNotNone(rule)
            self.assertEqual(rule.findtext('o:desktop', namespaces=ns), str(index))
            command = xml.findtext(f'.//o:keybind[@key="C-A-{index}"]/o:action/o:command', namespaces=ns)
            self.assertEqual(command, f'/opt/acf/bin/acf-browser {name} focus')

    def test_metadata_exposes_only_shared_novnc_and_local_cdp(self):
        services = supervisor.services()
        endpoint = services['novnc'].state()['endpoints']
        self.assertEqual(endpoint, [{'port': 6080, 'protocol': 'http', 'scope': 'network'}])
        for name in ('primary', 'secondary'):
            self.assertEqual(services['browser-' + name].state()['endpoints'][0]['scope'], 'local')


class BrowserFocusTests(unittest.TestCase):
    def test_focus_requests_stopped_profile_start_then_switches_and_raises(self):
        calls = []

        def run(arguments, **kwargs):
            calls.append(arguments)
            output = '410\n' if arguments[:2] == ['xdotool', 'search'] else ''
            return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr='')

        with tempfile.TemporaryDirectory() as directory, patch.object(browser, 'RUN', Path(directory)), \
                patch.object(browser, 'run', side_effect=run):
            browser.focus('secondary')
        self.assertEqual(calls[0], ['acf-services', 'start', 'browser-secondary'])
        self.assertEqual(calls[1], ['xdotool', 'set_desktop', '1'])
        self.assertEqual(calls[-1], ['xdotool', 'windowactivate', '--sync', '410'])
        self.assertIn('^ACFBrowserSecondary$', calls[2])

    def test_launch_waits_for_window_manager_before_creating_browser(self):
        ready = subprocess.CompletedProcess([], 0, stdout='_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x40020c', stderr='')
        with patch.object(browser, 'run', return_value=ready), \
                patch.object(browser, 'ensure_caido_trust') as trust, \
                patch.object(browser.os, 'execvpe', side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                browser.launch('primary')
        command, arguments, environment = execute.call_args.args
        self.assertEqual(command, 'chromium')
        self.assertIn('--class=ACFBrowserPrimary', arguments)
        self.assertEqual(environment['DISPLAY'], ':1')
        self.assertEqual(environment['HOME'], '/root')
        trust.assert_called_once_with()

    def test_caido_trust_failure_never_launches_a_direct_browser(self):
        with patch.object(browser, 'ensure_caido_trust', side_effect=RuntimeError('Caido unavailable')), \
                patch.object(browser.os, 'execvpe') as execute, patch.object(browser, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Caido unavailable'):
                browser.launch('primary')
            execute.assert_not_called()
            run.assert_not_called()

    def test_both_profiles_proxy_loopback_and_disable_direct_transports(self):
        for profile in browser.PROFILES:
            arguments = browser.launch_arguments(profile)
            self.assertIn('--proxy-server=http://127.0.0.1:8080', arguments)
            self.assertIn('--proxy-bypass-list=<-loopback>', arguments)
            self.assertIn('--disable-quic', arguments)
            self.assertIn('--force-webrtc-ip-handling-policy=disable_non_proxied_udp', arguments)
            self.assertIn('--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1', arguments)
            self.assertIn('--restore-last-session', arguments)
            self.assertFalse(any('ignore-certificate-errors' in argument for argument in arguments))
            self.assertFalse(any('direct://' in argument for argument in arguments))

    def test_sync_restarts_legacy_browser_once_and_keeps_current_or_stopped_profiles(self):
        for configured in (False, True):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                status_path = root / 'status.json'
                status = {'services': {'browser-primary': {'state': 'running', 'pid': 123},
                                       'browser-secondary': {'state': 'stopped', 'pid': None}}}
                status_path.write_text(json.dumps(status))
                proc = root / 'cmdline'
                arguments = ['/usr/lib/chromium/chromium', *(browser.PROXY_ARGUMENTS if configured else [])]
                proc.write_bytes('\0'.join(arguments).encode())

                def runtime_path(value):
                    return proc if str(value) == '/proc/123/cmdline' else Path(value)

                def restart(arguments, **kwargs):
                    status['services']['browser-primary']['pid'] = 124
                    status_path.write_text(json.dumps(status))

                with patch.object(browser, 'RUN', root), patch.object(browser, 'Path', side_effect=runtime_path), \
                        patch.object(browser, 'run', side_effect=restart) as run:
                    browser.refresh_proxy()
                if configured:
                    run.assert_not_called()
                else:
                    run.assert_called_once_with(['/opt/acf/bin/acf-services', 'restart', 'browser-primary'], check=True)

    def test_browser_stop_asks_chromium_to_flush_before_signalling(self):
        process = Mock()
        process.poll.side_effect = [None, 0]
        service = supervisor.Service('browser-secondary', [], process=process)
        with patch.object(supervisor.subprocess, 'run') as close, patch.object(supervisor.os, 'killpg') as signal:
            service.stop()
        self.assertEqual(close.call_args.args[0], ['node', '/opt/acf/close_browser.js', '9224'])
        signal.assert_not_called()
        self.assertIsNone(service.process)


class NoVNCInstallTests(unittest.TestCase):
    def test_toolbar_install_preserves_page_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'vnc.html').write_text('<html><head><title>original</title></head><body>existing noVNC</body></html>')
            installer.install(root)
            installer.install(root)
            page = (root / 'vnc.html').read_text()
            self.assertIn('existing noVNC', page)
            self.assertEqual(page.count('src="acf-profiles.js"'), 1)
            self.assertEqual(page.count('href="acf-profiles.css"'), 1)
            self.assertTrue((root / 'acf-profiles.js').is_file())
            self.assertIn('url=vnc.html', (root / 'index.html').read_text())

    def test_unexpected_upstream_page_fails_build_instead_of_omitting_toolbar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'vnc.html').write_text('unexpected upstream layout')
            with self.assertRaises(ValueError):
                installer.install(root)

    @unittest.skipUnless(shutil.which('node'), 'Node is required to exercise browser JavaScript')
    def test_toolbar_uses_current_connection_and_respects_disconnected_view_only(self):
        source = (RUNTIME / 'novnc/profiles.js').read_text().replace("import UI from './app/ui.js';", '')
        script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const UI = {connected:false, rfb:null};
const intervals = [];
const element = tag => ({tag, children:[], attrs:{}, listeners:{},
  append(child){this.children.push(child)}, setAttribute(k,v){this.attrs[k]=v},
  addEventListener(k,v){this.listeners[k]=v}});
const document = {createElement:element, body:{prepend(item){this.toolbar=item}}};
const context = {UI, document, setInterval:fn=>intervals.push(fn)};
vm.runInNewContext(SOURCE, context);
const buttons = document.body.toolbar.children.filter(node=>node.tag==='button');
assert.deepEqual(buttons.map(button=>button.textContent), ['Primary','Secondary']);
assert(buttons.every(button=>button.disabled));
const eventsA = [], eventsB = [];
const rfb = events=>({viewOnly:false, sendKey(...args){events.push(args)}, focus(){events.push(['focus'])}});
UI.connected=true; UI.rfb=rfb(eventsA); intervals[0]();
assert(buttons.every(button=>!button.disabled));
buttons[0].listeners.click();
assert.deepEqual(eventsA, [[0xffe3,'ControlLeft',true],[0xffe9,'AltLeft',true],
 [0x31,'Digit1',true],[0x31,'Digit1',false],[0xffe9,'AltLeft',false],
 [0xffe3,'ControlLeft',false],['focus']]);
UI.rfb=rfb(eventsB); buttons[1].listeners.click();
assert.equal(eventsA.length, 7); assert.equal(eventsB[2][0], 0x32);
assert.equal(eventsB[2][1], 'Digit2');
UI.rfb.viewOnly=true; intervals[0]();
assert(buttons.every(button=>button.disabled)); buttons[0].listeners.click();
assert.equal(eventsB.length, 7);
UI.rfb.viewOnly=false; UI.connected=false; intervals[0](); buttons[0].listeners.click();
assert(buttons.every(button=>button.disabled)); assert.equal(eventsB.length, 7);
'''.replace('SOURCE', json.dumps(source))
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
