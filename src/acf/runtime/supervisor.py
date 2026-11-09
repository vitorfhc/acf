"""Container service supervision with per-service logs and visible failures."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

RUN = Path('/run/acf')
LOG = Path('/var/log/acf')
SETTINGS = Path('/opt/acf/service-settings.json')
# 3: published noVNC endpoints require the host-issued access token.
CONTROL_PROTOCOL = 3
BROWSER_SERVICES = ('desktop', 'window-manager', 'vnc', 'novnc',
                    'browser-primary', 'browser-secondary')
DESKTOP_SERVICES = ('desktop-display', 'desktop-session', 'desktop-vnc', 'desktop-http')
SERVICE_GROUPS = {'browser': BROWSER_SERVICES, 'desktop': DESKTOP_SERVICES}
SERVICE_ENDPOINTS = {
    'files': [{'port': 8081, 'protocol': 'http', 'scope': 'network'}],
    'caido': [{'port': 8080, 'protocol': 'http', 'scope': 'network'}],
    'vnc': [{'port': 5901, 'protocol': 'tcp', 'scope': 'local'}],
    'novnc': [{'port': 6080, 'protocol': 'http', 'scope': 'network'}],
    'browser-primary': [{'port': 9222, 'protocol': 'http', 'scope': 'local'}],
    'browser-secondary': [{'port': 9224, 'protocol': 'http', 'scope': 'local'}],
    'desktop-vnc': [{'port': 5902, 'protocol': 'tcp', 'scope': 'local'}],
    'desktop-http': [{'port': 6082, 'protocol': 'http', 'scope': 'network', 'path': '/'}],
}


@dataclass
class Service:
    name: str
    command: list
    process: object = None
    enabled: bool = True
    restarts: int = 0
    retry_at: float = 0
    exit_code: object = None

    def start(self):
        with (LOG / f'{self.name}.log').open('ab', buffering=0) as log:
            self.process = subprocess.Popen(self.command, stdout=log, stderr=log, start_new_session=True)
        self.restarts += 1
        self.exit_code = None

    def stop(self):
        if self.process and self.process.poll() is None:
            if self.name in ('browser-primary', 'browser-secondary'):
                port = '9222' if self.name == 'browser-primary' else '9224'
                try:
                    subprocess.run(['node', '/opt/acf/close_browser.js', port],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                    self.process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if self.process.poll() is not None:
                self.process = None
                return
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
            except ProcessLookupError:
                pass
        self.process = None

    def state(self):
        running = self.process is not None and self.process.poll() is None
        return dict(state='running' if running else 'stopped' if not self.enabled else 'failed',
                    pid=self.process.pid if running else None, restarts=max(0, self.restarts - 1),
                    enabled=self.enabled,
                    exit_code=self.exit_code, log=f'/var/log/acf/{self.name}.log',
                    endpoints=SERVICE_ENDPOINTS.get(self.name, []))


def service_settings():
    try:
        settings = json.loads(SETTINGS.read_text())
    except FileNotFoundError:
        return {'desktop': False}
    except (ValueError, UnicodeError):
        raise ValueError('Invalid service settings. Sync this environment again.') from None
    if not isinstance(settings, dict) or type(settings.get('desktop', False)) is not bool:
        raise ValueError('Desktop autostart must be true or false.')
    return {'desktop': settings.get('desktop', False)}


def vnc_proxy(webroot, port, tokens):
    """Serve noVNC publicly, but connect only WebSockets that carry this boot's token.

    TokenFile rereads its source per connection, so Sync can deliver the token
    after boot; a missing file refuses every connection.
    """
    return ['websockify', '--web', webroot, '--token-plugin', 'TokenFile',
            '--token-source', str(tokens), f'0.0.0.0:{port}']


def services(settings=None):
    settings = service_settings() if settings is None else settings
    enabled = os.environ.get('ACF_INTEGRATIONS', 'codex,claude,openrouter,glm,caido,oob').split(',')
    result = [Service('files', ['python3', '/opt/acf/files.py']),
              Service('caido', ['/opt/acf/bin/acf-caido'], enabled='caido' in enabled),
              Service('desktop', ['Xvfb', ':1', '-screen', '0', '1440x900x24', '-nolisten', 'tcp', '-ac']),
              Service('window-manager', ['/opt/acf/bin/acf-display', ':1', 'openbox', '--config-file', '/opt/acf/openbox.xml']),
              Service('vnc', ['/opt/acf/bin/acf-display', ':1', 'x11vnc', '-display', ':1', '-localhost', '-rfbport', '5901', '-nopw', '-forever', '-shared', '-noxdamage']),
              Service('novnc', vnc_proxy('/usr/share/novnc', 6080, RUN / 'novnc-token'))]
    for name in ('primary', 'secondary'):
        result.append(Service(f'browser-{name}', ['/opt/acf/bin/acf-display', ':1', '/opt/acf/bin/acf-browser', name, 'launch']))
    desktop_enabled = settings.get('desktop', False)
    result.extend([
        Service('desktop-display', ['Xvfb', ':2', '-screen', '0', '1440x900x24', '-nolisten', 'tcp', '-ac'], enabled=desktop_enabled),
        Service('desktop-session', ['/opt/acf/bin/acf-display', ':2', 'python3', '/opt/acf/desktop.py', 'session'], enabled=desktop_enabled),
        Service('desktop-vnc', ['/opt/acf/bin/acf-display', ':2', 'x11vnc', '-display', ':2', '-localhost', '-rfbport', '5902', '-nopw', '-forever', '-shared', '-noxdamage'], enabled=desktop_enabled),
        Service('desktop-http', vnc_proxy('/usr/share/acf-desktop', 6082, RUN / 'desktop-token'), enabled=desktop_enabled),
    ])
    return {service.name: service for service in result}


def group_state(managed, names):
    members = [managed[name] for name in names]
    states = [service.state()['state'] for service in members]
    if all(state == 'running' for state in states):
        return 'running'
    if not any(service.enabled for service in members):
        return 'stopped'
    return 'failed' if any(service.exit_code is not None for service in members) else 'starting'


def snapshot(managed):
    controls = []
    for name, label in (('caido', 'Caido'), ('files', 'Files'), ('browser', 'Browser'), ('desktop', 'Desktop')):
        names = SERVICE_GROUPS.get(name, (name,))
        controls.append({'name': name, 'label': label, 'state': group_state(managed, names)})
    return {'control_protocol': CONTROL_PROTOCOL, 'service_controls': controls,
            'services': {name: service.state() for name, service in managed.items()}}


def control_service(managed, name, action):
    if action not in ('start', 'stop', 'restart'):
        raise ValueError('Unknown service action.')
    # Public Desktop refers to the optional GUI; the legacy browser display
    # retains its internal status name to keep existing inventory compatible.
    names = SERVICE_GROUPS.get(name, (name,))
    if any(member not in managed for member in names):
        raise ValueError('Unknown service.')
    if action != 'stop' and (name == 'desktop' or name in DESKTOP_SERVICES):
        required = ('xfce4-session', 'xfce4-panel', 'xfwm4', 'xfdesktop', 'thunar', 'xfce4-terminal')
        if any(shutil.which(command) is None for command in required):
            raise ValueError('Desktop packages are missing. Build container images, then Apply settings to this environment.')
    if action in ('stop', 'restart'):
        for member in reversed(names):
            managed[member].stop()
    for member in names:
        managed[member].enabled = action != 'stop'
        managed[member].retry_at = 0
        managed[member].exit_code = None
    # A browser profile started from the HTTP toolbar needs the display again
    # if its entire group was previously stopped from the dashboard.
    if action != 'stop' and name in ('browser-primary', 'browser-secondary'):
        for member in BROWSER_SERVICES[:4]:
            managed[member].enabled = True
            managed[member].retry_at = 0


def read_requests(managed):
    """Apply each request once; acknowledge only after the next status write."""
    replies = []
    queue = RUN / 'control'
    queue.mkdir(mode=0o700, exist_ok=True)
    results = RUN / 'control-results'
    results.mkdir(mode=0o700, exist_ok=True)
    # Accept the previous single-file protocol while an old shell command exits.
    paths = [RUN / 'control.json', *sorted(queue.glob('*.json'))[:32]]
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        request_id = None
        try:
            if path.stat().st_size > 16384:
                raise ValueError('Service request is too large.')
            request = json.loads(path.read_text())
            if not isinstance(request, dict):
                raise ValueError('Invalid service request.')
            request_id = request.get('id')
            if request_id is not None and (not isinstance(request_id, str) or len(request_id) != 32
                                          or any(value not in '0123456789abcdef' for value in request_id)):
                request_id = None
                raise ValueError('Invalid service request identifier.')
            if not isinstance(request.get('name'), str) or not isinstance(request.get('action'), str):
                raise ValueError('Invalid service request.')
            control_service(managed, request['name'], request['action'])
            reply = {'ok': True}
        except (ValueError, KeyError, OSError) as exc:
            reply = {'ok': False, 'error': str(exc)}
            if request_id is None:
                print(f'Invalid service request: {type(exc).__name__}', flush=True)
        finally:
            path.unlink(missing_ok=True)
        if request_id is not None:
            replies.append((results / (request_id + '.json'), reply))
    # Clients normally remove replies; clean up abandoned ones after five minutes.
    for path in results.glob('*.json'):
        try:
            if path.stat().st_mtime < time.time() - 300:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass  # The client consumed this acknowledgement concurrently.
    return replies


def write_snapshot(managed, replies=()):
    temporary = RUN / 'status.tmp'
    temporary.write_text(json.dumps(snapshot(managed)))
    temporary.replace(RUN / 'status.json')
    for path, reply in replies:
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(reply))
        temporary.replace(path)


def main():
    RUN.mkdir(parents=True, exist_ok=True)
    LOG.mkdir(parents=True, exist_ok=True)
    for name in ('recon', 'reports', 'artifacts', 'scope'):
        directory = Path('/workspace') / name
        directory.mkdir(exist_ok=True)
        os.chown(directory, 1000, 1000)
    # Leave existing project content and ownership untouched.
    os.chmod('/workspace', 0o1777)
    Path('/tmp/.X11-unix').mkdir(mode=0o1777, exist_ok=True)
    os.chmod('/tmp/.X11-unix', 0o1777)
    # Container crashes can leave display locks in its writable layer. No old
    # process survives a fresh container boot, so these owned sockets are stale.
    for display in (1, 2):
        Path(f'/tmp/.X{display}-lock').unlink(missing_ok=True)
        Path(f'/tmp/.X11-unix/X{display}').unlink(missing_ok=True)
    for path in ('/root/.config/acf/chromium-primary', '/root/.config/acf/chromium-secondary',
                 '/root/.config/acf/chromium-desktop'):
        Path(path).mkdir(parents=True, exist_ok=True)
        for filename in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
            (Path(path) / filename).unlink(missing_ok=True)
    # The host attaches to this tmux server. Closing an SSH connection leaves it alive.
    subprocess.run(['tmux', 'new-session', '-d', '-s', 'main', '-c', '/workspace'], check=True)
    managed = services()
    stopping = False

    def stop_signal(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    while not stopping:
        replies = read_requests(managed)
        for service in managed.values():
            if service.process and service.process.poll() is not None:
                service.exit_code = service.process.returncode
                service.process = None
                service.retry_at = time.monotonic() + min(60, 2 ** min(service.restarts, 6))
                print(f'{service.name} exited ({service.exit_code}); see /var/log/acf/{service.name}.log', flush=True)
            if service.enabled and service.process is None and time.monotonic() >= service.retry_at:
                try:
                    service.start()
                except OSError as exc:
                    service.exit_code = str(exc)
                    service.retry_at = time.monotonic() + 30
        write_snapshot(managed, replies)
        time.sleep(0.5)
    # Stop Chromium first so profile databases flush before their displays stop.
    for service in reversed(list(managed.values())):
        service.stop()
    subprocess.run(['tmux', 'kill-server'], check=False)


if __name__ == '__main__':
    main()
