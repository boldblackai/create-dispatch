---
name: manage-bclaw
description: >
  Manage a running ECS (EC2 launch type) bclaw. Five modes: (1) Overlay — push the
  repo's agent_home/ onto the bclaw's ~/.hermes (EBS-backed) to update
  skills, memories, system prompt, or personas without a redeploy (via ECS
  Exec); (2) Run — execute arbitrary commands on the live bclaw for inspection,
  debugging, or one-off operations (via ECS Exec); (3) Merge-config — key-level merge of agent_home/config.yaml into live config.yaml; (4) Upgrade image — roll
  the running bclaw onto a new ghcr.io/boldblackai/harness tag by bumping the
  HarnessImageTag stack parameter and redeploying (no image rebuild);
  (5) Host — retrieve a stuck container instance's console output or force a
  wedged instance to replace itself, scoped to the claw's instances via
  aws:ResourceTag/ClawName. Companion to setup-bclaw / teardown-bclaw.
---

# Manage Harness ECS on EC2

Manage a live ECS (EC2 launch type) claw. This skill handles five related tasks:

1. **Overlay** (default) — push the repo's `agent_home/` onto the claw's
   `~/.hermes` to update curated state (skills, memories, prompts,
   personas) without a CloudFormation redeploy or image rebuild. Transferred
   over ECS Exec (SSM Session Manager). **`config.yaml` is excluded** from the overlay — use Mode 3 (Merge-config) for it.
2. **Run** — execute arbitrary commands on the live claw: inspect files,
   check process state, run diagnostics, or perform one-off operations like
   deleting a file that was removed from `agent_home/`. Also over ECS Exec.
3. **Merge-config** — deep-merge the repo's `agent_home/config.yaml` onto the
   live `~/.hermes/config.yaml` at the **key** level (local curated values win on
   overlap; the live config's comments, order, and env-driven keys are
   preserved), surface conflicts + mtimes for confirmation, and push the
   combination back over ECS Exec. Needs Python via `uv`.
4. **Upgrade image** — roll the running claw onto a new
   `ghcr.io/boldblackai/harness` tag by bumping the `HarnessImageTag` stack
   parameter and redeploying. No image rebuild (the signed upstream image is
   used as-is); ECS performs a recreate deployment (stop-old-then-start-new —
   the claw is single-replica and binds the Slack socket, so two tasks can't
   coexist; ~10-20s downtime during the swap). This is a CloudFormation
   stack update, not an ECS Exec operation.
5. **Host** — operate on the underlying EC2 **container instance** rather than
   the running task: retrieve its console output (boot/UserData log) when it
   fails to register to ECS, or terminate a wedged instance to force the ASG
   to launch a successor that reattaches the EBS volume. These actions are
   scoped to the claw's own instances via `aws:ResourceTag/ClawName`. They are
   the path when there is no running task to ECS Exec into.

Modes 1, 2, and 3 share the same prerequisites and ECS Exec transport (the
"Shared first step" below). Mode 4 needs the same shell state and a RUNNING
service but does not use the exec session. Mode 5 needs only the shell state
(mise + AWS creds) and operates on the EC2 instance, not the task,
so it works even when no task is RUNNING. Determine which mode the user
needs from context, or ask. When in doubt, default to **Overlay** (the
common case); for `config.yaml` changes, default to **Merge-config**.

## Prerequisites

1. **The claw is already set up and RUNNING.** This skill manages a live
   claw; it does not create one (use `setup-bclaw` first).
   Verify the task is `RUNNING` in the first step of either mode.

