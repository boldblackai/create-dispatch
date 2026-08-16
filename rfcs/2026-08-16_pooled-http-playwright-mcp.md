# Pooled HTTP Playwright MCP server for generated claws

**Date:** 2026-08-16
**Status:** Proposed

## Goal

Give generated claws an optional **pooled browser backend**: a long-lived
Playwright MCP server in HTTP mode (`--port` + `--isolated`), started by the
container boot command and shared by every MCP client in the container —
**one chromium launch, one `BrowserContext` per client** — instead of the
default stdio server's one-browser-per-server, single-shared-context model.

This bounds browser memory for multi-client browser workloads and eliminates
cross-client tab/session collisions, while keeping the daemon's lifetime
structurally tied to the container (no orphaned browser processes).

## Motivation

### Today: stdio mode, one browser, one shared context

The upstream Playwright MCP server in stdio mode (the default MCP client
configuration) runs **one browser instance with a single default context** for
the lifetime of the server process. On a long-running claw this has three
consequences:

1. **Idle browser memory persists.** A live headless chromium tree measures
   ~0.6–1.5 GiB RSS depending on page weight. With a persistent-profile stdio
   server the browser stays up for the gateway's lifetime, whether or not any
   browser work is in flight — and it competes with the gateway baseline inside
   the ECS task's memory limit.
2. **Concurrent clients collide.** The stdio server exposes one shared context:
   parallel callers (subagent threads in-process, background review passes)
   fight over the same tabs and session state. Upstream issue
   `microsoft/playwright-mcp#893` documents exactly this failure mode.
3. **Per-client servers multiply cost.** Working around (2) by running one
   stdio server per client costs a full browser launch each: server chain
   (node + npm ~0.37 GiB) + chromium base (~0.7 GiB) per client. Three clients
   ≈ 3.3–5.6 GiB of browser+server memory before any page weight.

### The pooled shape

Playwright MCP's HTTP transport with `--isolated` implements the standard
Playwright pooling primitive — one `browserType.launch()` + N
`BrowserContext`s — natively (verified against the published 0.0.9x client
factory):

| deployment | browsers | contexts | ~3 clients, modest pages |
|---|---|---|---|
| N × stdio servers | N | 1 each (private) | 3.3–5.6 GiB |
| 1 × HTTP default | **N** (worse) | 1 each | same as stdio × N |
| 1 × HTTP `--isolated` | **1** | one per client | ~1.2–2.1 GiB |

Contexts are incognito-profile-equivalent: cookies, storage, service workers,
and tabs are fully partitioned per client — no cross-client collisions — while
the ~0.7 GiB fixed chromium cost is paid once. Still shared: the process tree
(a browser crash is a common fate), the compute stack, and the chromium build.

### Why the claw architecture makes this safe

