"""Normalize service reports into displayable, correctly scoped endpoints."""
from __future__ import annotations

import ipaddress
from urllib.parse import quote

from .addresses import host_port, published_host

KNOWN_ENDPOINTS = {
    'files': (8081, 'http', '/'), 'caido': (8080, 'http', '/'),
    'novnc': (6080, 'http', '/vnc.html?autoconnect=1&resize=remote'),
    'desktop-http': (6082, 'http', '/'),
    # Older running containers remain inspectable until the user applies updates.
    'novnc-primary': (6080, 'http', '/vnc.html?autoconnect=1&resize=remote'),
    'novnc-secondary': (6081, 'http', '/vnc.html?autoconnect=1&resize=remote'),
    'vnc': (5901, 'tcp', ''), 'vnc-primary': (5901, 'tcp', ''), 'vnc-secondary': (5902, 'tcp', ''),
    'browser-primary': (9222, 'http', '/'), 'browser-secondary': (9224, 'http', '/'),
}
LOCAL_PORTS = {5900, 5901, 5902, 9222, 9223, 9224, 9225}
VNC_PATH = '/vnc.html?autoconnect=1&resize=remote'
CONTROL_LABELS = {'browser': 'Browser', 'desktop': 'Desktop', 'files': 'Files', 'caido': 'Caido'}


def service_controls(services, controls=None, protocol=0):
    """Only known supervisor-managed services may receive dashboard actions."""
    services = services if isinstance(services, dict) else {}
    if type(protocol) is int and protocol >= 2 and isinstance(controls, list):
        states = {item.get('name'): item.get('state') for item in controls
                  if isinstance(item, dict) and isinstance(item.get('name'), str)
                  and item['name'] in CONTROL_LABELS}
    else:
        # Old supervisors accept individual services, but cannot safely toggle
        # complete browser/desktop groups. Keep those unavailable until upgraded.
        states = {name: services[name].get('state') for name in ('files', 'caido')
                  if isinstance(services.get(name), dict)}
    return [dict(name=name, label=label,
                 state=states[name] if states[name] in ('running', 'stopped', 'failed', 'starting') else 'unknown')
            for name, label in CONTROL_LABELS.items() if name in states]


def authenticated_path(port, path, access_token):
    """Add the per-boot token that the Files and noVNC services require.

    Files takes it as the first path segment and websockify as the WebSocket
    query, which noVNC reads from its path setting. Neither uses cookies:
    those are shared by every port on the host, including other projects.
    """
    if not access_token:
        return path
    if port == 8081:
        return '/' + access_token + path
    if port in (6080, 6082):
        return VNC_PATH + '&path=' + quote('websockify?token=' + access_token, safe='')
    return path


def endpoint_address(port, protocol, listeners, published, container_ip, path='/', *, advertise_host='', docker_host=None):
    """Prefer an actual published binding, without claiming loopback-only services are reachable."""
    network_listener = next((item for item in listeners if not ipaddress.ip_address(item['host']).is_loopback), None)
    bindings = published.get(f'{port}/tcp') or []
    local = bool(listeners) and network_listener is None
    if bindings and not local:
        # Prefer nonloopback publications and try each address family. A remote
        # IPv4 daemon address cannot reach an IPv6-only host binding, or vice versa.
        candidates = sorted(bindings, key=lambda item: ipaddress.ip_address(item.get('HostIp') or '0.0.0.0').is_loopback)
        address = ''
        for binding in candidates:
            host = published_host(binding.get('HostIp'), advertise_host, docker_host)
            if host:
                address = host_port(host, int(binding['HostPort']))
                break
        scope = 'published'
    elif local or port in LOCAL_PORTS:
        host = listeners[0]['host'] if listeners else '127.0.0.1'
        address, scope = host_port(host, port), 'local'
    elif container_ip:
        address, scope = host_port(container_ip, port), 'internal'
    else:
        return '', 'internal'
    if address and protocol in ('http', 'https'):
        address = f'{protocol}://{address}{path}'
    return address, scope


def service_items(services, listeners, published, container_ip, settings, *, docker_host=None, access_token=None):
    """Services are separate rows; configured extra ports can override detection."""
    services = services if isinstance(services, dict) else {}
    published = published if isinstance(published, dict) else {}
    sockets = {}
    for item in listeners if isinstance(listeners, list) else []:
        try:
            if not isinstance(item, dict) or type(item.get('port')) is not int or not 1 <= item['port'] <= 65535:
                continue
            ipaddress.ip_address(item['host'])
            if item.get('protocol') not in ('http', 'https', 'tcp'):
                continue
            sockets.setdefault(item['port'], []).append(item)
        except (ValueError, KeyError, TypeError):
            continue
    extra = {entry['container']: entry for entry in settings.get('ports', {}).get('extra', [])}
    advertise_host = settings.get('network', {}).get('advertise_host', '')
    result, claimed = [], set()
    for name, detail in services.items():
        if not isinstance(detail, dict):
            detail = {'state': str(detail)}
        state = detail.get('state', 'unknown')
        known = KNOWN_ENDPOINTS.get(name)
        if not known:
            result.append(dict(name=name, state=state, protocol='', address='', scope='process'))
            continue
        port, protocol, path = known
        path = authenticated_path(port, path, access_token)
        claimed.add(port)
        rows = sockets.get(port, [])
        address, scope = endpoint_address(port, protocol, rows, published, container_ip, path,
                                          advertise_host=advertise_host, docker_host=docker_host)
        result.append(dict(name=name, state=state, protocol=protocol, address=address, scope=scope, port=port))
    # A service that is configured/published but not listening remains visible as
    # stopped. Unconfigured listeners are discovered automatically inside the project.
    other_ports = set(sockets) | set(extra)
    other_ports.update(int(key.split('/')[0]) for key in published if key.endswith('/tcp') and key.split('/')[0].isdigit())
    for port in sorted(other_ports - claimed):
        rows = sockets.get(port, [])
        config = extra.get(port, {})
        owner = next((row.get('name') for row in rows if row.get('name')), f'tcp-{port}')
        name = str(config.get('name') or owner)
        override = config.get('protocol', 'auto')
        protocol = override if override != 'auto' else next((row['protocol'] for row in rows if row['protocol'] in ('http', 'https')), 'tcp')
        # A legacy/new image may lack supervisor metadata temporarily. Known
        # published web services still have a known protocol, never inferred for arbitrary ports.
        defaults = {8080: ('caido', 'http'), 8081: ('files', 'http'), 6080: ('novnc', 'http'),
                    6082: ('desktop-http', 'http')}
        if port in defaults and not rows and not config:
            name, protocol = defaults[port]
        path = VNC_PATH if port == 6080 and name == 'novnc' else '/'
        # Extra mappings cannot use these reserved ports, so only ACF serves them.
        path = authenticated_path(port, path, access_token)
        address, scope = endpoint_address(port, protocol, rows, published, container_ip, path,
                                          advertise_host=advertise_host, docker_host=docker_host)
        result.append(dict(name=name, state='running' if rows else 'not listening', protocol=protocol,
                           address=address, scope=scope, port=port))
    order = {'published': 0, 'internal': 1, 'local': 2, 'process': 3}
    return sorted(result, key=lambda item: (item['state'] != 'running', order.get(item['scope'], 4), str(item['name'])))