2. **ECS Exec permissions on the caller.** `aws ecs execute-command` uses SSM
   Session Manager. The deployer principal (the key in `.env`) needs
   `ecs:ExecuteCommand` (on the cluster + task) plus the four `ssmmessages:*`
   channel actions. These are already in the `bclaw-deploy` policy (`ECSExec`
   + `SSMMessages` statements) — no separate addition needed. If the caller
   still gets an `AccessDeniedException` naming `ssmmessages` or
   `ecs:ExecuteCommand`, re-attach the policy in the console (file edits
   don't take effect until re-attached).

3. **SSM Session Manager plugin installed locally.** Required by
   `aws ecs execute-command`. See the
   [install guide](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).
   If you lack root (no `sudo`/`dpkg -i`), extract the binary from the `.deb`
   without installing it:

   ```bash
   # ARM64 (aarch64) — adjust arch if needed
   curl -sL -o /tmp/smp.deb \
     "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_arm64/session-manager-plugin.deb"
   mkdir -p /tmp/smp && dpkg-deb -x /tmp/smp.deb /tmp/smp
   mkdir -p ~/.local/bin
   cp /tmp/smp/usr/local/sessionmanagerplugin/bin/session-manager-plugin ~/.local/bin/
   export PATH="$HOME/.local/bin:$PATH"   # add to shell rc for persistence
   session-manager-plugin --version       # verify: 1.2.xxx.x
   ```

   No mise/asdf plugin exists for this tool — `~/.local/bin` is the pragmatic
   install path.

4. **Shell with mise + AWS creds** (same as setup/teardown):

```bash
eval "$(/usr/local/bin/mise activate bash)" \
  && mise trust /workspace \
  && cd /workspace
```

All `aws` commands in this skill assume this shell state.

## Shared first step: connect to the claw

Both modes start here. Collect the **claw name** (default `bclaw`) and
**region** (default `us-east-1`) via `ask_user_question`. Then verify the
claw is live and ECS Exec works.

```bash
CLAW_NAME=bclaw
AWS_REGION=us-east-1
```

Verify the service exists and a task is `RUNNING`:

```bash
aws ecs describe-services --cluster "$CLAW_NAME" --services "$CLAW_NAME" \
  --region "$AWS_REGION" \
  --query 'services[0].{Status:status, Desired:desiredCount, Running:runningCount}' \
  --output table
```

Expected: `Status = ACTIVE`, `Running >= 1`. If `Running = 0`, the claw is
down — start it (`aws ecs update-service --cluster "$CLAW_NAME" --service
"$CLAW_NAME" --desired-count 1 --region "$AWS_REGION"`) or run the setup
skill first.

Capture the task ARN (reused in every later exec call):

```bash
TASK_ARN=$(aws ecs list-tasks --cluster "$CLAW_NAME" --region "$AWS_REGION" \
  --query 'taskArns[0]' --output text)
echo "$TASK_ARN"
```

**Smoke-test ECS Exec** — confirms the caller has exec perms and the plugin
works (the exec session runs as **root**):

```bash
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive --command "sh -c 'id && echo EXEC_OK'" \
  --region "$AWS_REGION"
```

> **`--command` must wrap compound commands in `sh -c`.** The exec session
> passes `--command` to the container's entrypoint, which does NOT interpret
> shell operators (`&&`, `||`, `;`, `|`). A bare `"id && echo EXEC_OK"` fails
> with `id: '&&': no such user` — each token becomes an argument to `id`.
> Always wrap compound commands: `--command "sh -c '...'"`. Single commands
> (`rm -f /path`, `wc -c < /path`) work fine unwrapped.

You should see `uid=0(root)...` and `EXEC_OK`. If you see an
`AccessDeniedException` naming `ssmmessages` or `ecs:ExecuteCommand`, the
caller lacks exec perms — see Prerequisites §2. If it complains about a
missing "Session Manager plugin", install it (Prerequisites §3).

---

## Mode 1: Overlay — push agent_home/ onto ~/.hermes

Overlays the repo's `agent_home/` directory onto the running claw's
`/home/harness/.hermes` — the EBS-backed home directory that persists config,
skills, memories, sessions, and plugins across task restarts.

### What "overlay" means

This is a **merge with overwrite**, not a replace:

- Files in `agent_home/` that are **new** → added to `~/.hermes/`.
- Files in `agent_home/` that **differ** → overwritten in `~/.hermes/`.
- Files in `~/.hermes/` **not in** `agent_home/` → **preserved** (sessions,
  runtime caches, `~/.config/gh` state, anything the claw generated at runtime).

You curate only what you want to control in `agent_home/` and leave the rest to
the claw. `tar` extraction into the destination implements exactly this — it
writes files present in the archive and never deletes anything. If a file was
removed from `agent_home/` and must also be deleted on the claw, use Mode 2
(Run) to `rm` it explicitly — or fold the `rm` into the Phase 3 extract step for
an atomic overlay+delete in one exec round-trip. This is always a manual,
per-run decision.

### Transport: tar + base64 over ECS Exec

There is no shared filesystem between the deployer machine and the ECS task,
and the deployer has no direct access to the container instance's `/data`
volume (the instance lives in the claw's VPC behind an inbound-less security
group). The transfer therefore goes over **ECS Exec** (SSM Session
Manager, already enabled by the setup template's `EnableExecuteCommand: true`):
locally tar+gzip+base64 the `agent_home/` tree, ship the base64 to the container
in chunks via `aws ecs execute-command`, then decode + extract + fix ownership
in a final call. No S3 bucket, no GitHub dependency, no image rebuild.

### The path mapping

`agent_home/` is the **root** that maps 1:1 onto `~/.hermes/`:

| Repo path | Lands at |
|---|---|
| `agent_home/config.yaml` | `/home/harness/.hermes/config.yaml` *(via Mode 3 Merge-config, NOT overlay)* |
| `agent_home/system-prompt.md` | `/home/harness/.hermes/system-prompt.md` |
| `agent_home/skills/foo/SKILL.md` | `/home/harness/.hermes/skills/foo/SKILL.md` |
| `agent_home/skills/.hub/taps.json` | `/home/harness/.hermes/skills/.hub/taps.json` |
| `agent_home/memories/...` | `/home/harness/.hermes/memories/...` |

Create `agent_home/` at the repo root and mirror the structure you want on the
claw. Include only files you intend to control — everything else is left alone.

### Phase 1: Prepare the payload

**Gate: shared first step (connect) passed.**

Confirm `agent_home/` exists and is non-empty:

```bash
test -d /workspace/agent_home \
  && find /workspace/agent_home -type f | head \
  || echo "agent_home/ does not exist or is empty"
```

If `agent_home/` is missing, there is nothing to overlay — stop and tell the
user to create it (see "The path mapping" above).

Create the tarball. Exclude things that should never be pushed (git metadata,
local caches, editor cruft):

```bash
( cd /workspace/agent_home \
  && tar czf /tmp/agent_home.tar.gz \
    --exclude='.git' --exclude='.DS_Store' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='.cache' \
    --exclude='AGENTHOME.md' \
    --exclude='config.yaml' \
    . ) \
  && ls -lh /tmp/agent_home.tar.gz
```

`AGENTHOME.md` (repo-level docs about the `agent_home/` directory itself) is
excluded so it doesn't land on the claw. `config.yaml` is excluded too — it is
handled by Mode 3 (Merge-config), not the file-level overlay; if you have one
staged in `agent_home/`, the overlay silently skips it (run Mode 3 to apply it).

Base64-encode it (single line, no wrapping) and measure:

```bash
base64 -w0 /tmp/agent_home.tar.gz > /tmp/agent_home.b64
B64_SIZE=$(wc -c < /tmp/agent_home.b64)
echo "base64 payload: ${B64_SIZE} bytes (~$(( B64_SIZE / 1024 )) KiB)"
```

**Size guidance:** payloads under ~300 KiB transfer in a handful of chunks and
take well under a minute. If the payload is much larger, you are probably
including caches or binaries that don't belong in `agent_home/` — review the
excludes. (For genuinely large payloads, see the S3-presigned-URL alternative in
Notes.)

### Phase 2: Dry-run — show what would change

**Gate: payload prepared (Phase 1).**

Before overwriting live state, show the user what the overlay contains and any
notable diffs against the claw's current copy.

List the files in the payload:

```bash
tar tzf /tmp/agent_home.tar.gz | sort
```

For the few files that matter most (e.g. `system-prompt.md`, a skill's `SKILL.md`),
diff against the claw's current copy (one exec round-trip each — do only for the
handful worth checking):

```bash
# Example: diff system-prompt.md against the live claw
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'cat /home/harness/.hermes/system-prompt.md 2>/dev/null || echo ABSENT'" \
  --region "$AWS_REGION" 2>/dev/null | sed 's/\r$//' > /tmp/claw_config.current
diff -u /tmp/claw_file.current /workspace/agent_home/system-prompt.md || true
```

> The `sed 's/\r$//'` strips the carriage returns the exec PTY appends to each
> line so the diff is clean. Apply the same filter when diffing any remote file. `config.yaml` is not in the
overlay (excluded — see Phase 1); use Mode 3 (Merge-config) to see and apply
`config.yaml` changes.

**Gate:** use `ask_user_question` to confirm the user wants to apply the overlay
(show the file list + any notable diffs). This overwrites live files — get
explicit confirmation before Phase 3.

### Phase 3: Transfer and overlay

**Gate: user confirmed the overlay (Phase 2).**

Ship the base64 payload to the container in chunks, verify the staged length
matches locally, then decode + extract + fix ownership. The exec session runs as
**root**, so `chown -R 1000:1000` is required after extraction — without it the
non-root gateway (uid/gid 1000) can't modify or delete the files later (the EBS
volume honors POSIX ownership).

```bash
DEST_B64=/tmp/.ah_update.b64          # staging path on the container
CHUNK_SIZE=30000                      # bytes per exec round-trip (see Notes)
B64_SIZE=$(wc -c < /tmp/agent_home.b64)  # re-derive here: Phase 1's B64_SIZE
                                         # variable doesn't survive a fresh
                                         # shell (each code block is its own
                                         # shell), and the verify step needs it.

# --- split the base64 into chunk files locally (numeric suffixes) ---
rm -f /tmp/ah_chunk_*
split -b "$CHUNK_SIZE" -d -a 4 /tmp/agent_home.b64 /tmp/ah_chunk_

# --- clear staging on the container ---
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive --command "rm -f $DEST_B64" \
  --region "$AWS_REGION" >/dev/null 2>&1

# --- append each chunk to the staging file ---
TOTAL=$(ls /tmp/ah_chunk_* 2>/dev/null | wc -l)
i=0
for f in /tmp/ah_chunk_*; do
  i=$((i + 1))
  CHUNK=$(cat "$f")
  # base64 chars (A-Za-z0-9+/=) are shell-safe unquoted, so printf %s embeds
  # the chunk verbatim into the staging file. No quoting pitfalls.
  aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
    --container hermes --interactive \
    --command "sh -c 'printf %s ${CHUNK} >> ${DEST_B64}'" \
    --region "$AWS_REGION" >/dev/null 2>&1 \
    && echo "  chunk $i/$TOTAL appended" \
    || { echo "  chunk $i FAILED — re-run this phase"; exit 1; }
done

# --- verify the staged base64 length matches the local source ---
# DO NOT parse this with `tr -dc '0-9'`: the exec PTY prints a line like
#   Starting session with SessionId: ecs-execute-command-9r7tqcdnqtrpjct34yjupfuif4
# whose SessionId contains digits. `tr -dc '0-9'` merges those with the real
# count and yields garbage (992 -> "67327272499264992"), tripping a false
# LENGTH MISMATCH. Instead, wrap the value in unique markers and extract the
# substring between them — the SessionId's digits then can't interfere.
REMOTE_LEN=$(aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'echo SIZEBEG\$(wc -c < $DEST_B64)SIZEEND'" \
  --region "$AWS_REGION" 2>/dev/null | grep -o 'SIZEBEG[0-9]*SIZEEND' | grep -oE '[0-9]+')
echo "local=${B64_SIZE} remote=${REMOTE_LEN}"
[ "$REMOTE_LEN" = "$B64_SIZE" ] || { echo "LENGTH MISMATCH — aborting before extract"; exit 1; }
```

If the lengths match, **decode + extract (overlay) + fix ownership + clean up**:

```bash
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'set -e; base64 -d $DEST_B64 | tar xzf - -C /home/harness/.hermes; chown -R 1000:1000 /home/harness/.hermes; rm -f $DEST_B64; echo OVERLAY_DONE'" \
  --region "$AWS_REGION"
```

Look for `OVERLAY_DONE`. If the `tar x` step fails with "unexpected EOF" or
"archive is truncated", the base64 was corrupted in transit (a chunk was dropped
or truncated by the SSM command-length limit) — lower `CHUNK_SIZE` (e.g. 10000)
and re-run this phase.

### Phase 4: Verify and decide on a restart

**Gate: Phase 3 printed `OVERLAY_DONE`.**

Spot-check that the files landed with the right ownership (should be
`harness:harness` / `1000:1000`):

```bash
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'stat -c \"%U:%G %n\" /home/harness/.hermes/system-prompt.md 2>/dev/null; find /home/harness/.hermes/skills -maxdepth 2 -type f 2>/dev/null | head -20'" \
  --region "$AWS_REGION"
```

**Does the gateway need a restart?** Depends on what changed:

- **Skills, memories, personas** → usually **no restart**. Hermes loads these
  dynamically per turn in most configurations.
- **`config.yaml`** → not changed by overlay (excluded); apply it via Mode 3
  (Merge-config), which recommends its own restart.
- **MCP server config, plugins** → **restart required** (loaded at boot).
- **`system-prompt.md`** → restart recommended (compiled at startup).

To restart (forces a new task that re-runs the full boot chain, including the
on-boot `gh auth login`):

```bash
aws ecs update-service --cluster "$CLAW_NAME" --service "$CLAW_NAME" \
  --force-new-deployment --region "$AWS_REGION"
aws ecs wait tasks-running --cluster "$CLAW_NAME" \
  --tasks "$(aws ecs list-tasks --cluster "$CLAW_NAME" --region "$AWS_REGION" \
    --query 'taskArns[0]' --output text)" \
  --region "$AWS_REGION"
```

Use `ask_user_question` to ask whether to restart now, given what changed.

---

## Mode 2: Run commands on the claw

Execute arbitrary commands on the live claw via ECS Exec. Use this for
inspection, debugging, or one-off operations that don't fit the overlay model —
e.g. deleting a file removed from `agent_home/`, checking process state,
inspecting logs, or running diagnostics.

**Gate: shared first step (connect) passed.**

The exec session runs as **root**. To act as the workload user (uid 1000),
prefix commands with `runuser -u harness --`:

```bash
# Read a file as the harness user
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'cat /home/harness/.hermes/config.yaml'" \
  --region "$AWS_REGION"

# List files the claw has generated (runtime state not in agent_home/)
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'runuser -u harness -- ls -la /home/harness/.hermes/'" \
  --region "$AWS_REGION"
```

### Deleting a file removed from agent_home/

The overlay (Mode 1) is merge-only — it never deletes files absent from
`agent_home/`. If a file was intentionally removed from `agent_home/` and
should also be removed from the claw, delete it explicitly:

```bash
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'rm -f /home/harness/.hermes/<file> && echo DELETED'" \
  --region "$AWS_REGION"
```

This is a one-off manual step — do not bake deletions into the overlay flow.

### Inspecting claw state

```bash
# Check running processes
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'ps aux | head -20'" \
  --region "$AWS_REGION"

# Check recent gateway logs (if writing to files)
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'ls -lt /home/harness/.hermes/logs/ 2>/dev/null | head; tail -50 /home/harness/.hermes/logs/*.log 2>/dev/null'" \
  --region "$AWS_REGION"

# Check disk usage on the EBS-backed bind-mount
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'df -h /home/harness'" \
  --region "$AWS_REGION"

# Inspect sessions count
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'ls /home/harness/.hermes/sessions/ 2>/dev/null | wc -l'" \
  --region "$AWS_REGION"
```

### Interactive shell

For exploratory work, drop into an interactive bash session:

```bash
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "/bin/bash" \
  --region "$AWS_REGION"
```

Then browse as the workload user: `runuser -u harness -- ls ~/.hermes/`

### Handling exec output

Every `execute-command` result includes boilerplate you must strip when parsing
output programmatically (see Notes for details):

- `The Session Manager plugin was installed successfully...`
- `Starting session with SessionId: ecs-execute-command-<base62>` ← the
  SessionId **contains digits**, so `tr -dc '0-9'` is unsafe for numbers (it
  merges them with your value — see Notes). Use the marker approach instead.
- `Cannot perform start session: EOF`
- Every line ends with `\r\n` from the PTY.

For numeric extraction: wrap the value in `SIZEBEG…SIZEEND` markers and
`grep -o` between them (see Notes) — **not** `tr -dc '0-9'`. For file content,
pipe through `sed 's/\r$//'`.

---

## Mode 3: Merge-config — merge agent_home/config.yaml into the live config

Apply the repo's curated `agent_home/config.yaml` onto the claw's live
`~/.hermes/config.yaml` at the **key** level — a deep merge, not a file
overwrite. The live config is the base; local curated values win on overlap;
the live config's comments, key order, and env-driven keys are all preserved.
This is the config-aware replacement for blindly overlaying `config.yaml`
(Mode 1 excludes it on purpose). Use this whenever you want to change
`config.yaml`.

The merge is driven by `merge_config.py` (alongside this skill), run with `uv`
so it pulls `ruamel.yaml` (comment/order-preserving round-trip) without touching
the system Python. `uv` comes from `mise.toml`, so activate mise first:

```bash
eval "$(/usr/local/bin/mise activate bash)"
```

**Gate: shared first step (connect) passed; `agent_home/config.yaml` exists.**

### Step 1: Fetch the live config.yaml + its mtime

Read the live `~/.hermes/config.yaml` and its mtime over ECS Exec. The file is
read in **byte-chunked base64** (<=1500 B/chunk, each chunk's decoded length
verified and retried) rather than a single `cat | base64`: ECS Exec stdout
truncates at roughly 4-5 KB of base64 in one shot, and a real `config.yaml`
(~10-12 KB) exceeds that ceiling, so a single-shot capture silently loses the
tail and produces invalid YAML. Chunking stays under the limit; per-chunk
verify + retry catches the occasional partial read. This mirrors the
chunked-append PUT the overlay (Mode 1 Phase 3) uses successfully.

First capture the byte size + mtime in one round-trip. The markers isolate
the numbers from the exec PTY's `SessionId` (which itself contains digits, so
`tr -dc '0-9'` is unsafe — it merges them with the real value):

