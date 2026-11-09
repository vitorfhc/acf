"""A dependency-free terminal dashboard; Docker work never runs on its UI thread."""

from __future__ import annotations

import copy
import curses
import locale
import re
import sys
import textwrap
import threading
import time
import unicodedata
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .config import ConfigError, validate_name, validate_settings
from .services import LOCAL_PORTS
from .wizard import STEPS, field_value, input_value, parse_value, review_lines


HELP = """ACF — Agent Containment Facility

Up / Down, j / k   Select an environment
Enter              Attach to its persistent tmux terminal
x / c              Launch Codex / Claude Code
o / g              Launch OpenRouter / GLM
n                  Create an environment with the setup wizard
s                  Start or stop the selected environment
r                  Restart the selected environment
u                  Synchronize instructions, skills, custom tools and credentials
i                  Show environment details
v                  Manage services and show connection addresses
Page Up / Down     Scroll the selected environment's service list
a                  Show service addresses
d                  Delete selected environment and its data (confirmation required)
m                  Open all management actions
R                  Refresh now
?                  Show this help
q / Escape         Close a panel or leave the dashboard

Detach from tmux with Ctrl-b, then d, to return here.
Agents and services continue running after you leave.
Service addresses point to the Docker host, including when using SSH.
Sign in once with ./acf login codex or ./acf login claude.
Manage environments here; run project commands in their terminals.

Actions menu (m): use arrows + Enter, or the displayed shortcut.
Services panel: Up/Down selects; Enter/s toggles; + starts; - stops.
Page Up/Down scrolls endpoints. Service actions require a running environment.
Build images; diagnostics; container settings and API credentials;
apply configuration; logs; import/export files; service addresses; permanent deletion.
The setup wizard starts with config.toml defaults; s skips setup and uses them.
Wizard and container edits are saved only for that container.
Edit config.toml directly to change defaults for future containers.
"""

# shortcut, label, action identifier, requires selected project
ACTIONS = (
    ("n", "Create environment (setup wizard)", "create", False),
    ("a", "Service addresses", "access", True),
    ("i", "Environment details", "details", True),
    ("t", "Start/stop services and endpoints", "services", True),
    ("e", "Edit container settings and API credentials", "project-config", True),
    ("u", "Sync selected environment", "sync", True),
    ("U", "Sync all running environments", "sync-all", False),
    ("p", "Apply configuration / recreate containers", "apply", True),
    ("l", "Workload logs", "logs", True),
    ("L", "Network guard logs", "guard-logs", True),
    ("I", "Import files from host", "import", True),
    ("X", "Export files to host", "export", True),
    ("b", "Build container images", "build", False),
    ("D", "Run diagnostics", "diagnostics", False),
    ("d", "Delete environment and all data", "purge", True),
)

# Remove whole escape sequences before stripping control characters, including
# OSC hyperlinks / clipboard controls and C1 control-sequence variants.
_ESCAPES = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)"
    r"|(?:\x1b[P^_]|[\x90\x98\x9e\x9f]).*?(?:\x1b\\|\x9c|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]*[@-~]",
    re.DOTALL,
)


def clean(value: Any) -> str:
    """Render untrusted container text without terminal controls or bidi tricks."""
    text = _ESCAPES.sub("", str(value))
    return "".join(c for c in text if c.isprintable() and unicodedata.category(c) != "Cf")


def field(value: Any, default: str = "—") -> str:
    return clean(value) if value is not None and value != "" else default


