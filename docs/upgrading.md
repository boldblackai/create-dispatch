# Upgrading

How to roll a running dispatch agent onto a new harness image tag, and how to
update its skills without a redeploy.

## Upgrade image (manage-dispatch mode 4)

The agent runs `ghcr.io/boldblackai/harness:<tag>` — the `HarnessImageTag`
stack parameter selects the tag. Rolling to a new release is a parameter bump
plus redeploy; **no image is rebuilt**:

1. Pick the tag from the
   [harness releases](https://github.com/boldblackai/harness/releases) page
   (e.g. `hermes-1.9.11`).
2. Run the `/manage-dispatch` skill in upgrade-image mode (mode 4), naming the
   new tag. The skill bumps the `HarnessImageTag` stack parameter and
   redeploys the stack.
3. The ECS service replaces the task; state survives on the EBS volume.

The generator's template tracks the latest released tag by default, so
**freshly generated agents already run the newest image** — upgrading applies
to agents deployed earlier.

## Update skills, memories, prompts (manage-dispatch mode 1 — overlay)

Everything in the repo's `agent_home/` — skills, memories, system prompt,
personas — updates **without any redeploy**:

1. Edit `agent_home/` in the generated repo and commit.
2. Run the `/manage-dispatch` skill in overlay mode (the default). It pushes
   the repo's `agent_home/` onto the agent's `~/.hermes` over ECS Exec
   (tar + base64 transport), with a dry-run diff first.
3. Restart if the change needs one (the skill tells you when).

`config.yaml` is excluded from the overlay — use merge-config mode (mode 3)
for it.

## Merge config (manage-dispatch mode 3)

`agent_home/config.yaml` merges into the live `config.yaml` at the key level:
fetch live config → merge → flag conflicts → show diff → push. Conflicts
resolve by choosing the repo value, the live value, or a hand-edited blend.

## Runtime storage

The generated template persists uv-managed Python installations and the uv
package cache on retained EBS, and routes mise's cache into its existing
writable data mount. The generated README's **Runtime storage upgrades** section
contains the migration and verification procedure.

Before applying these mounts to an existing deployment, pause work, preserve
required interpreters from the running task, and prepare the host directories
with uid/gid 1000 ownership. Updating launch-template UserData does not execute
it on the current host. The task update does not copy files out of its previous
writable layer; mounting an empty directory can hide interpreters needed by
persisted virtualenvs.

Preserve all current stack parameters when deploying the new template. Verify
runtime paths, write permissions, and a retained worktree's Python interpreter
as the workload user. Repeat those checks during an authorized host-replacement
test. Keep a migration backup until verification completes. An image rollback
alone does not undo the storage layout or recover the old writable layer.