```bash
CFG=/home/harness/.hermes/config.yaml
META=$(aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'echo NBEG\$(wc -c < $CFG 2>/dev/null)NEND MBEG\$(stat -c %Y $CFG 2>/dev/null)MEND'" \
  --region "$AWS_REGION" 2>/dev/null)
SIZE=$(printf '%s' "$META" | grep -o 'NBEG[0-9]*NEND' | grep -oE '[0-9]+' || true)
REMOTE_MTIME=$(printf '%s' "$META" | grep -o 'MBEG[0-9]*MEND' | grep -oE '[0-9]+' || true)
echo "live config: ${SIZE:-0} bytes, mtime=${REMOTE_MTIME:-none}"
```

If `SIZE` is empty/0 the live `config.yaml` is absent — `/tmp/claw_config.current`
stays empty and `REMOTE_MTIME` is unset; the merge helper then treats an
absent remote as "nothing to merge onto" and the result is just the local
config. Otherwise read it in verified base64 chunks:

```bash
: > /tmp/claw_config.current
if [ -n "$SIZE" ] && [ "$SIZE" -gt 0 ]; then
  BS=1500
  NCHUNKS=$(( (SIZE + BS - 1) / BS ))
  c=0
  while [ "$c" -lt "$NCHUNKS" ]; do
    exp=$BS; [ $(( (c + 1) * BS )) -gt "$SIZE" ] && exp=$((SIZE - c * BS))
    b64=""
    for attempt in 1 2 3 4 5; do
      b64=$(aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
            --container hermes --interactive \
            --command "sh -c 'dd if=$CFG bs=$BS skip=$c count=1 2>/dev/null | base64'" \
            --region "$AWS_REGION" 2>/dev/null \
            | sed 's/\r$//; s/Cannot perform start session: EOF//' \
            | grep -E '^[A-Za-z0-9+/=]+$')
      # accept this chunk only if its decoded length matches the expected size
      dec=$(printf '%s' "$b64" | base64 -d 2>/dev/null | wc -c)
      [ "$dec" = "$exp" ] && break
      echo "  chunk $((c+1))/$NCHUNKS: got $dec bytes (want $exp), retry $attempt/5"
      b64=""
    done
    [ -z "$b64" ] && { echo "  chunk $((c+1))/$NCHUNKS FAILED after retries — re-run Step 1"; exit 1; }
    printf '%s' "$b64" | base64 -d >> /tmp/claw_config.current
    echo "  chunk $((c+1))/$NCHUNKS ok ($dec bytes)"
    c=$((c + 1))
  done
fi

# sanity: assembled length must match the reported size before merging
GOT=$(wc -c < /tmp/claw_config.current)
[ "$GOT" = "$SIZE" ] || { echo "LENGTH MISMATCH: assembled $GOT vs reported $SIZE — re-run Step 1"; exit 1; }
```