def service_entries(project: dict[str, Any]) -> list[dict[str, str]]:
    """Show service states and only active addresses, hiding desktop internals."""
    reported = project.get("service_items")
    if isinstance(reported, list):
        entries = [{key: clean(item.get(key, "")) for key in ("name", "state", "protocol", "address", "scope", "port")}
                   for item in reported if isinstance(item, dict)]
    else:
        urls = project.get("urls")
        entries = [{"name": "desktop-http" if clean(name).lower() == "desktop" else clean(name),
                    "state": "running" if project.get("status") == "running" else "stopped",
                    "address": clean(address), "scope": "published"}
                   for name, address in urls.items()] if isinstance(urls, dict) else []

    hidden = {"browser-primary", "browser-secondary", "vnc", "vnc-primary", "vnc-secondary",
              "desktop", "desktop-primary", "desktop-secondary", "window-manager",
              "window-manager-primary", "window-manager-secondary", "desktop-display",
              "desktop-session", "desktop-vnc"}
    browser_names = {"novnc": 0, "novnc-primary": 1, "novnc-secondary": 2}
    managed = {"caido": "caido", "files": "files", "desktop-http": "desktop", "browser": "browser",
               **{name: "browser" for name in browser_names}}
    controls = {item["name"]: item for item in service_controls(project)}
    browser_ports = {str(port) for port in LOCAL_PORTS}
    entries = [item for item in entries if (item.get("address") or item["name"].lower() in managed
               or (item.get("protocol") in {"http", "https", "tcp"} and item.get("scope") != "process"))
               and item["name"].lower() not in hidden
               and not (item.get("scope") == "local" and item.get("port") in browser_ports)]
    browsers = [item for item in entries if item["name"].lower() in browser_names]
    browser = min(browsers, key=lambda item: browser_names[item["name"].lower()]) if browsers else None
    visible, represented = [], set()
    for item in entries:
        name = item["name"]
        logical = managed.get(name.lower())
        if name.lower() in browser_names:
            if item is not browser:
                continue
            item["name"] = "Browser"
            try:
                url = urlsplit(item["address"])
                # The root page cannot forward an access token; keep links that carry one.
                if url.scheme in {"http", "https"} and url.netloc and "path" not in parse_qs(url.query):
                    item["address"] = urlunsplit((url.scheme, url.netloc, "/", "", ""))
            except ValueError:
                pass
        elif name.lower() == "desktop-http":
            item["name"] = "Desktop"
        else:
            item["name"] = name[:1].upper() + name[1:]
        if logical:
            represented.add(logical)
            if logical in controls:
                item["state"] = controls[logical]["state"]
        visible.append(item)
    for name, item in controls.items():
        if name not in represented:
            visible.append({"name": item["label"], "state": item["state"], "address": "", "scope": "published"})

    environment_stopped = project.get("status") not in (None, "", "unknown", "running")
    for item in visible:
        running = not environment_stopped and item.get("state", "").lower() in {"running", "listening", "ready", "healthy"}
        item["state"] = "running" if running else "stopped"
        if not running:
            item["address"] = ""

    def order(item):
        return (item["state"] != "running", item.get("scope") != "published")
    return sorted(visible, key=order)


def service_label(item: dict[str, str]) -> str:
    return field(item.get("name"), "Unnamed service") + f" ({item['state']})"


def service_lines(project: dict[str, Any]) -> list[str]:
    """One service per row; stopped services have no connection address."""
    lines = [service_label(item) + (" " + item["address"] if item.get("address") else "")
             for item in service_entries(project)]
    return lines or ["No services reported."]


SERVICE_LABELS = {"caido": "Caido", "files": "Files", "browser": "Browser", "desktop": "Desktop"}


def service_controls(project: dict[str, Any]) -> list[dict[str, str]]:
    """Only expose the logical services supported by ACF's control API."""
    entries = project.get("service_controls")
    if not isinstance(entries, list):
        return []
    controls = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or entry["name"] not in SERVICE_LABELS:
            continue
        name = entry["name"]
        controls[name] = {"name": name, "label": SERVICE_LABELS[name],
                          "state": clean(entry.get("state", "unknown")).lower()}
    return [controls[name] for name in SERVICE_LABELS if name in controls]


def service_panel_data(project: dict[str, Any], name: str) -> dict[str, Any]:
    """Retain only service presentation fields, never credentials or other state."""
    items = project.get("service_items")
    urls = project.get("urls")
    return {"name": name, "status": clean(project.get("status", "unknown")),
            "service_notice": clean(project.get("service_notice", "")),
            "service_controls": service_controls(project),
            "service_items": [{key: clean(item.get(key, "")) for key in
                               ("name", "state", "protocol", "address", "scope", "port")}
                              for item in items if isinstance(item, dict)] if isinstance(items, list) else None,
            "urls": {clean(key): clean(value) for key, value in urls.items()} if isinstance(urls, dict) else None}


def service_panel_rows(project: dict[str, Any]) -> list[tuple[str, str | None]]:
    controls = service_controls(project)
    rows = [(f"{item['label']}  {item['state']}", item["name"]) for item in controls]
    if project.get("status") != "running":
        rows += [("Start the environment from the dashboard to manage services.", None)]
    elif not controls:
        rows += [("Service controls unavailable. Sync or recreate this environment to update its runtime.", None)]
    if project.get("service_notice"):
        rows += [(clean(project["service_notice"]), None)]
    rows += [("", None), ("Endpoints", None)]
    rows += [(line, None) for line in service_lines(project)]
    return rows


def service_panel_lines(project: dict[str, Any]) -> list[str]:
    return [line for line, _ in service_panel_rows(project)]


def summary_lines(project: dict[str, Any]) -> list[str]:
    """Only show operational fields; never dump credentials from details()."""
    lines = [
        f"{field(project.get('name'))}  ·  {field(project.get('status'))}",
        f"CPU {field(project.get('cpu'))}   Memory {field(project.get('memory'))}   Storage {field(project.get('storage'))}",
        f"Sandbox IP: {field(project.get('host_ip'))}",
        "", "Services:", *("  " + line for line in service_lines(project)),
    ]
    return lines


def access_lines(project: dict[str, Any]) -> list[str]:
    """Connection addresses only; ignore obsolete service credentials in old state."""
    lines = [f"Service addresses for {field(project.get('name'))}", ""]
    if service_entries(project):
        lines.extend(service_lines(project))
    else:
        lines.append("No service addresses available. Start the environment first.")
    lines.extend(["", "Published addresses belong to the Docker host. Loopback bindings require a tunnel for remote access."])
    return lines


