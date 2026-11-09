"""Opt-in real Docker verification. Uses private test state and cleans its own volumes.

Run with Docker available; missing images build automatically on the first run:
ACF_INTEGRATION=1 PYTHONPATH=src python3 -m unittest discover -s tests -p test_integration.py -v
"""
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from acf.addresses import host_port
from acf.config import Config
from acf.manager import Manager


# Complete the noVNC WebSocket/RFB handshake. websockify requires the access token
# in the URL; the loopback VNC server behind it has no separate password.
NOVNC_HANDSHAKE = r'''
const socket = new WebSocket(process.env.ACF_TEST_NOVNC_URL || 'ws://127.0.0.1:6080/websockify', ['binary']);
socket.binaryType = 'arraybuffer';
let buffer = Buffer.alloc(0), stage = 0;
const timeout = setTimeout(() => { console.error('noVNC handshake timed out'); process.exit(1); }, 10000);
function take(size) { const value = buffer.subarray(0, size); buffer = buffer.subarray(size); return value; }
socket.onerror = () => { console.error('noVNC connection failed'); process.exit(1); };
socket.onclose = () => { if (stage !== 4) { console.error('noVNC closed before the desktop was ready'); process.exit(1); } };
socket.onmessage = event => {
  try {
    buffer = Buffer.concat([buffer, Buffer.from(event.data)]);
    while (true) {
      if (stage === 0) {
        if (buffer.length < 12) return;
        if (!take(12).toString().startsWith('RFB ')) throw Error('Invalid RFB server');
        socket.send(Buffer.from('RFB 003.008\n')); stage = 1;
      } else if (stage === 1) {
        if (buffer.length < 1 || buffer.length < 1 + buffer[0]) return;
        const count = take(1)[0], offered = take(count);
        if (count !== 1 || offered[0] !== 1) throw Error('VNC requires authentication');
        socket.send(Buffer.from([1])); stage = 2;
      } else if (stage === 2) {
        if (buffer.length < 4) return;
        if (take(4).readUInt32BE() !== 0) throw Error('VNC rejected unauthenticated client');
        socket.send(Buffer.from([1])); stage = 3;
      } else {
        if (buffer.length < 24 || buffer.length < 24 + buffer.readUInt32BE(20)) return;
        if (!buffer.readUInt16BE(0) || !buffer.readUInt16BE(2)) throw Error('Missing desktop framebuffer');
        stage = 4; clearTimeout(timeout); socket.onmessage = null; socket.close();
        console.log('noVNC desktop connected'); return;
      }
    }
  } catch (error) { console.error(error.message); process.exit(1); }
};
'''


