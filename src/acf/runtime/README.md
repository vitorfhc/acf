# ACF container runtime

The dashboard builds this directory as the Docker context. The image is Debian
trixie with Python, Node, Codex, Claude Code, tmux, Chromium, noVNC, and Java 21.
Agent CLI versions are pinned in the Dockerfile; update those pins deliberately.
The host supplies the isolation boundary. This image must never receive the
Docker socket, privileged mode, host devices, or host home directories.

Node 22 comes from the official `node:22-trixie-slim` base, satisfying the pinned
Claude CLI's Node requirement. Debian's older packaged Node is not installed.

`acf-boot` starts a small process supervisor and the `main` tmux session. Passing
a command runs that command instead, which supports temporary login containers.
Closing a terminal or SSH connection leaves the tmux session running. Restarting
the container stops processes; agent CLI histories and project files persist in
their respective volumes.

Open `./acf` for environment management or use `./acf login codex` /
`./acf login claude` for endpoint authentication. The dashboard supplies the
project terminal; run the helpers below inside that terminal.

## Host contract

- Workspace: `/workspace`; persistent homes: `/root`, `/home/agent`.
- Tool cache: `/opt/acf-tools`; mount a separate named volume here to retain lazy
  installations when recreating the container.
- Staged resources: `/opt/acf/project/{AGENTS.md,CLAUDE.md,system-prompt.md,skills}`.
- Custom tools: the checkout's gitignored `bin/` is staged under
  `/opt/acf/project/bin` and copied to `/usr/local/bin` on creation, start, and
  sync. Regular files are installed executable with unchanged bytes; directories
  are preserved. Source symlinks are rejected. Matching files are replaced, while
  other installed tools remain. Each container gets an independent copy.
- Auth seeds: `/opt/acf/project/auth/codex/auth.json`,
  `/opt/acf/project/auth/claude/.credentials.json`, and optional `claude.json`.
  The launcher copies newer global credentials into the project-local homes.
- Credentials: `/run/acf/secrets.json`, a JSON object of environment-variable
  names and literal string values, written root-only with mode `0600` on tmpfs.
  Launchers read it when starting programs; the file is never evaluated as shell.
  The host supplies values from the container's saved settings, initially copied
  from `[credentials]` in `config.toml`. Optional OOB key: `/run/acf/oob_key`,
  copied separately from the host's `oob.ssh_key_path`, also root-only mode `0600`.
  Use `ssh -i /run/acf/oob_key -l USER HOST` (or `sudo ssh` as an unprivileged agent).
- Environment: `ACF_PROJECT` identifies the project. Files and Browser do not
  require service credentials.
- `ACF_INTEGRATIONS` is the comma-separated integration allowlist from host
  configuration. A disabled Caido integration leaves its service stopped and
  skips skill dependency installation. Disabled agent providers refuse launch.
  This is configuration convenience, not a boundary against container root.
- Ports: Caido `8080`, files `8081`, Browser noVNC `6080`, and optional Desktop
  noVNC `6082` (stopped by default, independently published as `ports.desktop`).
  These listen on `0.0.0.0` inside the container; host publishing follows `network.bind`
  (all IPv4 host interfaces by default).
  Raw VNC `5901`/`5902` and CDP listen only on container loopback. CDP ports are `9222`, `9224`.
- `acf-services --json` reports services and tmux sessions; `start`, `stop`, and
  `restart` accept a service name. Logical `browser` and `desktop` actions manage
  each group's display, session, VNC, and HTTP processes together. Desktop actions
  refer to the optional full desktop; the legacy `desktop` entry in raw status is
  the browser's internal X server. Logs are in `/var/log/acf`.
- Startup defaults: `/opt/acf/service-settings.json` contains only
  `{"desktop": false}` (or true), copied before boot and on Sync. Runtime toggles
  do not modify these defaults or host `config.toml`. The supervisor's version-2
  control protocol uses individually acknowledged requests and reports logical
  `service_controls` alongside internal process status.

The optional XFCE desktop runs on display `:2`, independently of Browser on `:1`.
It includes a panel, file manager, terminal, and a Chromium launcher with the
mandatory Caido proxy. Its separate noVNC webroot has no browser-profile toolbar.
All four desktop processes stay stopped until requested or enabled at boot.
Upgrading an existing environment to the desktop requires the updated image and
its published port; build and explicitly Apply configuration from the dashboard.