class Model:
    """Thread-safe inventory and operation state, independent of curses."""

    def __init__(self, manager: Any):
        self.manager = manager
        self.lock = threading.Lock()
        self.projects: list[dict[str, Any]] = []
        self.loaded = False
        self.error = ""
        self.message = ""
        self.busy = ""
        self.updated = 0.0
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.panel: tuple[str, list[str]] | None = None
        self.service_panel: dict[str, Any] | None = None
        notices = getattr(getattr(manager, "config", None), "migration_notices", None)
        if isinstance(notices, list) and notices:
            self.panel = ("Configuration updated", [clean(notice) for notice in notices])

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "projects": copy.deepcopy(self.projects), "loaded": self.loaded,
                "error": self.error, "message": self.message, "busy": self.busy,
                "updated": self.updated, "panel": copy.deepcopy(self.panel),
                "service_panel": copy.deepcopy(self.service_panel),
            }

    def say(self, message: str) -> None:
        with self.lock:
            self.message = clean(message)

    def refresh(self) -> None:
        try:
            projects = self.manager.list_projects()
            if not isinstance(projects, list):
                raise ValueError("Expected an environment list from Docker.")
            projects = sorted(projects, key=lambda project: str(project.get("name", "")))
            with self.lock:
                self.projects, self.error = projects, ""
                self.updated = time.monotonic()
                if self.service_panel is not None and not self.busy:
                    name = self.service_panel["name"]
                    project = next((item for item in projects if item.get("name") == name),
                                   {"status": "missing"})
                    self.service_panel = service_panel_data(project, name)
                    self.panel = (f"Services — {clean(name)}", service_panel_lines(self.service_panel))
        except Exception as exc:
            with self.lock:
                self.error = clean(str(exc) or type(exc).__name__)
        finally:
            with self.lock:
                self.loaded = True

    def poll(self) -> None:
        while not self.stop.is_set():
            self.wake.clear()
            self.refresh()
            self.wake.wait(3.0)

    def complete(self, label: str, result: Any, *, details: bool = False,
                 panel_title: str | None = None, formatter: Callable[[Any], list[str]] | None = None,
                 services_name: str | None = None) -> None:
        with self.lock:
            if services_name is not None:
                # Closing a panel while a command finishes must not reopen it.
                if self.service_panel is not None and self.service_panel["name"] == services_name:
                    self.service_panel = service_panel_data(result, services_name)
                    self.panel = (f"Services — {clean(services_name)}", service_panel_lines(self.service_panel))
            elif details:
                self.panel = ("Environment details", summary_lines(result) + ["", "Close this panel and press a for service addresses, or v for all services."])
            elif panel_title:
                lines = formatter(result) if formatter else (result if isinstance(result, list) else [str(result)])
                self.panel = (clean(panel_title), [clean(line) for line in lines] or ["No entries."])
            elif isinstance(result, list) and result:
                self.panel = ("Operation notices", [clean(label) + " complete.", "", *[clean(item) for item in result]])
            self.message = clean(label) + " complete."

    def operate(self, label: str, action: Callable[[], Any], *, details: bool = False,
                panel_title: str | None = None, formatter: Callable[[Any], list[str]] | None = None,
                services_name: str | None = None) -> bool:
        """Serialize lifecycle changes; inventory reads can still refresh."""
        with self.lock:
            if self.busy:
                self.message = f"Wait for {self.busy} to finish."
                return False
            self.busy = clean(label)
            self.message = clean(label) + "…"

        def worker() -> None:
            try:
                result = action()
                self.complete(label, result, details=details, panel_title=panel_title, formatter=formatter,
                              services_name=services_name)
            except Exception as exc:
                with self.lock:
                    self.message = "Error: " + clean(str(exc) or type(exc).__name__)
            finally:
                with self.lock:
                    self.busy = ""
                self.wake.set()

        threading.Thread(target=worker, name="acf-operation", daemon=True).start()
        return True


