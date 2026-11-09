"""Dashboard behavior without a terminal or a Docker daemon."""

import contextlib
import copy
import curses
import io
import threading
import time
import unittest
from unittest.mock import Mock, patch

from acf.config import DEFAULTS
from acf.tui import (ACTIONS, HELP, Dashboard, Model, access_lines, clean, run,
                     service_entries, service_lines, summary_lines)
from acf.wizard import review_lines


class Screen:
    def __init__(self, height=24, width=100):
        self.height, self.width = height, width
        self.writes = []

    def getmaxyx(self):
        return self.height, self.width

    def addnstr(self, y, x, text, length, attr):
        if not (0 <= y < self.height and 0 <= x < self.width and length >= 0):
            raise AssertionError("Out-of-bounds draw")
        self.writes.append((y, x, text[:length], attr))

    def erase(self):
        self.writes = []

    def refresh(self):
        pass

    def move(self, y, x):
        pass


def wait_until(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Background operation did not finish")
        time.sleep(0.005)


class TextTests(unittest.TestCase):
    def test_terminal_controls_and_bidi_are_removed(self):
        text = "safe\x1b[31mred\x1b[0m\x1b]52;c;clipboard\x07\u202ereversed\x00\nend"
        self.assertEqual(clean(text), "saferedreversedend")
        self.assertEqual(clean("a\x9b31mb\x9b0m"), "ab")
        self.assertEqual(clean("ok\x1b]8;;https://bad.example\x1b\\link\x1b]8;;\x1b\\"), "oklink")

    def test_details_do_not_dump_secrets(self):
        lines = summary_lines({"name": "test", "status": "running", "token": "SECRET",
                               "credentials": {"api_key": "SECRET"}, "ip": "172.31.0.2", "host_ip": "192.168.1.73",
                               "network": "restricted", "sessions": ["main"],
                               "urls": {"files": "http://127.0.0.1:33000/"}})
        result = "\n".join(lines)
        self.assertNotIn("SECRET", result)
        self.assertIn("Files (running) http://127.0.0.1:33000/", result)
        self.assertIn("Sandbox IP: 192.168.1.73", result)
        self.assertNotIn("172.31.0.2", result)
        self.assertIn("Sandbox IP: —", summary_lines({"ip": "172.31.0.2"}))
        self.assertNotIn("Network", result)
        self.assertNotIn("Agent sessions", result)

    def test_services_keep_exact_http_urls_and_raw_addresses(self):
        url = "http://127.0.0.1:49152/vnc.html?autoconnect=1&resize=remote"
        project = {"service_items": [
            {"name": "browser", "state": "running", "protocol": "http", "address": url, "scope": "published"},
            {"name": "postgres", "state": "running", "protocol": "tcp", "address": "172.30.0.2:5432", "scope": "internal"},
            {"name": "chromium", "state": "running", "protocol": "http", "address": "http://127.0.0.1:9222/", "scope": "local", "port": 9222},
            {"name": "x11vnc", "state": "running", "protocol": "tcp", "address": "127.0.0.1:5901", "scope": "local", "port": 5901},
            {"name": "worker", "state": "running", "protocol": "", "address": "", "scope": "process"},
        ]}
        lines = service_lines(project)
        self.assertEqual(lines, [f"Browser (running) {url}", "Postgres (running) 172.30.0.2:5432"])

    def test_service_items_override_legacy_and_prioritize_running_published_services(self):
        project = {"services": ["legacy: running"], "service_items": [
            {"name": "worker", "state": "running", "address": "", "scope": "process"},
            {"name": "stopped", "state": "stopped", "address": "http://127.0.0.1:1/", "scope": "published"},
            {"name": "database", "state": "running", "address": "172.30.0.2:5432", "scope": "internal"},
            {"name": "files", "state": "running", "address": "http://127.0.0.1:2/", "scope": "published"},
        ]}
        self.assertEqual([item["name"] for item in service_entries(project)], ["Files", "Database", "Stopped"])
        self.assertNotIn("legacy", "\n".join(service_lines(project)))

    def test_legacy_browser_links_collapse_without_exposing_internal_services(self):
        project = {"status": "running", "urls": {
            "novnc-secondary": "http://127.0.0.1:52283/vnc.html?autoconnect=1&resize=remote",
            "novnc-primary": "http://127.0.0.1:52282/vnc.html?autoconnect=1&resize=remote",
            "browser-primary": "http://127.0.0.1:9222/",
            "files": "http://127.0.0.1:52285/",
        }}
        self.assertEqual(service_lines(project), ["Browser (running) http://127.0.0.1:52282/", "Files (running) http://127.0.0.1:52285/"])
        project["urls"]["novnc"] = "http://127.0.0.1:52286/vnc.html?autoconnect=1&resize=remote"
        self.assertEqual([line for line in service_lines(project) if line.startswith("Browser (running)")],
                         ["Browser (running) http://127.0.0.1:52286/"])

    def test_browser_link_keeps_its_access_token(self):
        url = ("http://127.0.0.1:52282/vnc.html?autoconnect=1&resize=remote"
               "&path=websockify%3Ftoken%3Dfixture-access-token-0123456789abcdefABCDEF")
        project = {"status": "running", "service_items": [
            {"name": "novnc", "state": "running", "protocol": "http", "address": url, "scope": "published"}]}
        self.assertEqual(service_lines(project), [f"Browser (running) {url}"])

    def test_optional_desktop_has_one_address_without_internal_components(self):
        project = {"service_items": [
            {"name": "desktop-http", "state": "stopped", "address": "http://192.168.1.73:49300/", "scope": "published"},
            {"name": "desktop-display", "state": "stopped", "address": "", "scope": "process"},
            {"name": "desktop-session", "state": "stopped", "address": "", "scope": "process"},
            {"name": "desktop-vnc", "state": "stopped", "address": "127.0.0.1:5902", "scope": "local"},
        ]}
        self.assertEqual(service_lines(project), ["Desktop (stopped)"])

    def test_stopped_services_remain_visible_without_addresses_or_bullets(self):
        project = {"status": "running", "service_items": [
            {"name": "caido", "state": "stopped", "address": "", "scope": "published"},
            {"name": "files", "state": "stopped", "address": "", "scope": "published"},
            {"name": "desktop-http", "state": "stopped", "address": "", "scope": "published"},
        ]}
        self.assertEqual(service_lines(project), ["Caido (stopped)", "Files (stopped)", "Desktop (stopped)"])
        summary = summary_lines({**project, "host_ip": "192.168.1.73"})
        start = summary.index("Sandbox IP: 192.168.1.73")
        self.assertEqual(summary[start:], ["Sandbox IP: 192.168.1.73", "", "Services:",
                                          "  Caido (stopped)", "  Files (stopped)", "  Desktop (stopped)"])

    def test_stopped_environment_hides_stale_running_urls_in_stats_and_access(self):
        for status in ("stopped", "exited", "removed"):
            with self.subTest(status=status):
                project = {"status": status,
                           "service_items": [{"name": "files", "state": "running",
                                              "address": "http://192.168.1.73:49000/", "scope": "published"}],
                           "service_controls": [{"name": "files", "state": "running"}]}
                self.assertEqual(service_lines(project), ["Files (stopped)"])
                self.assertNotIn("http://192.168.1.73:49000/", "\n".join(access_lines(project)))

    def test_logical_service_state_wins_over_stale_endpoint_state(self):
        project = {"status": "running",
                   "service_items": [{"name": "desktop-http", "state": "running",
                                      "address": "http://192.168.1.73:49000/", "scope": "published"}],
                   "service_controls": [{"name": "desktop", "state": "stopped"},
                                        {"name": "files", "state": "stopped"}]}
        self.assertCountEqual(service_lines(project), ["Desktop (stopped)", "Files (stopped)"])

    def test_stats_show_binary_service_state_and_only_running_addresses(self):
        for reported, running in (("running", True), ("listening", True), ("ready", True),
                                  ("healthy", True), ("stopped", False), ("failed", False),
                                  ("starting", False), ("unknown", False), ("not listening", False)):
            with self.subTest(reported=reported):
                project = {"service_items": [{"name": "files", "state": reported,
                                              "address": "http://192.168.1.73:49000/", "scope": "published"}]}
                expected = "Files (running) http://192.168.1.73:49000/" if running else "Files (stopped)"
                self.assertEqual(service_lines(project), [expected])


class ModelTests(unittest.TestCase):
    def test_refresh_retains_last_inventory_on_failure(self):
        manager = Mock()
        manager.list_projects.side_effect = [[{"name": "z"}, {"name": "a"}], RuntimeError("Docker unavailable")]
        model = Model(manager)
        model.refresh()
        self.assertEqual([p["name"] for p in model.snapshot()["projects"]], ["a", "z"])
        model.refresh()
        state = model.snapshot()
        self.assertTrue(state["loaded"])
        self.assertEqual(len(state["projects"]), 2)
        self.assertEqual(state["error"], "Docker unavailable")

    def test_lifecycle_operations_are_serialized(self):
        release = threading.Event()
        entered = threading.Event()
        model = Model(Mock())

        def slow_action():
            entered.set()
            release.wait(2)

        try:
            self.assertTrue(model.operate("Starting", slow_action))
            self.assertTrue(entered.wait(1))
            second = Mock()
            self.assertFalse(model.operate("Stopping", second))
            second.assert_not_called()
            self.assertEqual(model.snapshot()["busy"], "Starting")
        finally:
            release.set()
        wait_until(lambda: not model.snapshot()["busy"])
        self.assertTrue(model.wake.is_set())

    def test_action_failure_is_visible_and_releases_busy_state(self):
        model = Model(Mock())
        model.operate("Starting", Mock(side_effect=RuntimeError("bad\x1b[2Jstart")))
        wait_until(lambda: not model.snapshot()["busy"])
        self.assertEqual(model.snapshot()["message"], "Error: badstart")

    def test_snapshot_does_not_expose_mutable_model_state(self):
        model = Model(Mock())
        model.projects = [{"name": "one", "ports": {"files": 12}}]
        model.snapshot()["projects"][0]["ports"]["files"] = 999
        self.assertEqual(model.snapshot()["projects"][0]["ports"]["files"], 12)

    def test_sync_warnings_are_displayed(self):
        model = Model(Mock())
        model.operate("Synchronizing", lambda: ["Unknown placeholder {{MISSING}}", "Container settings changed"])
        wait_until(lambda: not model.snapshot()["busy"])
        title, lines = model.snapshot()["panel"]
        self.assertEqual(title, "Operation notices")
        self.assertIn("Unknown placeholder {{MISSING}}", lines)
        self.assertIn("Container settings changed", lines)

    def test_details_offer_access_panel_without_displaying_credentials(self):
        model = Model(Mock())
        model.operate("Inspecting", lambda: {"name": "demo", "file_token": "SECRET"}, details=True)
        wait_until(lambda: not model.snapshot()["busy"])
        lines = "\n".join(model.snapshot()["panel"][1])
        self.assertIn("press a", lines)
        self.assertNotIn("SECRET", lines)

    def test_service_address_panel_ignores_obsolete_credentials(self):
        project = {"name": "demo", "status": "running", "file_token": "file-secret", "vnc_password": "vnc-secret",
                   "api_key": "hidden-secret", "urls": {"files": "http://127.0.0.1:1234/"}}
        model = Model(Mock())
        model.operate("Access", lambda: project, panel_title="Access", formatter=access_lines)
        wait_until(lambda: not model.snapshot()["busy"])
        lines = "\n".join(model.snapshot()["panel"][1])
        self.assertNotIn("file-secret", lines)
        self.assertNotIn("vnc-secret", lines)
        self.assertIn("http://127.0.0.1:1234/", lines)
        self.assertNotIn("hidden-secret", lines)
        self.assertNotIn("token", lines.lower())
        self.assertNotIn("password", lines.lower())
        self.assertNotIn("private", lines.lower())

    def test_output_panels_sanitize_untrusted_logs(self):
        model = Model(Mock())
        model.operate("Logs", lambda: ["hello\x1b[2J safe"], panel_title="Workload logs")
        wait_until(lambda: not model.snapshot()["busy"])
        self.assertEqual(model.snapshot()["panel"], ("Workload logs", ["hello safe"]))


class DashboardTests(unittest.TestCase):
    def make_dashboard(self, **kwargs):
        manager = Mock()
        manager.config.reload.return_value = copy.deepcopy(DEFAULTS)
        dashboard = Dashboard(Screen(**kwargs), manager)
        dashboard.model.projects = [{"name": "a", "status": "running"}, {"name": "b", "status": "stopped"}]
        dashboard.model.loaded = True
        return dashboard, manager

    def open_services(self, **kwargs):
        dashboard, manager = self.make_dashboard(**kwargs)
        project = {"name": "a", "status": "running", "service_controls": [
            {"name": "caido", "label": "Caido", "state": "running"},
            {"name": "files", "label": "Files", "state": "running"},
            {"name": "browser", "label": "Browser", "state": "running"},
            {"name": "desktop", "label": "Desktop", "state": "stopped"},
        ], "service_items": [{"name": "desktop-http", "address": "http://192.168.1.73:49300/", "scope": "published"}]}
        dashboard.model.projects[0] = copy.deepcopy(project)
        manager.details.return_value = project
        manager.service.return_value = copy.deepcopy(project)
        dashboard.handle("v")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        return dashboard, manager

    def test_services_menu_starts_selected_service_in_its_original_environment(self):
        dashboard, manager = self.open_services()
        dashboard.selected_name = "b"
        for _ in range(3):
            dashboard.handle("j")
        updated = manager.service.return_value
        updated["service_controls"][-1]["state"] = "running"
        dashboard.handle("s")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        manager.service.assert_called_once_with("a", "start", "desktop")
        panel = dashboard.model.snapshot()["service_panel"]
        self.assertEqual(panel["name"], "a")
        self.assertEqual(panel["service_controls"][-1]["state"], "running")
        self.assertEqual(dashboard.control_name, "desktop")
        manager.start.assert_not_called()
        manager.stop.assert_not_called()

    def test_services_menu_supports_explicit_start_stop_and_ignores_redundant_action(self):
        dashboard, manager = self.open_services()
        dashboard.handle("+")
        manager.service.assert_not_called()
        manager.service.return_value["service_controls"][0]["state"] = "stopped"
        dashboard.handle("-")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        manager.service.assert_called_once_with("a", "stop", "caido")
        dashboard.handle("-")
        self.assertEqual(manager.service.call_count, 1)
        manager.service.return_value["service_controls"][0]["state"] = "running"
        dashboard.handle("\n")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        self.assertEqual(manager.service.call_args.args, ("a", "start", "caido"))

    def test_services_controls_require_running_environment_and_known_ready_state(self):
        for status, service_state, busy in (("stopped", "stopped", ""),
                                            ("running", "starting", ""),
                                            ("running", "unknown", ""),
                                            ("running", "running", "Updating")):
            with self.subTest(status=status, service_state=service_state, busy=busy):
                dashboard, manager = self.open_services()
                dashboard.model.service_panel["status"] = status
                dashboard.model.service_panel["service_controls"][0]["state"] = service_state
                dashboard.model.busy = busy
                dashboard.handle("s")
                manager.service.assert_not_called()
                manager.start.assert_not_called()
                manager.stop.assert_not_called()

    def test_services_inventory_refresh_keeps_panel_pinned_and_selection_stable(self):
        dashboard, manager = self.open_services()
        dashboard.handle("j")
        projects = copy.deepcopy(dashboard.model.projects)
        projects[0]["service_controls"][1]["state"] = "stopped"
        manager.list_projects.return_value = list(reversed(projects))
        dashboard.model.refresh()
        dashboard.draw()
        self.assertEqual(dashboard.control_name, "files")
        self.assertIn("Files  stopped", dashboard.model.snapshot()["panel"][1])
        manager.list_projects.return_value = [projects[1]]
        dashboard.model.refresh()
        self.assertEqual(dashboard.model.snapshot()["service_panel"]["name"], "a")
        self.assertEqual(dashboard.model.snapshot()["service_panel"]["status"], "missing")
        dashboard.handle("s")
        manager.service.assert_not_called()

    def test_closing_services_panel_during_operation_does_not_reopen_it(self):
        dashboard, manager = self.open_services()
        entered, release = threading.Event(), threading.Event()
        project = manager.details.return_value
        def stop(*args):
            entered.set()
            release.wait(2)
            return project
        manager.service.side_effect = stop
        try:
            dashboard.handle("s")
            self.assertTrue(entered.wait(1))
            self.assertTrue(dashboard.handle("q"))
            self.assertIsNone(dashboard.model.snapshot()["service_panel"])
        finally:
            release.set()
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        self.assertIsNone(dashboard.model.snapshot()["panel"])

    def test_service_failure_keeps_controls_open_and_reports_error(self):
        dashboard, manager = self.open_services()
        manager.service.side_effect = RuntimeError("Service failed\x1b[2J")
        dashboard.handle("s")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        self.assertEqual(dashboard.model.snapshot()["message"], "Error: Service failed")
        self.assertIsNotNone(dashboard.model.snapshot()["service_panel"])

    def test_unmanaged_endpoints_and_legacy_reports_are_read_only(self):
        dashboard, manager = self.make_dashboard()
        manager.details.return_value = {"name": "a", "status": "running", "api_key": "PRIVATE",
            "service_controls": [{"name": "postgres", "state": "running"}],
            "service_items": [{"name": "postgres", "state": "running", "address": "192.168.1.73:5432", "secret": "PRIVATE"}]}
        dashboard.handle("v")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        dashboard.handle("s")
        manager.service.assert_not_called()
        panel = dashboard.model.snapshot()["panel"]
        self.assertIn("Postgres (running) 192.168.1.73:5432", panel[1])
        self.assertIn("Service controls unavailable", "\n".join(panel[1]))
        self.assertNotIn("PRIVATE", repr(dashboard.model.snapshot()["service_panel"]))

    def test_services_panel_draws_selection_and_scrolls_addresses_on_small_screens(self):
        for height, width in ((10, 40), (24, 80), (40, 150)):
            with self.subTest(size=(height, width)):
                dashboard, _ = self.open_services(height=height, width=width)
                for _ in range(3):
                    dashboard.handle("j")
                dashboard.draw()
                self.assertTrue(any("Desktop  stopped" in line[2] and line[3] == curses.A_REVERSE
                                    for line in dashboard.screen.writes))
                self.assertTrue(any("s toggle" in line[2] and "q close" in line[2]
                                    for line in dashboard.screen.writes))
                dashboard.handle(curses.KEY_NPAGE)
                dashboard.draw()
                self.assertEqual(dashboard.control_name, "desktop")

    def test_selection_follows_name_after_refresh(self):
        dashboard, _ = self.make_dashboard()
        dashboard.handle("j")
        self.assertEqual(dashboard.selected_name, "b")
        dashboard.model.projects.insert(0, {"name": "0"})
        self.assertEqual(dashboard.current(dashboard.model.projects)["name"], "b")
        self.assertEqual(dashboard.selected, 2)

    def test_menu_has_one_delete_option_and_deletes_volumes(self):
        dashboard, manager = self.make_dashboard()
        offered = dashboard.menu_actions(dashboard.model.projects[0])
        self.assertEqual([item[0] for item in offered if item[2] == "purge"], ["d"])
        self.assertFalse(any(item[0] == "P" or item[2] == "remove" for item in offered))
        dashboard.prompt = Mock(return_value="delete a")
        dashboard.handle("m")
        dashboard.handle("d")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        manager.remove.assert_called_once_with("a", purge=True)

    def test_busy_operation_prevents_exit(self):
        dashboard, _ = self.make_dashboard()
        dashboard.model.busy = "Creating a"
        self.assertTrue(dashboard.handle("q"))
        dashboard.model.busy = ""
        self.assertFalse(dashboard.handle("q"))

    def test_help_closes_without_quitting(self):
        dashboard, _ = self.make_dashboard()
        self.assertTrue(dashboard.handle("?"))
        self.assertIsNotNone(dashboard.model.snapshot()["panel"])
        self.assertTrue(dashboard.handle("q"))
        self.assertIsNone(dashboard.model.snapshot()["panel"])

    def test_terminal_sizes_do_not_draw_outside_screen(self):
        for height, width in [(1, 1), (8, 20), (10, 40), (24, 80), (40, 150)]:
            with self.subTest(size=(height, width)):
                dashboard, _ = self.make_dashboard(height=height, width=width)
                dashboard.model.projects[0].update(ip="172.31.0.2", host_ip="192.168.1.73")
                dashboard.draw()
                rendered = "\n".join(write[2] for write in dashboard.screen.writes)
                self.assertNotIn("172.31.0.2", rendered)
                if width >= 108:
                    self.assertIn("HOST IP", rendered)
                    self.assertIn("192.168.1.73", rendered)

    def test_narrow_terminal_still_offers_help_and_quit(self):
        dashboard, _ = self.make_dashboard(height=12, width=42)
        dashboard.draw()
        self.assertTrue(any("q quit" in line[2] for line in dashboard.screen.writes))

    def test_selected_lifecycle_shortcuts_are_visible_even_on_narrow_terminals(self):
        for width in (40, 76, 80, 120):
            with self.subTest(width=width):
                dashboard, _ = self.make_dashboard(width=width)
                for toggle in ("stop", "start"):
                    dashboard.draw()
                    shortcuts = next(row[2] for row in dashboard.screen.writes
                                     if row[0] == dashboard.screen.height - 3)
                    footer = next(row[2] for row in dashboard.screen.writes
                                  if row[0] == dashboard.screen.height - 1)
                    self.assertIn("s " + toggle, shortcuts)
                    self.assertIn("d delete", shortcuts)
                    self.assertIn("Enter terminal", shortcuts)
                    self.assertIn("q quit", footer)
                    dashboard.handle("j")

    def test_start_stop_shortcut_targets_selected_environment(self):
        dashboard, manager = self.make_dashboard()
        dashboard.handle("s")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        manager.stop.assert_called_once_with("a")
        manager.start.assert_not_called()
        dashboard.handle("j")
        dashboard.handle("s")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        manager.start.assert_called_once_with("b")

    def test_delete_shortcut_is_inactive_without_selection_or_during_other_operations(self):
        for state in ("empty", "busy", "panel"):
            with self.subTest(state=state):
                dashboard, manager = self.make_dashboard()
                dashboard.prompt = Mock(return_value="delete a")
                if state == "empty":
                    dashboard.model.projects = []
                elif state == "busy":
                    dashboard.model.busy = "Starting a"
                else:
                    dashboard.model.panel = ("Details", ["Selected environment"])
                dashboard.handle("d")
                dashboard.prompt.assert_not_called()
                manager.remove.assert_not_called()

    def test_agent_keyboard_routes_to_correct_provider(self):
        for key, provider in [("x", "codex"), ("c", "claude"), ("o", "openrouter"), ("g", "glm")]:
            with self.subTest(provider=provider):
                dashboard, manager = self.make_dashboard()
                dashboard.interactive = lambda action: action()
                dashboard.handle(key)
                manager.agent.assert_called_once_with("a", provider)

    def test_every_action_has_unique_shortcut_and_identifier(self):
        self.assertEqual(len(ACTIONS), len({item[0] for item in ACTIONS}))
        self.assertEqual(len(ACTIONS), len({item[2] for item in ACTIONS}))

    def test_actions_menu_supports_navigation_and_shortcuts(self):
        dashboard, manager = self.make_dashboard()
        manager.diagnostics.return_value = ["Docker ready"]
        dashboard.handle("m")
        self.assertTrue(dashboard.menu_open)
        dashboard.handle(curses.KEY_DOWN)
        self.assertEqual(dashboard.menu_index, 1)
        dashboard.handle("D")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        self.assertFalse(dashboard.menu_open)
        manager.diagnostics.assert_called_once_with()
        self.assertEqual(dashboard.model.snapshot()["panel"], ("Diagnostics", ["Docker ready"]))

    def test_global_management_is_available_without_projects(self):
        dashboard, manager = self.make_dashboard()
        dashboard.model.projects = []
        dashboard.interactive = lambda action: action()
        dashboard.handle("m")
        offered = {item[2] for item in dashboard.menu_actions(None)}
        self.assertTrue({"build", "diagnostics", "create"}.issubset(offered))
        self.assertNotIn("purge", offered)
        self.assertNotIn("global-config", offered)
        self.assertNotIn("global-secrets", offered)
        manager.build.return_value = None
        dashboard.handle("b")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        manager.build.assert_called_once_with()

    def test_config_editor_is_scoped_to_the_selected_container(self):
        dashboard, manager = self.make_dashboard()
        dashboard.interactive = lambda operation: operation()
        dashboard.action("project-config", dashboard.model.projects[0])
        manager.edit_config.assert_called_once_with(name="a")
        offered = {item[2] for item in ACTIONS}
        self.assertFalse({"global-config", "checkout-config", "project-secrets", "global-secrets", "checkout-secrets"} & offered)
        self.assertNotIn(".env", HELP + " ".join(item[1] for item in ACTIONS))

    def test_create_defaults_can_skip_wizard_and_snapshot_configuration(self):
        dashboard, manager = self.make_dashboard()
        defaults = manager.config.reload.return_value
        defaults["environment"]["cpus"] = 7
        dashboard.prompt = Mock(side_effect=["demo", "s"])
        dashboard.handle("n")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        manager.create.assert_called_once_with("demo", settings=defaults)
        saved = manager.create.call_args.kwargs["settings"]
        self.assertIsNot(saved, defaults)
        saved["environment"]["cpus"] = 3
        self.assertEqual(defaults["environment"]["cpus"], 7)
        self.assertIsNone(dashboard.model.snapshot()["panel"])

    def test_wizard_keeps_custom_values_after_validation_error_and_back(self):
        dashboard, manager = self.make_dashboard()
        defaults = copy.deepcopy(manager.config.reload.return_value)
        # Change CPUs; retry invalid value; go forward/back; review then create.
        dashboard.prompt = Mock(side_effect=["demo", "1", "nope", "2.5", "", "b", "", "", "", "", "", ""])
        dashboard.action("create", None)
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        self.assertEqual(manager.create.call_args.kwargs["settings"]["environment"]["cpus"], 2.5)
        self.assertEqual(manager.config.reload.return_value, defaults)
        self.assertTrue(any(call.args[:2] == ("CPUs:", "nope") for call in dashboard.prompt.call_args_list))

    def test_skip_after_customizing_restores_config_defaults(self):
        dashboard, manager = self.make_dashboard()
        dashboard.prompt = Mock(side_effect=["demo", "1", "2", "s"])
        dashboard.action("create", None)
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        manager.create.assert_called_once_with("demo", settings=manager.config.reload.return_value)

    def test_cancel_during_setup_or_review_never_creates(self):
        for answers in ([None], ["demo", None], ["demo", "", "", "", "", "", None]):
            with self.subTest(answers=answers):
                dashboard, manager = self.make_dashboard()
                dashboard.prompt = Mock(side_effect=answers)
                dashboard.action("create", None)
                manager.create.assert_not_called()
                self.assertIsNone(dashboard.model.snapshot()["panel"])

    def test_wizard_validates_name_before_setup(self):
        dashboard, manager = self.make_dashboard()
        dashboard.prompt = Mock(side_effect=["", "Bad Name", "a", "demo", "s"])
        dashboard.action("create", None)
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        self.assertEqual(manager.create.call_args.args, ("demo",))

    def test_wizard_api_keys_are_masked_in_prompts_and_review(self):
        dashboard, manager = self.make_dashboard()
        defaults = manager.config.reload.return_value
        defaults.setdefault("credentials", {})["OPENAI_API_KEY"] = "existing-secret"
        shown = []
        answers = iter(["demo", "", "", "", "", "1", "new-secret", "", ""])
        def answer(*args, **kwargs):
            panel = dashboard.model.snapshot()["panel"]
            if panel:
                shown.extend(panel[1])
            if kwargs.get("secret"):
                self.assertEqual(args[1], "")
            return next(answers)
        dashboard.prompt = Mock(side_effect=answer)
        dashboard.action("create", None)
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        settings = manager.create.call_args.kwargs["settings"]
        self.assertEqual(settings["credentials"]["OPENAI_API_KEY"], "new-secret")
        self.assertEqual(defaults["credentials"]["OPENAI_API_KEY"], "existing-secret")
        self.assertNotIn("existing-secret", "\n".join(shown))
        self.assertNotIn("new-secret", "\n".join(shown))
        self.assertIn("configured", "\n".join(review_lines("demo", settings)))
        self.assertTrue(any(call.kwargs.get("secret") for call in dashboard.prompt.call_args_list))

    def test_secret_prompt_hides_typed_text_and_distinguishes_cancel_from_empty(self):
        dashboard, _ = self.make_dashboard()
        frames = []
        keys = iter([*"hidden-value", "\n"])
        dashboard.screen.get_wch = lambda: next(keys)
        dashboard.screen.refresh = lambda: frames.extend(row[2] for row in dashboard.screen.writes)
        with patch("acf.tui.curses.curs_set"):
            self.assertEqual(dashboard.prompt("API key:", secret=True, cancel_value=None), "hidden-value")
            dashboard.screen.get_wch = lambda: "\x1b"
            self.assertIsNone(dashboard.prompt("API key:", secret=True, cancel_value=None))
            dashboard.screen.get_wch = lambda: "\n"
            self.assertEqual(dashboard.prompt("API key:", secret=True, cancel_value=None), "")
        self.assertNotIn("hidden-value", " ".join(frames))
        self.assertIn("*" * len("hidden-value"), " ".join(frames))

    def test_wizard_can_enable_optional_integration_with_its_credentials(self):
        dashboard, manager = self.make_dashboard()
        dashboard.prompt = Mock(side_effect=["demo", "", "", "", "", "6", "gmail-secret",
                                            "10", "codex,gmail", "", ""])
        dashboard.action("create", None)
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        settings = manager.create.call_args.kwargs["settings"]
        self.assertEqual(settings["integrations"]["enabled"], ["codex", "gmail"])
        self.assertEqual(settings["credentials"]["GMAIL_ACCESS_TOKEN"], "gmail-secret")
        self.assertNotIn("gmail", manager.config.reload.return_value["integrations"]["enabled"])

    def test_apply_confirmation_explains_data_lifetime_and_can_cancel(self):
        dashboard, manager = self.make_dashboard()
        shown = []
        def confirm(question):
            shown.extend(dashboard.model.snapshot()["panel"][1])
            return "wrong"
        dashboard.prompt = confirm
        dashboard.action("apply", dashboard.model.projects[0])
        manager.apply.assert_not_called()
        self.assertIn("Running sessions will stop", " ".join(shown))
        self.assertIn("writable OS layer is discarded", " ".join(shown))
        self.assertIn("Named volumes", " ".join(shown))
        dashboard.prompt = Mock(return_value="a")
        dashboard.action("apply", dashboard.model.projects[0])
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        manager.apply.assert_called_once_with("a")

    def test_purge_requires_delete_plus_project_name(self):
        dashboard, manager = self.make_dashboard()
        dashboard.handle("j")
        # A refresh can reorder rows; deletion must still target the selection.
        dashboard.model.projects.insert(0, {"name": "0", "status": "running"})
        for answer in ("b", "delete a", ""):
            dashboard.prompt = Mock(return_value=answer)
            dashboard.handle("d")
            manager.remove.assert_not_called()
        dashboard.prompt = Mock(return_value="delete b")
        dashboard.handle("d")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        manager.remove.assert_called_once_with("b", purge=True)

    def test_import_and_export_keep_project_paths_explicit(self):
        for action, answers, expected in (("import", ["/host/file with spaces", "/workspace/input"],
                                          ("/host/file with spaces", "a:/workspace/input")),
                                         ("export", ["/workspace/output", "/host/result"],
                                          ("a:/workspace/output", "/host/result"))):
            with self.subTest(action=action):
                dashboard, manager = self.make_dashboard()
                dashboard.prompt = Mock(side_effect=answers)
                dashboard.action(action, dashboard.model.projects[0])
                wait_until(lambda: not dashboard.model.snapshot()["busy"])
                manager.copy_files.assert_called_once_with(*expected)

    def test_cancelled_file_copy_does_nothing(self):
        dashboard, manager = self.make_dashboard()
        dashboard.prompt = Mock(side_effect=["/host/source", ""])
        dashboard.action("import", dashboard.model.projects[0])
        manager.copy_files.assert_not_called()
        self.assertIsNone(dashboard.model.snapshot()["panel"])

    def test_log_menu_selects_workload_or_guard(self):
        for action, guard in (("logs", False), ("guard-logs", True)):
            with self.subTest(action=action):
                dashboard, manager = self.make_dashboard()
                manager.logs.return_value = ["ready"]
                dashboard.action(action, dashboard.model.projects[0])
                wait_until(lambda: not dashboard.model.snapshot()["busy"])
                manager.logs.assert_called_once_with("a", guard=guard)

    def test_access_key_shows_addresses_without_obsolete_credentials(self):
        dashboard, manager = self.make_dashboard()
        manager.details.return_value = {"name": "a", "status": "running", "file_token": "file-secret", "vnc_password": "vnc-secret",
                                        "urls": {"files": "http://192.168.1.73:49150/"}}
        dashboard.handle("a")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        title, panel = dashboard.model.snapshot()["panel"]
        self.assertEqual(title, "Service addresses — a")
        self.assertIn("Files (running) http://192.168.1.73:49150/", panel)
        self.assertNotIn("file-secret", "\n".join(panel))
        self.assertNotIn("vnc-secret", "\n".join(panel))
        dashboard.handle("q")
        dashboard.draw()
        self.assertNotIn("file-secret", " ".join(line[2] for line in dashboard.screen.writes))

    def test_long_panel_lines_wrap_for_narrow_terminals(self):
        dashboard, _ = self.make_dashboard(height=24, width=40)
        dashboard.model.panel = ("Access", ["TOKEN" * 10])
        dashboard.draw()
        rendered = [line[2] for line in dashboard.screen.writes if 5 <= line[0] < 8]
        self.assertEqual("".join(rendered), "TOKEN" * 10)

    def test_no_removed_cli_commands_in_help_or_details(self):
        text = HELP + "\n".join(summary_lines({"name": "a"}))
        for command in ("create", "info", "list", "exec", "config", "build", "doctor", "remove"):
            self.assertNotIn("./acf " + command, text)

    def test_main_dashboard_shows_each_network_service_and_full_endpoint(self):
        dashboard, _ = self.make_dashboard(height=24, width=80)
        dashboard.model.projects[0]["service_items"] = [
            {"name": "caido", "state": "running", "address": "http://127.0.0.1:49149/", "scope": "published"},
            {"name": "files", "state": "running", "address": "http://127.0.0.1:49150/", "scope": "published"},
            {"name": "novnc-primary", "state": "running", "address": "http://127.0.0.1:49151/vnc.html?autoconnect=1&resize=remote", "scope": "published"},
            {"name": "novnc-secondary", "state": "running", "address": "http://127.0.0.1:49152/vnc.html?autoconnect=1&resize=remote", "scope": "published"},
            {"name": "postgres", "state": "running", "address": "172.30.0.2:5432", "scope": "internal"},
        ]
        dashboard.model.projects[0].update(ip="172.31.0.2", host_ip="192.168.1.73", network="restricted", sessions=["main"])
        for name, address in [("browser-primary", "http://127.0.0.1:9222/"),
                              ("browser-secondary", "http://127.0.0.1:9224/"),
                              ("vnc-primary", "127.0.0.1:5901"), ("vnc-secondary", "127.0.0.1:5902"),
                              ("desktop-primary", ""), ("desktop-secondary", ""),
                              ("window-manager-primary", ""), ("window-manager-secondary", "")]:
            dashboard.model.projects[0]["service_items"].append(
                {"name": name, "state": "running", "address": address, "scope": "local"})
        dashboard.draw()
        writes = dashboard.screen.writes
        lines = [write[2] for write in writes]
        start = lines.index("Sandbox IP: 192.168.1.73")
        self.assertEqual(lines[start:start + 6], ["Sandbox IP: 192.168.1.73", "Services:",
            "Caido (running) http://127.0.0.1:49149/", "Files (running) http://127.0.0.1:49150/",
            "Browser (running) http://127.0.0.1:49151/", "Postgres (running) 172.30.0.2:5432"])
        ip_row = next(write for write in writes if write[2] == "Sandbox IP: 192.168.1.73")
        services_row = next(write for write in writes if write[2] == "Services:")
        caido_row = next(write for write in writes if write[2].startswith("Caido (running)"))
        self.assertEqual(services_row[0], ip_row[0] + 2)
        self.assertEqual(caido_row[1], services_row[1] + 2)
        for removed in ("172.31.0.2", "[running]", "Network restricted", "Agent sessions:", "v all", "novnc-", "browser-", "vnc-", "desktop-", "window-manager-"):
            self.assertNotIn(removed, "\n".join(lines))

    def test_service_scrolling_is_independent_of_project_selection(self):
        dashboard, _ = self.make_dashboard(height=20, width=80)
        dashboard.model.projects[0]["service_items"] = [
            {"name": f"service-{index}", "state": "running", "address": f"http://127.0.0.1:{49000 + index}/", "scope": "published"}
            for index in range(20)]
        dashboard.draw()
        first = "\n".join(write[2] for write in dashboard.screen.writes)
        dashboard.handle(curses.KEY_NPAGE)
        dashboard.draw()
        second = "\n".join(write[2] for write in dashboard.screen.writes)
        self.assertEqual(dashboard.selected_name, "a")
        self.assertGreater(dashboard.service_offset, 0)
        self.assertIn("Service-0 (running)", first)
        self.assertNotIn("Service-0 (running)", second)
        dashboard.handle("j")
        self.assertEqual(dashboard.service_offset, 0)

    def test_services_panel_has_exact_copyable_addresses_and_no_credentials(self):
        dashboard, manager = self.make_dashboard()
        address = "https://127.0.0.1:49150/custom-app/"
        manager.details.return_value = {"name": "a", "status": "running", "file_token": "PRIVATE", "service_items": [
            {"name": "app", "state": "running", "address": address, "scope": "published"}]}
        dashboard.handle("v")
        wait_until(lambda: not dashboard.model.snapshot()["busy"])
        title, lines = dashboard.model.snapshot()["panel"]
        self.assertEqual(title, "Services — a")
        self.assertIn("App (running) " + address, lines)
        self.assertNotIn("PRIVATE", "\n".join(lines))

    def test_service_addresses_wrap_without_losing_characters_on_narrow_terminal(self):
        dashboard, _ = self.make_dashboard(height=24, width=40)
        address = "http://127.0.0.1:49151/vnc.html?autoconnect=1&resize=remote"
        dashboard.model.projects[0]["service_items"] = [
            {"name": "custom app", "state": "running", "address": address, "scope": "published"}]
        dashboard.draw()
        lines = [write[2] for write in dashboard.screen.writes]
        first = next(index for index, line in enumerate(lines) if line.startswith("http://"))
        self.assertEqual(lines[first] + lines[first + 1], address)


class NonInteractiveTests(unittest.TestCase):
    def test_non_tty_requires_dashboard_terminal_without_querying_projects(self):
        manager = Mock()
        error = io.StringIO()
        with patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stderr(error):
            self.assertEqual(run(manager), 1)
        self.assertIn("requires an interactive terminal", error.getvalue())
        self.assertIn("ssh -t", error.getvalue())
        manager.list_projects.assert_not_called()

    def test_redirected_output_is_not_used_to_dump_credentials(self):
        manager = Mock()
        error = io.StringIO()
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=False), contextlib.redirect_stderr(error):
            self.assertEqual(run(manager), 1)
        self.assertIn("interactive terminal", error.getvalue())
        manager.list_projects.assert_not_called()


if __name__ == "__main__":
    unittest.main()
