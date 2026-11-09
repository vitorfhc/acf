# Agent Containment Facility

ACF runs coding agents in separate Docker environments with persistent project
files, root access, browser desktops, and a terminal dashboard. The host
application uses Python's standard library; tools and agent CLIs run inside
containers.

There are two host entrypoints:

```sh
./acf                      # Open the dashboard
./acf login codex          # Authenticate an endpoint; codex or claude
```

Create and manage environments through the dashboard. Use each environment's
terminal for commands, tools, and custom agent invocations.

Each project gets a workload container, a dedicated network guard, its own Docker
network, and named volumes. ACF does not mount your home directory, checkout,
Docker socket, or other host paths into projects. Shared instructions and selected
credentials are copied into each environment.

## Quick start

You need **Python 3.11+** and a working **Docker CLI and daemon** on macOS or Linux.
Docker Desktop works on macOS; Docker Engine works on Linux servers and cloud
VMs. The runtime image supports Linux AMD64 and ARM64. Host tmux, Node.js, and pip
packages are not required. Run ACF directly on a remote host over SSH when useful;
ACF does not provision cloud machines.

1. Run `./acf login codex` or `./acf login claude` and complete the login flow.
   Login is optional when you supply the corresponding API key.
2. Run `./acf`. Configuration and state directories initialize automatically.
3. Press `n` and name your project, for example `research`. Review the setup
   wizard, or press `s` to skip it and create the container with your defaults.
4. Select the project and press `x` for Codex or `c` for Claude Code. Use `o` for
   OpenRouter or `g` for GLM after configuring their API keys.
5. Press Enter to attach a plain terminal, `v` for all service addresses, or `a`
   for browser/file URLs.

Missing runtime and guard images build automatically. The first build downloads
system packages, browsers, and pinned agent CLIs and can take several minutes.
The dashboard's actions menu (`m`) can also build images ahead of time. Caido
downloads on its first service start.

Detach from tmux with **Ctrl-b, then d** to return to the dashboard. Closing the
terminal or SSH connection leaves processes running. Stopping or restarting the
container ends those processes; stored files and agent history remain.

The active `prompts/` and `skills/` in this checkout were initially copied from
[`examples/`](examples/README.md). The supplied bug bounty prompt requires an
explicit scope in `/workspace/scope/` and a working Caido connection. Edit the
active prompt for other workflows. For a fresh checkout without those ignored
active directories, copy `examples/prompts/` to `prompts/` and `examples/skills/`
to `skills/` before creating a project.

## Dashboard

The dashboard shows status, CPU and memory usage, workspace/home storage usage,
and the Docker host's reachable address as `Sandbox IP: <ip>`. The container's private
Docker IP remains available to internal tooling and the `{{CONTAINER_IP}}`
placeholder. Its background refresh keeps the interface usable while
gathering information. Under **Services:**, each indented row appears as
`Name (running) address` or `Name (stopped)`, without bullets. Stopped services
have no address displayed. HTTP and HTTPS services show complete URLs; other TCP
services show `IP:port`. A single **Browser** link opens noVNC, where you can switch
profiles. Internal browser processes and their CDP/VNC endpoints are hidden.
Press `?` for keyboard help.
The selected environment's shortcut row shows `s stop` when running or `s start`
when stopped, plus `d delete`. These work directly from the main dashboard.

| Key | Action |
| --- | --- |
| Up/down or `k`/`j` | Select project |
| `n` | Open the new-container setup wizard |
| Enter | Attach persistent terminal |
| `x`, `c`, `o`, `g` | Codex, Claude, OpenRouter, GLM session |
| `s` | Start or stop selected project |
| `r` | Restart |
| `u` | Sync selected project's shared resources and credentials |
| `i` | Show details without API keys; service links include their access token |
| `v` | Start/stop services and show endpoints |
| Page Up / Page Down | Scroll services in the main dashboard |
| `a` | Show service addresses |
| `d` | Delete selected environment, volumes and state; confirm with `delete NAME` |
| `m` | Open actions menu |
| `R` | Refresh |
| `?` | Show help |
| `q` or Escape | Close a panel or leave dashboard |

The actions menu supports arrow keys and Enter, plus these shortcuts:

| Menu key | Action |
| --- | --- |
| `b` | Build runtime and guard images |
| `D` | Run diagnostics |
| `t` | Show services and endpoints |
| `e` | Edit selected container's settings and credentials |
| `p` | Apply current project configuration by recreating containers |
| `l`, `L` | View workload or network-guard logs |
| `I`, `X` | Import or export a file |
| `d` | Delete the selected environment, volumes and state |
| `U` | Sync all running projects |

Service addresses use a published host port when available. Published addresses
use the Docker host's reachable IP or configured hostname, so you can open them
from another computer. When ACF runs over SSH, it uses the server's address;
when using a remote Docker context, it uses that Docker endpoint's hostname.
If the endpoint uses an SSH-only alias, set `network.advertise_host` to an IP or
DNS name that your browser can reach.
Unpublished services use their container address, which may not be reachable
from your computer, particularly with Docker Desktop. Discovering a listener
does not publish it. To access a custom service from the host, configure an
extra port mapping, apply the configuration, and make the service listen on
`0.0.0.0` inside the environment.

The menu also offers creation, deletion, details, and access information. Global
actions remain available when no project is selected. Deletion requires typing
`delete NAME`; user-authored project configuration, prompts, and skills on the
host remain. Applying configuration asks for the project name because it stops
sessions and discards the container's writable OS layer.

The service-address panel lists copyable endpoints. Files, Browser, and Desktop
links contain a random access token that ACF issues each time the environment
starts; treat them like passwords. Opening those ports without the token's link
is refused. Restarting an environment replaces the token, so open its services
from the dashboard again afterwards.
Diagnostics inspect deployed container settings; they are not a security
certification or a complete attack simulation.

## Configuration

ACF creates `~/.local/share/acf/config.toml` on startup. Use `ACF_HOME` to choose a
different state directory, consistently for both entrypoints:

```sh
export ACF_HOME=/path/to/acf-data
./acf
```

Configuration is independent of the current working directory. All creation
defaults, API keys, and the OOB SSH key path belong in `config.toml`. Creation
defaults merge in this order, with later values taking precedence:

1. Built-in defaults.
2. `ACF_HOME/config.toml`.
3. `config.toml` in this checkout.

Press `n` and enter a name to open the setup wizard. It starts with those defaults
and walks through resources, network, service ports, OOB access, and API
credentials before a final review. Enter keeps the defaults and advances; `s`
skips the wizard and creates the container using `config.toml` defaults. Escape
cancels. Credential values are masked while entering and reviewing them.
Each container saves its own copy of the resulting settings, including its
credentials. Wizard choices never rewrite either defaults file. Later changes to
`config.toml` apply to future containers only.

For an existing container, use **Edit container settings and API credentials**
(`m`, then `e`). The
dashboard opens a TOML draft in `VISUAL` or `EDITOR`, falling back to `nano` or
`vi`, and validates it before saving to that container's private state. Invalid
edits leave the saved settings intact and reopen as a draft next time. These
edits also leave `config.toml` unchanged. Edit `config.toml` directly when you
intend to change defaults for future containers.

The full template is [`config.example.toml`](config.example.toml). Unknown options
and invalid values produce errors. For example:

```toml
[environment]
cpus = 2
memory = "4g"
pids_limit = 1024
shm_size = "1g"

[network]
mode = "restricted"
bind = "0.0.0.0"
advertise_host = ""
allow = []
block = []

[ports]
# Host ports: 0 asks Docker to choose a free port.
files = 0
novnc = 0
desktop = 0
caido = 0
extra = [
  { host = 0, container = 3000, name = "app", protocol = "auto" },
  { host = 0, container = 3333, name = "custom-tcp", protocol = "tcp" },
]

[services]
# Optional full desktop: start manually from Services unless enabled here.
desktop = false
```

Service ports inside the container are fixed. `extra` publishes additional TCP
services; an application on port 3000 should listen on `0.0.0.0` inside the
container. ACF rejects conflicting ports and direct publication of its raw VNC or
browser debugging ports. The dashboard shows assigned host ports and usable
addresses.

`network.bind` controls which host interfaces accept published connections. Its
default, `0.0.0.0`, listens on all IPv4 interfaces. `network.advertise_host`
controls the address shown in the dashboard. Leave it empty to select the host's
primary network address automatically, or set an IP address or DNS name such as
`acf.example.com` for a cloud server behind NAT. Use only the hostname or IP,
without a scheme, path, or port. This setting does not create DNS records or
change port forwarding. Changes to `advertise_host` appear on the next dashboard
refresh without restarting containers; binding changes require applying the
project configuration.