The `sed` strips the exec PTY's glued-on `Cannot perform start session: EOF`
boilerplate and carriage returns; the `grep -E '^[A-Za-z0-9+/=]+$'` keeps only
base64 lines (dropping the `Session Manager` / `Starting session` lines).

### Step 2: Run the merge

```bash
uv run --no-project --with 'ruamel.yaml==0.19.1' python3 \
  .agents/skills/manage-bclaw/merge_config.py \
  --local /workspace/agent_home/config.yaml \
  --remote /tmp/claw_config.current \
  --remote-mtime "${REMOTE_MTIME:-0}" \
  --out /tmp/config.merged.yaml
```

Read the report it prints:

- **`added`** — local-only keys, applied (additive, safe).
- **`overridden`** — leaf differs and the local file is at least as new as the
  live file; local applied, **not** flagged (the curated edit is the newer intent).
- **`FLAGGED … confirm before applying`** — staged as local-wins but needs your
  sign-off:
  - `override_remote_newer` — the live `config.yaml` was modified *after* the
    local edit, so this override might revert an intentional remote change.
  - `override_unreliable_mtime` — mtimes are missing or the remote clock looks
    skewed, so "who's newer" can't be trusted.
  - `type_mismatch` — the same path is a mapping on one side and a scalar on the
    other (e.g. you turned `feature_flags: true` into a block).