The web file explorer is copied from Rookgaard and serves the container
filesystem below a capability path: every route except the public
`/assets/` libraries starts with `/<token>/`, where the token is read from
`/run/acf/access-token` on each request. The host issues a new token at each
environment start and writes it during Sync, on stdin like other secrets; until
then the explorer answers 503. Cookies are deliberately not used, because
browsers send them to every port on a host, including other projects' services.
It provides a folder tree, breadcrumbs, text previews,
rendered/raw Markdown, copy contents, uploads, and downloads. Quick access opens
`/root`, `/root/Downloads`, or `/workspace`. Upload files shows the current folder
(or a previewed file's parent) and lets the user choose Home Downloads instead
for the whole selection. Each file has its own status and failed files can be
retried without resending completed uploads.
`GET /<token>/api/config` creates Downloads if missing. `POST /<token>/api/upload?path=DIR&name=NAME`
accepts one raw octet-stream file with `X-ACF-Upload: 1` per request, with no
application size cap. The UI sends selected files in sequence. Uploads stream
in 64 KiB chunks into temporary files and publish without overwriting existing names;
cross-origin browser requests and incomplete uploads are rejected. Existing
files are edited in the terminal. Downloads are attachments rather than
executable HTML. Virtual device and process file
contents are blocked, text previews and copies are bounded, and directory lists
are paginated. The bundled Markdown parser and its licenses are in `vendor/`.

The single noVNC page has Primary and Secondary profile buttons. These send
Ctrl+Alt+1/2 through the VNC connection to switch Openbox desktops;
selecting a stopped profile starts it again. Both profiles share X display `:1`
and one VNC connection while retaining separate Chromium processes, CDP ports,
and the existing `/root/.config/acf/chromium-primary` and `chromium-secondary`
data directories. Their cookies and sign-ins survive this layout change. They
share the container filesystem and are not security boundaries. websockify
serves the noVNC page publicly but uses its `TokenFile` plugin, so a WebSocket
connects only with `?token=<access token>` (from noVNC's `path` setting).
Sync writes `/run/acf/novnc-token` and `/run/acf/desktop-token`, which map the
token to the loopback-only VNC servers on 5901 and 5902; without them every
connection is refused. All Chromium web traffic uses Caido at
`http://127.0.0.1:8080`, including HTTP, HTTPS, WebSockets, localhost and link-local
destinations. Mandatory Chromium policy also covers manual Chromium launches;
there is no direct fallback when Caido is stopped or unavailable. QUIC and
non-proxied WebRTC UDP are disabled, while DNS prediction, browser DoH and direct
hostname resolution are disabled so target DNS resolution happens at the proxy.
Each managed browser launch imports the active Caido CA into root and agent NSS
trust stores before opening, preserving other trusted certificates and leaving
TLS certificate verification enabled. A disabled Caido integration therefore
leaves browsers without network access. Caido still needs its account/project
setup to capture requests. Policies are browser configuration, not an isolation
boundary against container root deliberately replacing them or installing a
different browser.
Sync installs the policy and refreshes legacy managed browser processes. New
containers receive current browser startup code before their first start, even
when using an older image. Profile data persists, and restarted profiles request
restoration of their previous tabs.
Browser stops first request Chromium's `Browser.close` through its local CDP
connection so profile databases flush; process signals remain a bounded fallback.

## Agents and integrations

`acf-agent codex|claude|openrouter|glm [arguments...]` loads fresh staged resources
on every launch. Codex runs as root with the merged instructions injected as
developer instructions. Claude runs as `agent` with passwordless sudo and merged
instructions appended to its system prompt. Both agents discover the staged
skills through their normal home skill paths. Provider modes use distinct Claude
configuration directories. Caido skill npm dependencies install from its lockfile
on first agent launch; this requires network access once.

`install_codex_config.py` seeds `/etc/codex/config.toml` with
`sandbox_mode = "danger-full-access"` and `approval_policy = "never"` at build,
boot, and resource sync. This makes native `codex` use the same outer-container
isolation as the ACF launcher, without requiring nested Bubblewrap namespaces.
The defaults work for both users and custom `CODEX_HOME` directories, independently
of authentication. Existing system settings are preserved; Codex user/project
configuration and explicit command-line settings can override these defaults.

`agent_auth.py` installs staged login seeds into the root and agent homes during
resource sync, so a direct `codex` shell command sees the saved login too. This
step is independent of agent launch and skill dependency setup. Credentials and
seed fingerprints are private; unchanged seeds preserve locally refreshed
tokens, while a new global login replaces the previous seed. Login delivery can
select a single provider and does not restart services or agent sessions.

The host stages skill folders, standalone Markdown, and `.skill` ZIP archives
under `/opt/acf/project/skills/<name>/SKILL.md`, including supporting files.
GLM and OpenRouter discover these through `.claude-glm/skills` and
`.claude-openrouter/skills` in the agent home. Host changes require dashboard
Sync (`u`); a shell launch cannot fetch host files. Run `/reload-skills` in an
existing Claude-backed session after syncing, or restart the agent.

From a container root shell, use `glm` or `openrouter` to launch those provider
modes. Both accept the Claude Code arguments, including an explicit model:

```sh
export GLM_API_KEY='your-zai-key'
glm --model glm-5.3

export OPENROUTER_API_KEY='your-openrouter-key'
openrouter --model anthropic/claude-sonnet-4.6
```

Omitting `--model` uses the configured container default. An explicit model
overrides the session's model without changing host defaults or recreating the
container. Keys exported in the container shell take precedence over the private
runtime JSON credentials, including explicitly empty values. Without an export,
the saved key remains the fallback. `unset GLM_API_KEY` or
`unset OPENROUTER_API_KEY` restores that fallback. Exports need no configuration
edit or sync and do not change saved settings. Launch from the same shell;
already-running agents keep the credentials they started with.
The launcher handles the user switch from root. `--help` works without credentials
or agent preparation. Managed commands are installed in `/usr/local/bin`, which
remains on the root login shell's PATH; synchronization repairs existing
container installations without restarting their sessions.

OpenRouter uses `OPENROUTER_API_KEY` and optional `OPENROUTER_MODEL`. GLM uses
`GLM_API_KEY`, optional `GLM_MODEL`, and optional `GLM_BASE_URL`. Caido skills use
`CAIDO_PAT` and the default local `CAIDO_URL`. The user must sign into Caido and
select a project before research traffic can be recorded.
The host passes configured model defaults as `ACF_OPENROUTER_MODEL` and
`ACF_GLM_MODEL`, which the launcher also honors.

The default is `glm-5.3`, as documented in [Z.ai's GLM-5.3 guide](https://docs.z.ai/guides/llm/glm-5.3).
Sync upgrades the legacy ACF container default `glm-5` at launch time, so existing
containers do not need recreation. An explicit `--model` or exported `GLM_MODEL`
always wins, including when deliberately selecting an older model.

`acf-install caido|ghidra|interactsh` installs tools inside the container, checking
upstream archive hashes before extraction. Caido installs automatically at first
service start. Ghidra 12.1.4 is pinned by SHA-256 for compatibility with JDK21
(12.2 requires JDK25); on ARM64 it builds native tools
using the release's Gradle wrapper. `ghidra`, `analyzeHeadless`, and
`interactsh-client` install their tools on first invocation. Run
`acf-install ghidra` before invoking the Ghidra skill scripts directly.

`acf-gmail list --query 'is:unread'` and `acf-gmail get MESSAGE_ID` read the official
Gmail API. Supply `GMAIL_ACCESS_TOKEN`, or `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`,
and `GMAIL_REFRESH_TOKEN` authorized with the `gmail.readonly` OAuth scope. This
helper does not send mail or change mailbox state. Credentials must be explicitly
enabled for a project by host configuration.

Root inside a project can read all credentials intentionally supplied to that
project. Shared login accounts are reusable authority, even though their files
are copied rather than sharing a writable directory. Only supply credentials
appropriate for the workload. No host credential discovery is performed here.

## Upstream references checked for implementation

- [noVNC RFB API](https://novnc.com/noVNC/docs/API.html)
- [Chrome DevTools Browser protocol](https://chromedevtools.github.io/devtools-protocol/tot/Browser/)
- [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Claude Code settings](https://code.claude.com/docs/en/settings)
- [OpenRouter Claude integration](https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration)
- [Z.ai Claude integration](https://docs.z.ai/devpack/tool/claude)
- [Caido releases](https://caido.download/releases/latest)
- [Ghidra releases](https://github.com/NationalSecurityAgency/ghidra/releases)
- [Interactsh](https://github.com/projectdiscovery/interactsh)
- [Gmail messages API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)
