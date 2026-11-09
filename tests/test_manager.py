"""Lifecycle and containment invariants, without touching a real Docker daemon."""

import copy
import hashlib
import json
from pathlib import Path
import shlex
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from acf.config import Config, ConfigError
from acf.docker import DockerError
from acf.manager import FILE_EXPLORER_ASSETS, SERVICE_RUNTIME_ASSETS, Manager, VOLUMES, _source_signature


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "checkout"
        self.root.mkdir()
        self.config = Config(root=self.root, home=Path(self.temporary.name) / "state")
        self.docker = Mock()
        self.docker.run.return_value = Mock(returncode=0, stdout="", stderr="")
        self.manager = Manager(config=self.config, docker=self.docker)
        self.settings = self.config.project("demo")
        self.state = {"name": "demo", "settings": self.settings, "settings_version": 2}

    def object(self, kind="container", running=True, health="healthy"):
        labels = {"acf.instance": self.manager.instance, "acf.project": "demo"}
        if kind != "container":
            return {"Labels": labels}
        return {"Config": {"Labels": labels},
                "State": {"Running": running, "Status": "running" if running else "exited",
                          "Health": {"Status": health}},
                "NetworkSettings": {"Networks": {}, "Ports": {}}, "HostConfig": {}}

    def test_project_names_cannot_collide_with_other_project_roles(self):
        all_names = []
        for name in ("demo", "demo-guard", "demo-net", "demo-guard-guard"):
            all_names.extend(self.manager.names(name))
        self.assertEqual(len(all_names), len(set(all_names)), "Project and guard names must use distinct namespaces")

    def test_invalid_project_names_never_reach_docker(self):
        for name in ("../private", "--privileged", "x;touch x", "X", "a/b", "a:tag", "a\nnext"):
            with self.subTest(name=name), self.assertRaises(ConfigError):
                self.manager.start(name)
        self.docker.run.assert_not_called()

    def test_stale_dashboard_cannot_replace_resources_or_restart_containers(self):
        for action in ("create", "start", "sync", "apply", "restart"):
            with self.subTest(action=action), patch("acf.manager._source_signature", return_value=b"updated"):
                with self.assertRaisesRegex(DockerError, r"Quit the dashboard and run ./acf again"):
                    getattr(self.manager, action)("demo")
                self.docker.run.assert_not_called()
                self.docker.inspect.assert_not_called()
                self.assertIsNone(self.manager.state("demo"))

    def test_code_check_does_not_prevent_stopping_an_existing_container(self):
        self.manager.owned = Mock(return_value=self.object())
        with patch("acf.manager._source_signature", return_value=b"updated"):
            self.manager.stop("demo")
        self.assertEqual(self.docker.run.call_count, 2)
        self.assertTrue(all(call.args[0] == "stop" for call in self.docker.run.call_args_list))

    def test_source_signature_tracks_code_changes_but_not_skill_replacements(self):
        code = self.root / "src/acf"
        code.mkdir(parents=True)
        source = code / "resources.py"
        source.write_text("original loader")
        original = _source_signature(code)
        for path in (self.root / "skills/custom/example.skill", self.root / "config.toml",
                     code / "runtime/bin/custom"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("new user resource")
        source.touch()
        self.assertEqual(_source_signature(code), original)
        source.write_text("updated loader")
        self.assertNotEqual(_source_signature(code), original)
        source.unlink()
        self.assertNotEqual(_source_signature(code), original)

    def test_workload_has_only_named_volumes_and_no_host_privileges(self):
        args = self.manager.container_args("demo", self.settings, self.state)
        self.assertNotIn("--privileged", args)
        self.assertNotIn("--cap-add", args)
        self.assertNotIn("--device", args)
        self.assertNotIn("/var/run/docker.sock", " ".join(args))
        self.assertNotIn("unconfined", " ".join(args))
        self.assertEqual(args[args.index("--network") + 1], "container:" + self.manager.names("demo")[1])
        self.assertEqual(args[args.index("--restart") + 1], "no")
        mounts = [args[index + 1] for index, value in enumerate(args) if value == "--mount"]
        self.assertEqual(len(mounts), len(VOLUMES))
        self.assertTrue(all(mount.startswith("type=volume,") for mount in mounts))
        self.assertTrue(all("/Users/" not in mount and "type=bind" not in mount for mount in mounts))
        dropped = [args[index + 1] for index, value in enumerate(args) if value == "--cap-drop"]
        self.assertIn("NET_RAW", dropped)
        self.assertIn("MKNOD", dropped)

    def test_resource_limits_are_applied(self):
        args = self.manager.container_args("demo", self.settings, self.state)
        for flag, expected in (("--cpus", "4"), ("--memory", "8g"), ("--memory-swap", "8g"),
                               ("--pids-limit", "2048"), ("--shm-size", "1g")):
            self.assertEqual(args[args.index(flag) + 1], expected)

    def test_service_actions_validate_before_running_container_commands(self):
        for action, service in (('delete', 'desktop'), ('stop', 'unmanaged'), ('start', 'files;touch x')):
            with self.subTest(action=action, service=service), self.assertRaises(DockerError):
                self.manager.service('demo', action, service)
        self.docker.run.assert_not_called()
        self.docker.inspect.assert_not_called()

    def test_service_control_requires_running_owned_guarded_environment(self):
        self.manager.owned = Mock(return_value=self.object(running=False))
        with self.assertRaisesRegex(DockerError, 'not running'):
            self.manager.service('demo', 'start', 'desktop')
        self.docker.run.assert_not_called()
        self.manager.owned = Mock(return_value=self.object())
        with patch.object(self.manager, 'guard_matches', return_value=False):
            with self.assertRaisesRegex(DockerError, 'network guard'):
                self.manager.service('demo', 'stop', 'files')
        self.docker.run.assert_not_called()

    def test_service_control_targets_only_selected_group_and_preserves_defaults(self):
        self.manager.save('demo', self.state)
        original = self.manager.state_path('demo').read_bytes()
        self.manager.owned = Mock(return_value=self.object())
        before = {'control_protocol': 2, 'ports': {'desktop': 49000},
                  'service_controls': [{'name': 'desktop', 'state': 'stopped'}]}
        after = {'control_protocol': 2, 'service_controls': [{'name': 'desktop', 'state': 'running'}]}
        with patch.object(self.manager, 'guard_matches', return_value=True), \
                patch.object(self.manager, 'details', side_effect=[before, after]):
            self.assertEqual(self.manager.service('demo', 'start', 'desktop'), after)
        self.docker.run.assert_called_once_with('exec', '--env', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
                                               self.manager.names('demo')[0], '/usr/bin/python3',
                                               '/opt/acf/bin/acf-services', 'start', 'desktop',
                                               input=None, max_output=131072, timeout=120)
        self.assertEqual(self.manager.state_path('demo').read_bytes(), original)

    def test_unavailable_services_and_missing_desktop_publication_require_explicit_upgrade(self):
        self.manager.owned = Mock(return_value=self.object())
        for details in ({'service_controls': []},
                        {'service_controls': [{'name': 'desktop', 'state': 'stopped'}], 'ports': {}}):
            with self.subTest(details=details), patch.object(self.manager, 'guard_matches', return_value=True), \
                    patch.object(self.manager, 'details', return_value=details):
                with self.assertRaisesRegex(DockerError, 'Apply settings'):
                    self.manager.service('demo', 'start', 'desktop')
        self.docker.run.assert_not_called()

    def test_service_defaults_bundle_contains_only_autostart_and_is_not_a_live_toggle(self):
        self.settings['services']['desktop'] = True
        self.settings['credentials']['OPENAI_API_KEY'] = 'fixture-private-key'
        captured = []
        def run(*args, **kwargs):
            self.assertEqual(args[0], 'cp')
            path = Path(args[1])
            captured.append(json.loads(path.read_text()))
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(args[2], 'container:/opt/acf/service-settings.json')
        self.docker.run.side_effect = run
        self.manager._copy_service_settings('container', self.settings)
        self.assertEqual(captured, [{'desktop': True}])
        self.assertNotIn('fixture-private-key', str(self.docker.run.call_args_list))

    def test_legacy_service_credentials_are_not_forwarded_or_reported(self):
        self.state.update(file_token='obsolete-files-secret', vnc_password='obsolete-vnc-secret')
        self.manager.save('demo', self.state)
        self.manager.owned = Mock(return_value=self.object())
        args = self.manager.container_args('demo', self.settings, self.state)
        for removed in ('ACF_FILE_TOKEN', 'ACF_VNC_PASSWORD', 'obsolete-files-secret', 'obsolete-vnc-secret'):
            self.assertNotIn(removed, ' '.join(args))
        details = self.manager.details('demo')
        self.assertNotIn('file_token', details)
        self.assertNotIn('vnc_password', details)

    def test_guard_is_only_network_admin_and_ports_default_to_host_interfaces(self):
        args = self.manager.guard_args("demo", self.settings, "network", "172.30.0.0/24", "172.30.0.1")
        self.assertIn("--read-only", args)
        self.assertIn("no-new-privileges", args)
        self.assertNotIn("--privileged", args)
        self.assertNotIn("--mount", args)
        capabilities = [args[index + 1] for index, value in enumerate(args) if value == "--cap-add"]
        self.assertEqual(set(capabilities), {"NET_ADMIN", "SETUID", "SETGID"})
        ports = [args[index + 1] for index, value in enumerate(args) if value == "-p"]
        self.assertTrue(ports)
        self.assertTrue(all(port.startswith("0.0.0.0:") for port in ports))
        self.assertIn("net.ipv6.conf.all.disable_ipv6=1", args)

    def test_ownership_check_prevents_other_installation_mutations(self):
        foreign = self.object()
        foreign["Config"]["Labels"]["acf.instance"] = "someone-else"
        self.docker.inspect.return_value = foreign
        with self.assertRaisesRegex(DockerError, "not owned"):
            self.manager.remove("demo")
        self.docker.run.assert_not_called()

    def test_ownership_requires_project_and_role_not_only_instance(self):
        container, guard, network = self.manager.names("demo")
        cases = [("container", container, "environment"), ("container", guard, "guard"),
                 ("network", network, "network"), ("volume", container + ".workspace", "storage")]
        for kind, name, role in cases:
            for key, wrong in (("acf.project", "another-project"), ("acf.role", "another-role")):
                with self.subTest(kind=kind, role=role, mismatch=key):
                    obj = self.object(kind)
                    labels = obj["Config"]["Labels"] if kind == "container" else obj["Labels"]
                    labels["acf.role"] = role
                    labels[key] = wrong
                    self.docker.inspect.return_value = obj
                    with self.assertRaisesRegex(DockerError, "project or role"):
                        self.manager.owned(kind, name)
        self.docker.run.assert_not_called()

    def test_correct_instance_project_and_role_are_accepted(self):
        container, guard, network = self.manager.names("demo")
        for kind, name, role in (("container", container, "environment"), ("container", guard, "guard"),
                                 ("network", network, "network"), ("volume", container + ".workspace", "storage")):
            with self.subTest(kind=kind, role=role):
                obj = self.object(kind)
                labels = obj["Config"]["Labels"] if kind == "container" else obj["Labels"]
                labels["acf.role"] = role
                self.docker.inspect.return_value = obj
                self.assertEqual(self.manager.owned(kind, name), obj)

    def setup_start(self):
        self.manager.save("demo", self.state)
        self.manager.ensure_images = Mock()
        self.manager._network = Mock(return_value=("network", "172.30.0.0/24", "172.30.0.1"))
        self.manager.owned = Mock(return_value=None)
        self.manager._sync = Mock()

    def test_guard_health_is_verified_before_workload_creation_or_start(self):
        self.setup_start()
        events = []
        self.docker.run.side_effect = lambda *args, **kwargs: events.append(args)
        self.manager._guard_ready = lambda guard: events.append(("guard-ready", guard))
        self.manager.start("demo")
        container, guard, _ = self.manager.names("demo")
        ready = events.index(("guard-ready", guard))
        created = next(i for i, args in enumerate(events) if args[:3] == ("create", "--name", container))
        self.assertLess(events.index(("start", guard)), ready)
        self.assertLess(ready, created)
        self.assertLess(ready, events.index(("start", container)))
        runtime_copy = next(i for i, args in enumerate(events)
                            if args[0] == 'cp' and args[-1] == f'{container}:/opt/acf')
        self.assertLess(created, runtime_copy)
        self.assertLess(runtime_copy, events.index(("start", container)))

    def test_failed_guard_never_starts_workload(self):
        self.setup_start()
        self.manager._guard_ready = Mock(side_effect=DockerError("policy installation failed"))
        with self.assertRaisesRegex(DockerError, "policy installation"):
            self.manager.start("demo")
        container = self.manager.names("demo")[0]
        for call in self.docker.run.call_args_list:
            self.assertNotEqual(call.args[:3], ("create", "--name", container))
            self.assertNotEqual(call.args, ("start", container))
        self.manager._sync.assert_not_called()

    def test_running_workload_is_stopped_when_guard_is_gone(self):
        self.setup_start()
        container, guard, _ = self.manager.names("demo")
        self.manager.owned.side_effect = lambda kind, name: self.object() if name == container else None
        with self.assertRaisesRegex(DockerError, "guard was removed"):
            self.manager.start("demo")
        self.docker.run.assert_called_once_with("stop", container)

    def test_start_rejects_wrong_guard_before_resource_or_secret_sync(self):
        self.setup_start()
        container, guard, _ = self.manager.names("demo")
        workload = self.object()
        workload["HostConfig"]["NetworkMode"] = "container:some-other-guard"
        guard_object = self.object()
        guard_object["Id"] = "a" * 64
        self.manager.owned.side_effect = lambda kind, name: workload if name == container else guard_object
        self.manager._guard_ready = Mock()
        with self.assertRaisesRegex(DockerError, "expected network guard"):
            self.manager.start("demo")
        self.docker.run.assert_called_once_with("stop", container)
        self.manager._sync.assert_not_called()
        self.manager._guard_ready.assert_not_called()
        self.manager._network.assert_not_called()

    def test_guard_match_requires_exact_project_guard_name_or_id(self):
        guard_name = self.manager.names("demo")[1]
        guard = {"Id": "a" * 64}
        for reference in (guard_name, guard["Id"]):
            with self.subTest(reference=reference):
                self.assertTrue(self.manager.guard_matches({"HostConfig": {"NetworkMode": "container:" + reference}}, guard, guard_name))
        for mode in ("host", "bridge", "container:unrelated", "container:" + "a" * 12):
            with self.subTest(mode=mode):
                self.assertFalse(self.manager.guard_matches({"HostConfig": {"NetworkMode": mode}}, guard, guard_name))

    def test_sync_does_not_deliver_credentials_to_wrong_namespace(self):
        container, guard, _ = self.manager.names("demo")
        workload = self.object()
        workload["HostConfig"]["NetworkMode"] = "container:some-other-guard"
        guard_object = self.object()
        guard_object["Id"] = "a" * 64
        self.manager.owned = Mock(side_effect=lambda kind, name: workload if name == container else guard_object)
        with self.assertRaisesRegex(DockerError, "Refusing to copy credentials"):
            self.manager.sync("demo")
        self.docker.run.assert_not_called()

    def test_stale_health_status_does_not_admit_exited_guard(self):
        self.manager.owned = Mock(return_value=self.object(running=False, health="healthy"))
        with self.assertRaisesRegex(DockerError, "guard failed"):
            self.manager._guard_ready("guard")

    def test_remove_preserves_volumes_and_project_state(self):
        self.manager.save("demo", self.state)
        self.manager.owned = Mock(side_effect=lambda kind, name: self.object(kind))
        self.manager.remove("demo")
        self.assertTrue(self.manager.state_path("demo").exists())
        for call in self.docker.run.call_args_list:
            self.assertNotEqual(call.args[:2], ("volume", "rm"))
            self.assertNotIn("--volumes", call.args)

    def test_explicit_purge_deletes_only_managed_state_and_volumes(self):
        self.manager.save("demo", self.state)
        prompt = self.config.project_dir("demo") / "AGENTS.md"
        prompt.write_text("Keep my instructions")
        self.manager.owned = Mock(side_effect=lambda kind, name: self.object(kind))
        self.manager.remove("demo", purge=True)
        self.assertFalse(self.manager.state_path("demo").exists())
        self.assertEqual(prompt.read_text(), "Keep my instructions")
        removed = [call.args[2] for call in self.docker.run.call_args_list if call.args[:2] == ("volume", "rm")]
        self.assertEqual(len(removed), len(VOLUMES))

    def test_state_file_is_private_and_can_be_recovered_after_partial_create(self):
        self.manager.ensure_images = Mock()
        self.manager._start = Mock(side_effect=DockerError("network creation failed"))
        with self.assertRaises(DockerError):
            self.manager.create("demo")
        path = self.manager.state_path("demo")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text())["name"], "demo")

    def test_create_snapshots_wizard_settings_without_rewriting_defaults(self):
        self.config.initialize()
        before = self.config.path.read_bytes()
        proposed = self.config.reload()
        proposed['environment']['cpus'] = 2
        proposed['credentials']['OPENAI_API_KEY'] = 'private-test-key'
        self.manager.ensure_images = Mock()
        self.manager._start = Mock(return_value=[])
        self.manager.create('demo', settings=proposed)
        proposed['environment']['cpus'] = 7
        state = self.manager.state('demo')
        self.assertEqual(state['settings']['environment']['cpus'], 2)
        self.assertEqual(state['settings']['credentials']['OPENAI_API_KEY'], 'private-test-key')
        self.assertEqual(self.config.path.read_bytes(), before)
        self.assertNotIn('private-test-key', ' '.join(self.manager.container_args('demo', state['settings'], state)))
        self.assertFalse((self.config.project_dir('demo') / 'config.toml').exists())

    def test_invalid_wizard_settings_fail_before_docker_or_state_changes(self):
        proposed = copy.deepcopy(self.settings)
        proposed['environment']['cpus'] = -1
        self.manager.ensure_images = Mock()
        with self.assertRaises(ConfigError):
            self.manager.create('demo', settings=proposed)
        self.manager.ensure_images.assert_not_called()
        self.assertFalse(self.manager.state_path('demo').exists())

    def test_apply_uses_container_edits_and_clears_pending_without_changing_defaults(self):
        self.config.initialize()
        self.manager.save('demo', self.state)
        before = self.config.path.read_bytes()
        proposed = self.config.project('demo')
        proposed['environment']['cpus'] = 2
        self.config.save_project_settings('demo', proposed)
        self.manager.ensure_images = Mock()
        self.manager._remove = Mock()
        self.manager._start = Mock(return_value=[])
        self.manager.apply('demo')
        self.assertNotIn('pending_settings', self.manager.state('demo'))
        self.assertEqual(self.manager.state('demo')['settings']['environment']['cpus'], 2)
        self.assertEqual(self.config.path.read_bytes(), before)

    def test_sync_delivers_only_allowed_json_credentials_and_explicit_key_via_stdin(self):
        self.config.initialize()
        key = self.root / 'oob key'
        key.write_text('test-only-private-key\n')
        self.state['settings']['credentials']['OPENAI_API_KEY'] = 'literal-$HOME-`command`'
        self.state['settings']['credentials']['GMAIL_ACCESS_TOKEN'] = 'disabled-token'
        self.state['settings']['oob']['ssh_key_path'] = str(key)
        self.manager.save('demo', self.state)
        container, guard, _ = self.manager.names('demo')
        workload, guard_object = self.object(), self.object()
        workload['HostConfig']['NetworkMode'] = 'container:' + guard
        self.manager.owned = Mock(side_effect=lambda kind, name: workload if name == container else guard_object)
        self.assertEqual(self.manager.sync('demo'), [])
        calls = self.docker.run.call_args_list
        json_write = next(call for call in calls if 'cat > /run/acf/secrets.json' in str(call.args))
        self.assertEqual(json.loads(json_write.kwargs['input']), {'OPENAI_API_KEY': 'literal-$HOME-`command`'})
        key_write = next(call for call in calls if 'cat > /run/acf/oob_key' in str(call.args))
        self.assertEqual(key_write.kwargs['input'], key.read_text())
        for call in calls:
            self.assertNotIn('literal-$HOME-`command`', str(call.args))
            self.assertNotIn('test-only-private-key', str(call.args))
        self.assertIn('chmod 600', json_write.args[-1])
        self.assertIn('chmod 600', key_write.args[-1])

    def running_project(self, save=True):
        """A running workload in its expected guard namespace."""
        self.config.initialize()
        if save:
            self.manager.save('demo', self.state)
        container, guard, _ = self.manager.names('demo')
        workload, guard_object = self.object(), self.object()
        workload['HostConfig']['NetworkMode'] = 'container:' + guard
        self.manager.owned = Mock(side_effect=lambda kind, name: workload if name == container else guard_object)
        return guard_object

    def test_access_token_rotates_on_each_boot_but_not_when_attaching(self):
        self.setup_start()
        self.manager._guard_ready = Mock()
        self.manager.start('demo')
        first = self.manager.state('demo')['access_token']
        self.assertRegex(first, r'^[A-Za-z0-9_-]{43}$')
        self.manager.start('demo')
        second = self.manager.state('demo')['access_token']
        self.assertNotEqual(second, first)
        # Attaching to a running environment must not break open browser tabs.
        self.running_project(save=False)
        self.manager.start('demo')
        self.assertEqual(self.manager.state('demo')['access_token'], second)

    def test_sync_delivers_access_token_on_stdin_to_files_and_vnc_proxies(self):
        self.running_project()
        self.manager.sync('demo')
        token = self.manager.state('demo')['access_token']
        self.assertRegex(token, r'^[A-Za-z0-9_-]{43}$')
        calls = self.docker.run.call_args_list
        write = next(call for call in calls if '/run/acf/access-token' in str(call.args))
        self.assertEqual(write.kwargs['input'], token + '\n')
        script = write.args[-1]
        self.assertIn('umask 077', script)
        self.assertIn("'%s: 127.0.0.1:5901\\n'", script)
        self.assertIn("'%s: 127.0.0.1:5902\\n'", script)
        for name in ('access', 'novnc', 'desktop'):
            self.assertIn(f'/run/acf/{name}-token', script.replace('$name', name))
        for call in calls:
            self.assertNotIn(token, str(call.args))
        # A repeated Sync keeps the token the running services already accept.
        self.manager.sync('demo')
        self.assertEqual(self.manager.state('demo')['access_token'], token)

    def test_dashboard_urls_carry_the_access_token_and_old_supervisors_are_flagged(self):
        token = 'fixture-access-token-0123456789abcdefABCDEF'
        self.state['access_token'] = token
        guard = self.running_project()
        guard['NetworkSettings']['Ports'] = {'8081/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '45000'}],
                                             '6080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '45001'}]}
        report = {'services': {'files': {'state': 'running'}, 'novnc': {'state': 'running'}}, 'control_protocol': 2}
        self.docker.run.return_value = Mock(returncode=0, stdout=json.dumps(report), stderr='')
        details = self.manager.details('demo')
        self.assertEqual(details['urls']['files'], f'http://127.0.0.1:45000/{token}/')
        self.assertTrue(details['urls']['novnc'].endswith('path=websockify%3Ftoken%3D' + token))
        self.assertIn('without an access token until this environment restarts', details['service_notice'])
        report['control_protocol'] = 3
        self.docker.run.return_value = Mock(returncode=0, stdout=json.dumps(report), stderr='')
        self.assertNotIn('service_notice', self.manager.details('demo'))
        # A malformed saved token is never shown as part of a link.
        state = self.manager.state('demo')
        state['access_token'] = 'short'
        self.manager.save('demo', state)
        self.assertEqual(self.manager.details('demo')['urls']['files'], 'http://127.0.0.1:45000/')

    def test_missing_ssh_key_does_not_recreate_existing_container(self):
        self.config.initialize()
        self.manager.save('demo', self.state)
        desired = self.config.project('demo')
        desired['oob']['ssh_key_path'] = str(self.root / 'missing-key')
        self.config.save_project_settings('demo', desired)
        self.manager._remove = Mock()
        self.manager.ensure_images = Mock()
        with self.assertRaisesRegex(DockerError, 'ssh_key_path'):
            self.manager.apply('demo')
        self.manager._remove.assert_not_called()
        self.manager.ensure_images.assert_not_called()

    def test_launchers_are_reinstalled_before_json_delivery_with_literal_private_payload(self):
        self.manager.save('demo', self.state)
        container, guard, _ = self.manager.names('demo')
        workload, guard_object = self.object(), self.object()
        workload['HostConfig']['NetworkMode'] = 'container:' + guard
        self.manager.owned = Mock(side_effect=lambda kind, name: workload if name == container else guard_object)
        bundles = []
        def result(*args, **kwargs):
            if args[0] == 'cp' and args[-1] == f'{container}:/opt/acf':
                bundle = Path(args[1])
                bundles.append({str(path.relative_to(bundle)) for path in bundle.rglob('*') if path.is_file()})
            return Mock(returncode=0, stdout='', stderr='')
        self.docker.run.side_effect = result
        self.manager.sync('demo')
        self.assertEqual(bundles, [{'bin/acf-agent', 'bin/acf-gmail', 'bin/glm', 'bin/openrouter',
                                   'bin/acf-boot', 'bin/acf-browser', 'browser.py', 'browser_trust.py',
                                   'install_browser_policy.py', 'install_codex_config.py',
                                   'common.py', 'agent_auth.py', 'install_commands.py', 'install_user_commands.py',
                                   *FILE_EXPLORER_ASSETS, *SERVICE_RUNTIME_ASSETS}])
        calls = self.docker.run.call_args_list
        install = next(i for i, call in enumerate(calls) if call.args[-1] == '/opt/acf/install_commands.py')
        transfer = next(i for i, call in enumerate(calls) if 'cat > /run/acf/secrets.json' in str(call.args))
        self.assertLess(install, transfer)
        codex_config = next(i for i, call in enumerate(calls)
                            if call.args[-1] == '/opt/acf/install_codex_config.py')
        self.assertLess(install, codex_config)
        self.assertLess(codex_config, transfer)
        policy_install = next(i for i, call in enumerate(calls)
                              if call.args[-1] == '/opt/acf/install_browser_policy.py')
        proxy_refresh = next(i for i, call in enumerate(calls) if call.args[-1] == '--refresh-proxy')
        self.assertLess(install, policy_install)
        self.assertLess(policy_install, proxy_refresh)
        custom_install = next(i for i, call in enumerate(calls)
                              if call.args[-1] == '/opt/acf/install_user_commands.py')
        resources_copy = next(i for i, call in enumerate(calls)
                              if call.args[-1] == f'{container}:/opt/acf/project')
        self.assertLess(install, resources_copy)
        self.assertLess(resources_copy, custom_install)
        auth_install = next(i for i, call in enumerate(calls)
                            if call.args[-1] == '/opt/acf/agent_auth.py')
        self.assertLess(resources_copy, auth_install)
        for call in calls:
            if call.args[0] == 'exec':
                self.assertIn('--env', call.args)
                self.assertIn('PATH=/usr/sbin:/usr/bin:/sbin:/bin', call.args)

    def test_file_explorer_updates_reload_only_a_changed_running_service(self):
        runtime = Path(__file__).resolve().parents[1] / 'src/acf/runtime'
        hashes = {relative: hashlib.sha256((runtime / relative).read_bytes()).hexdigest()
                  for relative in FILE_EXPLORER_ASSETS}
        for changed, state in ((True, 'running'), (False, 'running'), (True, 'stopped')):
            with self.subTest(changed=changed, state=state):
                self.manager._copy_runtime_commands = Mock()
                self.manager._control_exec = Mock(return_value=Mock(stdout=json.dumps({
                    'state': state, 'hashes': {} if changed else hashes})))
                self.manager._sync_runtime_commands('container')
                self.manager._copy_runtime_commands.assert_called_once_with('container')
                restarts = [call.args for call in self.manager._control_exec.call_args_list
                            if '/opt/acf/bin/acf-services' in call.args]
                self.assertEqual(restarts, [('container', '/usr/bin/python3', '/opt/acf/bin/acf-services',
                                             'restart', 'files')] if changed and state == 'running' else [])

    def test_concurrent_project_mutations_are_rejected(self):
        with self.manager.lock("demo"):
            with self.assertRaisesRegex(DockerError, "operation is in progress"):
                self.manager.stop("demo")
        self.docker.run.assert_not_called()

    def test_agent_arguments_are_not_shell_interpolated(self):
        self.manager.start = Mock(return_value=[])
        self.manager.exec = Mock()
        arguments = ["$(touch /tmp/pwned)", "a; echo injection", "a'b", "two words"]
        self.manager.agent("demo", "codex", arguments)
        command = self.manager.exec.call_args.args[1][-1]
        self.assertEqual(shlex.split(command), ["/usr/local/bin/acf-agent", "codex", *arguments])

    def test_new_login_is_delivered_only_to_the_requested_enabled_provider(self):
        self.manager.save('demo', self.state)
        auth = self.config.home / 'auth/codex/auth.json'
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text('{"fixture": "codex-login"}')
        container, guard, _ = self.manager.names('demo')
        workload = self.object()
        workload['HostConfig']['NetworkMode'] = 'container:' + guard
        self.manager.owned = Mock(side_effect=lambda kind, name: workload if name == container else self.object())
        bundles = []
        def run(*args, **kwargs):
            if args[0] == 'cp' and args[-1] == f'{container}:/opt/acf/project/auth/codex':
                path = Path(args[1])
                bundles.append([p.name for p in path.iterdir()])
                self.assertEqual((path / 'auth.json').stat().st_mode & 0o777, 0o600)
            return Mock(returncode=0, stdout='', stderr='')
        self.docker.run.side_effect = run
        self.assertEqual(self.manager._sync_saved_login('codex'), [])
        self.assertEqual(bundles, [['auth.json']])
        self.assertTrue(any(call.args[-2:] == ('/opt/acf/agent_auth.py', 'codex')
                            for call in self.docker.run.call_args_list))
        self.assertFalse(any('restart' in call.args or 'claude' in str(call.args)
                             for call in self.docker.run.call_args_list))
        self.assertNotIn('codex-login', str(self.docker.run.call_args_list))

    def test_new_login_skips_stopped_and_disabled_environments(self):
        self.manager.save('demo', self.state)
        self.manager.owned = Mock(return_value=self.object(running=False))
        self.assertEqual(self.manager._sync_saved_login('codex'), [])
        self.docker.run.assert_not_called()
        self.state['settings']['integrations']['enabled'] = ['claude']
        self.manager.save('demo', self.state)
        self.manager.owned.reset_mock()
        self.assertEqual(self.manager._sync_saved_login('codex'), [])
        self.manager.owned.assert_not_called()
        self.docker.run.assert_not_called()

    def test_new_login_preserves_guard_check_before_delivery(self):
        self.manager.save('demo', self.state)
        self.manager.owned = Mock(return_value=self.object())
        warnings = self.manager._sync_saved_login('codex')
        self.assertEqual(len(warnings), 1)
        self.assertIn('expected network guard', warnings[0])
        self.docker.run.assert_not_called()

    def test_login_delivery_failure_does_not_skip_other_environments(self):
        self.manager.save('demo', self.state)
        self.manager.save('other', {**self.state, 'name': 'other'})
        self.manager._deliver_login = Mock(side_effect=[DockerError('Environment is busy'), None])
        warnings = self.manager._sync_saved_login('codex')
        self.assertEqual(self.manager._deliver_login.call_count, 2)
        self.assertEqual(len(warnings), 1)
        self.assertIn('demo: login saved', warnings[0])
        self.assertIn('Sync this environment', warnings[0])

    def test_login_exports_private_credentials_before_delivering_to_running_environments(self):
        self.manager.ensure_images = Mock()
        self.manager._sync_saved_login = Mock(return_value=[])
        def run(*args, **kwargs):
            if args[0] == 'cp':
                Path(args[-1]).write_text('{"fixture": "saved-login"}')
            return Mock(returncode=0, stdout='', stderr='')
        self.docker.run.side_effect = run
        self.manager.login('codex')
        auth = self.config.home / 'auth/codex/auth.json'
        self.assertEqual(json.loads(auth.read_text()), {'fixture': 'saved-login'})
        self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
        self.manager._sync_saved_login.assert_called_once_with('codex')
        self.assertEqual(self.docker.run.call_args.args[:2], ('rm', '-f'))
        self.assertNotIn('saved-login', str(self.docker.run.call_args_list))

    def test_malformed_service_json_does_not_break_dashboard(self):
        self.manager.save("demo", self.state)
        self.manager.owned = Mock(return_value=self.object())
        self.docker.run.return_value = Mock(returncode=0, stdout="[]\n\nACF_STORAGE\n4K\t/workspace\n", stderr="")
        details = self.manager.details("demo")
        self.assertEqual(details["name"], "demo")
        self.assertIn("4K", details["storage"])

    def test_service_addresses_use_remote_docker_host_and_live_public_host_override(self):
        self.manager.save("demo", self.state)
        obj = self.object()
        obj['NetworkSettings']['Ports'] = {'8081/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '45000'}]}
        obj['NetworkSettings']['Networks'] = {'project': {'IPAddress': '172.31.0.2'}}
        self.manager.owned = Mock(return_value=obj)
        self.docker.endpoint.return_value = 'ssh://operator@cloud.example:2222'
        self.docker.run.return_value = Mock(returncode=0, stdout=json.dumps({'services': {'files': {'state': 'running'}}}), stderr='')
        details = self.manager.details('demo')
        self.assertEqual(details['urls']['files'], 'http://cloud.example:45000/')
        self.assertEqual(details['host_ip'], 'cloud.example')
        self.assertEqual(details['ip'], '172.31.0.2')
        desired = self.config.project('demo')
        desired['network']['advertise_host'] = 'public.example'
        self.config.save_project_settings('demo', desired)
        details = self.manager.details('demo')
        self.assertEqual(details['urls']['files'], 'http://public.example:45000/')
        self.assertEqual(details['host_ip'], 'public.example')
        self.assertEqual(self.manager.state('demo')['settings']['network']['advertise_host'], '')
        obj['NetworkSettings']['Ports']['8081/tcp'][0]['HostIp'] = '127.0.0.1'
        self.assertEqual(self.manager.details('demo')['urls']['files'], 'http://127.0.0.1:45000/')

    def test_daemon_location_does_not_assume_local_for_unknown_or_remote_context(self):
        for endpoint, expected in [('unix:///var/run/docker.sock', None),
                                   ('npipe:////./pipe/docker_engine', None),
                                   ('tcp://198.51.100.9:2376', '198.51.100.9'),
                                   ('server.example:2375', 'server.example'),
                                   ('[2001:db8::9]:2375', '2001:db8::9'),
                                   ('ssh://user@[2001:db8::9]:22', '2001:db8::9'),
                                   ('', ''), ('bad://unknown', '')]:
            with self.subTest(endpoint=endpoint):
                self.docker.endpoint.return_value = endpoint
                self.assertEqual(self.manager.docker_host(), expected)

    def test_missing_guard_is_not_reported_as_healthy_running_environment(self):
        self.manager.save("demo", self.state)
        container = self.manager.names("demo")[0]
        self.manager.owned = Mock(side_effect=lambda kind, name: self.object() if name == container else None)
        details = self.manager.details("demo")
        self.assertNotEqual(details["status"], "running")
        self.assertIn("guard", details["status"])

    def test_doctor_rejects_namespace_shared_with_wrong_container(self):
        container, guard, _ = self.manager.names("demo")
        workload = self.object()
        workload["HostConfig"] = {"NetworkMode": "container:unrelated-container", "CapDrop": ["NET_RAW"],
                                  "RestartPolicy": {"Name": "no"}}
        guard_object = self.object()
        guard_object["Id"] = "a" * 64
        self.manager.owned = Mock(side_effect=lambda kind, name: workload if name == container else guard_object)
        self.manager.list_projects = Mock(return_value=[{"name": "demo", "status": "running"}])
        checks = self.manager.doctor()
        self.assertFalse(checks[0]["ok"], "Sharing any container namespace is insufficient: it must be this project's guard")
        self.assertTrue(any("guard" in issue or "namespace" in issue for issue in checks[0]["issues"]))


if __name__ == "__main__":
    unittest.main()