- **`kept_remote`** — paths you passed via `--keep-remote`; remote value kept.
- **`NEEDS_CONFIRM: yes|no`** — `yes` iff anything is flagged.

### Step 3: Resolve flagged conflicts (ask if not sure)

If `NEEDS_CONFIRM: yes`, surface the flagged items with `ask_user_question`
(show each path with its remote → local value). The merged file already stages
local-wins, so the two resolutions are:

- **Apply curated (local) values** → proceed to Step 4 as-is.
- **Keep the remote value for a key** → re-run Step 2 adding
  `--keep-remote <dotted.path>` (repeatable; works on a leaf or a whole
  subtree, e.g. `--keep-remote model` or `--keep-remote secrets.bitwarden`),
  then proceed.

If `NEEDS_CONFIRM: no`, skip to Step 4.

### Step 4: Show the diff and confirm

```bash
diff -u /tmp/claw_config.current /tmp/config.merged.yaml || true
```

Use `ask_user_question` to confirm the user wants to push this merged
`config.yaml` (showing the diff). This overwrites the live config — get explicit
confirmation before Step 5.

### Step 5: Push the merged config + fix ownership

`config.yaml` is tiny, so a single exec round-trip (no chunking) base64-pushes
it. The exec session runs as **root**, so `chown 1000:1000` after the write:

```bash
B64=$(base64 -w0 /tmp/config.merged.yaml)
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'printf %s ${B64} | base64 -d > /home/harness/.hermes/config.yaml && chown 1000:1000 /home/harness/.hermes/config.yaml && echo CONFIG_MERGED'" \
  --region "$AWS_REGION"
```

