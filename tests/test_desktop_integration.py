"""Opt-in desktop/service verification using disposable containers without egress.

Build the current acf:local image first (or set ACF_DESKTOP_TEST_IMAGE), then run:
ACF_DESKTOP_INTEGRATION=1 PYTHONPATH=src python3 -m unittest discover -s tests -p test_desktop_integration.py -v

Uses private configuration and credentials-free test state. The only fake Caido
endpoint serves a disposable CA to local Chromium and rejects proxy requests.
"""
import json
import contextlib
import os
from pathlib import Path
import tempfile
import time
import unittest
import urllib.request

from acf.config import Config
from acf.manager import Manager
from test_integration import NOVNC_HANDSHAKE


DESKTOP = ('desktop-display', 'desktop-session', 'desktop-vnc', 'desktop-http')
BROWSER = ('desktop', 'window-manager', 'vnc', 'novnc', 'browser-primary', 'browser-secondary')
CA_STUB = r'''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess

directory = Path('/tmp/acf-desktop-integration-ca')
directory.mkdir(mode=0o700, exist_ok=True)
certificate = directory / 'certificate.pem'
if not certificate.exists():
    subprocess.run(['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                    '-nodes', '-days', '1', '-subj', '/CN=ACF Desktop Integration CA',
                    '-addext', 'basicConstraints=critical,CA:TRUE',
                    '-keyout', str(directory / 'key.pem'), '-out', str(certificate)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
payload = certificate.read_bytes()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/ca.crt':
            self.send_error(403, 'This test endpoint never proxies requests')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-pem-file')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def do_CONNECT(self):
        self.send_error(403, 'This test endpoint never proxies requests')
    def log_message(self, *args):
        pass
ThreadingHTTPServer(('127.0.0.1', 8080), Handler).serve_forever()
'''


@unittest.skipUnless(os.environ.get('ACF_DESKTOP_INTEGRATION') == '1',
                     'set ACF_DESKTOP_INTEGRATION=1 for isolated desktop Docker tests')