Custom TCP listeners are detected automatically. A short local HTTP `HEAD`
request, optionally over TLS, recognizes HTTP/HTTPS; results are cached per
socket for 30 seconds. Requests stay inside the environment and send no
credentials. Optional `ports.extra` fields are `name` and `protocol`, where
`protocol` is `auto` (the default), `http`, `https`, or `tcp`. An explicit protocol
controls address formatting when automatic recognition is unsuitable.

The old `ports.novnc_secondary` setting is accepted and ignored for compatibility.
Browser profiles share one noVNC endpoint using `ports.novnc`. The optional full
desktop has its own HTTP endpoint using `ports.desktop`.

`services.desktop` controls whether the full desktop starts with the environment.
It defaults to `false`; the setup wizard offers the same choice. Setting the
Desktop host port does not start the service. Later container settings edits are
independent of `config.toml`; Sync saves the autostart choice for the next start.

Synchronizing copies current instructions, skills, custom tools from `bin/`,
login seeds, and the selected container's saved credentials. Changes to container
resources, ports, network policy, integration startup settings, or provider model defaults
require **Apply configuration / recreate containers** from the actions menu. This stops
sessions, recreates containers using current settings, and retains named volumes.
See the persistence limits below before applying.

## Data and persistence

The host stores configuration and coordination data, while Docker stores project
files:

```text
ACF_HOME/                         default: ~/.local/share/acf
├── config.toml                  creation defaults, API keys, OOB key path
├── auth/
│   ├── codex/                   saved Codex login
│   └── claude/                  saved Claude login
├── shared/
│   ├── AGENTS.md                optional global instructions
│   ├── prompts/                 optional global prompts
│   └── skills/                  optional global skills
└── projects/<name>/
    ├── state.json               container settings, credentials, pending edits
    ├── AGENTS.md                optional project instructions
    ├── prompts/                 optional project prompts
    └── skills/                  optional project skills
```

| Container path | Storage and lifetime |
| --- | --- |
| `/workspace` | Project named volume; research, code, reports, artifacts |
| `/root` | Project named volume; Codex state and browser profiles |
| `/home/agent` | Project named volume; Claude/provider state |
| `/opt/acf-tools` | Project named volume; installed research tools and skill dependencies |
| `/opt/acf/project` | Copied instructions, skills, auth seeds, and custom tools; replaced during sync |
| `/usr/local/bin` | Commands in the writable layer; checkout `bin/` tools copied on creation, start, and sync |
| `/run/acf` | Temporary runtime state and copied secrets, recreated on start |
| Other paths, including `/usr`, `/etc`, `/var` | Container writable layer |

Stopping and starting the same container preserves its writable layer. **Removing
or applying a project discards that layer**, including packages installed with
`apt`, system configuration changes, and service logs outside named volumes. Use
a custom image (`environment.image`) for repeatable OS changes, or keep tools and
important data in persistent paths. ACF does not snapshot the entire machine or
provide automatic backups. External Docker cleanup can also delete unused named
volumes; keep independent backups of valuable data.

Use the dashboard's **Import files from host** and **Export files to host** actions
to move files under `/workspace` without granting ongoing host filesystem access.
The web file explorer also uploads and downloads files. Imported and
exported files remain untrusted workload content; copying them does not make
them safe to execute on the host.

## Custom binaries and scripts

Put custom tools in this checkout's `bin/` folder. ACF copies its contents to
`/usr/local/bin` inside each container when creating, starting, or synchronizing
it. For an existing container, press **u** in the dashboard, or use **Sync all
running environments** for all running containers.

For example, `bin/mytool` becomes `/usr/local/bin/mytool`; run `mytool` from the
container's root shell. Files are installed executable, with their contents
unchanged. Subdirectories and empty directories are preserved. Host permissions
stay unchanged, and placeholders in scripts are not expanded. Use scripts with
an appropriate shebang or Linux binaries built for the container's architecture.
Source symlinks are rejected; put the actual files in `bin/`.

Matching destination files are replaced on sync. Removing a tool from `bin/`
leaves its installed copy in existing containers; remove that copy from the
container shell when needed. Each container gets its own copy. `/bin/` is ignored
by Git so custom tools remain local to this checkout.

## Shared instructions and skills