Look for `CONFIG_MERGED`. (If it complains the command is too long, the file got
unusually large — split the base64 and chunk it like Mode 1 Phase 3.)

### Step 6: Restart decision

`config.yaml` is read at startup (model, provider, toolsets, MCP, gateway
behavior), so **restart recommended**. Cloud mode re-seeds the env-driven keys
on restart — that's fine: the merged file already carries the live (re-seeded)
values for those, and your curated non-env keys (e.g. the `secrets:` block)
persist through the re-seed.

```bash
aws ecs update-service --cluster "$CLAW_NAME" --service "$CLAW_NAME" \
  --force-new-deployment --region "$AWS_REGION"
aws ecs wait tasks-running --cluster "$CLAW_NAME" \
  --tasks "$(aws ecs list-tasks --cluster "$CLAW_NAME" --region "$AWS_REGION" \
    --query 'taskArns[0]' --output text)" \
  --region "$AWS_REGION"
```

Use `ask_user_question` to ask whether to restart now.

### Notes

- **Comments are preserved.** `merge_config.py` loads the live config with
  ruamel.yaml round-trip mode, so comments and key order survive on every key
  you don't touch. (Comments inside a key you override are replaced by the local
  value, as expected.)
- **No deletes.** The merge never removes a key — local only adds/overrides.
  To remove a key, use Mode 2 (Run) to edit the live config explicitly.
- **`secrets:` is the canonical use case.** Adding a secret source (e.g. the
  `aws_ssm` block) is a non-env, local-only key — it lands cleanly via this mode
  and survives the cloud-mode re-seed on restart.
- **Idempotent.** Re-running with an unchanged local + the freshly-merged live
  config reports no diffs (`NEEDS_CONFIRM: no`, empty report).

---

## Mode 4: Upgrade the running image

Roll the running claw onto a new `ghcr.io/boldblackai/harness` tag. The image
tag is a CloudFormation parameter (`HarnessImageTag`, default
`hermes-1.9.3`); bumping it and redeploying creates a new task-definition
revision and ECS rolls the task — **no image rebuild** (the signed upstream
image is used as-is). EBS-backed state (sessions, memories, `~/.config/gh`)
survives the roll; only the container image changes.

**Gate: shell state active (mise + AWS creds); `CLAW_NAME` +
`AWS_REGION` collected and the service is `RUNNING` (shared first step, up to
the RUNNING check).** Mode 4 is a CloudFormation stack update — it does not
need the ECS Exec session, the Session Manager plugin, or `TASK_ARN`.

### Step 1: Capture the current tag and choose the target

What's running now (from the live task — no exec needed):

```bash
aws ecs describe-tasks --cluster "$CLAW_NAME" \
  --tasks "$(aws ecs list-tasks --cluster "$CLAW_NAME" --region "$AWS_REGION" \
    --query 'taskArns[0]' --output text)" \
  --region "$AWS_REGION" \
  --query 'tasks[0].containers[0].image' --output text
# → ghcr.io/boldblackai/harness:hermes-1.9.3
```

And what the stack is parameterized with:

```bash
aws cloudformation describe-stacks --stack-name "$CLAW_NAME" --region "$AWS_REGION" \
  --query 'Stacks[0].Parameters[?ParameterKey==`HarnessImageTag`].ParameterValue' \
  --output text
```

Tags are `hermes-X.Y.Z`. If the user doesn't already know the target, discover
the latest published tags from the ghcr package (needs `gh` auth):

```bash
gh api /orgs/boldblackai/packages/container/harness/versions \
  --jq '[.[].metadata.container.tags[]?] | map(select(startswith("hermes-"))) | sort | reverse | .[0:5][]'
```

Falls back to the GitHub Releases page if `gh` isn't authed.

**Gate:** use `ask_user_question` to confirm the target tag with the user
before proceeding.

### Step 2: Capture the current non-default parameters (critical)

`cloudformation deploy` applies the template `Default` to every parameter
omitted from `--parameter-overrides` — it does **not** remember the prior
stack's values. Several of this stack's parameters default to `false` /
placeholder AZs, so omitting them silently reverts: GitHub auth turns off
(`EnableGitHubKey` defaults to `false`) and the AZ reverts to a placeholder.
(The inference-provider key is resolved from SSM by the aws_ssm plugin, not a
stack parameter, so it is unaffected by an upgrade.) Capture and re-pass
`EnableGitHubKey`, `AZ1`, and `DesiredCount`.

Capture every current parameter as `Key=Value` (excluding `HarnessImageTag`,
which we're changing) into a reusable list:

```bash
OVERRIDES=$(aws cloudformation describe-stacks --stack-name "$CLAW_NAME" \
  --region "$AWS_REGION" \
  --query "Stacks[0].Parameters[?ParameterKey!='HarnessImageTag'].join('=', [ParameterKey, ParameterValue])" \
  --output text | tr '\t' '\n')
echo "$OVERRIDES"
```

> Every value in this stack's parameters is a bare token (claw name, AZ, a
> `true`/`false`, a number, an image tag) — no embedded spaces — so the
> unquoted `$OVERRIDES` expansion below is word-split into one `Key=Value`
> per arg, which is exactly what `--parameter-overrides` expects.

### Step 3: Persist the new tag in the repo

Edit the `HarnessImageTag` default so the choice survives the next deploy — a
version bump is just this edit plus the redeploy in Step 4, then commit:

```
# .agents/skills/setup-bclaw/template.yaml
HarnessImageTag:
  Type: String
  Default: hermes-1.9.4     # ← was hermes-1.9.3
```