class DesktopIntegrationTests(unittest.TestCase):
    def status(self, manager, project):
        result = manager.exec(project, ['/usr/bin/python3', '-c',
                              "from pathlib import Path; print(Path('/run/acf/status.json').read_text())"],
                              interactive=False)
        return json.loads(result.stdout)

    def wait_services(self, manager, project, names, expected, timeout=60):
        deadline = time.monotonic() + timeout
        states = {}
        while time.monotonic() < deadline:
            status = self.status(manager, project)
            states = {name: status.get('services', {}).get(name, {}).get('state') for name in names}
            if all(state == expected for state in states.values()):
                return status
            time.sleep(.5)
        self.fail(f'Services did not become {expected}: {states}')

    def assert_no_listener(self, manager, project, port):
        probe = "import socket,sys; s=socket.socket(); s.settimeout(1); assert s.connect_ex(('127.0.0.1',int(sys.argv[1]))) != 0"
        manager.exec(project, ['/usr/bin/python3', '-c', probe, str(port)], interactive=False)

    def wait_http(self, url, timeout=30):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + timeout
        while True:
            try:
                with opener.open(url, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    return response.read()
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.5)

    def token_desktop(self, manager, project, container, port):
        """The framebuffer is reachable with this boot's token and refused without it."""
        token = manager.state(project)['access_token']
        deadline = time.monotonic() + 30
        while True:
            result = manager.docker.run('exec', '--env',
                f'ACF_TEST_NOVNC_URL=ws://127.0.0.1:{port}/websockify?token={token}',
                container, 'node', '-e', NOVNC_HANDSHAKE, check=False, timeout=15)
            if result.returncode == 0:
                self.assertIn('noVNC desktop connected', result.stdout)
                break
            if time.monotonic() >= deadline:
                self.fail(result.stderr or 'noVNC did not expose a usable framebuffer')
            time.sleep(.5)
        refused = manager.docker.run('exec', '--env', f'ACF_TEST_NOVNC_URL=ws://127.0.0.1:{port}/websockify',
                                     container, 'node', '-e', NOVNC_HANDSHAKE, check=False, timeout=15)
        self.assertNotEqual(refused.returncode, 0, 'noVNC accepted a connection without the access token')

    def start_fake_ca(self, manager, project, container):
        manager.docker.run('exec', '-d', container, '/usr/bin/python3', '-c', CA_STUB)
        # Read only this local fixture's certificate; never send browser traffic
        # or real provider credentials to the stub.
        probe = "import urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); assert o.open('http://127.0.0.1:8080/ca.crt',timeout=2).status == 200"
        deadline = time.monotonic() + 15
        while True:
            result = manager.docker.run('exec', container, '/usr/bin/python3', '-c', probe,
                                        check=False, timeout=5)
            if result.returncode == 0:
                return
            if time.monotonic() >= deadline:
                self.fail('The isolated certificate fixture did not become ready')
            time.sleep(.2)

    def wait_xfce(self, manager, project):
        probe = """from pathlib import Path
import subprocess
expected = {'xfce4-session', 'xfce4-panel', 'xfwm4', 'xfdesktop'}
names = set()
for process in Path('/proc').iterdir():
    if process.name.isdigit():
        try:
            names.add((process / 'comm').read_text().strip())
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
assert expected <= names
result = subprocess.run(['xprop', '-display', ':2', '-root', '_NET_SUPPORTING_WM_CHECK'], capture_output=True, text=True, check=True)
assert 'window id #' in result.stdout
"""
        deadline = time.monotonic() + 45
        while True:
            result = manager.docker.run('exec', manager.names(project)[0], '/usr/bin/python3', '-c',
                                        probe, check=False, timeout=5)
            if result.returncode == 0:
                return
            if time.monotonic() >= deadline:
                self.fail('XFCE session, panel, window manager, and desktop did not become ready')
            time.sleep(.5)

    @staticmethod
    def pids(status, names):
        return {name: status['services'][name]['pid'] for name in names}

    def test_desktop_defaults_controls_independence_and_autostart(self):
        with tempfile.TemporaryDirectory(prefix='acf-desktop-integration-') as temporary:
            root = Path(temporary) / 'checkout'
            root.mkdir()
            config_path = root / 'config.toml'
            image = os.environ.get('ACF_DESKTOP_TEST_IMAGE', 'acf:local')
            config_path.write_text(f'''[environment]
image = {json.dumps(image)}
cpus = 2
memory = "3g"
shm_size = "512m"
[network]
mode = "restricted"
bind = "127.0.0.1"
advertise_host = "127.0.0.1"
block = ["0.0.0.0/0"]
[ports]
desktop = 0
[integrations]
enabled = []
''')
            manager = Manager(Config(root=root, home=Path(temporary) / 'state'))
            manager.docker.ensure(autostart=False)
            if manager.docker_host() is not None:
                self.skipTest('Desktop integration requires a local Docker daemon for loopback HTTP checks')
            self.assertIsNotNone(manager.docker.inspect('image', image),
                                 'Build the current test image before running this test')
            self.assertIsNotNone(manager.docker.inspect('image', manager.guard_image),
                                 'Build the ACF guard image before running this test')
            manager.config.initialize()
            defaults_before = config_path.read_bytes()
            global_before = manager.config.path.read_bytes()
            project = 'desktop-test'
            container, guard, network = manager.names(project)
            try:
                manager.create(project)
                self.start_fake_ca(manager, project, container)
                initial = self.wait_services(manager, project, DESKTOP, 'stopped')
                self.assertTrue(all(initial['services'][name]['enabled'] is False for name in DESKTOP))
                self.assert_no_listener(manager, project, 6082)
                self.assert_no_listener(manager, project, 5902)
                self.assertFalse(manager.config.project(project)['services']['desktop'])
                info = manager.details(project)
                ports = manager.owned('container', guard)['NetworkSettings']['Ports']
                self.assertTrue(ports['6082/tcp'])
                self.assertTrue(all(binding['HostIp'] == '127.0.0.1'
                                    for bindings in ports.values() for binding in bindings or []))
                self.assertNotEqual(info['ports']['desktop'], info['ports']['novnc'])
                token = manager.state(project)['access_token']
                desktop_url = f"http://127.0.0.1:{info['ports']['desktop']}/"
                browser_url = f"http://127.0.0.1:{info['ports']['novnc']}/"
                files_url = f"http://127.0.0.1:{info['ports']['files']}/{token}/"
                self.assertEqual(next(item['address'] for item in info['service_items']
                                      if item['name'] == 'desktop-http'),
                                 desktop_url + 'vnc.html?autoconnect=1&resize=remote&path=websockify%3Ftoken%3D' + token)
                before = self.wait_services(manager, project, BROWSER, 'running')
                browser_pids = self.pids(before, BROWSER)
                self.wait_http(browser_url)
                self.token_desktop(manager, project, container, 6080)

                manager.service(project, 'start', 'desktop')
                started = self.wait_services(manager, project, DESKTOP, 'running')
                self.wait_xfce(manager, project)
                self.wait_http(desktop_url)
                page = self.wait_http(desktop_url + 'vnc.html')
                self.assertNotIn(b'acf-profiles.js', page)
                self.token_desktop(manager, project, container, 6082)
                self.assertEqual(self.pids(started, BROWSER), browser_pids)
                hold_seconds = min(300, max(0, int(os.environ.get('ACF_DESKTOP_TEST_HOLD_SECONDS', '0'))))
                if hold_seconds:
                    print('\nDesktop fixture ready for visual inspection: ' + json.dumps({
                        'container': container, 'desktop_url': desktop_url,
                        'browser_url': browser_url, 'hold_seconds': hold_seconds}), flush=True)
                    time.sleep(hold_seconds)

                manager.service(project, 'stop', 'desktop')
                stopped = self.wait_services(manager, project, DESKTOP, 'stopped')
                self.assert_no_listener(manager, project, 6082)
                self.assertEqual(self.pids(stopped, BROWSER), browser_pids)
                self.wait_http(browser_url)
                self.token_desktop(manager, project, container, 6080)

                manager.service(project, 'start', 'desktop')
                desktop_pids = self.pids(self.wait_services(manager, project, DESKTOP, 'running'), DESKTOP)
                manager.service(project, 'stop', 'browser')
                stopped = self.wait_services(manager, project, BROWSER, 'stopped')
                self.assert_no_listener(manager, project, 6080)
                self.assertEqual(self.pids(stopped, DESKTOP), desktop_pids)
                self.wait_http(desktop_url)
                self.token_desktop(manager, project, container, 6082)
                manager.service(project, 'start', 'browser')
                restarted = self.wait_services(manager, project, BROWSER, 'running')
                self.assertEqual(self.pids(restarted, DESKTOP), desktop_pids)
                self.wait_http(browser_url)
                self.token_desktop(manager, project, container, 6080)

                manager.service(project, 'stop', 'files')
                self.wait_services(manager, project, ('files',), 'stopped')
                self.assert_no_listener(manager, project, 8081)
                manager.service(project, 'start', 'files')
                self.wait_services(manager, project, ('files',), 'running')
                self.wait_http(files_url)

                # A manual start is transient; the false creation default is
                # restored on boot without changing either defaults file.
                manager.stop(project)
                manager.start(project)
                self.start_fake_ca(manager, project, container)
                self.wait_services(manager, project, DESKTOP, 'stopped')
                self.assert_no_listener(manager, project, 6082)
                desired = manager.config.project(project)
                desired['services']['desktop'] = True
                manager.config.save_project_settings(project, desired)
                manager.sync(project)
                self.wait_services(manager, project, DESKTOP, 'stopped')
                manager.stop(project)
                manager.start(project)
                self.start_fake_ca(manager, project, container)
                self.wait_services(manager, project, DESKTOP, 'running')
                self.wait_xfce(manager, project)
                info = manager.details(project)
                self.wait_http(f"http://127.0.0.1:{info['ports']['desktop']}/")
                self.token_desktop(manager, project, container, 6082)
                self.assertTrue(manager.config.project(project)['services']['desktop'])
                self.assertFalse(manager.config.reload()['services']['desktop'])
                self.assertEqual(config_path.read_bytes(), defaults_before)
                self.assertEqual(manager.config.path.read_bytes(), global_before)
                self.assertEqual(list((manager.config.home / 'auth').iterdir()), [])
            except Exception:
                # Diagnostics come only from this credential-free fixture.
                with contextlib.suppress(Exception):
                    guard_info = manager.owned('container', guard) or {}
                    print('\nDesktop fixture port diagnostics: ' + json.dumps({
                        'bindings': guard_info.get('HostConfig', {}).get('PortBindings'),
                        'published': guard_info.get('NetworkSettings', {}).get('Ports')}), flush=True)
                    result = manager.docker.run('exec', container, '/usr/bin/tail', '-n', '15',
                        '/var/log/acf/desktop-display.log', '/var/log/acf/desktop-session.log',
                        '/var/log/acf/desktop-http.log', '/var/log/acf/browser-primary.log',
                        check=False, max_output=16384)
                    print(result.stdout, flush=True)
                raise
            finally:
                # Ownership checks and the private ACF instance restrict cleanup
                # to this test's containers, network, volumes, and managed state.
                manager.remove(project, purge=True)
            self.assertIsNone(manager.owned('container', container))
            self.assertIsNone(manager.owned('container', guard))
            self.assertIsNone(manager.owned('network', network))


if __name__ == '__main__':
    unittest.main()