Resource precedence is checkout `AGENTS.md`/`prompts/`/`skills/`, then
`ACF_HOME/shared`, then `ACF_HOME/projects/<name>`. A later prompt with the same
relative path replaces an earlier one. A later skill with the same name replaces
its earlier package. Instruction files combine, and prompts are read in sorted
relative-path order. Optional `CLAUDE.md` instruction files are included in the
common bundle too.

Standard skill folders contain `SKILL.md` and supporting files. Standalone
Markdown skills, such as `skills/custom/reporting.md`, become skill packages
during staging. Exported `.skill` ZIP archives also work: put one in
`skills/custom/` and ACF unpacks its instructions, scripts, references, and
assets into the sandbox. An archive must contain one `SKILL.md` at its root or
inside a single top-level skill folder. A containing folder supplies the skill
name; flat archives use the archive filename without `.skill`. Invalid archives
produce a sync error instead of silently disappearing. Archives are limited to
10,000 entries and 100 MiB unpacked. Nested vendor packages are discovered and
flattened by package name. Source symlinks, archive symlinks, and paths escaping
the package are rejected. Binary resources and executable permissions are
preserved; the staged skill tree is readable by nonroot agents.

Skills are copied on creation, start, and dashboard Sync (`u`), rather than
mounted live from the host. After adding or editing a host skill for an existing
container, select that container and press `u`. In an already running GLM,
OpenRouter, or Claude session, run `/reload-skills` or restart the agent to
refresh discovery. [Claude's skill reload documentation](https://code.claude.com/docs/en/skills#edit-a-skill-during-a-session)
describes session refresh. Starting `glm` from the container shell reads the
latest copied snapshot; it does not copy new host files itself.

After updating ACF's Python code, quit and reopen `./acf` before synchronizing.
The dashboard keeps its loaded code until it exits. ACF checks for code changes
before operations that copy resources and asks for a dashboard restart if its
loaded version is stale. Replacing skills or editing `config.toml` does not
require restarting the dashboard. Closing it preserves containers and agent
sessions.

Dashboard agent shortcuts use the `acf-agent` wrapper inside the environment. It
passes combined global and project instructions to Codex as developer
instructions and to Claude as an appended system prompt. Skills appear through
the agents' normal home skill paths. From the container's root shell, launch
GLM or OpenRouter directly:

```sh
glm
openrouter

# Select a model for this session.
glm --model glm-5.3
openrouter --model anthropic/claude-sonnet-4.6
```

These commands launch Claude Code using the selected provider's API key and an
independent provider configuration directory. You can supply keys directly in
the container's root shell:

```sh
export GLM_API_KEY='your-zai-key'
glm --model glm-5.3

export OPENROUTER_API_KEY='your-openrouter-key'
openrouter --model anthropic/claude-sonnet-4.6
```

Exported keys take precedence over credentials saved for the container. No
global setting, file edit, or sync is needed. Run the command from that same
shell; an already-running agent keeps its existing key until you exit and launch
it again. The exports do not change `config.toml` or the container's saved
settings. `unset GLM_API_KEY` or `unset OPENROUTER_API_KEY` restores use of a saved
key, if one exists. An explicitly empty exported key disables that fallback.

The launcher handles the switch from root to the `agent` user. Omit `--model`
to use the container's configured model. A model selected on
the command line applies only to that session; it does not change `config.toml`
or require recreating the container. Use a model identifier available to your
provider account. `glm --help` and `openrouter --help` work before adding a key.

The commands are installed in `/usr/local/bin`, so they are available in root
login shells as well as dashboard terminals. Sync installs or repairs them in
existing containers without restarting sessions. The original wrapper also
remains available for custom arguments and the other agents:

```sh
acf-agent codex
acf-agent claude
```

Invoking raw `codex` or `claude` does not apply ACF's prompt injection.
Plain `codex` uses the container's `/etc/codex/config.toml` defaults:
`sandbox_mode = "danger-full-access"` and `approval_policy = "never"`.
Docker and ACF's network guard provide isolation; Codex does not try to create
a nested Bubblewrap sandbox, which Docker can block with
`bwrap: No permissions to create a new namespace`. This follows the
[official guidance for Codex in Docker](https://learn.chatgpt.com/docs/agent-approvals-security#os-level-sandbox).
Build, startup, and Sync install these defaults independently of login.
Explicit Codex user/project settings and CLI flags take precedence. Restart an
already-running Codex session after syncing to load the defaults.

Project-local workspace instructions can also be read according to each agent's
normal behavior. Starting a project, attaching a terminal, or launching an agent
from the dashboard refreshes the staged bundle. Press `u` to update a running
project explicitly, or choose the menu's **Sync all running environments** action.
Existing conversations do not reload system instructions automatically; exit the
agent and start a fresh session after editing prompts.

The active resource directories are ignored by Git; example and vendor snapshots
remain versioned. Placeholders resolve in prompts and text skill resources:

| Placeholder | Value |
| --- | --- |
| `{{PROJECT_NAME}}` | Project name |
| `{{PROJECT_PATH}}` | `/workspace` |
| `{{CONTAINER_IP}}` | Current internal Docker IP |
| `{{OOB_DNS}}` | `oob.dns` |
| `{{OOB_SSH_HOST}}` | `oob.ssh_host` |
| `{{OOB_SSH_USERNAME}}` | `oob.ssh_username` |
| `{{OOB_SSH_KEY}}` | `/run/acf/oob_key` |
| `{{CAIDO_PORT}}` | Internal port `8080` |
| `{{NOVNC_PORT}}` | Primary internal port `6080` |
| `{{FILE_MANAGER_PORT}}` | Internal port `8081` |

Unknown placeholders remain unchanged with a warning. ACF never substitutes API
keys, tokens, or passwords. The OOB placeholder names a path; it does not include
the key's contents or guarantee that a key has been configured.

## Credentials and providers

Put API keys in the `[credentials]` table of `config.toml`; the setup wizard and
container settings editor can override them for one container. Values are
literal TOML strings: ACF does not evaluate shell commands, expand variables in
keys, or discover credentials from the host environment. Keep manually created
configuration and key files private (`chmod 600`). Managed settings and drafts
are written with private permissions.

```toml
[credentials]
OPENAI_API_KEY = ""
ANTHROPIC_API_KEY = ""
OPENROUTER_API_KEY = ""
GLM_API_KEY = ""
CAIDO_PAT = ""

[oob]
dns = "oob.example.com"
ssh_host = "oob.example.com"
ssh_username = "researcher"
ssh_key_path = "~/.ssh/acf_oob"
```

The private key stays in a separate file; `ssh_key_path` contains only its path.
Relative paths in a defaults file resolve against that file's directory. ACF
expands `~` on the host and saves a resolved path in each container's settings.
No `.env` file is needed. On first startup after upgrading, ACF migrates legacy
`.env` credentials and project settings into the new configuration/state,
keeps private `.bak` copies under `ACF_HOME/migration-backups/`, and removes the
migrated `.env` files.

Only keys belonging to enabled integrations are copied:

| Integration | Allowed secret keys |
| --- | --- |
| `codex` | `OPENAI_API_KEY` |
| `claude` | `ANTHROPIC_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY` |
| `glm` | `GLM_API_KEY` |
| `caido` | `CAIDO_PAT` |
| `oob` | `oob.ssh_key_path` — dedicated host key file, copied separately |
| `gmail` | `GMAIL_ACCESS_TOKEN`, `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` |

All listed integrations except Gmail are enabled by default. For a project that
only needs Codex, set `integrations.enabled = ["codex"]`. Host agent login files
are never imported implicitly. `./acf login codex` and `./acf login claude`
authenticate in temporary containers and save selected credential files under
`ACF_HOME/auth` with private permissions. A successful login also delivers that
provider's credentials to running environments where it is enabled. Stopped
environments receive them on their next start. If an environment cannot be
updated, login prints a warning so you can retry with dashboard Sync.

Projects receive copies rather than sharing a writable authentication directory.
Sync installs these copies into the agents' native home directories, including
`/root/.codex/auth.json`, so plain `codex` in a root shell can use your saved
login. This follows [Codex's file-based authentication](https://learn.chatgpt.com/docs/auth#credential-storage).
An unchanged login seed preserves credentials refreshed inside the container;
a new global login replaces the previously installed seed. Local refreshes are
not exported to the global account. Repeat the login entrypoint when global
authentication needs renewal. Close and reopen an agent already displaying its
login screen after the credentials arrive.

API keys are copied into the root-owned, mode-`0600` JSON file
`/run/acf/secrets.json` on the container's temporary filesystem. Agent launchers
and helpers load them into the environments of the programs they start; they
are not automatically exported into every container shell.

The dedicated OOB key is copied to `/run/acf/oob_key` with mode `0600` for root.
It is not placed in an environment variable or `~/.ssh`, and the host SSH
directory is not mounted. Connect using the configured username and host:

```sh
ssh -i /run/acf/oob_key -l researcher oob.example.com
```

An unprivileged agent can use `sudo ssh` to read the root-owned key. The prompt
placeholders include the container key path, host, and username so agents can
use them without receiving the private key as prompt text. Removing a credential from
configuration cannot retract authority from a running process or erase copies a
root agent has made; revoke credentials at the provider when necessary.

**Root inside a project can read every credential supplied to it.** Reusing a
global account deliberately gives multiple projects authority over the same
external account. Filesystem separation does not separate that authority. Supply
credentials appropriate for the workload.

Provider sessions use Claude Code with separate provider configuration folders:

```toml
[integrations]
enabled = ["codex", "claude", "openrouter", "glm", "caido", "oob"]
openrouter_model = "anthropic/claude-sonnet-4.6"
glm_model = "glm-5.3"
```

Select models supported by your provider account and launch them with `o` or `g`
in the dashboard. Codex runs as root. Claude runs as `agent` with passwordless
sudo because its unattended mode cannot run directly as root. The wrappers
enable unattended CLI execution within the Docker environment.

## Browsers, files, and research tools

Press `a` on the selected project to open its service-address panel. Services publish to
all host IPv4 interfaces by default. The displayed links use the host's detected
network IP; custom DNS is optional.

| Service | Container port | Access |
| --- | --- | --- |
| Caido UI and proxy | `8080` | Caido's own account/project setup |
| Web file explorer | `8081` | Dashboard link with the access token in its path |
| Browser profiles | `6080` | Dashboard link with the access token; Primary and Secondary profile buttons |
| Full desktop | `6082` | Stopped by default; separate HTTP port, dashboard link with the access token |

Press `v`, or choose **Start/stop services and endpoints** in the actions menu,
to manage Browser, Desktop, Files, and Caido. Select with Up/Down, then press
Enter or `s` to toggle; `+` starts and `-` stops. Service actions affect the
selected running environment and do not change `config.toml` or its saved
autostart setting. Custom discovered endpoints remain visible for access, but
ACF only controls services managed by its supervisor.

The full desktop provides XFCE with a taskbar, file manager, and terminal on
its own display. Start **Desktop**, then open its full HTTP URL from the dashboard.
Browser profiles and Desktop start and stop independently. The desktop browser
also uses the mandatory Caido proxy. Stopping a service closes that service's
processes; stopping Caido makes proxied browsing unavailable until it starts again.
Runtime service choices last until the environment stops; startup restores the
saved defaults.

The file explorer uses Rookgaard's interface: a folder tree,
breadcrumbs, file details, text previews, rendered/raw Markdown, copy contents,
uploads, and downloads. Quick access opens Home (`/root`), Downloads
(`/root/Downloads`), and Workspace (`/workspace`). **Upload files** lets you
select one or more files and shows their destination before you submit: the
current folder, or the containing folder when previewing a file. Check
**Use Home Downloads** to upload the selected files there instead. Downloads is
created automatically. Uploads stream without an application size cap. You can
change filenames; existing files are never overwritten. Each file shows its
upload result, and failed files can be retried without uploading completed ones
again. Files download
as attachments; virtual device and process file contents are not served.
Previews remain read-only. Use the container terminal to edit existing files,
or the dashboard's import/export actions for other transfers.

Sync updates the file explorer in existing containers and restarts its service
only when the files changed. Browsers and agent sessions continue running.

Open the single noVNC URL and use the **Primary** and **Secondary** toolbar
buttons, or **Ctrl+Alt+1** / **Ctrl+Alt+2**, to switch browser profiles on the
shared desktop. Selecting a stopped profile starts it. Both profiles retain
independent Chromium processes, cookies, data directories, and debugging ports;
they share the display, VNC connection, project filesystem and credentials.
The noVNC WebSocket requires the environment's access token, which the dashboard
link passes in noVNC's `path` setting. Environments started before this change
keep accepting tokenless noVNC connections until they are stopped and started
again; the services panel shows a notice until then. CDP listens inside the container on
`127.0.0.1:9222` and `127.0.0.1:9224`. Raw CDP and VNC ports are not published to
the host.

Existing projects keep running until you choose to update them. To add the new
desktop packages, its published port, and service controls, use **Build container
images** (`m`, then `b`), then select
the project and choose **Apply configuration / recreate containers** (`m`, then
`p`). Building alone does not restart or recreate a project. Applying stops
sessions and discards its writable OS layer while preserving named volumes,
including the existing `chromium-primary` and `chromium-secondary` profile data
under `/root/.config/acf/`. Profile cookies and sign-ins stay in those directories.

Both browser profiles and manual Chromium launches always use Caido at
`http://127.0.0.1:8080`, including localhost, HTTP, HTTPS and WebSocket requests.
There is no direct fallback if Caido is unavailable. QUIC, direct WebRTC UDP and
direct browser DNS are disabled. Managed launches trust the container's Caido CA
automatically without disabling certificate checks. Complete
Caido sign-in and project selection; ACF does not authenticate your Caido account.
Supply `CAIDO_PAT` for
the bundled Caido API skill.

Ghidra and Interactsh install on demand with verified upstream archives and
persistent tool storage. Press Enter in the dashboard, then run these commands
**inside the project terminal**:

```sh
acf-install ghidra
# The GUI opens on the shared noVNC desktop:
ghidra
acf-install interactsh
interactsh-client -h
acf-services --json
acf-services restart browser-primary
tail -n 100 /var/log/acf/caido.log
```

Install Ghidra before invoking its bundled skill scripts directly. ARM64 setup
builds native components and takes longer. Prepare tools before switching to an
offline policy. Caido skill dependencies install from the included npm lockfile
and are cached in the tools volume.

Gmail is opt-in and read-only through the supplied helper. Enable `gmail` and
supply an access token or OAuth client/refresh credentials with `gmail.readonly`
scope. ACF does not conduct Gmail OAuth enrollment or send mail. Inside the
project terminal:

```sh
acf-gmail list --query 'is:unread'
acf-gmail get MESSAGE_ID
```

The [runtime documentation](src/acf/runtime/README.md) contains implementation
contracts and primary upstream references. The [example library](examples/README.md)
identifies the official Caido skill and community Ghidra skill. Vendor packages
include licenses and provenance; [the manifest](examples/skills/vendor/manifest.json)
pins revisions and file hashes.

## Network policy and remote access

The default `restricted` policy permits outbound public IPv4 traffic and blocks
RFC1918 private ranges, link-local/cloud metadata ranges, carrier-grade NAT,
multicast, and reserved ranges. The project's Docker subnet and gateway are
always denied under this policy. IPv6 egress is disabled. The workload lacks
`NET_ADMIN` and cannot change the guard's firewall.

Use IPv4 addresses or CIDRs in `network.allow` for specific private targets.
Explicit `network.block` entries, the project subnet, and its gateway take
precedence over `allow`. ACF cannot infer which public IPs belong to your host or
organization; add those to `block` where appropriate. The policy is not a domain
allowlist or authorization to test targets.

`mode = "offline"` blocks outbound connections while permitting replies through
published services. `mode = "open"` explicitly allows unrestricted IPv4 egress,
including private networks; its allow/block lists do not constrain traffic.
Apply changed policy from the dashboard's actions menu. Workloads start only
after their guard is healthy. Projects do not automatically restart after Docker
or the host restarts; select them and press `s` to restore them with the required
startup ordering.

The same dashboard works when launched locally, over SSH, or on a cloud server.
Published service ports listen on `0.0.0.0` by default, while their links show a
usable host address rather than the wildcard bind address. The `restricted`
policy controls outbound traffic; it does not block replies to LAN clients
connecting through published ports.

For a cloud public IP or DNS name that differs from the machine's interface
address, edit the selected container's settings in the dashboard, or change
`config.toml` to use that address for future containers:

```toml
[network]
bind = "0.0.0.0"
advertise_host = "acf.example.com"
```

Allow the assigned host ports through the host firewall and cloud security group;
if a router performs NAT, forward those ports to the Docker host. Fixed host
ports in `[ports]` can simplify those rules. ACF does not alter external
firewalls, create router mappings, or supply a TLS gateway. An HTTPS reverse
proxy or SSH tunnel can provide encrypted remote access.

Existing configuration files retain their saved bind address. To update a
project still bound to `127.0.0.1`, change its effective `network.bind` setting
and choose **Apply configuration / recreate containers** (`m`, then `p`). This
stops sessions and discards the writable OS layer while preserving named volumes.
Changing the dashboard's displayed hostname alone cannot make a loopback-only
listener reachable remotely.

For access through an SSH tunnel, explicitly set `network.bind = "127.0.0.1"`
and apply that configuration. If the server's service-address panel reports files
`49170`, noVNC `49171`, and Caido `49173`, run this on your laptop:

```sh
ssh -N \
  -L 8081:127.0.0.1:49170 \
  -L 6080:127.0.0.1:49171 \
  -L 8080:127.0.0.1:49173 \
  user@your-server
```

Then open the links from the service-address panel after replacing each
`host:port` with your local tunnel port, for example
`http://127.0.0.1:8081/<token>/`. Keep the path and query: they carry the access
token.
ACF respects Docker contexts and `DOCKER_HOST`; published URLs refer to the
Docker daemon's host. Internal Docker IPs may not be reachable directly on Docker
Desktop.

## Docker startup and troubleshooting

ACF checks Docker when opening the dashboard or logging in. If a local daemon is
stopped, it attempts to open Docker Desktop on macOS. On Linux with systemd it
starts the rootless user service when applicable, or uses root/noninteractive
`sudo -n` to start the system service. ACF never installs Docker, changes its
configuration, or requests a sudo password. Failed automatic startup produces an
actionable error. An unavailable remote Docker context does not start a local
daemon.

- Use the actions menu's diagnostics and network-guard logs when a project cannot
  start. Diagnostics inspect configuration rather than proving isolation.
- Check service status with `acf-services` and read `/var/log/acf/*.log` in the
  project terminal; the menu also provides workload logs.
- If a guard was externally removed, apply the project configuration to recreate
  the pair. Named volumes persist; the writable OS layer does not.
- Offline environments cannot download missing tools or contact AI providers.
- The service-address panel shows newly assigned host ports after recreation when ports
  are configured as 0.

## Isolation limits

ACF grants agents broad authority inside containers. Workloads are not
privileged, receive no Docker socket or host bind mounts, drop raw-socket and
device-node capabilities, and run behind a separate guard. CPU, memory, and
process counts are limited by configuration. The guard shares its network
namespace with the workload but exposes no control API or Docker socket.

Docker shares a kernel and is **not a virtual-machine security boundary** against
hostile native code. Kernel/runtime vulnerabilities, resource exhaustion,
container-controlled terminal output, browser content, downloaded artifacts, and
supplied credentials remain risks. Named volumes have no per-project disk quota.
Public egress can reach publicly exposed host services unless explicitly blocked.
For live malware or actively hostile workloads, use a disposable dedicated VM or
host without personal files or reusable credentials, with appropriate outer
network isolation.

## Development and verification

Host code lives in `src/acf/`, the runtime image in `src/acf/runtime/`, the guard
image in `src/acf/guard/`, and tests in `tests/`. The file explorer is copied from
the neighboring `rookgaard` project, with ACF paths, branding, and port settings.
That project also informed Docker lifecycle, persistence, network guards, and
terminal session design; ACF has its own code and state layout. The bundled
Markdown parser's notices and licenses are in `src/acf/runtime/vendor/`.

Run standard-library unit tests without Docker:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The suite covers configuration, credentials, resources, bounded subprocess
transport, dashboard behavior, and runtime components. Unit tests are not proof
of isolation against arbitrary hostile software.

The opt-in integration test uses the internal manager directly, creates temporary
projects, and builds missing images automatically. The first run can take several
minutes and needs network access. It checks guarded startup, root/sudo access,
credential-free file access, the shared noVNC page and profile-toolbar assets, structured
service reporting, automatic custom HTTP/TCP discovery, resource synchronization,
network restrictions, persistence, and offline policy, then cleans up its own
resources:

```sh
ACF_INTEGRATION=1 PYTHONPATH=src python3 -m unittest discover -s tests -p test_integration.py -v
```

Run it with a running daemon and Internet access. The test connects to published
HTTP and TCP services through the detected Docker host address, so that address
and its assigned ports must be reachable from the test runner. Provider
authentication and live AI requests need your accounts and are not exercised by
this test.

To check native skill discovery using Claude Code in an existing ACF container:

```sh
ACF_SKILL_TEST_CONTAINER=<container-name> PYTHONPATH=src python3 -m unittest discover -s tests -p test_skill_registration_integration.py -v
```

This stages a benign `.skill` fixture and checks registration in isolated Claude,
GLM, and OpenRouter configurations. It uses fake credentials and a loopback API
endpoint, sends no model prompts, and removes its temporary files afterward.
