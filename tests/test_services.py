"""Service discovery separates web URLs, raw endpoints, and process status."""
import importlib.util
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import Mock, patch

from acf import addresses
from acf.management import service_urls
from acf.services import service_controls, service_items

spec = importlib.util.spec_from_file_location('acf_service_report', Path(__file__).parents[1] / 'src/acf/runtime/service_report.py')
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


class HostAddressTests(unittest.TestCase):
    def setUp(self):
        addresses._local_host.cache_clear()
        self.addCleanup(addresses._local_host.cache_clear)

    def test_ssh_uses_server_address_not_client_address(self):
        with patch.dict(os.environ, {'SSH_CONNECTION': '198.51.100.2 4567 10.20.0.8 22'}), \
                patch.object(addresses.socket, 'socket') as connect:
            self.assertEqual(addresses.local_host(), '10.20.0.8')
            connect.assert_not_called()

    def test_route_address_is_used_without_sending_or_resolving_and_cached(self):
        connection = Mock()
        connection.getsockname.return_value = ('192.168.1.40', 41000)
        with patch.dict(os.environ, {'SSH_CONNECTION': ''}), \
                patch.object(addresses.socket, 'socket') as create, \
                patch.object(addresses.socket, 'getaddrinfo') as resolve:
            create.return_value.__enter__.return_value = connection
            self.assertEqual(addresses.local_host(), '192.168.1.40')
            self.assertEqual(addresses.local_host(), '192.168.1.40')
            create.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
            connection.connect.assert_called_once_with(('192.0.2.1', 9))
            connection.send.assert_not_called()
            connection.sendto.assert_not_called()
            resolve.assert_not_called()

    def test_bad_or_local_only_ssh_addresses_use_offline_hostname_fallback(self):
        for server in ('127.0.0.1', '0.0.0.0', '169.254.1.2', 'bad.example'):
            with self.subTest(server=server), \
                    patch.dict(os.environ, {'SSH_CONNECTION': f'198.51.100.2 4567 {server} 22'}), \
                    patch.object(addresses.socket, 'socket', side_effect=OSError('no route')), \
                    patch.object(addresses.socket, 'gethostname', return_value='acf-host.local'):
                self.assertEqual(addresses.local_host(), 'acf-host.local')

    def test_ipv6_uses_matching_ssh_address_and_brackets(self):
        with patch.dict(os.environ, {'SSH_CONNECTION': '2001:db8::2 4567 2001:db8::8 22'}):
            host = addresses.published_host('::')
            self.assertEqual(addresses.host_port(host, 49153), '[2001:db8::8]:49153')

    def test_remote_daemon_never_uses_workstation_address(self):
        with patch.object(addresses, 'local_host') as local:
            self.assertEqual(addresses.published_host('0.0.0.0', docker_host='docker.example.com'), 'docker.example.com')
            self.assertEqual(addresses.published_host('0.0.0.0', docker_host=''), '')
            self.assertEqual(addresses.published_host('0.0.0.0', 'public.example.com', ''), 'public.example.com')
            local.assert_not_called()

    def test_remote_daemon_ip_must_match_wildcard_binding_family(self):
        for bind, host in (('::', '192.0.2.8'), ('0.0.0.0', '2001:db8::8')):
            with self.subTest(bind=bind, host=host), patch.object(addresses, 'local_host') as local:
                self.assertEqual(addresses.published_host(bind, docker_host=host), '')
                self.assertEqual(addresses.published_host(bind, 'public.example.com', host), 'public.example.com')
                local.assert_not_called()

    def test_explicit_bind_and_loopback_are_not_replaced_by_detected_host(self):
        with patch.object(addresses, 'local_host') as local:
            self.assertEqual(addresses.published_host('192.168.1.9'), '192.168.1.9')
            self.assertEqual(addresses.published_host('127.0.0.1', 'public.example.com'), '127.0.0.1')
            self.assertEqual(addresses.published_host('::1', 'public.example.com'), '::1')
            local.assert_not_called()