> To roll without touching the repo, skip this edit and add
> `HarnessImageTag=<new-tag>` to the overrides in Step 4 — but the next
> `cloudformation deploy` run from the repo reverts it. Persisting in the
> template is the recommended path.

### Step 4: Redeploy with the new tag (re-passing current params)

```bash
aws cloudformation deploy \
  --template-file .agents/skills/setup-bclaw/template.yaml \
  --stack-name "$CLAW_NAME" \
  --region "$AWS_REGION" \
  --capabilities CAPABILITY_NAMED_IAM \
  --role-arn arn:aws:iam::$(aws sts get-caller-identity \
    --query 'Account' --output text):role/${CLAW_NAME}-cfn-exec \
  --parameter-overrides $OVERRIDES HarnessImageTag=hermes-1.9.4
```

> If you edited the `Default` in Step 3, passing `HarnessImageTag` here is
> optional (the new default applies) — but passing it explicitly is harmless
> and self-documenting, so prefer it.

This creates a new task-definition revision (the image changed). Because the
service is configured `MinimumHealthyPercent: 0` (MaximumPercent stays at the
ECS-required 200), ECS performs a RECREATE deployment: it can't place the new
task alongside the old one (single t4g.large, and the claw binds the Slack
socket so two tasks must never coexist), so it stops the old task first (min 0
permits 0 running), then places the new one. Expect ~10-20s of bot downtime
during the swap; EBS state (sessions/memories/SQLite DBs) survives untouched.

### Step 5: Wait for the roll, then verify

```bash
aws ecs wait services-stable --cluster "$CLAW_NAME" --services "$CLAW_NAME" \
  --region "$AWS_REGION"
```

`services-stable` blocks until `runningCount == desiredCount` (new task
RUNNING, old drained). First pull of a new tag takes 2–3 min (image layer
download); faster if layers are cached.

Confirm the new image is live:

```bash
TASK_ARN=$(aws ecs list-tasks --cluster "$CLAW_NAME" --region "$AWS_REGION" \
  --query 'taskArns[0]' --output text)
aws ecs describe-tasks --cluster "$CLAW_NAME" --tasks "$TASK_ARN" \
  --region "$AWS_REGION" \
  --query 'tasks[0].{Image:containers[0].image, Status:lastStatus, StartedAt:startedAt}' \
  --output table
```

Expect `Image` ending in `:hermes-1.9.4`, `Status = RUNNING`. Tail the gateway
log to confirm the bot reconnected:

```bash
aws logs tail "/ecs/${CLAW_NAME}" --region "$AWS_REGION" --follow
```

### Rollback

If the new image is bad, re-run this mode with the previous tag
(`HarnessImageTag=<old-tag>`). EBS state survives — only the image rolls back.

---

## Mode 5: Debug or force-replace the container instance

Operate on the underlying **EC2 container instance** (the host) rather than the
running ECS task. Use this when the instance itself is the problem — it failed
to register to ECS, it is wedged but passing health checks, or you need its boot
log. Modes 1–4 all assume a RUNNING task to ECS Exec into; Mode 5 is the path
when there is no task (or the task is not the issue).

Both actions are scoped by the `bclaw-deploy` policy's `EC2InstanceOps`
statement to the claw's own instances (`aws:ResourceTag/ClawName`), so they
cannot touch co-tenant instances in the same account.

### Prerequisites

- **Shell with mise + AWS creds** (same as the other modes). All
  `aws` commands below assume this shell state.
- **The claw's region** (`AWS_REGION`, default `us-east-1`) and **claw name**
  (`CLAW_NAME`, default `bclaw`). The container instance is tagged
  `ClawName=<claw name>` and `Name=<claw name>-instance`.

Find the claw's running container instance:

```bash
INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:ClawName,Values=$CLAW_NAME" \
            "Name=instance-state-name,Values=running" \
  --region "$AWS_REGION" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
echo "$INSTANCE_ID"
```

### get-console-output — boot / UserData debugging

Retrieves the instance's console output, which captures the boot log and the
UserData script's execution. Use when the instance fails to register to ECS — a
UserData format error, an EBS-attach race, a mount failure — at that point there
is no ECS task to ECS Exec into and no running agent to reach, so the console
log is the only diagnostic.

```bash
aws ec2 get-console-output --instance-id "$INSTANCE_ID" \
  --latest --region "$AWS_REGION" \
  --output text --query 'Output' | tail -100
```