@unittest.skipUnless(os.environ.get('ACF_INTEGRATION') == '1', 'set ACF_INTEGRATION=1 for real Docker tests')
class DockerIntegrationTests(unittest.TestCase):
    def test_isolation_services_sync_and_persistence(self):
        with tempfile.TemporaryDirectory(prefix='acf-integration-') as temporary:
            root = Path(temporary) / 'checkout'
            root.mkdir()
            (root / 'prompts').mkdir()
            prompt = root / 'prompts' / 'test.md'
            prompt.write_text('Project {{PROJECT_NAME}} lives at {{PROJECT_PATH}}.\n')
            (root / 'config.toml').write_text('''[environment]
cpus = 1
memory = "2g"
shm_size = "256m"
[ports]
extra = [
    { host = 0, container = 3000, name = "test-http", protocol = "auto" },
    { host = 0, container = 3333, name = "test-tcp", protocol = "tcp" },
]
[integrations]
enabled = []
''')
            custom_bin = root / 'bin'
            custom_bin.mkdir()
            custom_script = custom_bin / 'acf-custom-proof'
            custom_script.write_text("#!/bin/sh\nprintf '%s\\n' '{{PROJECT_NAME}}'\n")
            custom_script.chmod(0o644)
            (custom_bin / '.support/empty').mkdir(parents=True)
            custom_data = b'\x7fELF\0\xffcustom{{UNCHANGED}}'
            (custom_bin / '.support/payload').write_bytes(custom_data)
            defaults_before = (root / 'config.toml').read_bytes()
            manager = Manager(Config(root=root, home=Path(temporary) / 'state'))
            manager.docker.ensure(autostart=False)
            try:
                with self.subTest('create and guarded startup'):
                    self.assertEqual(manager.create('alpha'), [])
                    name, guard, network = manager.names('alpha')
                    obj = manager.owned('container', name)
                    self.assertFalse(obj['HostConfig']['Privileged'])
                    self.assertTrue(all(m['Type'] == 'volume' for m in obj['Mounts']))
                    self.assertIn('NET_RAW', {cap.removeprefix('CAP_') for cap in obj['HostConfig']['CapDrop']})
                    self.assertEqual(manager.doctor()[0]['issues'], [])
                    self.assertEqual(manager.exec('alpha', ['id', '-u'], interactive=False).stdout.strip(), '0')
                    self.assertEqual(manager.exec('alpha', ['sudo', '-u', 'agent', 'sudo', '-n', 'id', '-u'], interactive=False).stdout.strip(), '0')
                with self.subTest('checkout custom tools copy on create and update through sync'):
                    self.assertEqual(manager.exec('alpha', ['bash', '-lc', 'acf-custom-proof'], interactive=False).stdout, '{{PROJECT_NAME}}\n')
                    self.assertEqual(manager.exec('alpha', ['stat', '-c', '%a', '/usr/local/bin/acf-custom-proof'], interactive=False).stdout.strip(), '755')
                    manager.exec('alpha', ['test', '-d', '/usr/local/bin/.support/empty'], interactive=False)
                    check_payload = "from pathlib import Path; assert Path('/usr/local/bin/.support/payload').read_bytes().hex() == " + repr(custom_data.hex())
                    manager.exec('alpha', ['python3', '-c', check_payload], interactive=False)
                    custom_script.write_text("#!/bin/sh\nprintf '%s\\n' 'updated tool'\n")
                    manager.sync('alpha')
                    self.assertEqual(manager.exec('alpha', ['bash', '-lc', 'acf-custom-proof'], interactive=False).stdout, 'updated tool\n')
                    self.assertEqual(custom_script.stat().st_mode & 0o777, 0o644)
                with self.subTest('custom utility names do not run during sync or dashboard inspection'):
                    collisions = ('mkdir', 'cat', 'sh', 'python3')
                    for command in collisions:
                        (custom_bin / command).write_text('#!/bin/sh\nexit 99\n')
                    try:
                        manager.sync('alpha')
                        manager.sync('alpha')
                        report = manager.details('alpha')
                        self.assertEqual(report['status'], 'running')
                        self.assertNotEqual(report['services'], ['status unavailable'])
                        self.assertIn('files', report['urls'])
                    finally:
                        for command in collisions:
                            (custom_bin / command).unlink()
                        manager.exec('alpha', ['/bin/rm', '-f', *['/usr/local/bin/' + command for command in collisions]], interactive=False)
                with self.subTest('root login shells expose launchers and sync repairs copied permissions'):
                    shell_check = 'for cmd in glm openrouter acf-agent; do command -v "$cmd"; test -x "$(command -v "$cmd")" || exit 1; done'
                    paths = manager.exec('alpha', ['bash', '-lc', shell_check], interactive=False).stdout
                    self.assertEqual(paths.splitlines(), ['/usr/local/bin/glm', '/usr/local/bin/openrouter', '/usr/local/bin/acf-agent'])
                    for provider in ('glm', 'openrouter'):
                        help_text = manager.exec('alpha', ['bash', '-lc', provider + ' --help'], interactive=False).stdout
                        self.assertIn('--model', help_text)
                    manager.exec('alpha', ['chmod', '644', '/opt/acf/bin/acf-agent'], interactive=False)
                    manager.exec('alpha', ['rm', '/usr/local/bin/glm', '/usr/local/bin/openrouter'], interactive=False)
                    manager.sync('alpha')
                    self.assertEqual(manager.exec('alpha', ['bash', '-lc', shell_check], interactive=False).stdout, paths)
                with self.subTest('provider commands pass model and credentials to Claude from the root shell'):
                    # Shell exports must win even when this container has saved
                    # provider keys. These are fake values used only by a stub.
                    saved_keys = {'GLM_API_KEY': 'saved-glm-test-key',
                                  'OPENROUTER_API_KEY': 'saved-router-test-key'}
                    manager._control_exec(name, 'sh', '-c',
                        'umask 077; cat > /run/acf/secrets.json', input=json.dumps(saved_keys))
                    stub = root / 'claude-stub'
                    stub.write_text("#!/usr/bin/python3\nimport os,sys,json\nprint(json.dumps({'uid':os.getuid(),'args':sys.argv[1:],'base_url':os.environ['ANTHROPIC_BASE_URL'],'token':os.environ['ANTHROPIC_AUTH_TOKEN'],'model':os.environ['ANTHROPIC_DEFAULT_SONNET_MODEL'],'subagent':os.environ['CLAUDE_CODE_SUBAGENT_MODEL'],'config':os.environ['CLAUDE_CONFIG_DIR'],'cwd':os.getcwd()}))\n")
                    stub.chmod(0o755)
                    manager.exec('alpha', ['mkdir', '-p', '/tmp/acf-test-bin'], interactive=False)
                    manager.docker.run('cp', str(stub), name + ':/tmp/acf-test-bin/claude')
                    manager.exec('alpha', ['chmod', '755', '/tmp/acf-test-bin/claude'], interactive=False)
                    for provider, base_url, key in (
                            ('glm', 'https://api.z.ai/api/anthropic', 'GLM_API_KEY'),
                            ('openrouter', 'https://openrouter.ai/api', 'OPENROUTER_API_KEY')):
                        chosen_model = 'test-model' if provider == 'glm' else 'test-provider/test-model'
                        output = manager.exec('alpha', ['env', 'ACF_INTEGRATIONS=glm,openrouter',
                            'PATH=/tmp/acf-test-bin:/usr/local/bin:/usr/bin:/bin',
                            key + '=fake-local-test-token', provider, '--model', chosen_model], interactive=False).stdout
                        report = json.loads(output)
                        self.assertEqual(report['uid'], 1000)
                        self.assertEqual(report['args'][-2:], ['--model', chosen_model])
                        self.assertEqual(report['base_url'], base_url)
                        self.assertEqual(report['token'], 'fake-local-test-token')
                        self.assertEqual(report['model'], chosen_model)
                        self.assertEqual(report['subagent'], chosen_model)
                        self.assertEqual(report['config'], '/home/agent/.claude-' + provider)
                        self.assertEqual(report['cwd'], '/workspace')
                    manager.exec('alpha', ['rm', '-rf', '/tmp/acf-test-bin'], interactive=False)
                    self.assertEqual(json.loads(manager.exec('alpha', ['cat', '/run/acf/secrets.json'], interactive=False).stdout), saved_keys)
                    manager.sync('alpha')
                with self.subTest('LAN service exposure requires the access token'):
                    info = manager.details('alpha')
                    network_info = manager.owned('container', guard)['NetworkSettings']
                    for mappings in network_info['Ports'].values():
                        self.assertTrue(all(m['HostIp'] == '0.0.0.0' for m in mappings))
                    token = manager.state('alpha')['access_token']
                    base = info['urls']['files'].rstrip('/')
                    self.assertTrue(base.endswith('/' + token))
                    host = urlsplit(base).hostname
                    self.assertNotIn(host, ('127.0.0.1', 'localhost', '0.0.0.0', '::', '::1'))
                    deadline = time.monotonic() + 45
                    while True:
                        try:
                            urllib.request.urlopen(base + '/', timeout=2).close()
                            break
                        except OSError:
                            if time.monotonic() > deadline:
                                raise
                            time.sleep(.5)
                    with urllib.request.urlopen(base + '/api/list?path=/workspace', timeout=5) as response:
                        self.assertIn('entries', json.load(response))
                    manager.exec('alpha', ['python3', '-c', "from pathlib import Path; Path('/workspace/proof.txt').write_bytes(b'persistent proof')"], interactive=False)
                    with urllib.request.urlopen(base + '/api/preview?path=/workspace/proof.txt', timeout=5) as response:
                        self.assertEqual(json.load(response)['text'], 'persistent proof')
                    with urllib.request.urlopen(base + '/download/workspace/proof.txt', timeout=5) as response:
                        self.assertEqual(response.read(), b'persistent proof')
                        self.assertIn('attachment', response.headers['Content-Disposition'])
                    with urllib.request.urlopen(base + '/browse/workspace/proof.txt', timeout=5) as response:
                        page = response.read()
                        self.assertIn(b'id="sidebar"', page)
                        self.assertIn(b'/assets/markdown-it.min.js', page)
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(urllib.request.Request(base + '/api/file?path=/workspace/proof.txt', data=b'replace', method='PUT'), timeout=5)
                    self.assertEqual(error.exception.code, 405)
                    error.exception.close()
                    files_origin = 'http://' + host_port(host, info['ports']['files'])
                    for path in ('/', '/browse/', '/api/list?path=/workspace', '/download/workspace/proof.txt',
                                 '/' + 'x' * len(token) + '/api/list?path=/workspace'):
                        with self.assertRaises(urllib.error.HTTPError) as error:
                            urllib.request.urlopen(files_origin + path, timeout=5)
                        self.assertEqual(error.exception.code, 403)
                        error.exception.close()
                    self.assertNotIn('novnc_secondary', info['ports'])
                    self.assertNotIn('6081/tcp', network_info['Ports'])
                    browser_base = 'http://' + host_port(host, info['ports']['novnc'])
                    with urllib.request.urlopen(browser_base + '/vnc.html', timeout=10) as response:
                        self.assertEqual(response.status, 200)
                        page = response.read().decode()
                        self.assertIn('acf-profiles.js', page)
                        self.assertIn('acf-profiles.css', page)
                    with urllib.request.urlopen(browser_base + '/acf-profiles.js', timeout=10) as response:
                        toolbar = response.read().decode()
                        self.assertIn('Primary', toolbar)
                        self.assertIn('Secondary', toolbar)
                        self.assertIn('sendKey', toolbar)
                    with urllib.request.urlopen(browser_base + '/acf-profiles.css', timeout=10) as response:
                        self.assertIn(b'acf-profiles', response.read())
                    deadline = time.monotonic() + 30
                    while True:
                        connection = manager.docker.run('exec', '--env', 'ACF_TEST_NOVNC_URL=ws://127.0.0.1:6080/websockify?token=' + token,
                                                        name, 'node', '-e', NOVNC_HANDSHAKE, check=False, timeout=15)
                        if connection.returncode == 0:
                            break
                        if time.monotonic() > deadline:
                            self.fail(connection.stderr or 'No noVNC desktop connection with the access token')
                        time.sleep(.5)
                    refused = manager.docker.run('exec', name, 'node', '-e', NOVNC_HANDSHAKE, check=False, timeout=15)
                    self.assertNotEqual(refused.returncode, 0, 'noVNC accepted a connection without the access token')
                with self.subTest('dashboard service rows include built-in endpoints without secrets'):
                    rows = manager.list_projects()
                    listed = next(project for project in rows if project['name'] == 'alpha')
                    self.assertNotIn('file_token', listed)
                    self.assertNotIn('vnc_password', listed)
                    self.assertNotIn('file_token', info)
                    self.assertNotIn('vnc_password', info)
                    builtins = {item['name']: item for item in listed['service_items']}
                    self.assertEqual(builtins['files']['address'], base + '/')
                    self.assertEqual(builtins['files']['scope'], 'published')
                    self.assertEqual(builtins['files']['protocol'], 'http')
                    self.assertEqual(builtins['novnc']['address'], browser_base + '/vnc.html?autoconnect=1&resize=remote'
                                     '&path=websockify%3Ftoken%3D' + token)
                    self.assertEqual(builtins['novnc']['scope'], 'published')
                    self.assertEqual(builtins['caido']['state'], 'stopped')
                    for item in builtins.values():
                        if item['scope'] == 'process':
                            self.assertEqual(item['address'], '')
                with self.subTest('custom HTTP and TCP listeners are discovered and reachable'):
                    manager.docker.run('exec', '-d', name, 'python3', '-m', 'http.server', '3000',
                                       '--bind', '0.0.0.0', '--directory', '/workspace')
                    raw_server = """import socket
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(('0.0.0.0', 3333))
server.listen()
while True:
    connection, address = server.accept()
    with connection:
        try:
            connection.sendall(b'ACF raw TCP\\n')
        except OSError:
            pass
"""
                    manager.docker.run('exec', '-d', name, 'python3', '-c', raw_server)
                    deadline = time.monotonic() + 30
                    while True:
                        custom_info = manager.details('alpha')
                        custom = {item['name']: item for item in custom_info['service_items']}
                        if (custom.get('test-http', {}).get('state') == 'running'
                                and custom['test-http'].get('protocol') == 'http'
                                and custom.get('test-tcp', {}).get('state') == 'running'):
                            break
                        if time.monotonic() > deadline:
                            self.fail('Custom services were not discovered: ' + json.dumps(custom_info['service_items']))
                        time.sleep(.5)
                    http_port = custom_info['ports']['3000/tcp']
                    tcp_port = custom_info['ports']['3333/tcp']
                    self.assertEqual(custom['test-http']['address'], 'http://' + host_port(host, http_port) + '/')
                    self.assertEqual(custom['test-http']['scope'], 'published')
                    self.assertEqual(custom['test-tcp']['address'], host_port(host, tcp_port))
                    self.assertEqual(custom['test-tcp']['protocol'], 'tcp')
                    self.assertEqual(custom['test-tcp']['scope'], 'published')
                    with urllib.request.urlopen(custom['test-http']['address'] + 'proof.txt', timeout=5) as response:
                        self.assertEqual(response.read(), b'persistent proof')
                    with socket.create_connection((host, tcp_port), timeout=5) as connection:
                        self.assertEqual(connection.recv(64), b'ACF raw TCP\n')
                with self.subTest('instructions, secrets and shell persistence'):
                    self.assertIn('Project alpha lives at /workspace', manager.exec('alpha', ['cat', '/opt/acf/project/AGENTS.md'], interactive=False).stdout)
                    self.assertEqual(json.loads(manager.exec('alpha', ['cat', '/run/acf/secrets.json'], interactive=False).stdout), {})
                    desired = manager.config.project('alpha')
                    desired['integrations']['enabled'] = ['codex', 'oob']
                    fake_key = 'test-only-$(never-executed)-key'
                    desired['credentials']['OPENAI_API_KEY'] = fake_key
                    host_key = root / 'test-oob-key'
                    host_key.write_text('test-only-ssh-key\n')
                    desired['oob']['ssh_key_path'] = str(host_key)
                    manager.config.save_project_settings('alpha', desired)
                    manager.sync('alpha')
                    transferred = manager.exec('alpha', ['cat', '/run/acf/secrets.json'], interactive=False).stdout
                    self.assertEqual(json.loads(transferred), {'OPENAI_API_KEY': fake_key})
                    self.assertEqual(manager.exec('alpha', ['cat', '/run/acf/oob_key'], interactive=False).stdout, host_key.read_text())
                    self.assertEqual(manager.exec('alpha', ['stat', '-c', '%a', '/run/acf/oob_key', '/run/acf/secrets.json'], interactive=False).stdout, '600\n600\n')
                    runtime_check = "import sys; sys.path.insert(0, '/opt/acf'); from common import environment; assert environment()['OPENAI_API_KEY'] == sys.argv[1]"
                    manager.exec('alpha', ['python3', '-c', runtime_check, fake_key], interactive=False)
                    self.assertEqual((root / 'config.toml').read_bytes(), defaults_before)
                    self.assertFalse((root / '.env').exists())
                    self.assertFalse((manager.config.project_dir('alpha') / 'config.toml').exists())
                    manager.exec('alpha', ['tmux', 'new-session', '-d', '-s', 'persistent-test'], interactive=False)
                    self.assertIn('persistent-test', manager.exec('alpha', ['tmux', 'list-sessions', '-F', '#S'], interactive=False).stdout)
                    prompt.write_text('Updated {{PROJECT_NAME}} instructions.\n')
                    manager.sync('alpha')
                    self.assertIn('Updated alpha', manager.exec('alpha', ['cat', '/opt/acf/project/AGENTS.md'], interactive=False).stdout)
                with self.subTest('separate projects cannot reach peer services'):
                    manager.create('beta')
                    peer_ip = manager.details('beta')['ip']
                    code = "import socket,sys; s=socket.socket();s.settimeout(3);sys.exit(0 if s.connect_ex((sys.argv[1],8081)) else 1)"
                    manager.exec('alpha', ['python3', '-c', code, peer_ip], interactive=False)
                    for address in ('169.254.169.254', '192.168.1.1'):
                        manager.exec('alpha', ['python3', '-c', code, address], interactive=False)
                    # Unlike an absent listener, a known reachable public service proves egress.
                    manager.exec('alpha', ['python3', '-c', "import urllib.request; assert urllib.request.urlopen('https://example.com',timeout=15).status==200"], interactive=False)
                with self.subTest('stop/start and remove/recreate preserve project data'):
                    manager.stop('alpha')
                    manager.start('alpha')
                    self.assertEqual(manager.exec('alpha', ['cat', '/workspace/proof.txt'], interactive=False).stdout, 'persistent proof')
                    manager.remove('alpha')
                    self.assertIsNotNone(manager.state('alpha'))
                    self.assertIsNotNone(manager.owned('volume', name + '.workspace'))
                    manager.start('alpha')
                    self.assertEqual(manager.exec('alpha', ['cat', '/workspace/proof.txt'], interactive=False).stdout, 'persistent proof')
                with self.subTest('offline policy blocks public egress'):
                    desired = manager.config.project('beta')
                    desired['network']['mode'] = 'offline'
                    manager.config.save_project_settings('beta', desired)
                    manager.apply('beta')
                    manager.exec('beta', ['python3', '-c', "import socket; s=socket.socket();s.settimeout(3);assert s.connect_ex(('1.1.1.1',443)) != 0"], interactive=False)
                    manager.exec('beta', ['python3', '-c', "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(2);\ntry:\n s.sendto(bytes.fromhex('123401000001000000000000076578616d706c6503636f6d0000010001'),('127.0.0.11',53)); s.recv(512)\nexcept OSError: pass\nelse: raise AssertionError('Offline DNS forwarded a query')"], interactive=False)
            finally:
                for project in ('alpha', 'beta'):
                    manager.remove(project, purge=True)


if __name__ == '__main__':
    unittest.main()