class ServiceItemsTests(unittest.TestCase):
    def setUp(self):
        self.settings = {'ports': {'extra': []}}
        self.published = {'8081/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '49153'}],
                          '6080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '49154'}]}

    def items(self, services=None, listeners=None):
        return service_items(services or {}, listeners or [], self.published, '172.30.0.2', self.settings)

    def test_published_builtin_web_servers_have_full_urls(self):
        result = self.items({'files': {'state': 'running'}, 'novnc': {'state': 'running'}})
        by_name = {row['name']: row for row in result}
        self.assertEqual(by_name['files']['address'], 'http://127.0.0.1:49153/')
        self.assertEqual(by_name['novnc']['address'], 'http://127.0.0.1:49154/vnc.html?autoconnect=1&resize=remote')
        self.assertEqual(by_name['files']['scope'], 'published')
        self.assertNotIn('novnc_secondary', by_name)

    def test_files_and_vnc_urls_carry_the_access_token_but_other_services_do_not(self):
        token = 'fixture-access-token-0123456789abcdefABCDEF'
        self.published.update({'6082/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '49155'}],
                               '8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '49156'}],
                               '3000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '49157'}]})
        listeners = [{'host': '0.0.0.0', 'port': 3000, 'protocol': 'http', 'name': 'app'}]
        services = {name: {'state': 'running'} for name in ('files', 'novnc', 'desktop-http', 'caido')}
        result = service_items(services, listeners, self.published, '172.30.0.2', self.settings,
                               access_token=token)
        by_name = {row['name']: row['address'] for row in result}
        vnc = '/vnc.html?autoconnect=1&resize=remote&path=websockify%3Ftoken%3D' + token
        self.assertEqual(by_name['files'], f'http://127.0.0.1:49153/{token}/')
        self.assertEqual(by_name['novnc'], 'http://127.0.0.1:49154' + vnc)
        self.assertEqual(by_name['desktop-http'], 'http://127.0.0.1:49155' + vnc)
        self.assertEqual(by_name['caido'], 'http://127.0.0.1:49156/')
        self.assertEqual(by_name['app'], 'http://127.0.0.1:49157/')
        # Images without supervisor metadata still receive tokenized published URLs.
        fallback = {row['name']: row['address'] for row in service_items(
            {}, [], self.published, '172.30.0.2', self.settings, access_token=token)}
        self.assertEqual(fallback['files'], f'http://127.0.0.1:49153/{token}/')
        self.assertEqual(fallback['novnc'], 'http://127.0.0.1:49154' + vnc)
        self.assertEqual(fallback['desktop-http'], 'http://127.0.0.1:49155' + vnc)

    def test_desktop_has_its_own_published_http_url_while_stopped(self):
        self.published['6082/tcp'] = [{'HostIp': '0.0.0.0', 'HostPort': '49180'}]
        with patch.object(addresses, 'local_host', return_value='192.168.1.40'):
            result = self.items({'desktop-http': {'state': 'stopped'}, 'novnc': {'state': 'running'}})
        by_name = {row['name']: row for row in result}
        self.assertEqual(by_name['desktop-http']['address'], 'http://192.168.1.40:49180/')
        self.assertEqual(by_name['desktop-http']['state'], 'stopped')
        self.assertEqual(by_name['novnc']['port'], 6080)

    def test_only_known_managed_services_receive_control_actions(self):
        rows = service_controls({}, [
            {'name': 'desktop', 'label': 'untrusted label', 'state': 'stopped'},
            {'name': 'files', 'state': 'running'},
            {'name': 'unknown', 'state': 'running'},
            {'name': ['malformed'], 'state': 'running'},
        ], 2)
        self.assertEqual(rows, [{'name': 'desktop', 'label': 'Desktop', 'state': 'stopped'},
                                {'name': 'files', 'label': 'Files', 'state': 'running'}])
        self.assertEqual(service_controls({'files': {'state': 'running'}, 'desktop': {'state': 'running'}},
                                          [{'name': 'desktop', 'state': 'running'}], 0),
                         [{'name': 'files', 'label': 'Files', 'state': 'running'}])

    def test_arbitrary_http_and_non_http_servers_are_distinguished(self):
        self.settings['ports']['extra'] = [{'host': 0, 'container': 3000, 'name': 'web'},
                                           {'host': 0, 'container': 5432, 'name': 'db', 'protocol': 'tcp'}]
        self.published.update({'3000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '49155'}],
                               '5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '49156'}]})
        result = self.items(listeners=[{'host': '0.0.0.0', 'port': 3000, 'protocol': 'http', 'name': 'python3'},
                                      {'host': '0.0.0.0', 'port': 5432, 'protocol': 'tcp', 'name': 'postgres'}])
        by_name = {row['name']: row for row in result}
        self.assertEqual(by_name['web']['address'], 'http://127.0.0.1:49155/')
        self.assertEqual(by_name['db']['address'], '127.0.0.1:49156')
        self.assertEqual(by_name['db']['protocol'], 'tcp')
        urls = service_urls({'service_items': result})
        self.assertNotIn('db', urls)
        self.assertEqual(urls['web'], by_name['web']['address'])

    def test_unpublished_listeners_are_labelled_internal_or_container_local(self):
        result = self.items(listeners=[{'host': '0.0.0.0', 'port': 7000, 'protocol': 'http', 'name': 'web'},
                                      {'host': '127.0.0.1', 'port': 6379, 'protocol': 'tcp', 'name': 'redis'}])
        by_name = {row['name']: row for row in result}
        self.assertEqual(by_name['web']['address'], 'http://172.30.0.2:7000/')
        self.assertEqual(by_name['web']['scope'], 'internal')
        self.assertEqual(by_name['redis']['address'], '127.0.0.1:6379')
        self.assertEqual(by_name['redis']['scope'], 'local')

    def test_loopback_bound_custom_server_does_not_claim_working_host_publication(self):
        self.published['3000/tcp'] = [{'HostIp': '127.0.0.1', 'HostPort': '49155'}]
        result = self.items(listeners=[{'host': '127.0.0.1', 'port': 3000, 'protocol': 'http', 'name': 'app'}])
        row = next(row for row in result if row['name'] == 'app')
        self.assertEqual(row['address'], 'http://127.0.0.1:3000/')
        self.assertEqual(row['scope'], 'local')

    def test_explicit_https_override_and_ipv6_binding(self):
        self.settings['ports']['extra'] = [{'host': 0, 'container': 4443, 'name': 'secure', 'protocol': 'https'}]
        self.published['4443/tcp'] = [{'HostIp': '::', 'HostPort': '9443'}]
        with patch.object(addresses, 'local_host', return_value='2001:db8::8'):
            result = self.items(listeners=[{'host': '::', 'port': 4443, 'protocol': 'tcp'}])
        row = next(row for row in result if row['name'] == 'secure')
        self.assertEqual(row['address'], 'https://[2001:db8::8]:9443/')

    def test_wildcard_http_and_tcp_publications_use_host_lan_address(self):
        self.published = {'8081/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '49153'}],
                          '5432/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '49154'}]}
        with patch.object(addresses, 'local_host', return_value='192.168.1.40'):
            result = self.items({'files': {'state': 'running'}},
                                [{'host': '0.0.0.0', 'port': 5432, 'protocol': 'tcp', 'name': 'db'}])
        self.assertEqual({row['address'] for row in result}, {'http://192.168.1.40:49153/', '192.168.1.40:49154'})

    def test_public_nat_hostname_overrides_interface_and_preserves_protocol(self):
        self.settings['network'] = {'advertise_host': 'acf.example.com'}
        self.published = {'8081/tcp': [{'HostIp': '10.20.0.8', 'HostPort': '49153'}],
                          '5432/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '49154'}]}
        result = self.items({'files': {'state': 'running'}},
                            [{'host': '0.0.0.0', 'port': 5432, 'protocol': 'tcp', 'name': 'db'}])
        self.assertEqual({row['address'] for row in result}, {'http://acf.example.com:49153/', 'acf.example.com:49154'})

    def test_unknown_remote_host_does_not_produce_invalid_or_local_urls(self):
        self.published = {'8081/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '49153'}]}
        result = service_items({'files': {'state': 'running'}}, [], self.published, '172.30.0.2', self.settings, docker_host='')
        self.assertEqual(result[0]['address'], '')
        self.assertEqual(result[0]['scope'], 'published')
        self.assertEqual(service_urls({'service_items': result}), {})

    def test_reachable_binding_is_preferred_over_additional_loopback_binding(self):
        self.published = {'8081/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '49153'},
                                     {'HostIp': '192.168.1.40', 'HostPort': '49154'}]}
        result = self.items({'files': {'state': 'running'}})
        self.assertEqual(result[0]['address'], 'http://192.168.1.40:49154/')

    def test_remote_host_family_mismatch_never_claims_a_working_url(self):
        for bind, host in (('::', '192.0.2.8'), ('0.0.0.0', '2001:db8::8')):
            with self.subTest(bind=bind, host=host):
                published = {'8081/tcp': [{'HostIp': bind, 'HostPort': '49153'}]}
                result = service_items({'files': {'state': 'running'}}, [], published, '172.30.0.2', self.settings, docker_host=host)
                self.assertEqual(result[0]['address'], '')
                self.assertEqual(result[0]['scope'], 'published')
                self.assertEqual(service_urls({'service_items': result}), {})

    def test_multiple_publications_choose_the_remote_hosts_address_family(self):
        for host, bindings, expected in (
                ('192.0.2.8', [('::', '49153'), ('0.0.0.0', '49154')], 'http://192.0.2.8:49154/'),
                ('2001:db8::8', [('0.0.0.0', '49153'), ('::', '49154')], 'http://[2001:db8::8]:49154/')):
            with self.subTest(host=host):
                published = {'8081/tcp': [{'HostIp': bind, 'HostPort': port} for bind, port in bindings]}
                result = service_items({'files': {'state': 'running'}}, [], published, '172.30.0.2', self.settings, docker_host=host)
                self.assertEqual(result[0]['address'], expected)

    def test_processes_have_no_fabricated_endpoint_and_services_are_not_duplicated(self):
        result = self.items({'desktop': {'state': 'running'}, 'files': {'state': 'running'}},
                            [{'host': '0.0.0.0', 'port': 8081, 'name': 'python3', 'protocol': 'http'}])
        self.assertEqual(len([row for row in result if row.get('port') == 8081]), 1)
        desktop = next(row for row in result if row['name'] == 'desktop')
        self.assertEqual(desktop['address'], '')
        self.assertEqual(desktop['scope'], 'process')
        self.assertEqual(result[0]['name'], 'files')

    def test_stopped_supervised_services_are_not_reported_running(self):
        row = next(row for row in self.items({'caido': {'state': 'stopped'}}) if row['name'] == 'caido')
        self.assertEqual(row['state'], 'stopped')

    def test_malformed_listener_addresses_and_protocols_are_ignored(self):
        result = self.items(listeners=[{'host': 'attacker.example', 'port': 3000, 'protocol': 'http'},
                                      {'host': '127.0.0.1', 'port': True, 'protocol': 'http'},
                                      {'host': '127.0.0.1', 'port': 3001, 'protocol': 'javascript'}])
        self.assertFalse(any(row.get('port') in (3000, 3001) for row in result))

    def test_same_process_name_on_two_http_ports_keeps_both_urls(self):
        self.published = {'3000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '49155'}],
                          '3001/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '49156'}]}
        result = self.items(listeners=[{'host': '0.0.0.0', 'port': port, 'protocol': 'http', 'name': 'python3'} for port in (3000, 3001)])
        self.assertEqual(set(service_urls({'service_items': result}).values()),
                         {'http://127.0.0.1:49155/', 'http://127.0.0.1:49156/'})


class RuntimeReportTests(unittest.TestCase):
    def test_proc_tcp_parser_only_returns_listeners_and_ignores_docker_dns(self):
        rows = '''header
  0: 00000000:0BB8 00000000:0000 0A 0:0 00:0 0 0 0 123
  1: 0B00007F:1234 00000000:0000 0A 0:0 00:0 0 0 0 456
  2: 0100007F:1770 00000000:0000 01 0:0 00:0 0 0 0 789
'''
        self.assertEqual(reporter.parse_tcp(rows, socket.AF_INET), [{'host': '0.0.0.0', 'port': 3000, 'inode': '123'}])

    def test_proc_ipv6_parser_handles_word_endianness(self):
        rows = 'header\n 0: 00000000000000000000000001000000:1F90 0:0 0A 0:0 00:0 0 0 0 123\n'
        self.assertEqual(reporter.parse_tcp(rows, socket.AF_INET6)[0]['host'], '::1')

    def test_http_probe_recognizes_real_local_http_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.send_response(204)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(reporter.probe_protocol({'host': '127.0.0.1', 'port': server.server_port}), 'http')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_raw_protocol_is_not_labelled_http(self):
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()
        def serve():
            connection, _ = server.accept()
            with connection:
                connection.recv(512)
                connection.sendall(b'SSH-2.0-test\r\n')
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            self.assertEqual(reporter.probe_protocol({'host': '127.0.0.1', 'port': server.getsockname()[1]}), 'tcp')
        finally:
            server.close()
            thread.join(2)

    def test_known_protocol_never_probes_database_or_vnc(self):
        with patch.object(reporter.socket, 'create_connection') as connect:
            self.assertEqual(reporter.probe_protocol({'host': '0.0.0.0', 'port': 5432}), 'tcp')
            self.assertEqual(reporter.probe_protocol({'host': '127.0.0.1', 'port': 5901}), 'tcp')
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
