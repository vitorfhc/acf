"""Project lifecycle. Only labelled ACF objects and named Docker volumes are managed."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import sys
import tempfile
import time
from urllib.parse import urlsplit

from .config import Config, INTERNAL_PORTS, normalize_settings, validate_name
from .addresses import published_host
from .docker import Docker, DockerError
from .resources import stage_resources
from .management import copy_files, edit_config, service_urls
from .services import CONTROL_LABELS, service_controls, service_items

SERVICE_PORTS = INTERNAL_PORTS
VOLUMES = {"workspace": "/workspace", "root": "/root", "home": "/home/agent", "tools": "/opt/acf-tools"}
CONTROL_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
# Files and noVNC accept only this token; token_urlsafe(32) produces 43 characters.
ACCESS_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
FILE_EXPLORER_ASSETS = (
    "files.py", "files.html", "vendor/markdown-it.min.js", "vendor/markdown-it.NOTICE",
    "vendor/markdown-it.LICENSE", "vendor/markdown-it.THIRD-PARTY-LICENSES",
)
SERVICE_RUNTIME_ASSETS = (
    'supervisor.py', 'service_report.py', 'desktop.py', 'install_novnc.py',
    'bin/acf-services', 'bin/acf-desktop-browser', 'novnc/profiles.js', 'novnc/profiles.css',
    'desktop-share/applications/chromium.desktop', 'desktop-share/xfce4/helpers/acf-browser.desktop',
)
FILE_EXPLORER_PROBE = """import hashlib, json, sys
from pathlib import Path
hashes = {}
for relative in sys.argv[1:]:
    try:
        hashes[relative] = hashlib.sha256((Path('/opt/acf') / relative).read_bytes()).hexdigest()
    except OSError:
        hashes[relative] = None
try:
    state = json.loads(Path('/run/acf/status.json').read_text())['services']['files']['state']
except (OSError, ValueError, KeyError, TypeError):
    state = None