The claw container runs one main process (`hermes gateway`, via the boot
`Command`'s `exec`), single-replica with recreate deployments. Starting the
MCP daemon in that same boot chain before the `exec` means:

- **No orphan window.** Container death ≡ gateway death; ECS reaps the whole
  PID namespace including the daemon and its browser tree. A standalone daemon
  can never outlive its clients.
- **Atomic bounces.** Recreate deployments restart daemon + gateway together.
- **Persistence for free.** The EBS-backed bind mounts already carry
  `~/.hermes`; the chromium install and daemon logs live there and survive
  instance replacement.

## Technical details

### 1. CloudFormation: opt-in parameter + boot command

New stack parameter `EnablePooledPlaywright` (default `false` — generated claws
are unchanged unless opted in). When enabled, the container `Command` gains a
supervised daemon loop before the gateway exec:

```yaml
Command:
  - sh
  - -c
  - |
    if [ -n "$GH_TOKEN_VAL" ]; then printf "%s" "$GH_TOKEN_VAL" | gh auth login --with-token 2>&1 || echo "[gh-auth] login failed (non-fatal)"; fi;
    if [ "$(echo "$ENABLE_POOLED_PLAYWRIGHT")" = "true" ]; then
      mkdir -p /data/.hermes/logs;
      ( while true; do
          npx -y @playwright/mcp@0.0.79 --headless --no-sandbox \
            --host 127.0.0.1 --port 8931 --isolated \
            --executable-path /data/.hermes/ms-playwright/chromium-*/chrome-linux/chrome \
            >> /data/.hermes/logs/pw-mcp.log 2>&1;
          echo "[pw-mcp] exited; restarting in 5s" >> /data/.hermes/logs/pw-mcp.log;
          sleep 5;
        done ) &
    fi;
    exec hermes gateway
```

Notes:

- **Bind loopback only.** The task uses host networking; `--host 127.0.0.1`
  keeps the daemon unreachable from outside the container's process namespace.
  The security group is already inbound-less; loopback binding plus
  `--allowed-hosts 127.0.0.1` defense-in-depth the HTTP surface.
- **No auth on the port** is accepted deliberately: the surface is loopback
  inside a single-purpose container. If a future claw shares the host with
  other tasks, this must be revisited.
- **Restart loop, not systemd.** The container has no init supervising
  optional services; a `while`/`sleep` wrapper matches the template's existing
  shell-first style. Crash logs land on the persistent volume.
- **Version pinning.** `@playwright/mcp` is pinned to an exact version, and the
  chromium install must match the server's `playwright-core` requirement
  (installed via `PLAYWRIGHT_BROWSERS_PATH` on the persistent volume, see
  below). No `@latest`: unversioned deploys break reproducibility and risk
  server/chromium skew. Bumping the pin is a template change that regenerates
  the golden test.

### 2. `agent_home/config.yaml`: the client entry

When the parameter is enabled, the curated config gains the HTTP client entry
(deep-merged onto the claw's live config by the manage skill's merge tool):

```yaml
mcp_servers:
  playwright:
    type: streamable-http
    url: http://127.0.0.1:8931/mcp
```

**Coupling caveat:** the stack parameter controls the daemon; the overlay
controls the client. They are deployed by different mechanisms (stack update
vs. manage overlay) and must converge. The manage skill's instructions gain an
ordering note: enable the stack parameter **first**, then push the overlay
entry. A client entry with no daemon is non-fatal — the gateway retries the
connection and parks the server — but noisy; a daemon with no client wastes
~0.4 GiB idle.

### 3. Browser install

The setup skill gains a phase (run when `EnablePooledPlaywright=true`):

```bash
export PLAYWRIGHT_BROWSERS_PATH=/data/.hermes/ms-playwright
npx -y @playwright/mcp@0.0.79 install-browser chromium
```

The `install-browser` subcommand installs the chromium build matching the
pinned server version into the persistent volume, so the executable path in
the boot command resolves across instance replacements.

### 4. What stays out of scope

- Repo-level Playwright test runners (`@playwright/test`) keep their own
  ephemeral browsers; pointing them at the pooled server via CDP/ws endpoints
  was considered and rejected (couples test results to long-lived gateway
  state, version-skews the test library, breaks failure isolation).
- Multi-container or cross-host pooling. This is one daemon per container.

## Memory accounting (why/when this pays)

With one MCP client the pooled shape costs the same as one stdio server
(single gateway client is the common case today) minus idle persistence, which
is better addressed for single clients by the MCP client's own idle-recycle
lifecycle settings. Pooling pays when the number of concurrent MCP clients
exceeds one — e.g. in-process subagent threads that each hold a browser
session, or future per-worktree agents connecting as separate HTTP clients.
Fixed-cost saving: ~(N−1) × 1.0 GiB for N clients. Page/renderer weight is
identical in both shapes and is not saved by pooling.

The daemon and its chromium count inside the same ECS task cgroup as the
gateway; `TaskMemory` remains the hard ceiling for the combined footprint.

## Migration notes

- **New claws:** no change unless `EnablePooledPlaywright=true` is passed; the
  parameter default preserves current behavior and the golden-test snapshot
  for the default path.
- **Existing claws:** (1) stack update enabling the parameter (new task-def
  revision, recreate deployment); (2) manage overlay pushes the client entry.
  Order matters (daemon first). Rollback is the reverse and returns the claw
  to stdio mode; the daemon loop simply never starts on next boot.
- **Golden test:** template diff is expected (parameter, command text, skill
  phase, config entry); regenerate the snapshot as part of the PR.

## Implementation checklist

- [ ] `template/.agents/skills/setup-dispatch/template.yaml`: parameter
      `EnablePooledPlaywright` (default `false`), container `Command` daemon
      loop, loopback + allowed-hosts hardening
- [ ] `template/agent_home/config.yaml`: `mcp_servers.playwright`
      streamable-http entry (documented as paired with the parameter)
- [ ] `template/.agents/skills/setup-dispatch/SKILL.md`: browser-install phase
      (`PLAYWRIGHT_BROWSERS_PATH` on the persistent volume, pinned
      `install-browser`)
- [ ] `template/.agents/skills/manage-dispatch/SKILL.md`: ordering note
      (stack parameter before overlay entry) + rollback note
- [ ] README: parameter documented in the deploy table
- [ ] Pin chosen `@playwright/mcp` version; record the chromium build it maps
      to; note the bump procedure (template change + golden regen)
- [ ] Regenerate golden test snapshot; verify default-path output is
      byte-identical to pre-RFC for claws generated with the parameter off
- [ ] CHANGELOG entry