class Dashboard:
    def __init__(self, screen: Any, manager: Any):
        self.screen = screen
        self.manager = manager
        self.model = Model(manager)
        self.selected_name: str | None = None
        self.selected = 0
        self.offset = 0
        self.panel_offset = 0
        self.menu_open = False
        self.menu_index = 0
        self.menu_offset = 0
        self.service_offset = 0
        self.service_page_size = 1
        self.control_index = 0
        self.control_name: str | None = None
        self.reveal_control = False

    def selected_control(self, controls: list[dict[str, str]]) -> dict[str, str] | None:
        if not controls:
            self.control_index, self.control_name = 0, None
            return None
        names = [item["name"] for item in controls]
        if self.control_name in names:
            self.control_index = names.index(self.control_name)
        self.control_index = min(max(self.control_index, 0), len(controls) - 1)
        self.control_name = controls[self.control_index]["name"]
        return controls[self.control_index]

    def menu_actions(self, current: dict[str, Any] | None) -> list[tuple]:
        return [action for action in ACTIONS if current is not None or not action[3]]

    def current(self, projects: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not projects:
            self.selected_name, self.selected = None, 0
            return None
        names = [project.get("name") for project in projects]
        if self.selected_name in names:
            self.selected = names.index(self.selected_name)
        self.selected = min(max(self.selected, 0), len(projects) - 1)
        name = projects[self.selected].get("name")
        if name != self.selected_name:
            self.service_offset = 0
        self.selected_name = name
        return projects[self.selected]

    def put(self, y: int, x: int, text: Any, attribute: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width - 1:
            return
        try:
            self.screen.addnstr(y, x, clean(text), width - x - 1, attribute)
        except (curses.error, UnicodeError):
            # Resizing or a wide final character may reach the lower-right cell.
            pass

    def draw(self, *, refresh: bool = True) -> None:
        state = self.model.snapshot()
        projects = state["projects"]
        current = self.current(projects)
        height, width = self.screen.getmaxyx()
        self.screen.erase()
        self.put(0, 1, "ACF  /  Agent Containment Facility", curses.A_BOLD)
        if height < 10 or width < 40:
            self.put(2, 1, "Resize to at least 40 × 10.")
            self.put(height - 1, 1, "q quit  ·  ? help")
            if refresh:
                self.screen.refresh()
            return
        running = sum(project.get("status") == "running" for project in projects)
        loading = " · refreshing…" if not state["loaded"] else ""
        self.put(1, 1, f"{len(projects)} environments · {running} running{loading}", curses.A_DIM)
        footer = "v services · a URLs · ? help · R refresh · q quit" if width >= 54 else "v services · a URLs · ? help · q quit"

        if state["panel"]:
            title, lines = state["panel"]
            wrapped = []
            service_panel = state["service_panel"]
            selected = self.selected_control(service_controls(service_panel)) if service_panel else None
            rows = service_panel_rows(service_panel) if service_panel else [(line, None) for line in lines]
            for line, service_name in rows:
                wrapped.extend((piece, service_name) for piece in
                               (textwrap.wrap(clean(line), max(1, width - 5),
                                              replace_whitespace=False, drop_whitespace=False,
                                              break_on_hyphens=False) or [""]))
            self.put(3, 1, title, curses.A_BOLD)
            available = max(1, height - 8)
            if service_panel and self.reveal_control and selected:
                position = next((i for i, (_, name) in enumerate(wrapped) if name == selected["name"]), 0)
                self.panel_offset = min(self.panel_offset, position)
                if position >= self.panel_offset + available:
                    self.panel_offset = position - available + 1
                self.reveal_control = False
            self.panel_offset = max(0, min(self.panel_offset, max(0, len(wrapped) - available)))
            for row, (line, service_name) in enumerate(wrapped[self.panel_offset:self.panel_offset + available], 5):
                self.put(row, 2, line, curses.A_REVERSE if selected and service_name == selected["name"] else 0)
            if service_panel:
                self.put(height - 3, 1, "↑↓ select · s toggle · q close", curses.A_DIM)
                footer = "+ start · - stop · PgUp/PgDn URLs · R refresh"
            else:
                self.put(height - 3, 1, "Up/Down scroll  ·  Esc/q close", curses.A_DIM)
        elif self.menu_open:
            actions = self.menu_actions(current)
            self.menu_index = min(self.menu_index, len(actions) - 1)
            available = max(1, height - 8)
            self.menu_offset = min(self.menu_offset, self.menu_index)
            if self.menu_index >= self.menu_offset + available:
                self.menu_offset = self.menu_index - available + 1
            label = "Actions — " + (clean(current["name"]) if current else "global")
            self.put(3, 1, label, curses.A_BOLD)
            self.put(2, 1, f"Showing {self.menu_offset + 1}–{min(len(actions), self.menu_offset + available)} of {len(actions)} actions", curses.A_DIM)
            for index, (key, label, _, _) in enumerate(actions[self.menu_offset:self.menu_offset + available], self.menu_offset):
                self.put(4 + index - self.menu_offset, 2, f"{key:>2}  {label}",
                         curses.A_REVERSE if index == self.menu_index else 0)
            self.put(height - 3, 1, "Up/Down select · Enter run · shortcut key · Esc close", curses.A_DIM)
        else:
            details_rows = max(6, min(17, height - 11)) if height >= 15 else 0
            available = max(1, height - 8 - details_rows)
            self.offset = max(0, min(self.offset, self.selected))
            if self.selected >= self.offset + available:
                self.offset = self.selected - available + 1
            if width >= 108:
                columns = [("name", "ENVIRONMENT", 24), ("status", "STATUS", 12),
                           ("cpu", "CPU", 9), ("memory", "MEMORY", 20),
                           ("storage", "STORAGE", 12), ("host_ip", "HOST IP", 16)]
            elif width >= 76:
                columns = [("name", "ENVIRONMENT", 22), ("status", "STATUS", 12),
                           ("cpu", "CPU", 9), ("memory", "MEMORY", 18),
                           ("storage", "STORAGE", 11)]
            else:
                columns = [("name", "ENVIRONMENT", max(15, width - 24)),
                           ("status", "STATUS", 12), ("cpu", "CPU", 9)]
            header = " ".join(title.ljust(size) for _, title, size in columns)
            self.put(3, 2, header, curses.A_DIM | curses.A_BOLD)
            for index, project in enumerate(projects[self.offset:self.offset + available], self.offset):
                text = " ".join(field(project.get(key))[:size].ljust(size) for key, _, size in columns)
                attribute = curses.A_REVERSE if index == self.selected else 0
                self.put(4 + index - self.offset, 1, ("> " if index == self.selected else "  ") + text, attribute)
            if not projects:
                self.put(5, 3, "Loading environments…" if not state["loaded"] else "No environments yet. Press n to create one.")
            if details_rows and current:
                start = height - 4 - details_rows
                self.put(start, 1, "─" * (width - 3), curses.A_DIM)
                self.put(start + 1, 2, f"Sandbox IP: {field(current.get('host_ip'))}")
                items = service_entries(current)
                rows = []
                for item in items:
                    label, address = service_label(item), item.get("address", "")
                    # Leave full URLs on one line whenever the terminal can fit them.
                    line = label + (" " + address if address else "")
                    contents = [line] if len(line) <= width - 6 or not address else [label, address]
                    for line in contents:
                        rows.extend(textwrap.wrap(line, max(1, width - 6), replace_whitespace=False,
                                                  drop_whitespace=False, break_on_hyphens=False) or [""])
                self.service_page_size = max(1, height - 4 - (start + 4) + 1)
                self.service_offset = min(max(0, self.service_offset), max(0, len(rows) - self.service_page_size))
                visible = rows[self.service_offset:self.service_offset + self.service_page_size]
                self.put(start + 3, 2, "Services:", curses.A_BOLD)
                for row, text in enumerate(visible or ["No services reported."], start + 4):
                    self.put(row, 4, text)
            if current:
                toggle = "stop" if current.get("status") == "running" else "start"
                shortcuts = f"s {toggle} · d delete"
                if width >= 76:
                    shortcuts += " · r restart · Enter terminal · m actions"
                    footer = "n new · x Codex · c Claude · v services · ? help · R refresh · q quit"
                else:
                    shortcuts += " · Enter terminal"
                    footer = "n new · m actions · ? help · q quit"
            else:
                shortcuts = "Enter terminal · x Codex · c Claude · n new · m actions"
            self.put(height - 3, 1, shortcuts, curses.A_DIM)

        status = state["error"] or state["message"] or "Ready. Environments keep running when you leave."
        self.put(height - 2, 1, ("Error: " if state["error"] else "") + status,
                 curses.A_BOLD if state["error"] or state["busy"] else 0)
        self.put(height - 1, 1, "Working: " + state["busy"] if state["busy"] else footer, curses.A_DIM)
        if refresh:
            self.screen.refresh()

    def prompt(self, question: str, initial: str = "", *, secret: bool = False,
               cancel_value: str | None = "") -> str | None:
        value = initial
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                self.draw(refresh=False)
                height, width = self.screen.getmaxyx()
                if height < 2 or width < 8:
                    try:
                        if self.screen.get_wch() == "\x1b":
                            return cancel_value
                    except curses.error:
                        pass
                    continue
                self.put(height - 1, 0, " " * (width - 1))
                line = question + " " + ("*" * len(value) if secret else value)
                shown = line[-max(1, width - 3):]
                self.put(height - 1, 1, shown, curses.A_BOLD)
                try:
                    self.screen.move(height - 1, min(len(shown) + 1, width - 2))
                except curses.error:
                    pass
                self.screen.refresh()
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    continue
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return value if secret else value.strip()
                if key == "\x1b":
                    return cancel_value
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    value = value[:-1]
                elif key in (curses.KEY_UP, curses.KEY_PPAGE):
                    self.panel_offset = max(0, self.panel_offset - (10 if key == curses.KEY_PPAGE else 1))
                elif key in (curses.KEY_DOWN, curses.KEY_NPAGE):
                    self.panel_offset += 10 if key == curses.KEY_NPAGE else 1
                elif isinstance(key, str) and key.isprintable() and len(value) < 4096:
                    value += clean(key)
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def wizard_field(self, settings: dict, field: tuple) -> None:
        """Edit one value; Escape returns to the step without changing it."""
        section, key, label, kind = field
        value = settings.get(section, {}).get(key, "")
        initial = "" if kind == "secret" else input_value(value, kind)
        question = f"{label}:"
        if kind == "secret":
            question = f"{label} (Enter keeps, - clears):"
        while True:
            answer = self.prompt(question, initial, secret=kind == "secret", cancel_value=None)
            if answer is None or (kind == "secret" and not answer):
                return
            try:
                changed = copy.deepcopy(settings)
                changed.setdefault(section, {})[key] = "" if kind == "secret" and answer == "-" else parse_value(answer, kind)
                validate_settings(changed)
            except (ConfigError, ValueError, OverflowError):
                # Do not echo input: it may contain an API credential.
                self.model.say(f"Invalid {label.lower()}. Check the value and try again; Escape keeps the previous value.")
                initial = "" if kind == "secret" else answer
                continue
            settings.clear()
            settings.update(changed)
            self.model.say(f"Updated {label.lower()} for this container.")
            return

    def setup_wizard(self) -> tuple[str, dict] | None:
        """Collect a private settings snapshot, with no writes or Docker calls."""
        try:
            defaults = copy.deepcopy(self.manager.config.reload())
            validate_settings(defaults)
        except (ConfigError, OSError) as exc:
            self.model.say(f"Cannot load config.toml defaults: {exc}")
            return None
        while True:
            name = self.prompt("New environment name (Esc cancels):", cancel_value=None)
            if name is None:
                self.model.say("Setup cancelled.")
                return None
            try:
                validate_name(name)
            except ConfigError as exc:
                self.model.say(str(exc))
                continue
            if any(project.get("name") == name for project in self.model.snapshot()["projects"]):
                self.model.say("An environment with that name already exists. Choose a different name.")
                continue
            break
        settings = copy.deepcopy(defaults)
        step = 0
        try:
            while True:
                if step == len(STEPS):
                    title = f"Create {name} — review"
                    lines = review_lines(name, settings)
                else:
                    label, fields = STEPS[step]
                    title = f"Set up {name} — {step + 1}/{len(STEPS)} {label}"
                    lines = ["Defaults come from config.toml; changes apply to this container only.",
                             "Enter: next   number: edit   b: back   Esc: cancel",
                             "s: Skip wizard and create using config.toml defaults", ""]
                    lines.extend(f"{number}. {field[2]}: {field_value(settings, field)}"
                                 for number, field in enumerate(fields, 1))
                    if step == 3:
                        lines.extend(["", "Use a path to a key file on this host. Relative paths start at the checkout."])
                    if step == 4:
                        lines.extend(["", "Credentials are optional. Existing provider logins remain available.",
                                      "Keys are hidden while typing. Only enabled integrations receive credentials.",
                                      "Integration names: codex, claude, openrouter, glm, caido, oob, gmail."])
                with self.model.lock:
                    self.model.panel = (title, lines)
                choice = self.prompt("Choice:", cancel_value=None)
                if choice is None or choice == "q":
                    self.model.say("Setup cancelled.")
                    return None
                if choice.lower() == "s":
                    return name, copy.deepcopy(defaults)
                if choice.lower() == "b":
                    step = max(0, step - 1)
                    self.panel_offset = 0
                    continue
                if not choice or choice.lower() == "n":
                    if step == len(STEPS):
                        return name, settings
                    step += 1
                    self.panel_offset = 0
                    continue
                if choice.isdecimal():
                    selected = int(choice) - 1
                    if step == len(STEPS) and 0 <= selected < len(STEPS):
                        step = selected
                        self.panel_offset = 0
                        continue
                    if step < len(STEPS) and 0 <= selected < len(STEPS[step][1]):
                        self.wizard_field(settings, STEPS[step][1][selected])
                        continue
                self.model.say("Choose a listed field, Enter to continue, s to use defaults, or Escape to cancel.")
        finally:
            with self.model.lock:
                self.model.panel = None
            self.panel_offset = 0

    def interactive(self, action: Callable[[], Any]) -> None:
        """Hand the real terminal to docker exec / tmux, restoring curses afterward."""
        curses.def_prog_mode()
        curses.endwin()
        try:
            result = action()
            if isinstance(result, list):
                self.model.complete("Settings update", result)
        except (Exception, KeyboardInterrupt) as exc:
            self.model.say("Error: " + str(exc) if str(exc) else "Terminal interrupted.")
        finally:
            curses.reset_prog_mode()
            self.screen.keypad(True)
            self.screen.timeout(150)
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self.screen.clearok(True)
            self.model.wake.set()

    def confirm(self, title: str, lines: list[str], expected: str) -> bool:
        self.panel_offset = 0
        with self.model.lock:
            self.model.panel = (title, lines + ["", f"Type {expected} to continue. Escape cancels."])
        try:
            accepted = self.prompt(f"Type {expected}:") == expected
        finally:
            with self.model.lock:
                self.model.panel = None
        if not accepted:
            self.model.say("Action cancelled.")
        return accepted

    def copy(self, name: str, *, export: bool) -> None:
        self.panel_offset = 0
        with self.model.lock:
            self.model.panel = ("Export files" if export else "Import files", [
                f"Environment: {name}", "Copy a file or directory between the host and /workspace.",
                "Enter paths directly; do not add shell quotes.",
                "Existing destination files may be replaced. Escape cancels."])
        try:
            if export:
                source = self.prompt("Container source path:", "/workspace/")
                if not source:
                    return
                destination = self.prompt("Host destination path:")
                source = f"{name}:{source}"
            else:
                source = self.prompt("Host source path:")
                if not source:
                    return
                destination = self.prompt("Container destination path:", "/workspace/")
                if destination:
                    destination = f"{name}:{destination}"
        finally:
            with self.model.lock:
                self.model.panel = None
        if destination:
            self.model.operate(f"{'Exporting' if export else 'Importing'} files for {name}",
                               lambda: self.manager.copy_files(source, destination))

    def action(self, action: str, current: dict[str, Any] | None) -> None:
        """One route for direct keys and menu choices."""
        if self.model.snapshot()["busy"]:
            self.model.say("Wait for the current operation to finish.")
            return
        self.menu_open = False
        self.panel_offset = 0
        if action == "create":
            result = self.setup_wizard()
            if result is not None:
                name, settings = result
                self.model.operate(f"Creating {name}", lambda: self.manager.create(name, settings=settings))
                self.selected_name = name
            return
        if action == "build":
            self.model.operate("Building container images", self.manager.build)
            return
        if action == "diagnostics":
            self.model.operate("Running diagnostics", self.manager.diagnostics, panel_title="Diagnostics")
            return
        if action == "sync-all":
            names = [project["name"] for project in self.model.snapshot()["projects"] if project.get("status") == "running"]
            def synchronize():
                notices = []
                for name in names:
                    notices.extend(f"{name}: {warning}" for warning in (self.manager.sync(name) or []))
                return [f"Synchronized {len(names)} running environments.", *notices]
            self.model.operate("Synchronizing all running environments", synchronize)
            return
        if not current:
            self.model.say("Select an environment first.")
            return
        name = current["name"]
        if action == "project-config":
            self.interactive(lambda: self.manager.edit_config(name=name))
        elif action == "access":
            self.model.operate(f"Loading service addresses for {name}", lambda: self.manager.details(name),
                               panel_title=f"Service addresses — {name}", formatter=access_lines)
        elif action == "details":
            self.model.operate(f"Inspecting {name}", lambda: self.manager.details(name), details=True)
        elif action == "services":
            self.control_index, self.control_name = 0, None
            self.reveal_control = True
            with self.model.lock:
                self.model.service_panel = service_panel_data(current, name)
                self.model.panel = (f"Services — {clean(name)}", service_panel_lines(self.model.service_panel))
            self.model.operate(f"Loading services for {name}", lambda: self.manager.details(name),
                               services_name=name)
        elif action == "sync":
            self.model.operate(f"Synchronizing {name}", lambda: self.manager.sync(name))
        elif action in ("logs", "guard-logs"):
            self.model.operate(f"Loading {name} logs", lambda: self.manager.logs(name, guard=action == "guard-logs"),
                               panel_title=f"{name} — {'network guard' if action == 'guard-logs' else 'workload'} logs")
        elif action == "apply":
            if self.confirm("Apply configuration", [f"Recreate {name} using the current settings.",
                    "Running sessions will stop. The writable OS layer is discarded, including apt installs and system changes.",
                    "Named volumes (workspace, homes, tools) are preserved."], name):
                self.model.operate(f"Applying configuration to {name}", lambda: self.manager.apply(name))
        elif action == "purge":
            if self.confirm("Permanently delete environment data", [f"Delete {name}'s containers, named volumes and managed state.",
                    "This permanently deletes workspace files, agent homes, browser profiles and installed tools.",
                    "Host-authored configuration, prompts and skills remain."], f"delete {name}"):
                self.model.operate(f"Permanently deleting {name}", lambda: self.manager.remove(name, purge=True))
        elif action in ("import", "export"):
            self.copy(name, export=action == "export")

    def handle(self, key: Any) -> bool:
        state = self.model.snapshot()
        projects = state["projects"]
        current = self.current(projects)
        if state["service_panel"] is not None:
            panel = state["service_panel"]
            controls = service_controls(panel)
            selected = self.selected_control(controls)
            if key in ("q", "\x1b"):
                with self.model.lock:
                    self.model.panel = None
                    self.model.service_panel = None
                self.panel_offset = 0
            elif key in (curses.KEY_UP, "k", curses.KEY_DOWN, "j") and controls:
                step = -1 if key in (curses.KEY_UP, "k") else 1
                self.control_index = min(max(self.control_index + step, 0), len(controls) - 1)
                self.control_name = controls[self.control_index]["name"]
                self.reveal_control = True
            elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                self.panel_offset = max(0, self.panel_offset + (10 if key == curses.KEY_NPAGE else -10))
            elif key == "R":
                self.model.wake.set()
            elif key in ("s", "\n", "\r", curses.KEY_ENTER, "+", "-"):
                if state["busy"]:
                    self.model.say(f"Wait for {state['busy']} to finish.")
                elif panel.get("status") != "running":
                    self.model.say("Start the environment from the dashboard before managing services.")
                elif not selected:
                    self.model.say("No managed service selected. Other endpoints are read-only.")
                elif selected["state"] not in {"running", "stopped", "failed"}:
                    self.model.say("Service state is unavailable or changing. Refresh before trying again.")
                else:
                    action = "start" if key == "+" else "stop" if key == "-" else (
                        "stop" if selected["state"] == "running" else "start")
                    if (action == "start" and selected["state"] == "running") or (
                            action == "stop" and selected["state"] == "stopped"):
                        self.model.say(f"{selected['label']} is already {selected['state']}.")
                    else:
                        name, service_name = panel["name"], selected["name"]
                        label = "Starting" if action == "start" else "Stopping"
                        self.model.operate(f"{label} {selected['label']} for {name}",
                                           lambda: self.manager.service(name, action, service_name), services_name=name)
            return True
        if state["panel"]:
            if key in ("q", "\x1b", "?", "i"):
                with self.model.lock:
                    self.model.panel = None
                self.panel_offset = 0
            elif key in (curses.KEY_DOWN, "j"):
                self.panel_offset += 1
            elif key in (curses.KEY_UP, "k"):
                self.panel_offset = max(0, self.panel_offset - 1)
            elif key == curses.KEY_NPAGE:
                self.panel_offset += 10
            elif key == curses.KEY_PPAGE:
                self.panel_offset = max(0, self.panel_offset - 10)
            return True
        if self.menu_open:
            actions = self.menu_actions(current)
            self.menu_index = min(self.menu_index, len(actions) - 1)
            if key in ("q", "\x1b", "m"):
                self.menu_open = False
            elif key in (curses.KEY_DOWN, "j"):
                self.menu_index = min(self.menu_index + 1, len(actions) - 1)
            elif key in (curses.KEY_UP, "k"):
                self.menu_index = max(self.menu_index - 1, 0)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                self.action(actions[self.menu_index][2], current)
            else:
                chosen = next((item for item in actions if item[0] == key), None)
                if chosen:
                    self.action(chosen[2], current)
            return True
        if key in ("q", "\x1b"):
            if state["busy"]:
                self.model.say(f"Wait for {state['busy']} to finish before quitting.")
                return True
            return False
        if key == "?":
            self.panel_offset = 0
            with self.model.lock:
                self.model.panel = ("Keyboard help", HELP.splitlines())
            return True
        if key == "R":
            self.model.wake.set()
            return True
        if key in (curses.KEY_DOWN, "j", curses.KEY_UP, "k") and projects:
            self.selected += 1 if key in (curses.KEY_DOWN, "j") else -1
            self.selected = min(max(self.selected, 0), len(projects) - 1)
            self.selected_name = projects[self.selected]["name"]
            self.service_offset = 0
            return True
        if key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            self.service_offset = max(0, self.service_offset + (-self.service_page_size if key == curses.KEY_PPAGE else self.service_page_size))
            return True
        if state["busy"]:
            self.model.say(f"Wait for {state['busy']} to finish.")
            return True
        if key == "m":
            self.menu_open = True
            self.menu_index = self.menu_offset = 0
            return True
        direct = {"n": "create", "a": "access", "i": "details", "v": "services", "u": "sync",
                  "d": "purge"}
        if key in direct:
            self.action(direct[key], current)
            return True
        if not current:
            return True
        name = current["name"]
        if key in ("\n", "\r", curses.KEY_ENTER):
            self.interactive(lambda: self.manager.attach(name))
        elif key in ("x", "c", "o", "g"):
            agent = {"x": "codex", "c": "claude", "o": "openrouter", "g": "glm"}[key]
            self.interactive(lambda: self.manager.agent(name, agent))
        elif key == "s":
            action = self.manager.stop if current.get("status") == "running" else self.manager.start
            label = "Stopping" if current.get("status") == "running" else "Starting"
            self.model.operate(f"{label} {name}", lambda: action(name))
        elif key == "r":
            self.model.operate(f"Restarting {name}", lambda: self.manager.restart(name))
        return True

    def run(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.screen.keypad(True)
        self.screen.timeout(150)
        thread = threading.Thread(target=self.model.poll, name="acf-inventory", daemon=True)
        thread.start()
        try:
            while True:
                self.draw()
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    continue
                if not self.handle(key):
                    break
        finally:
            self.model.stop.set()
            self.model.wake.set()


def run(manager: Any) -> int:
    """The dashboard is interactive; there is no alternate management CLI."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("ACF dashboard requires an interactive terminal. Run ./acf in a terminal; use ssh -t when connecting remotely.", file=sys.stderr)
        return 1
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    try:
        curses.wrapper(lambda screen: Dashboard(screen, manager).run())
    except KeyboardInterrupt:
        return 130
    except curses.error as exc:
        print(f"ACF: unable to initialize terminal: {clean(exc)}. Check TERM and run ./acf in an interactive terminal.", file=sys.stderr)
        return 1
    return 0
