# dispatch


`dispatch` is a Hermes Agent claw (a long-running gateway) deployed as a Slack
socket-mode bot. It is outbound-only — no load balancer, no inbound ports.

## Search

You can use web-search-prime to look things up that aren't obvious in the repository.


## Tooling

- Use `mise` for all tool installation (`aws-cli`, `jj`). Add tools
  to `mise.toml` — do not install system-wide. Activate mise in every shell:
  `eval "$(/usr/local/bin/mise activate bash)"`, then `mise trust`.
- AWS credentials live in `.env` (gitignored) and are loaded by `mise` via the
  `[env] _.file` entry in `mise.toml`. In every shell that runs `aws`:
  ```bash
  eval "$(/usr/local/bin/mise activate bash)" \
    && mise trust /workspace \
    && cd /workspace
  ```
- `.env` holds `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (or `AWS_PROFILE`)
  and `AWS_REGION`. Never commit it.

## Deploy

- Deploys to **AWS ECS (EC2 launch type)** — a single container instance in an
  Auto Scaling Group (`min=max=desired=1`) with a persistent **EBS data volume** —
  managed by the `setup-dispatch` / `teardown-dispatch` agent skills in
  `.agents/skills/`. The CloudFormation template lives alongside the setup
  skill at `.agents/skills/setup-dispatch/template.yaml`.
- No derived image is built. The signed upstream
  `ghcr.io/boldblackai/harness` image is deployed as-is — host bind-mounts on
  the EBS volume support the 4-way mount layout directly, so no custom
  `Dockerfile`/`entrypoint.sh` are needed.
- See `README.md` for prerequisites (tooling, AWS creds, IAM, secrets).

### AWS infrastructure

- Stack name = claw name (default `dispatch`), region `us-east-1`.
- Dedicated VPC (10.0.0.0/16) with **1 public subnet in a single AZ** (EBS is
  zonal, so the volume, instance, and task all live in one AZ). The setup skill
  probes Graviton AZ availability via `describe-instance-type-offerings` and
  passes one ARM64-capable AZ to the stack.
- **Persistent EBS data volume** (gp3, retained) mounted at `/data`, surfaced
  into the container as 4 host bind-mounts (`.hermes`, `.config`, mise
  data/state) owned by the `harness` user (uid/gid 1000). The volume is
  standalone + `DeletionPolicy: Retain`; the instance's UserData finds it by
  baked ID (CFN injects the volume ID into the launch template), attaches it,
  and mounts it by filesystem label on every boot, so data survives ASG instance
  replacement. SQLite's WAL mode needs a real local block device (it is unsafe
  on NFS), which is the reason state is on EBS.
- Secrets are **SSM SecureString** parameters under the claw's `/dispatch/KEY`
  namespace, written by the user in setup Phase 3. Not
  stack-owned, so they survive stack updates/deletes. A Hermes secret-source
  plugin (`aws_ssm`, installed in setup Phase 5) resolves every `/dispatch/*`
  parameter into the gateway env at startup, using the TaskRole's SSM-read
  grant — so adding/rotating a key is an SSM write + restart, no redeploy. This
  carries the Slack tokens (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`,
  `SLACK_ALLOWED_USERS`, `SLACK_HOME_CHANNEL`) and the inference-provider key
  (`OPENROUTER_API_KEY`/`ANTHROPIC_API_KEY`/`ZAI_API_KEY`). The ONE exception is
  `GH_TOKEN_VAL` (optional, `EnableGitHubKey`): the on-boot `gh auth login` in
  the container `Command` runs before Hermes starts, so it is still injected via
  CloudFormation `secrets[]` (skipped when disabled). Requires harness >= 1.9.3
  (#64189).
- The task uses **host networking** (it shares the container instance's ENI,
  which has a public IP for outbound to Slack/ghcr/SSM — no NAT gateway). The
  security group is inbound-less.
- The ECS service is **single-replica** (`DesiredCount` parameter, default `1`)
  with `DeploymentConfiguration: MinimumHealthyPercent: 0` — two tasks can
  never coexist (they'd fight over the Slack socket), so deployments are
  **recreate** (stop-old-then-start-new, ~10-20s downtime). The setup skill
  passes `DesiredCount=0` on the first deploy (before the SSM params exist, so
  the gateway doesn't crash-loop on missing env vars) then scales to 1. The
  default is `1` so a stack update that omits it keeps the claw running.
- `EnableExecuteCommand: true` → shell-in via `aws ecs execute-command` (SSM
  Session Manager) over the host's internet path. Exec sessions run as **root**;
  use `runuser -u harness --` to act as the workload user (uid 1000).

## Optional Linear integration

`integrations/linear/` contains a disabled-by-default Hermes directory plugin,
a signed Lambda webhook receiver, and an SQS FIFO side stack. Follow its
README for image compatibility, installation, SSM mapping, and verification.
The gateway consumes outbound and persists its inbox on the same retained
EBS volume. Remove the side stack before the main stack during teardown.