print(json.dumps({'hashes': hashes, 'state': state}))
"""


def _source_signature(directory=Path(__file__).parent):
    """Identify host code only; edits to config, skills, and tools need no restart."""
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.digest()


_LOADED_SOURCE_SIGNATURE = _source_signature()


class Manager:
    def __init__(self, config=None, docker=None):
        self.config = config or Config()
        self.docker = docker or Docker()
        self.instance = hashlib.sha256(str(self.config.home.resolve()).encode()).hexdigest()[:10]
        self.guard_image = "acf-guard:local"

    @staticmethod
    def _require_current_code():
        try:
            current = _source_signature() == _LOADED_SOURCE_SIGNATURE
        except OSError:
            current = False
        if not current:
            raise DockerError("ACF code changed while this dashboard was open. Quit the dashboard and run ./acf again, then Sync. Running containers and agent sessions are preserved.")

    def names(self, name):
        validate_name(name)
        base = f"acf-{self.instance}-{name}"
        return base, base + ".guard", base + ".net"

    def labels(self, name, role):
        return ["--label", f"acf.instance={self.instance}", "--label", f"acf.project={name}",
                "--label", f"acf.role={role}"]

    def owned(self, kind, name):
        obj = self.docker.inspect(kind, name)
        if obj:
            labels = obj.get("Config", {}).get("Labels", {}) if kind == "container" else obj.get("Labels", {})
            if (labels or {}).get("acf.instance") != self.instance:
                raise DockerError(f"Refusing to change {name}: it is not owned by this ACF installation.")
            prefix = f"acf-{self.instance}-"
            local_name = name.removeprefix(prefix)
            project = local_name.split(".", 1)[0]
            role = {"network": "network", "volume": "storage"}.get(kind, "guard" if name.endswith(".guard") else "environment")
            if not name.startswith(prefix) or labels.get("acf.project") != project or labels.get("acf.role") != role:
                raise DockerError(f"Refusing to change {name}: project or role ownership labels do not match.")
        return obj

    @staticmethod
    def guard_matches(workload, guard, guard_name):
        if not guard:
            return False
        expected = {"container:" + guard_name}
        if guard.get("Id"):
            expected.add("container:" + guard["Id"])
        return workload.get("HostConfig", {}).get("NetworkMode") in expected

    @contextlib.contextmanager
    def lock(self, name):
        validate_name(name)
        self.config.initialize()
        locks = self.config.home / "locks"
        locks.mkdir(mode=0o700, exist_ok=True)
        with (locks / f"{name}.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DockerError(f"Another ACF operation is in progress for {name}.") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def state_path(self, name):
        validate_name(name)
        return self.config.project_dir(name) / "state.json"

    def state(self, name):
        path = self.state_path(name)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError) as exc:
            raise DockerError(f"Cannot read project state {path}: {exc}") from None

    def save(self, name, state):
        path = self.state_path(name)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(state, file, indent=2)
                file.write("\n")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def build(self):
        self.config.reload()
        source = Path(__file__).parent
        self.docker.run("build", "-t", self.guard_image, str(source / "guard"), timeout=None)
        self.docker.run("build", "-t", self.config.settings["environment"]["image"],
                        str(source / "runtime"), timeout=None)

    def ensure_images(self, settings):
        if not self.docker.inspect("image", self.guard_image):
            self.docker.run("build", "-t", self.guard_image, str(Path(__file__).parent / "guard"), timeout=None)
        image = settings["environment"]["image"]
        if not self.docker.inspect("image", image):
            if image != "acf:local":
                self.docker.run("pull", image, timeout=None)
            else:
                self.docker.run("build", "-t", image, str(Path(__file__).parent / "runtime"), timeout=None)

    def _network(self, name, settings):
        _, _, network = self.names(name)
        obj = self.owned("network", network)
        if not obj:
            args = ["network", "create", *self.labels(name, "network"), "--driver", "bridge",
                    "--opt", "com.docker.network.bridge.enable_icc=false"]
            if settings["network"]["mode"] == "offline":
                args.append("--internal")
            self.docker.run(*args, network)
            obj = self.owned("network", network)
        address = obj["IPAM"]["Config"][0]
        return network, address["Subnet"], address["Gateway"]

    def guard_args(self, name, settings, network, subnet, gateway):
        _, guard, _ = self.names(name)
        policy = settings["network"]
        args = ["create", "--name", guard, *self.labels(name, "guard"), "--network", network,
                "--restart", "no", "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
                "--cap-add", "SETUID", "--cap-add", "SETGID", "--security-opt", "no-new-privileges",
                "--read-only", "--tmpfs", "/run:rw,nosuid,noexec,size=4m", "--pids-limit", "64",
                "--memory", "64m", "--sysctl", "net.ipv6.conf.all.disable_ipv6=1",
                "-e", f"ACF_NETWORK={policy['mode']}", "-e", f"ACF_SUBNET={subnet}",
                "-e", f"ACF_GATEWAY={gateway}", "-e", f"ACF_ALLOW={' '.join(policy['allow'])}",
                "-e", f"ACF_BLOCK={' '.join(policy['block'])}"]
        bind = policy["bind"]
        bind = f"[{bind}]" if ":" in bind else bind
        for service, internal in SERVICE_PORTS.items():
            port = settings["ports"].get(service, 0)
            args += ["-p", f"{bind}:{port or ''}:{internal}/tcp"]
        for mapping in settings.get("ports", {}).get("extra", []):
            args += ["-p", f"{bind}:{mapping['host'] or ''}:{mapping['container']}/tcp"]
        return [*args, self.guard_image]

    def container_args(self, name, settings, state):
        container, guard, _ = self.names(name)
        env = settings["environment"]
        args = ["create", "--name", container, *self.labels(name, "environment"),
                "--network", f"container:{guard}", "--restart", "no", "--init", "--stop-timeout", "30",
                "--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cpus", str(env["cpus"]),
                "--memory", env["memory"], "--memory-swap", env["memory"],
                "--pids-limit", str(env["pids_limit"]), "--shm-size", env["shm_size"],
                "--tmpfs", "/run:rw,nosuid,size=128m", "--workdir", "/workspace",
                "-e", f"ACF_PROJECT={name}"]
        for key in ("openrouter_model", "glm_model"):
            value = settings["integrations"].get(key)
            if value:
                args += ["-e", f"ACF_{key.upper()}={value}"]
        args += ["-e", "ACF_INTEGRATIONS=" + ",".join(settings["integrations"]["enabled"])]
        for key, target in VOLUMES.items():
            args += ["--mount", f"type=volume,source={container}.{key},target={target}"]
        return [*args, env["image"]]

    def _guard_ready(self, guard):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            obj = self.owned("container", guard)
            state = (obj or {}).get("State", {})
            if state.get("Running") and state.get("Health", {}).get("Status") == "healthy":
                return
            if state.get("Status") in {"exited", "dead"}:
                log = self.docker.run("logs", "--tail", "20", guard, check=False).stdout
                raise DockerError(f"Network guard failed; environment was not started. {log}")
            time.sleep(.5)
        raise DockerError("Network guard did not become healthy. Open Guard logs from the dashboard's management menu.")

    def create(self, name, settings=None):
        with self.lock(name):
            self._require_current_code()
            if self.state(name):
                raise DockerError(f"Project {name} already exists. Select it in the dashboard and choose Start.")
            settings = normalize_settings(settings if settings is not None else self.config.project(name),
                                          base_dir=self.config.root)
            settings["name"] = name
            self._oob_key(settings)
            self.ensure_images(settings)
            state = {"name": name, "created": time.time(), "settings": settings, "settings_version": 2}
            self.save(name, state)
            return self._start(name)

    def start(self, name):
        with self.lock(name):
            return self._start(name)

    def _start(self, name):
        self._require_current_code()
        state = self.state(name)
        if not state:
            raise DockerError(f"Unknown project {name}. Create an environment in the dashboard.")
        settings = state["settings"]
        self.ensure_images(settings)
        container, guard, _ = self.names(name)
        obj = self.owned("container", container)
        guard_obj = self.owned("container", guard)
        running = bool(obj and obj["State"]["Running"])
        if running and not (guard_obj and guard_obj["State"]["Running"]):
            self.docker.run("stop", container)
            running = False
        if obj and guard_obj and not self.guard_matches(obj, guard_obj, guard):
            if obj["State"]["Running"] and guard_obj["State"]["Running"]:
                self.docker.run("stop", container)
            raise DockerError(f"Project {name} is outside its expected network guard. Choose Apply settings in the dashboard to recreate it safely.")
        network, subnet, gateway = self._network(name, settings)
        if not guard_obj:
            # Workloads refer to a concrete guard ID. A missing guard requires recreation.
            if obj:
                raise DockerError("The project's network guard was removed. Choose Apply settings in the dashboard to recreate safely with saved volumes.")
            self.docker.run(*self.guard_args(name, settings, network, subnet, gateway))
        self.docker.run("start", guard)
        self._guard_ready(guard)
        if not obj:
            for suffix in VOLUMES:
                volume = f"{container}.{suffix}"
                if not self.owned("volume", volume):
                    self.docker.run("volume", "create", *self.labels(name, "storage"), volume)
            self.docker.run(*self.container_args(name, settings, state))
        # Update the boot/browser code before an older image can launch a browser.
        self._copy_runtime_commands(container)
        self._copy_service_settings(container, self.config.project(name))
        # A boot may receive different host ports, so links saved for old ports
        # (including noVNC's stored settings) must stop working. Attaching to a
        # running environment keeps the current token and open browser tabs.
        self._access_token(name, rotate=not running)
        self.docker.run("start", container)
        return self._sync(name)

    def stop(self, name):
        with self.lock(name):
            container, guard, _ = self.names(name)
            for item in (container, guard):
                if self.owned("container", item):
                    self.docker.run("stop", "--time", "20", item, timeout=50)

    def restart(self, name):
        self._require_current_code()
        self.stop(name)
        return self.start(name)

    def remove(self, name, purge=False):
        with self.lock(name):
            self._remove(name, purge)

    def _remove(self, name, purge=False):
        container, guard, network = self.names(name)
        for item in (container, guard):
            if self.owned("container", item):
                self.docker.run("stop", "--time", "20", item, timeout=50)
                self.docker.run("rm", item)
        if self.owned("network", network):
            self.docker.run("network", "rm", network)
        if purge:
            for suffix in VOLUMES:
                volume = f"{container}.{suffix}"
                if self.owned("volume", volume):
                    self.docker.run("volume", "rm", volume)
            # Keep user-authored project config/skills/prompts; remove only ACF state.
            self.state_path(name).unlink(missing_ok=True)

    def apply(self, name):
        with self.lock(name):
            self._require_current_code()
            state = self.state(name)
            if not state:
                raise DockerError(f"Unknown project {name}.")
            settings = self.config.project(name)
            self._oob_key(settings)
            self.ensure_images(settings)
            self._remove(name)
            state["settings"] = settings
            state.pop("pending_settings", None)
            state["settings_version"] = 2
            self.save(name, state)
            return self._start(name)

    def sync(self, name):
        with self.lock(name):
            return self._sync(name)

    def service(self, name, action, service):
        """Change one supervised service without changing container defaults."""
        if action not in {'start', 'stop'} or service not in CONTROL_LABELS:
            raise DockerError('Choose Start or Stop for Browser, Desktop, Files, or Caido.')
        with self.lock(name):
            self._require_current_code()
            container, guard, _ = self.names(name)
            obj = self.owned('container', container)
            if not obj or not obj['State']['Running']:
                raise DockerError(f'{name} is not running. Start the environment before controlling services.')
            if not self.guard_matches(obj, self.owned('container', guard), guard):
                raise DockerError('Refusing to control services outside the expected network guard.')
            before = self.details(name)
            available = {item['name'] for item in before.get('service_controls', [])}
            if service not in available:
                raise DockerError('This environment needs the updated service supervisor. Build container images, '
                                  'then Apply settings from the dashboard when you are ready to restart its sessions.')
            if service == 'desktop' and action == 'start' and not before.get('ports', {}).get('desktop'):
                raise DockerError('This environment has no Desktop port published. Apply settings from the dashboard '
                                  'when you are ready to restart its sessions.')
            self._control_exec(container, '/usr/bin/python3', '/opt/acf/bin/acf-services', action, service, timeout=120)
            # The current supervisor acknowledges commands after updating its
            # snapshot. Legacy individual-service commands need one status poll.
            if before.get('control_protocol', 0) < 2:
                deadline = time.monotonic() + 10
                while True:
                    result = self.details(name)
                    state = next((item['state'] for item in result.get('service_controls', [])
                                  if item['name'] == service), None)
                    if state == ('running' if action == 'start' else 'stopped') or state == 'failed':
                        return result
                    if time.monotonic() >= deadline:
                        raise DockerError('The service did not report the requested state. Refresh Services and inspect its logs.')
                    time.sleep(.2)
            return self.details(name)

    def _control_exec(self, container, *command, input=None, timeout=None):
        # Every executable in a root-controlled workload may have been replaced.
        # Never collect its output without a bound, including secret stdin writes.
        flags = ["exec", "-i"] if input is not None else ["exec"]
        options = {'timeout': timeout} if timeout is not None else {}
        # Custom commands belong to interactive/agent shells. Internal copying
        # and credential delivery must use the container's system utilities.
        return self.docker.run(*flags, "--env", f"PATH={CONTROL_PATH}", container,
                               *command, input=input, max_output=131072, **options)

    def _access_token(self, name, rotate=False):
        """Return the host-issued token for published Files and noVNC services."""
        state = self.state(name)
        token = state.get("access_token")
        if rotate or not isinstance(token, str) or not ACCESS_TOKEN.fullmatch(token):
            token = secrets.token_urlsafe(32)
            state["access_token"] = token
            self.save(name, state)
        return token

    def _oob_key(self, settings):
        path = self.config.secrets(settings).get("OOB_SSH_KEY")
        if not path:
            return None
        key = Path(path)
        if not key.is_file() or key.is_symlink():
            raise DockerError(f"oob.ssh_key_path must point to a regular file: {key}")
        try:
            return key.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise DockerError(f"Cannot read the SSH private key at {key}.") from None

    def _copy_runtime_commands(self, container):
        # Ship the managed launchers with their credential reader, including to
        # containers created from older images. Standard PATH links also cover
        # login shells, sudo's secure_path, and existing tmux sessions.
        source = Path(__file__).parent / "runtime"
        with tempfile.TemporaryDirectory(prefix="acf-commands-") as temporary:
            bundle = Path(temporary)
            bundle.chmod(0o755)
            for relative in ("bin/acf-agent", "bin/acf-gmail", "bin/glm", "bin/openrouter",
                             "bin/acf-boot", "bin/acf-browser", "browser.py", "browser_trust.py",
                             "install_browser_policy.py", "install_codex_config.py", "common.py", "agent_auth.py", "install_commands.py", "install_user_commands.py",
                             *FILE_EXPLORER_ASSETS, *SERVICE_RUNTIME_ASSETS):
                target = bundle / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.parent.chmod(0o755)
                shutil.copyfile(source / relative, target)
                target.chmod(0o755 if relative.startswith("bin/") else 0o644)
            self.docker.run("cp", str(bundle) + "/.", f"{container}:/opt/acf")

    def _copy_service_settings(self, container, settings):
        # This non-secret file is present before first boot and survives stops.
        # Sync changes the next-boot defaults without toggling running services.
        with tempfile.TemporaryDirectory(prefix='acf-service-settings-') as temporary:
            path = Path(temporary) / 'service-settings.json'
            path.write_text(json.dumps({'desktop': settings.get('services', {}).get('desktop', False)}) + '\n')
            path.chmod(0o644)
            self.docker.run('cp', str(path), f'{container}:/opt/acf/service-settings.json')

    def _sync_runtime_commands(self, container):
        source = Path(__file__).parent / "runtime"
        hashes = {relative: hashlib.sha256((source / relative).read_bytes()).hexdigest()
                  for relative in FILE_EXPLORER_ASSETS}
        result = self._control_exec(container, "/usr/bin/python3", "-c", FILE_EXPLORER_PROBE,
                                    *FILE_EXPLORER_ASSETS)
        try:
            previous = json.loads(result.stdout)
        except (ValueError, TypeError):
            previous = {}
        self._copy_runtime_commands(container)
        self._control_exec(container, "/usr/bin/python3", "/opt/acf/install_commands.py")
        self._control_exec(container, "/usr/bin/python3", "/opt/acf/install_codex_config.py")
        self._control_exec(container, "/usr/bin/python3", "/opt/acf/install_novnc.py")
        self._control_exec(container, "/usr/bin/python3", "/opt/acf/install_browser_policy.py")
        self._control_exec(container, "/usr/bin/python3", "/opt/acf/browser.py", "--refresh-proxy")
        if isinstance(previous, dict) and previous.get("state") == "running" and previous.get("hashes") != hashes:
            # Reload changed assets without interrupting browsers or agents.
            # A service the user stopped stays stopped; unchanged syncs keep
            # active file transfers alive.
            self._control_exec(container, "/usr/bin/python3", "/opt/acf/bin/acf-services", "restart", "files")

    def _sync(self, name):
        self._require_current_code()
        container, guard, _ = self.names(name)
        obj = self.owned("container", container)
        if not obj or not obj["State"]["Running"]:
            raise DockerError(f"Project {name} is not running. Start it before syncing.")
        guard_obj = self.owned("container", guard)
        if not self.guard_matches(obj, guard_obj, guard):
            raise DockerError(f"Refusing to copy credentials: {name} is outside its expected network guard. Choose Apply settings in the dashboard.")
        networks = (guard_obj or {}).get("NetworkSettings", {}).get("Networks", {})
        ip = next((v.get("IPAddress", "") for v in networks.values()), "")
        settings = self.config.project(name)
        oob_key = self._oob_key(settings)
        with tempfile.TemporaryDirectory(prefix="acf-sync-") as temporary:
            staging = Path(temporary) / "project"
            warnings = stage_resources(self.config, name, staging, container_ip=ip)
            auth = self.config.home / "auth"
            for agent in ("codex", "claude"):
                if agent not in settings["integrations"]["enabled"]:
                    continue
                for filename in (("auth.json",) if agent == "codex" else (".credentials.json", "claude.json")):
                    source = auth / agent / filename
                    if source.is_file() and not source.is_symlink():
                        target = staging / "auth" / agent / filename
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                        target.chmod(0o600)
            values = self.config.secrets(settings)
            secret_text = json.dumps({key: value for key, value in values.items() if key != "OOB_SSH_KEY"}) + "\n"
            self._control_exec(container, "mkdir", "-p", "/opt/acf", "/run/acf")
            self._sync_runtime_commands(container)
            self._copy_service_settings(container, settings)
            # Resource destination is a snapshot, so deleting a global skill removes its copy.
            self._control_exec(container, "rm", "-rf", "/opt/acf/project")
            self.docker.run("cp", str(staging), f"{container}:/opt/acf/project")
            self._control_exec(container, "chmod", "755", "/opt/acf/project")
            self._control_exec(container, "/usr/bin/python3", "/opt/acf/agent_auth.py")
            self._control_exec(container, "/usr/bin/python3", "/opt/acf/install_user_commands.py")
            # docker cp does not reliably target live tmpfs mounts. Transfer secret
            # data on stdin, never as a shell argument, into the running namespace.
            self._control_exec(container, "sh", "-c",
                            "set -e; umask 077; cat > /run/acf/secrets.json.tmp; chmod 600 /run/acf/secrets.json.tmp; "
                            "mv -f /run/acf/secrets.json.tmp /run/acf/secrets.json; rm -f /run/acf/secrets.env", input=secret_text)
            # Files reads the plain token; websockify's TokenFile plugin maps it to
            # each local VNC server. Like other secrets, it travels on stdin.
            self._control_exec(container, "sh", "-c",
                            "set -e; umask 077; IFS= read -r token; "
                            "printf '%s\\n' \"$token\" > /run/acf/access-token.tmp; "
                            "printf '%s: 127.0.0.1:5901\\n' \"$token\" > /run/acf/novnc-token.tmp; "
                            "printf '%s: 127.0.0.1:5902\\n' \"$token\" > /run/acf/desktop-token.tmp; "
                            "for name in access novnc desktop; do "
                            "mv -f /run/acf/$name-token.tmp /run/acf/$name-token; done",
                            input=self._access_token(name) + "\n")
            self._control_exec(container, "rm", "-f", "/run/acf/oob_key")
            if oob_key is not None:
                self._control_exec(container, "sh", "-c",
                                "set -e; umask 077; cat > /run/acf/oob_key; chmod 600 /run/acf/oob_key", input=oob_key)
        state = self.state(name)
        if state and any(state["settings"].get(section) != settings[section]
                         for section in ("environment", "network", "ports", "integrations")):
            warnings.append(f"Container settings changed. Choose Apply settings for {name} in the dashboard to apply ports/resources/network settings (recreates containers).")
        return warnings

    def exec(self, name, command, interactive=True):
        container = self.names(name)[0]
        obj = self.owned("container", container)
        if not obj or not obj["State"]["Running"]:
            raise DockerError(f"Project {name} is not running.")
        args = ["exec"]
        if interactive:
            args += ["-i"]
            if sys.stdin.isatty() and sys.stdout.isatty():
                args += ["-t"]
        return self.docker.run(*args, container, *command, capture=not interactive, timeout=None,
                               max_output=None if interactive else 8 * 1024 * 1024)

    def attach(self, name):
        self._announce(self.start(name))
        return self.exec(name, ["tmux", "new-session", "-A", "-s", "shell", "-c", "/workspace"])

    @staticmethod
    def _announce(warnings):
        from .tui import clean
        for warning in warnings or ():
            print("Warning: " + clean(warning), file=sys.stderr)

    def agent(self, name, kind, args=()):
        if kind not in {"codex", "claude", "openrouter", "glm"}:
            raise DockerError("Unknown agent. Choose codex, claude, openrouter or glm.")
        self._announce(self.start(name))
        # tmux accepts a shell command string; quote every user-provided argument.
        command = shlex.join(["/usr/local/bin/acf-agent", kind, *args])
        return self.exec(name, ["tmux", "new-session", "-A", "-s", f"agent-{kind}", "-c", "/workspace", command])

    def list_projects(self):
        directory = self.config.home / "projects"
        names = sorted(path.parent.name for path in directory.glob("*/state.json")) if directory.exists() else []
        if not names:
            return []
        stats = {}
        try:
            output = self.docker.run("stats", "--no-stream", "--format", "{{json .}}",
                                    *[self.names(name)[0] for name in names], check=False, timeout=12).stdout
        except DockerError:
            output = ""
        for line in output.splitlines():
            try:
                row = json.loads(line)
                stats[row.get("Name", "")] = row
            except ValueError:
                continue
        return [self.details(name, stats=stats, live=False) for name in names]

    def docker_host(self):
        """None is local Docker; an empty host means its location is unknown."""
        try:
            endpoint = self.docker.endpoint()
            if not isinstance(endpoint, str):
                return ""
            # Docker also accepts DOCKER_HOST=hostname:2375 and [IPv6]:2375.
            location = urlsplit(endpoint if '://' in endpoint else '//' + endpoint)
            if location.scheme in {"unix", "npipe"}:
                return None
            if location.scheme in {"", "ssh", "tcp", "http", "https"}:
                return location.hostname or ""
        except (DockerError, ValueError):
            pass
        return ""

    def details(self, name, stats=None, live=True):
        state = self.state(name)
        if not state:
            raise DockerError(f"Unknown project {name}.")
        container, guard, _ = self.names(name)
        obj = self.owned("container", container)
        guard_obj = self.owned("container", guard)
        status = obj["State"]["Status"] if obj else "removed"
        ports, ip, published = {}, "", {}
        if status == "running" and not guard_obj:
            status = "guard missing"
        if guard_obj:
            net = guard_obj.get("NetworkSettings", {})
            published = net.get("Ports", {})
            ip = next((value.get("IPAddress", "") for value in net.get("Networks", {}).values()), "")
            for service, internal in SERVICE_PORTS.items():
                bindings = net.get("Ports", {}).get(f"{internal}/tcp")
                if bindings:
                    ports[service] = int(bindings[0]["HostPort"])
            for port, bindings in net.get("Ports", {}).items():
                if bindings and int(port.split('/')[0]) not in SERVICE_PORTS.values():
                    ports[port] = int(bindings[0]["HostPort"])
            if status == "running" and (not guard_obj.get("State", {}).get("Running") or guard_obj.get("State", {}).get("Health", {}).get("Status") != "healthy"):
                status = "guard unhealthy"
        row = (stats or {}).get(container, {})
        data = {"name": name, "status": status, "cpu": row.get("CPUPerc", "—"),
                "memory": row.get("MemUsage", "—"), "storage": "—", "ip": ip,
                "ports": ports, "services": [], "sessions": [], "network": state["settings"]["network"]["mode"]}
        controls, protocol = None, 0
        service_states, listeners = {}, []
        if obj and obj["State"]["Running"]:
            # One bounded exec collects all dashboard details; no host shell evaluates its output.
            # The fallback keeps already-running images inspectable before Apply.
            script = "if test -f /opt/acf/service_report.py; then exec python3 /opt/acf/service_report.py; fi; cat /run/acf/status.json 2>/dev/null || echo '{}'; printf '\\nACF_SESSIONS\\n'; tmux list-sessions -F '#S' 2>/dev/null; printf '\\nACF_STORAGE\\n'; du -sh /workspace /root /home/agent /opt/acf-tools 2>/dev/null"
            try:
                report = self.docker.run("exec", "--env", f"PATH={CONTROL_PATH}", container,
                                         "/bin/sh", "-c", script, check=False, timeout=4, max_output=131072).stdout
                if "\nACF_STORAGE\n" not in report:
                    parsed = json.loads(report)
                    if not isinstance(parsed, dict) or not isinstance(parsed.get('services'), dict):
                        raise ValueError("Invalid service report")
                    service_states = parsed['services']
                    controls = parsed.get('service_controls')
                    protocol = parsed.get('control_protocol', 0)
                    listeners = parsed.get('listeners', [])
                    data['services'] = [f"{key}: {value.get('state', 'unknown') if isinstance(value, dict) else value}" for key, value in service_states.items()]
                    data['sessions'] = parsed.get('sessions', []) if isinstance(parsed.get('sessions'), list) else []
                    data['storage'] = str(parsed.get('storage', '—'))
                    report = ''
                before, _, storage = report.partition("\nACF_STORAGE\n")
                service_text, _, session_text = before.partition("\nACF_SESSIONS\n")
                try:
                    services = json.loads(service_text) if report else service_states
                    if not isinstance(services, dict):
                        raise ValueError("Invalid service status")
                    if report:
                        controls = services.get('service_controls')
                        protocol = services.get('control_protocol', 0)
                    services = services.get("services", services)
                    if not isinstance(services, dict):
                        raise ValueError("Invalid service status")
                    data["services"] = [f"{key}: {value.get('state', 'unknown') if isinstance(value, dict) else value}" for key, value in services.items()]
                    service_states = services
                except ValueError:
                    data["services"] = ["status unavailable"]
                if report:
                    data["sessions"] = [line for line in session_text.splitlines() if line]
                    data["storage"] = ", ".join(line.replace("\t", " ") for line in storage.splitlines()) or "—"
            except (DockerError, ValueError, TypeError):
                data["services"] = ["status unavailable"]
        data['control_protocol'] = protocol if type(protocol) is int and protocol >= 0 else 0
        data['service_controls'] = service_controls(service_states, controls, data['control_protocol'])
        if status == 'running' and data['control_protocol'] < 2:
            data['service_notice'] = ('Browser/Desktop controls need the updated image and service supervisor. '
                                      'Build container images, then Apply settings when you are ready to restart sessions.')
        elif status == 'running' and data['control_protocol'] < 3:
            data['service_notice'] = ('Browser and Desktop accept connections without an access token until '
                                      'this environment restarts. Stop and Start it to require the token.')
        # Display-host edits take effect on refresh. Binding changes still need
        # Apply; actual Docker publications remain the source of truth.
        current_network = self.config.project(name)['network']
        advertise_host = current_network.get('advertise_host', '')
        settings = {**state['settings'], 'network': {**state['settings']['network'], 'advertise_host': advertise_host}}
        data['bind'] = settings['network']['bind']
        data['advertise_host'] = advertise_host
        data['docker_host'] = self.docker_host()
        data['host_ip'] = published_host(current_network['bind'], advertise_host, data['docker_host'])
        token = state.get('access_token')
        data['service_items'] = service_items(service_states, listeners, published, ip, settings, docker_host=data['docker_host'],
                                              access_token=token if isinstance(token, str) and ACCESS_TOKEN.fullmatch(token) else None)
        data['urls'] = service_urls(data)
        return data

    def copy_files(self, source, destination):
        return copy_files(self, source, destination)

    def edit_config(self, name):
        with self.lock(name):
            return edit_config(self.config, name=name)

    def logs(self, name, guard=False):
        container = self.names(name)[1 if guard else 0]
        if not self.owned("container", container):
            raise DockerError("Project container does not exist. Start it in the dashboard first.")
        result = self.docker.run("logs", "--tail", "100", container, max_output=1048576)
        from .tui import clean
        return [clean(line) for line in ((result.stdout or "") + (result.stderr or "")).splitlines()] or ["No log entries yet."]

    def diagnostics(self):
        self.docker.ensure(autostart=False)
        checks = self.doctor()
        lines = ["Docker: ready", f"Configuration: {self.config.path}"]
        lines.extend(f"{check['name']}: " + ("isolation settings OK" if check["ok"] else "; ".join(check["issues"])) for check in checks)
        if not checks:
            lines.append("No deployed environments to inspect.")
        return lines

    def login(self, agent):
        if agent not in {"codex", "claude"}:
            raise DockerError("Login supports codex or claude.")
        self.config.initialize()
        settings = self.config.settings
        self.ensure_images(settings)
        name = f"acf-{self.instance}-login-{agent}-{secrets.token_hex(4)}"
        self.docker.run("create", "--name", name, "--cap-drop", "NET_RAW", "--cap-drop", "MKNOD",
                        "--pids-limit", "256", "--memory", "2g", "--entrypoint", "sleep",
                        settings["environment"]["image"], "infinity")
        try:
            self.docker.run("start", name)
            args = ["exec", "-i"]
            if sys.stdin.isatty():
                args.append("-t")
            command = (["codex", "-c", 'cli_auth_credentials_store="file"', "login", "--device-auth"]
                       if agent == "codex" else ["sudo", "-H", "-u", "agent", "claude", "auth", "login"])
            self.docker.run(*args, name, *command, capture=False, timeout=None)
            destination = self.config.home / "auth" / agent
            destination.mkdir(parents=True, mode=0o700, exist_ok=True)
            sources = ({"/root/.codex/auth.json": "auth.json"} if agent == "codex" else
                       {"/home/agent/.claude/.credentials.json": ".credentials.json", "/home/agent/.claude.json": "claude.json"})
            copied = False
            with tempfile.TemporaryDirectory(prefix="acf-login-") as temporary:
                for source, filename in sources.items():
                    target = Path(temporary) / filename
                    result = self.docker.run("cp", f"{name}:{source}", str(target), check=False)
                    if result.returncode == 0 and target.is_file() and not target.is_symlink():
                        json.loads(target.read_text())
                        target.chmod(0o600)
                        shutil.copyfile(target, destination / filename)
                        (destination / filename).chmod(0o600)
                        copied = True
            if not copied:
                raise DockerError("Login finished without a credential file. Use an API key or retry the CLI login.")
        finally:
            self.docker.run("rm", "-f", name, check=False)
        self._announce(self._sync_saved_login(agent))

    def _sync_saved_login(self, agent):
        """Deliver an explicit login to running, enabled projects only."""
        warnings = []
        for state in sorted((self.config.home / "projects").glob("*/state.json")):
            name = state.parent.name
            try:
                self._deliver_login(name, agent)
            except (DockerError, OSError, ValueError) as exc:
                warnings.append(f"{name}: login saved, but delivery was deferred: {exc} Start or Sync this environment to retry.")
        return warnings

    def _deliver_login(self, name, agent):
        if agent not in {"codex", "claude"}:
            raise DockerError("Login supports codex or claude.")
        with self.lock(name):
            self._require_current_code()
            if agent not in self.config.project(name)["integrations"]["enabled"]:
                return
            container, guard, _ = self.names(name)
            obj = self.owned("container", container)
            if not obj or not obj["State"]["Running"]:
                return
            guard_obj = self.owned("container", guard)
            if not self.guard_matches(obj, guard_obj, guard):
                raise DockerError("Refusing to copy credentials outside the expected network guard.")
            # Only deliver this provider's login. Do not resync project tools,
            # browser services, prompts, or unrelated provider credentials.
            with tempfile.TemporaryDirectory(prefix="acf-login-sync-") as temporary:
                bundle = Path(temporary) / agent
                bundle.mkdir(mode=0o700)
                filenames = ("auth.json",) if agent == "codex" else (".credentials.json", "claude.json")
                for filename in filenames:
                    source = self.config.home / "auth" / agent / filename
                    if source.is_file() and not source.is_symlink():
                        shutil.copy2(source, bundle / filename)
                        (bundle / filename).chmod(0o600)
                if not any(bundle.iterdir()):
                    raise DockerError("The saved login credential file is unavailable.")
                self._control_exec(container, "mkdir", "-p", "/opt/acf/project/auth")
                helper = Path(__file__).parent / "runtime/agent_auth.py"
                self.docker.run("cp", str(helper), f"{container}:/opt/acf/agent_auth.py")
                self._control_exec(container, "rm", "-rf", f"/opt/acf/project/auth/{agent}")
                self.docker.run("cp", str(bundle), f"{container}:/opt/acf/project/auth/{agent}")
                self._control_exec(container, "/usr/bin/python3", "/opt/acf/agent_auth.py", agent)

    def doctor(self):
        checks = []
        for row in self.list_projects():
            name = row["name"]
            container, guard, _ = self.names(name)
            obj = self.owned("container", container)
            if not obj:
                continue
            host = obj["HostConfig"]
            guard_obj = self.owned("container", guard)
            issues = []
            if host.get("Privileged"):
                issues.append("privileged container")
            if not self.guard_matches(obj, guard_obj, guard):
                issues.append("workload is outside its expected guard namespace")
            if any(mount.get("Type") == "bind" for mount in obj.get("Mounts", [])):
                issues.append("unexpected host bind mount")
            dropped = {cap.removeprefix("CAP_") for cap in (host.get("CapDrop") or [])}
            added = {cap.removeprefix("CAP_") for cap in (host.get("CapAdd") or [])}
            if "NET_RAW" not in dropped and "ALL" not in dropped:
                issues.append("NET_RAW not dropped")
            if added & {"NET_ADMIN", "SYS_ADMIN", "ALL"}:
                issues.append("unsafe capability")
            if host.get("RestartPolicy", {}).get("Name") not in ("no", ""):
                issues.append("automatic restart bypasses guard readiness")
            if row["status"].startswith("guard "):
                issues.append(row["status"] + "; stop the project")
            checks.append({"name": name, "ok": not issues, "issues": issues})
        return checks