The output is the raw boot log (firmware, kernel, `cloud-init`, and the
UserData script's output). `--latest` returns the output from the instance's
current boot. Pipe through `tail` / `grep` to find the relevant section, e.g.
`grep -i -A5 'cloud-init\|userdata\|error'`.

### terminate-instances — manual force-replace

Terminates the container instance, forcing the Auto Scaling Group to launch a
successor that reattaches the retained EBS volume. Use when an instance is
wedged-but-passing-health (ECS agent stuck, task zombie, attached volume in a
bad state) before the ASG's health check trips — a manual kill forces the
replacement.

```bash
aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$AWS_REGION"
```

The ASG detects the termination and launches a new instance from the launch
template; EBS state (sessions, memories, SQLite DBs on the `/data` volume)
survives because the volume is reattached to the successor. Expect the new
instance to take a few minutes to boot, register to ECS, and start the task.

To preview the authorization decision without terminating, use `--dry-run` —
it returns `Request would have succeeded, but DryRun flag is set` when the
policy allows the call:

```bash
aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --dry-run --region "$AWS_REGION"
```

---

## Notes

- **Exec runs as root.** `aws ecs execute-command` opens a **root** shell (see
  the setup skill's Phase 5 note). Files written or modified by exec are owned
  by root unless you `chown -R 1000:1000` afterward. Without it the non-root
  gateway can't later modify or delete those files (the EBS volume honors POSIX ownership).
  Use `runuser -u harness --` to run commands as the workload user (uid 1000)
  when file ownership matters.

- **Exec PTY output noise.** Every `execute-command` result includes boilerplate
  lines you must strip when parsing output programmatically:
  - `The Session Manager plugin was installed successfully. Use the AWS CLI to start a session.`
  - `Starting session with SessionId: ecs-execute-command-<base62>` ← the
    SessionId **contains digits**, so `tr -dc '0-9'` does *not* isolate your
    number — it concatenates them with the real value (a `wc -c` of `992`
    became `67327272499264992` in practice, tripping a false length-mismatch).
  - `Cannot perform start session: EOF`
  - Every line ends with `\r\n` (carriage return) from the PTY.

  For **numeric** parsing, do *not* rely on `tr -dc '0-9'` (the SessionId's
  digits pollute it) — wrap the value in unique markers and extract the
  substring between them: run `--command "sh -c 'echo SIZEBEG\$(...)SIZEEND'"`
  then `| grep -o 'SIZEBEG[0-9]*SIZEEND' | grep -oE '[0-9]+'`. The markers make
  the SessionId's digits irrelevant. For **file content**, pipe through
  `sed 's/\r$//'`. When using `execute_code` to drive exec calls, filter with
  regex rather than string matching — the boilerplate lines vary by plugin
  version.

- **Overlay preserves runtime state.** Because `tar x` only writes files present
  in the archive, the claw's `sessions/`, runtime caches, `~/.config/gh`
  credentials, and anything else absent from `agent_home/` survive untouched.
  This is the key difference from a fresh deploy — you don't lose live sessions
  or stored credentials.

- **Chunk size tuning.** `CHUNK_SIZE=30000` (30 KiB) is a safe default that
  stays well under SSM's command-length limits. If you hit "command too long" or
  a truncated/corrupted chunk, lower it (e.g. 10000). If round-trips feel slow
  and your payload is large, you may raise it toward 60000 — test one chunk
  first and confirm the staged length still matches before extracting.

- **`agent_home/` is the source of truth for curated state.** Anything you put
  there is what the claw gets on every overlay. Keep it lean: config, prompts,
  skills, memories, personas. Do **not** put `sessions/`, `.cache/`, model
  weights, or other runtime-generated/large data in it — the excludes in the
  update flow guard against `.cache`, but any other large file you add gets
  tarred and shipped over exec.

- **`config.yaml` caveat (cloud mode).** In cloud mode
  (`HARNESS_CLOUD_MODE=1`, set by the task definition), the boot entrypoint
  reconciles `config.yaml` from environment variables on every boot: it re-seeds
  the **env-driven** keys (model, provider, API keys, Slack config) but leaves
  non-env keys (skills, toolsets, MCP, the `secrets:` block) as written. The
  overlay therefore **excludes** `config.yaml` — a file-level overwrite would
  discard non-env keys the re-seed doesn't restore. Apply `config.yaml` with
  **Mode 3 (Merge-config)**, which merges at the key level (preserving comments
  and order) and stages non-env curated keys without touching the re-seeded
  ones. For env-driven keys, update the SSM parameters instead
  (see the setup skill's Phase 3 / Phase 5a token-rotation recipe).

- **Idempotent.** Running the overlay mode twice with the same `agent_home/` is a
  no-op (identical files overwrite themselves). Safe to re-run after fixing a
  typo.

- **Large-payload alternative: S3 presigned URL.** If `agent_home/` ever grows
  past a few hundred KiB, skip the base64 chunking: upload the tarball to S3
  (`aws s3 cp /tmp/agent_home.tar.gz s3://<bucket>/agent_home.tar.gz`), generate
  a presigned URL (`aws s3 presign s3://<bucket>/agent_home.tar.gz`), then on
  the container run `curl -sL "<url>" | tar xzf - -C /home/harness/.hermes`
  followed by `chown -R 1000:1000 /home/harness/.hermes`, then delete the S3
  object. This needs an S3 bucket but has no size limit and uses one exec
  round-trip. The container needs only `curl` (present in the base image) — the
  presigned URL carries no credentials, so no task-role S3 permissions are
  needed.

- **Companion skills.** `setup-bclaw` (create the claw),
  `teardown-bclaw` (destroy it). This skill sits between them:
  Modes 1 (Overlay), 2 (Run), and 3 (Merge-config) mutate or inspect the live claw over ECS Exec without
  touching the CloudFormation stack or task definition; Mode 4 (Upgrade image)
  performs an in-place stack update that re-renders the task definition and
  triggers an ECS recreate deployment; Mode 5 (Host) operates on the EC2
  container instance directly, independent of the task or stack.

- **Image upgrades are stack updates, not overlays.** Mode 4 bumps
  `HarnessImageTag` (a CloudFormation parameter, default `hermes-1.9.3`)
  and redeploys, creating a new task-definition revision; ECS recreates the
  task (stop-old-then-start-new).
  It does not touch EBS state — sessions, memories, and `~/.config/gh` survive.
  Persist the new tag by editing the `Default` in the repo's `template.yaml`
  (commit it like any version bump), or pass it as a one-off
  `--parameter-overrides` value. On any stack update you MUST re-pass every
  non-default parameter (`EnableGitHubKey`, `AZ1`, `DesiredCount`) —
  `cloudformation deploy` applies template defaults to omitted ones, so
  forgetting `EnableGitHubKey=true` silently turns GitHub auth back off. (The
  inference-provider key is resolved from SSM by the aws_ssm plugin, not a
  stack parameter, so it is unaffected by an upgrade.)
