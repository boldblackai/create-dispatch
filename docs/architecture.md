# Architecture

What a deployed dispatch agent looks like on AWS, and why it is shaped that
way.

## One repo, one agent, one Slack app

Each generated repository corresponds to one long-running agent and one Slack
application/user. Generate as many as you like — a `@swe-pal` for code reviews
and PRs, a `@reporter` that posts scheduled reports, a `@docs` that keeps
documentation in sync.

## The stack

The CloudFormation stack (`template.yaml` in the setup skill) owns the full
infrastructure:

| Resource | Details |
|---|---|
| VPC + subnets | Public subnet, internet gateway; inbound-less security group |
| EBS data volume | Standalone gp3 volume, mounted at `/data`, `DeletionPolicy: Retain` |
| Launch template + ASG | Single ARM64 container instance, `min=max=desired=1` |
| IAM roles | Task execution, task, container-instance instance profile |
| KMS key | `alias/dispatch-ssm` — encrypts the SSM secrets |
| Log group | `/ecs/dispatch` |
| ECS service | EC2 launch type, host networking, `DesiredCount` gated by parameter |

**Key decisions:**

- **Socket mode, outbound-only.** The agent is a Slack socket-mode bot. It
  makes an outbound WebSocket connection to Slack, so there is no load balancer
  and no inbound port — the security group is inbound-less. Public IP on the
  instance ENI provides the outbound path (no NAT gateway).
- **EC2 launch type, not Fargate.** The persistent state is SQLite, and
  SQLite's WAL mode needs a real local block device (it is unsafe on NFS).
  EBS gives that, and lets the data volume survive instance replacement: the
  volume is standalone with `DeletionPolicy: Retain`, and the instance's
  UserData reattaches it on every boot.
- **Host networking.** The task shares the container instance's ENI. With no
  inbound ports there is nothing to map.
- **ARM64.** The instance probes for an ARM64 availability zone at setup and
  the harness image ships multi-arch (amd64 + arm64).

## The persistent volume

The EBS volume is surfaced into the container as four agent-state bind-mounts
and two narrow uv runtime/cache mounts:

| Host path (on `/data`) | Container path |
|---|---|
| `hermes/` | `~/.hermes` |
| `xdg-config/` | `~/.config` |
| `mise-data/` | `~/.local/share/mise` |
| `mise-state/` | `~/.local/state/mise` |
| `uv-python/` | `~/.local/share/uv/python` |
| `uv-cache/` | `~/.cache/uv` |

Because state lives on EBS, an instance replacement (ASG recycle, instance
failure, manual rebuild) does not lose sessions, memories, or skills.
`UV_PYTHON_INSTALL_DIR` and `UV_CACHE_DIR` point at the two uv mounts so
managed interpreters remain at the paths referenced by retained worktree
virtualenvs. `MISE_CACHE_DIR` uses `~/.local/share/mise/cache` inside the
existing writable mise data mount. These mounts do not cover the whole home,
`.local`, or `.cache` directory.

Existing deployments require directory preparation and interpreter migration
before updating their task definition; see [runtime storage upgrades](upgrading.md#runtime-storage).

## The overlay

`agent_home/` in the generated repo is the **curated state** that ships with
the repo: skills, memories, system prompt, personas. It is not baked into the
image.

The `manage-dispatch` skill's overlay mode pushes this directory onto the
running agent's `~/.hermes` over ECS Exec — updating skills, memories, and
prompts **without a CloudFormation redeploy or image rebuild**. This is the
day-2 workflow: edit `agent_home/` in the repo, push the overlay, restart.

`config.yaml` is excluded from the overlay — it is managed by merge-config
mode, which does a key-level merge into the live config.

## The image

The agent runs the hardened `ghcr.io/boldblackai/harness` image
(`hermes-<version>` tags). The `HarnessImageTag` stack parameter selects the
tag; bumping it and redeploying rolls the agent to a new image **without
rebuilding anything**. The
[harness releases](https://github.com/boldblackai/harness/releases) page lists
the tags.

## The secrets path

Secrets live as SSM SecureStrings under `/dispatch/`, encrypted with the
stack's own KMS key (`alias/dispatch-ssm`). A Hermes secret-source plugin
(`aws_ssm`, installed during setup) resolves every `/dispatch/*` parameter into
the gateway's environment at startup. Adding or rotating a key is an SSM write
plus a task restart — no template edit, no redeploy.

See [Concepts: secrets](secrets.md) for the full inventory.
