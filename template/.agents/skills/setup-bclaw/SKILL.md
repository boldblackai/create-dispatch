---
name: setup-bclaw
description: >
  Bootstraps a Hermes Agent bclaw on AWS ECS (EC2 launch type) from scratch to
  a running gateway. Follows a gated sequence: probe one ARM64 AZ → deploy
  CloudFormation (VPC, persistent EBS volume, a single-instance Auto Scaling
  Group, ECS service at DesiredCount 0 on the first deploy) → write SSM secrets
  → scale to 1 → overlay agent_home/ + install the aws_ssm plugin + merge its
  secrets config → restart → verify. Use when setting up a new bclaw on AWS or re-deploying
  after teardown. Companion to teardown-bclaw.
---

# Setup Harness ECS on EC2

Bootstraps a Hermes Agent claw on AWS ECS using the **EC2 launch type**: a
single container instance in an Auto Scaling Group (`min=max=desired=1`) with a
**persistent, retained EBS data volume** for the claw's SQLite databases. The
claw is a Slack socket-mode bot (see `README.md` + `slack-manifest.json`) — it
is outbound-only, so there is no load balancer and no inbound ports (the
security group is inbound-less).

The task uses **host networking** (it shares the container instance's ENI, which
has a public IP for outbound to Slack/ghcr/SSM — no NAT gateway). Persistent
state lives on a **standalone gp3 EBS volume** mounted at `/data`, surfaced into
the container as 4 host bind-mounts that mirror the harness CLI bind-mounts
(`~/.hermes`, `~/.config`, `~/.local/share/mise`, `~/.local/state/mise`). The
volume is `DeletionPolicy: Retain` and is reattached by the instance's UserData
on every boot, so data survives ASG instance replacement. SQLite's WAL mode
needs a real local block device (it is unsafe on NFS), which is why state is on
EBS and not on a network filesystem.

The CloudFormation stack (`template.yaml` alongside this skill) owns the VPC,
the EBS volume, the launch template, the ASG, IAM roles (exec, task, container
instance + instance profile), the log group, the task definition, and an ECS
service whose `DesiredCount` is a parameter (default `1`); the setup skill
passes `0` on the first deploy — before the SSM secrets exist and the aws_ssm
plugin is installed and configured (Phase 5) — then scales to 1. Secrets are **not**
owned by the stack — they live in SSM Parameter Store as namespaced
SecureStrings that the user writes in Phase 3. This is the piranesi pattern: it
keeps secrets out of template diffs and lets them survive stack deletes.

## Prerequisites

This skill assumes AWS credentials are already configured. See `README.md` →
**Setup** for the one-time IAM onboarding (create the deployer user, attach the
`bclaw-deploy-policy.json` policy, add the access key to `.env`). That must be
completed before running this skill. Permissions are not pre-checked — if the
deployer principal is missing an action, CloudFormation will surface the exact
`is not authorized to perform` error at deploy time (Phase 2).

The deployer's IAM powers are deliberately narrow: it manages the
CloudFormation stack and a single dedicated **service role**
(`bclaw-cfn-exec`) that CloudFormation assumes to perform the actual
infrastructure creates. That service role is created in **Phase 0** below
(it cannot be a stack resource — the stack needs it to exist before it can be
created), so onboarding attaches only `bclaw-deploy-policy.json`; nothing else
is created up front.

Before starting, ensure the shell has `mise` active and AWS credentials
loaded. `mise` manages `aws-cli` (see `mise.toml`); credentials live in
`.env` (gitignored) and are loaded by `mise` via the `[env] _.file` entry in
`mise.toml`:

```bash
eval "$(/usr/local/bin/mise activate bash)" \
  && mise trust /workspace \
  && cd /workspace
```

All `aws` commands in this skill assume this shell state. Verify the caller:

```bash
aws sts get-caller-identity --query 'Account' --output text
```

If that fails, stop — the user hasn't completed the README Setup steps yet.

## Setup Sequence

Follow these phases **in order**. Each phase has a gate that must be satisfied
before proceeding. Use `ask_user_question` (the `clarify` tool) to confirm
completion and collect input where called for.

---

### Phase 0: Create the CloudFormation service role (`${CLAW_NAME}-cfn-exec`)

**Gate: AWS access confirmed (Prerequisites).**

CloudFormation runs every deploy (Phase 2) and the stack delete under a
dedicated service role, `${CLAW_NAME}-cfn-exec`, which carries the broad
infrastructure-create lifecycle (EC2/ASG/ECS/IAM/KMS/logs) so those powers
never sit on the deployer's long-lived access key. The role is **not** a stack
resource — the stack cannot create the role it assumes to create itself — so
it is created here, before the first deploy, idempotently, and deleted last in
teardown.

The role's trust policy and inline execution policy ship alongside this skill's
deploy policy as `bclaw-cfn-exec-trust.json` (trusts only
`cloudformation.amazonaws.com`) and `bclaw-cfn-exec-policy.json` (the lifecycle
permissions). Run from the repo root so the `file://` paths resolve:

```bash
CLAW_NAME=bclaw                              # the claw name (fixed at generation)
AWS_REGION=<user-provided>
CFN_EXEC="${CLAW_NAME}-cfn-exec"

# create the role if absent, else refresh its trust policy (idempotent)
aws iam create-role \
  --role-name "$CFN_EXEC" \
  --assume-role-policy-document file://${CLAW_NAME}-cfn-exec-trust.json \
  --description "CloudFormation service role for the ${CLAW_NAME} stack (assumed by cloudformation.amazonaws.com)" \
  2>/dev/null \
  || aws iam update-assume-role-policy \
       --role-name "$CFN_EXEC" \
       --policy-document file://${CLAW_NAME}-cfn-exec-trust.json

# (re)apply the inline execution policy — idempotent overwrite
aws iam put-role-policy \
  --role-name "$CFN_EXEC" \
  --policy-name "$CFN_EXEC" \
  --policy-document file://${CLAW_NAME}-cfn-exec-policy.json

# verify
aws iam get-role --role-name "$CFN_EXEC" --query 'Role.RoleName' --output text
```

If `update-assume-role-policy` runs (the role already existed from a prior
setup), `put-role-policy` still re-applies the inline policy — re-running this
phase after editing `bclaw-cfn-exec-policy.json` is the way to update the
service role's permissions, and it takes effect on the next `cloudformation
deploy`. The role's ARN is passed to the deploy as `--role-arn` in Phase 2.

---

### Phase 1: Collect configuration and probe one ARM64 AZ

**Gate: user confirms the AWS region and inference provider.**

The claw name is the name the repo was generated under — it is fixed, not
collected. `$CLAW_NAME` is used as a shell variable in the commands below.

Use `ask_user_question` to collect:

1. **AWS region** (default `us-east-1`). Read the current region with
   `aws configure get region` and offer it as the default.

2. **Inference provider** — which LLM provider the gateway should use. Use
   `ask_user_question` with these three choices:
   - **OpenRouter** (recommended) — multi-model router, broadest model access
   - **Anthropic** — direct Claude API access
   - **Z.AI (GLM)** — Zhipu/z.ai GLM models

   The provider choice determines which provider API-key SSM parameter the user
   creates in Phase 3. The aws_ssm secret-source plugin resolves it (and the
   other `/bclaw/*` secrets mapped in `agent_home/config.yaml`'s `env:`) into
   the env at gateway startup, so there is no per-provider stack parameter —
   adding or swapping a provider key later is an SSM write (+ an `env:` entry
   if it's a brand-new param name) + task restart, no template edit or
   redeploy. The gateway needs at least one provider key to run; create more
   than one if the user wants (e.g. OpenRouter plus Anthropic) — the provider
   keys mapped in `env:` resolve if present in SSM (missing ones warn-skip).

   Provider → SSM key mapping:

   | Provider | SSM parameter |
   |---|---|
   | openrouter | `/bclaw/OPENROUTER_API_KEY` |
   | anthropic | `/bclaw/ANTHROPIC_API_KEY` |
   | zai | `/bclaw/ZAI_API_KEY` |

3. **GitHub authentication** — whether the agent should make authenticated
   `gh`/HTTPS-git calls. This is OPTIONAL: the claw is a Slack bot and runs
   fine without it. Use `ask_user_question` with these two choices:
   - **Yes** — the claw authenticates `gh` automatically on every boot from
     `/bclaw/GH_TOKEN_VAL` (the container `Command` runs
     `gh auth login --with-token`). Requires creating that SSM parameter in
     Phase 3 and passing `EnableGitHubKey=true` in Phase 2.
   - **No** (default) — no GitHub credential is injected; `gh`/HTTPS-git
     operations will be unauthenticated. The on-boot login is skipped
     entirely (the `Command` guards on `$GH_TOKEN_VAL` being non-empty).

Store them as shell variables used in every later command:

```bash
CLAW_NAME=bclaw                              # the claw name (fixed at generation)
AWS_REGION=<user-provided>
INFER_PROVIDER=<openrouter|anthropic|zai>   # from step 2
ENABLE_GH=<true|false>                      # from step 3 (default false)
```

#### 1a. Probe one ARM64-capable availability zone

ARM64 (Graviton) is ~20% cheaper and the harness image is published multi-arch.
The container instance runs on Graviton hardware (`t4g.*` by default), and EBS
is zonal — so the volume, the instance, and the task all live in a **single**
AZ. Probe which AZs in the region offer `t4g.*`, then pick one:

```bash
aws ec2 describe-instance-type-offerings \
  --location-type availability-zone \
  --filters Name=instance-type,Values=t4g.* \
  --region "$AWS_REGION" \
  --query 'InstanceTypeOfferings[].Location' --output text | tr '\t' '\n' | sort -u
```

Take the first as `AZ1` and pass it to the stack deploy in Phase 2 as
`--parameter-overrides AZ1=...`.

**Edge cases:**
- If the probe returns **no AZs**, ARM64 capacity is unavailable in this
  account/region. Use `ask_user_question` to offer the user a choice:
  (a) proceed on a non-Graviton ARM64 AZ if any appeared, or
  (b) switch to `X86_64` for `CpuArchitecture` AND `InstanceType` to an
      Intel/AMD family (e.g. `t3.large`) AND `EcsAmiId` to the
      `.../amazon-linux-2023/x86_64/recommended/image_id` SSM parameter
      (~20% more expensive; all three must change together).
- If the probe **errors** (e.g. permissions), fall back to the template default
  (literal string `us-east-1a` — the template can't use `!GetAZs` in a
  parameter default) and warn the user.

Report the chosen AZ to the user before proceeding.

---

### Phase 2: Deploy the CloudFormation stack (first deploy at DesiredCount 0)

**Gate: Phase 1 collected the AWS region, inference provider, and AZ1; AND
2-pre found no half-started stack** (`describe-stacks` returns `does not exist`
or a healthy `CREATE_COMPLETE`/`UPDATE_COMPLETE`).

#### 2-pre: Detect a half-started or existing stack

Before deploying, check whether a stack named `$CLAW_NAME` already exists. This
is the step that prevents the #1 source of stray stacks: a *previous* run whose
deploy failed and rolled back (stack now in `ROLLBACK_COMPLETE`) or whose
teardown didn't finish (`DELETE_FAILED`). `cloudformation deploy` refuses to run
into a stack in those states — it errors out, and the temptation is then to
deploy under a *different* name, leaving the dead `bclaw` stack orphaned (still
billing its retained EBS volume, still squatting on the `/bclaw/*` secret
namespace). Detect it here and fix it instead.

```bash
aws cloudformation describe-stacks \
  --stack-name "$CLAW_NAME" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].StackStatus' --output text 2>&1
```

This prints the existing stack's status, or an error containing `does not exist`
if there is none. Act on the result:

| Result | State | What to do |
|---|---|---|
| `does not exist` error | Fresh — no prior attempt | Proceed to the deploy below (this is a `CREATE`). |
| `CREATE_COMPLETE` / `UPDATE_COMPLETE` | Already fully deployed | This run is an in-place `UPDATE`, not a fresh deploy. Usually fine (e.g. pushing a `template.yaml` change). But if the user wanted a clean rebuild, run the `teardown-bclaw` skill first. Tell the user it's an update before deploying. |
| `ROLLBACK_COMPLETE` / `CREATE_FAILED` / `ROLLBACK_FAILED` | Half-started: a deploy failed and rolled back | **STOP.** The stack exists but is unusable — `deploy` will refuse to touch it. Tear it down (below), then re-run setup. |
| `UPDATE_ROLLBACK_COMPLETE` / `UPDATE_FAILED` / `UPDATE_ROLLBACK_FAILED` | Half-started: an update on a good stack failed | **STOP.** Cleanest fix is `delete-stack` + redeploy; alternatively `continue-update-rollback` recovers the prior good state. |
| `DELETE_IN_PROGRESS` | A teardown is mid-flight | **STOP.** Wait for it to finish (`stack-delete-complete` waiter), then re-check this step. |
| `DELETE_FAILED` | A teardown stalled (often a stuck ASG instance or a volume still in-use) | **STOP.** See the force-delete fix below, then re-check. |
| `CREATE_IN_PROGRESS` / `UPDATE_IN_PROGRESS` / `*_ROLLBACK_IN_PROGRESS` | A deploy/update is in flight | **STOP.** Wait for a terminal state, then re-check. |
| `REVIEW_IN_PROGRESS` | A stack with a pending change set (rare for `deploy`) | **STOP.** `delete-stack` then redeploy. |

**If the gate stopped on a half-started stack, never abandon it under the
`bclaw` name.** Run the `teardown-bclaw` skill (it scales to 0 first, deletes
the stack, and handles the retained-EBS + force-delete gotchas), or for a quick
rollback cleanup:

```bash
aws cloudformation delete-stack --stack-name "$CLAW_NAME" --region "$AWS_REGION" \
  --role-arn arn:aws:iam::$(aws sts get-caller-identity --query 'Account' --output text):role/${CLAW_NAME}-cfn-exec
aws cloudformation wait stack-delete-complete --stack-name "$CLAW_NAME" --region "$AWS_REGION"
```

Two caveats specific to this stack when cleaning up a stale `bclaw`:

- **`DELETE_FAILED` on the ASG is common.** CloudFormation's resource handler
  can fail to confirm an Auto Scaling Group or its instance is gone (the
  instance may still be terminating, or the handler times out). Verify directly
  with `aws autoscaling describe-auto-scaling-groups` and
  `aws ec2 describe-instances`; if they're empty/gone but the stack is stuck on
  handler confirmation, re-run `delete-stack --deletion-mode FORCE_DELETE_STACK`
  to skip stuck resources and continue.

- **Retained EBS survives `delete-stack`** — `EbsDataVolume` has
  `DeletionPolicy: Retain`, so deleting a stale stack leaves its volume behind,
  billed and tagged `${CLAW_NAME}-data`. And if the stack was updated several
  times there may be *several* retained volumes. Before re-deploying, sweep for
  orphans so the fresh deploy doesn't leave stragglers around:
  ```bash
  aws ec2 describe-volumes --region "$AWS_REGION" \
    --filters "Name=tag:Name,Values=${CLAW_NAME}-data" \
    --query 'Volumes[].{Id:VolumeId,State:State,Size:Size}' \
    --output table
  ```
  Keep one if the user wants to preserve sessions/memories; delete the rest
  (see teardown Phase 3) before re-running this skill.

Only proceed to the deploy once this step reports `does not exist` (fresh
`CREATE`) or a healthy `CREATE_COMPLETE`/`UPDATE_COMPLETE` (in-place `UPDATE`).

#### 2-deploy: Create / update the stack

Deploy the stack. On a **first deploy** (2-pre reported `does not exist`) pass
`DesiredCount=0`: defers the first boot until the SSM secrets are written
(Phase 3). (With only `GH_TOKEN_VAL` in `secrets[]`, the task no longer
crash-loops on missing Slack secrets — the aws_ssm plugin carries them, and
the gateway tolerates a no-secrets boot — but deferring keeps the sequence
clean: the Slack bot only connects after Phase 5 installs the plugin + merges
its config + a restart.) Phase 4 scales it to 1. On a **stack update** (2-pre
reported `CREATE_COMPLETE` /
`UPDATE_COMPLETE`) pass the live service's actual count instead — see the note
after the command.

If the user opted into GitHub auth in Phase 1 step 3 (`ENABLE_GH=true`), pass
`EnableGitHubKey=true`; otherwise omit it (default `false`, no `GH_TOKEN_VAL`
injected, on-boot login skipped). There is **no provider-key stack parameter**
— the inference-provider key (and the Slack tokens) are resolved from SSM by
the aws_ssm plugin at gateway startup, not injected here, so the deploy
overrides carry no `Enable*Key`:

```bash
aws cloudformation deploy \
  --template-file .agents/skills/setup-bclaw/template.yaml \
  --stack-name "$CLAW_NAME" \
  --region "$AWS_REGION" \
  --capabilities CAPABILITY_NAMED_IAM \
  --role-arn arn:aws:iam::$(aws sts get-caller-identity \
    --query 'Account' --output text):role/${CLAW_NAME}-cfn-exec \
  --parameter-overrides \
    ClawName="$CLAW_NAME" \
    AZ1=<az1-from-phase-1> \
    EnableGitHubKey="$ENABLE_GH" \
    DesiredCount=0 \
  --no-disable-rollback
```

`--role-arn` makes CloudFormation assume the `${CLAW_NAME}-cfn-exec` service
role created in Phase 0 to perform the create/update — the deployer identity
never touches the infrastructure resources directly (that is the whole point
of the service role). Every deploy and every `delete-stack` MUST pass this
flag: without it, CloudFormation falls back to the deployer's own (narrowed)
permissions and the deploy fails on the first infra-create.
`EnableGitHubKey="$ENABLE_GH"` is safe to always pass — it's `"true"` or
`"false"` straight from Phase 1 step 3.

The instance type (`InstanceType`, default `t4g.large`), the ECS-optimized AMI
(`EcsAmiId`, default the arm64 AL2023 AMI via SSM), task CPU/memory, and the
CPU architecture all default sensibly for ARM64 — omit them unless Phase 1's
edge case switched to X86_64 (then also pass `CpuArchitecture=X86_64`,
`InstanceType=t3.large`, and the x86_64 `EcsAmiId`).

> **On a stack UPDATE, re-pass every non-default parameter — including
> DesiredCount.** `cloudformation deploy` applies the template’s parameter
> `Default` to anything omitted from `--parameter-overrides`; it does NOT
> remember the prior stack’s values. `DesiredCount` defaults to `1` (chosen so a
> forgotten override keeps the claw running instead of scaling it to 0), but to
> preserve a count the user changed (e.g. scaled to 2), capture the live value
> and re-pass it:
>
> ```bash
> # current desired count of the running service
> DESIRED=$(aws ecs describe-services --cluster "$CLAW_NAME" --services "$CLAW_NAME" \
>   --region "$AWS_REGION" --query 'services[0].desiredCount' --output text)
> # then add  DesiredCount="$DESIRED"  to --parameter-overrides on the deploy
> ```
>
> The GitHub override is stricter — its default is `false`, so omitting it
> reverts silently: forgetting to re-pass `EnableGitHubKey=true` turns GitHub
> auth back off. (The inference-provider key is resolved from SSM by the aws_ssm
> plugin, not a stack parameter, so it is unaffected.) Capture current params
> with `describe-stacks` and re-pass them, then verify the live task def
> matches intent.

Wait for `CREATE_COMPLETE` (the `deploy` command blocks until it finishes).

**Verify the stack and capture outputs:**

```bash
aws cloudformation describe-stacks \
  --stack-name "$CLAW_NAME" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs' --output table
```

Confirm `ClusterName`, `ServiceName`, `EbsVolumeId`, `AutoScalingGroupName`,
`KmsKeyArn`, `KmsKeyAlias` (should be `alias/${CLAW_NAME}-ssm`), and
`SsmParameterPrefix` (should be `/bclaw`) are all present.

> **First-deploy instance boot takes a few minutes.** The ASG launches the
> container instance, whose UserData installs the AWS CLI (if missing), finds +
> attaches the retained EBS volume, formats/mounts it, creates the 4 subdirs,
> and only THEN registers with the ECS cluster. The instance won't appear as a
> container instance in the cluster until that finishes. Phase 4 waits for
> registration before scaling.

---

### Phase 3: Write the SSM secrets

**Gate: stack is `CREATE_COMPLETE`.**

The claw needs SSM SecureString parameters under the `/bclaw/` namespace —
**4 Slack tokens** (always required), the **inference-provider key** chosen in
Phase 1 step 2, and an **optional GitHub key** (only if `ENABLE_GH=true` from
Phase 1 step 3). The namespace is hardcoded in the template
(not constructed from `ClawName`), which means the deployer's IAM policy can be
scoped to `arn:aws:ssm:*:*:parameter/bclaw/*` instead of `*`. They are **not** created by CloudFormation — the user
writes them here so they survive stack updates and deletes. Every one of them
is resolved into the container env at gateway startup by the aws_ssm
secret-source plugin (installed in Phase 5), using the TaskRole's SSM-read
grant — none are in the task definition's `secrets[]` except `GH_TOKEN_VAL`
(which the on-boot `gh auth login` needs before the plugin loads). The gateway
tolerates a boot where these are absent (it logs "No messaging platforms
enabled" and stays RUNNING), but the Slack bot won't connect until they exist
and the plugin is configured.

Use `ask_user_question` to give the user the tables below and tell them to enter
each parameter in the **AWS Management Console** (Systems Manager → Application
Management → Parameter Store → **Create parameter**), then wait for
confirmation. The console is the primary path: secret values stay out of the
user's shell history and terminal scrollback (the console masks SecureString
inputs).

**Always required (4):**

| SSM key | What it is | Where to find it |
|---|---|---|
| `/bclaw/SLACK_BOT_TOKEN` | Slack bot OAuth token (`xoxb-`) | Slack app → OAuth & Permissions → Bot User OAuth Token |
| `/bclaw/SLACK_APP_TOKEN` | Slack app-level token (`xapp-`, enables socket mode) | Slack app → Basic Information → App-Level Tokens |
| `/bclaw/SLACK_ALLOWED_USERS` | Comma-separated Slack user IDs allowed to use the bot | Slack profile → "Copy member ID" |
| `/bclaw/SLACK_HOME_CHANNEL` | Slack channel ID the bot treats as home | Right-click channel → "Copy link", take the trailing ID |

**GitHub key (optional, from Phase 1 step 3):** create this only if
`ENABLE_GH=true` — it authenticates `gh`/HTTPS-git on every boot. Skip this
entire subsection if the user opted out.

| SSM key | What it is | Where to find it |
|---|---|---|
| `/bclaw/GH_TOKEN_VAL` | GitHub PAT — used for on-boot `gh auth login` (see Phase 6a). Named `*_VAL`, not `GH_TOKEN`, to dodge `gh`'s reserved env var | https://github.com/settings/tokens (classic PAT or fine-grained; needs the scopes the claw's `gh`/git usage requires) |

**Inference-provider key (1, from Phase 1 `$INFER_PROVIDER`):** create the one
matching the chosen provider — this is the key the gateway uses as its model
backend.

| `$INFER_PROVIDER` | SSM key | Where to find it |
|---|---|---|
| openrouter | `/bclaw/OPENROUTER_API_KEY` | https://openrouter.ai/keys |
| anthropic | `/bclaw/ANTHROPIC_API_KEY` | https://console.anthropic.com/settings/keys |
| zai | `/bclaw/ZAI_API_KEY` | https://z.ai/manage-apikey/apikey-list (Zhipu AI / open.bigmodel.cn for mainland China) |

For each parameter the user creates in the console, the settings are:

- **Tier:** `Standard`
- **Type:** `SecureString`
- **KMS Key ID:** `alias/${CLAW_NAME}-ssm` — the claw's own CMK, created by the
  stack in Phase 2. **NOT** the default `alias/aws/ssm`. Type the alias name
  (e.g. `alias/bclaw-ssm`) into the console's KMS key picker; it resolves to
  the key the template just created.
- **Value:** the secret itself (masked input).

> **CLI fallback.** If the console is unavailable or the user prefers
> scripting, the same parameters can be written from a shell. Prefix the
> command with a leading space so the value stays out of shell history:
>
> ```bash
>  aws ssm put-parameter --name "/bclaw/SLACK_BOT_TOKEN" \
>    --type SecureString --key-id "alias/${CLAW_NAME}-ssm" \
>    --value "<token>" --region "$AWS_REGION"
> # repeat for the other 3 Slack secrets + the provider key (+ GH_TOKEN_VAL if
> # ENABLE_GH=true), substituting the real values
> ```

**Gate: verify all required parameters exist before proceeding** (values not
displayed) — the 4 Slack secrets plus the provider key (plus `GH_TOKEN_VAL` iff
`ENABLE_GH=true`). Every parameter mapped in `agent_home/config.yaml`'s `env:`
should exist before the gateway boots with the plugin configured (Phase 5); a
missing one is non-fatal (the plugin warns and skips it, the gateway boots
without it) but the Slack tokens and the provider key are required for a
working claw, so confirm they are present:

```bash
PROVIDER_KEY=$(case "$INFER_PROVIDER" in
  openrouter) echo OPENROUTER_API_KEY ;;
  anthropic)  echo ANTHROPIC_API_KEY ;;
  zai)        echo ZAI_API_KEY ;;
esac)

REQUIRED="SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS SLACK_HOME_CHANNEL $PROVIDER_KEY"
[ "$ENABLE_GH" = "true" ] && REQUIRED="$REQUIRED GH_TOKEN_VAL"

for k in $REQUIRED; do
  aws ssm get-parameter --name "/bclaw/$k" \
    --region "$AWS_REGION" --query 'Parameter.Name' --output text 2>&1
done
```

Every listed parameter should resolve successfully. A missing one is
non-fatal (the gateway boots without it), but the Slack tokens and the
provider key are required for a working claw — do not proceed until they
exist.

---

### Phase 4: Scale the service to 1 and verify

**Gate: all required SSM parameters exist.**

First confirm the container instance the ASG launched has registered with the
cluster (the UserData finishes the EBS wiring before writing `ECS_CLUSTER`, so
registration only happens once `/data` is mounted — this prevents a task from
bind-mounting an unmounted `/data` and silently landing on the ephemeral root
filesystem):

```bash
aws ecs list-container-instances --cluster "$CLAW_NAME" --region "$AWS_REGION" \
  --query 'containerInstanceArns' --output text
```

If this is empty, the instance is still booting (UserData running) — re-check
every 30s. It takes a few minutes on a first deploy. If it stays empty, shell
into the instance via Session Manager (the instance has `AmazonSSMManagedInstanceCore`)
and inspect `/var/log/user-data.log`:

```bash
INSTANCE_ID=$(aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names "${CLAW_NAME}-asg" --region "$AWS_REGION" \
  --query 'AutoScalingGroups[0].Instances[0].InstanceId' --output text)
aws ssm start-session --target "$INSTANCE_ID" --region "$AWS_REGION"
# then on the host:  tail -100 /var/log/user-data.log
```

Once a container instance is registered, scale the service up. This starts the
task. The only `secrets[]` entry is `GH_TOKEN_VAL` (if GitHub auth is enabled),
which the ECS agent fetches via the execution role's `ssm:GetParameters`
grant and decrypts with KMS. The Slack tokens and the provider key are NOT
injected here — they are resolved from SSM by the aws_ssm plugin, which is not
installed or configured until Phase 5. So on this first boot the gateway
starts without Slack: it logs "No messaging platforms enabled" and stays
RUNNING. Slack connects after Phase 5 installs the plugin + merges its
`secrets:` config block + a restart.

```bash
aws ecs update-service \
  --cluster "$CLAW_NAME" \
  --service "$CLAW_NAME" \
  --desired-count 1 \
  --region "$AWS_REGION"
```

#### 4a. Wait for the task to reach RUNNING

Initial placement takes 2–3 minutes (mostly the ~500 MB image pull from
`ghcr.io`). Poll until the task is `RUNNING`:

```bash
aws ecs wait tasks-running \
  --cluster "$CLAW_NAME" \
  --tasks "$(aws ecs list-tasks --cluster "$CLAW_NAME" --region "$AWS_REGION" \
    --query 'taskArns[0]' --output text)" \
  --region "$AWS_REGION"
```

If you see a transient `CannotPullContainerError` in service events, don't
panic — ECS automatically stops the failed task and starts a fresh one.
Persistent failures usually mean a real problem (SG/subnet/IAM/image-not-found).

```bash
aws ecs describe-services --cluster "$CLAW_NAME" --services "$CLAW_NAME" \
  --region "$AWS_REGION" --query 'services[0].events[:5]' --output table
```

#### 4b. Confirm ARM64 placement and the AZ

```bash
TASK_ARN=$(aws ecs list-tasks --cluster "$CLAW_NAME" --region "$AWS_REGION" \
  --query 'taskArns[0]' --output text)

aws ecs describe-tasks --cluster "$CLAW_NAME" --tasks "$TASK_ARN" \
  --region "$AWS_REGION" \
  --query 'tasks[0].{AZ:availabilityZone, CPU:cpu, Mem:memory, Arch:runtimePlatform.cpuArchitecture, Status:lastStatus}' \
  --output table
```

Expected: `Arch = ARM64`, `Status = RUNNING`, `AZ` is the single AZ from Phase 1.

#### 4c. Tail the gateway logs — expect a healthy boot WITHOUT Slack yet

```bash
aws logs tail "/ecs/${CLAW_NAME}" --region "$AWS_REGION" --follow
```

The gateway starts cleanly but is NOT connected to Slack on this first boot —
the aws_ssm plugin isn't installed or configured until Phase 5, so the Slack
tokens aren't in the env. Expect a `WARNING gateway.run: No messaging
platforms enabled.` line (and `No env user allowlists configured`); this is
expected, not an error, and the gateway stays `RUNNING`. You may also see an
early `[gh-auth] login failed (non-fatal)` line if the GitHub login didn't
take (only when GitHub auth is enabled — `ENABLE_GH=true`; an opt-out claw
logs nothing here) — non-blocking (see Phase 6a to verify/fix). `Ctrl-C` once
the gateway is up; the Slack connection is confirmed in Phase 5 (after the
plugin is configured).

---

### Phase 5: Overlay, install the aws_ssm plugin, and merge the secrets config

**Gate: task is `RUNNING` (Phase 4).**

On the first boot (Phase 4) the gateway started on self-seeded defaults with
NO secrets in the env — the aws_ssm plugin isn't installed and its `secrets:`
config block isn't present, so Slack isn't connected yet. This phase completes
the bootstrapping: overlay the curated `agent_home/`, install the plugin,
merge its config block into the live `config.yaml`, then restart. After the
restart the plugin resolves every `/bclaw/*` secret at the first env load and
the Slack bot connects.

You need the manage skill's ECS Exec transport for all three steps below.
Setup has already satisfied its entry conditions:

- **Claw name + region** — `$CLAW_NAME` / `$AWS_REGION` from Phase 1.
- **A RUNNING task + `$TASK_ARN`** — from Phase 4b. This satisfies the
  manage skill's shared-first-step RUNNING check.
- **Session Manager plugin** — the manage skill's Prereq §3 applies (it is
  also needed for shell-in in Phase 6); its smoke-test exec command is the
  first ECS Exec call of this setup, so run it to confirm the plugin works
  and the caller has exec perms.

#### 5a. Overlay agent_home/ (manage-bclaw Mode 1)

Establish the curated baseline — skills, memories, system prompt, `SOUL.md`
persona, and the default `boldblackai/skills` marketplace tap
(`agent_home/skills/.hub/taps.json`) — on the claw's `/home/harness/.hermes`.
Run `manage-bclaw` in
**Mode 1 (Overlay)** now; it owns the full procedure (tar+base64 over ECS
Exec, chunked transfer, decode/extract/`chown`, merge-with-overwrite
semantics, dry-run gate). `config.yaml` is excluded from the overlay on
purpose (a file-level overwrite would discard non-env keys the cloud-mode
re-seed doesn't restore) — it is handled in 5c.

If `agent_home/` is absent or contains only excluded files, there is nothing
to overlay — skip this step; the claw keeps its self-seeded defaults.

#### 5b. Install the aws_ssm secret-source plugin

Install the plugin that resolves the `/bclaw/*` SSM parameters into env vars
at gateway startup. It writes into `~/.hermes/plugins/` (EBS-backed, persists
across restarts), so this is a one-time setup step. Run it as the harness user
(uid 1000) from an exec session (which runs as root):

```bash
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'runuser -u harness -- hermes plugins install boldblackai/hermes-aws-ssm-secret-source --enable 2>&1'" \
  --region "$AWS_REGION"
```

Confirm it is enabled (`Status: enabled`):

```bash
aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive \
  --command "sh -c 'runuser -u harness -- hermes plugins list 2>/dev/null | grep -i aws_ssm'" \
  --region "$AWS_REGION"
```

> The plugin is inert until its `secrets:` config block is present in
> `~/.hermes/config.yaml` (5c) AND the gateway restarts (it loads plugins at
> startup). The `secrets.sources names unknown source(s): aws_ssm` warning
> hermes prints is benign — it appears before #64189's post-discovery re-pull
> resolves the source; the gateway log then shows
> `AWS SSM Parameter Store: applied N secrets`.

#### 5c. Merge the secrets config block (manage-bclaw Mode 3)

The plugin's config lives in `agent_home/config.yaml` (a minimal `secrets:`
block). The overlay excluded it, so merge it into the live
`~/.hermes/config.yaml` at the key level — this preserves the live config's
comments/order/env-driven keys while adding the non-env `secrets:` block
(which survives the cloud-mode re-seed on restart). Run `manage-bclaw` in
**Mode 3 (Merge-config)** now; it fetches the live config (byte-chunked over
ECS Exec), runs `merge_config.py` (ruamel.yaml round-trip via `uv`), and
pushes the merged result back. Confirm the merge when it asks.

#### Restart to apply (unconditional)

Restart so the gateway re-reads its config and loads the plugin. After the
restart the plugin resolves every `/bclaw/*` secret at the first env load and
the Slack bot connects (the service's recreate deployment config makes this a
stop-old-then-start-new swap, ~10-20s downtime):

```bash
aws ecs update-service --cluster "$CLAW_NAME" --service "$CLAW_NAME" \
  --force-new-deployment --region "$AWS_REGION"
aws ecs wait tasks-running --cluster "$CLAW_NAME" \
  --tasks "$(aws ecs list-tasks --cluster "$CLAW_NAME" --region "$AWS_REGION" \
    --query 'taskArns[0]' --output text)" \
  --region "$AWS_REGION"
```

Confirm Slack is now connected (expect an `applied N secrets` line including
`SLACK_BOT_TOKEN`, and NO `No messaging platforms enabled` line):

```bash
aws logs tail "/ecs/${CLAW_NAME}" --region "$AWS_REGION" --since 5m 2>/dev/null \
  | grep -iE "ssm parameter store|slack|No messaging platforms"
```

If you still see "No messaging platforms", the plugin install (5b) or merge
(5c) didn't take — re-run that step.

---

### Phase 6: Shell-in

**Gate: task is `RUNNING` and gateway logs show a healthy connection.**

The claw is now live. GitHub (`gh`) authentication is **automatic on boot when
enabled** (`ENABLE_GH=true`, Phase 1 step 3) — see Phase 6a below; there is no
manual auth step. To shell into the container
(uses SSM Session Manager under the hood — requires the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
locally):

```bash
TASK_ARN=$(aws ecs list-tasks --cluster "$CLAW_NAME" --region "$AWS_REGION" \
  --query 'taskArns[0]' --output text)

aws ecs execute-command --cluster "$CLAW_NAME" --task "$TASK_ARN" \
  --container hermes --interactive --command "/bin/bash" \
  --region "$AWS_REGION"
```

> **Exec sessions run as root.** `aws ecs execute-command` opens a **root**
> shell by default, even though the gateway (PID 1) runs as the `harness` user
> (uid 1000). Prefix commands with `runuser -u harness --` to act as the
> workload user. To check the workload's actual uid from outside:
> `stat -c %u /proc/1` — `id -u` inside the exec session reports root.

> **ECS Exec over host networking works via the instance's public IP.** The
> task uses `NetworkMode: host`, so the SSM agent bind-mounted into the
> container reaches the `ssmmessages` endpoints over the container instance's
> ENI (which has a public IP) — no NAT gateway and no VPC endpoints required.

#### 6a. GitHub authentication (automatic on boot)

`gh`/HTTPS-git authentication is **not** a manual step — when GitHub auth is
enabled (`ENABLE_GH=true`), the task definition injects `GH_TOKEN_VAL` from
the `/bclaw/GH_TOKEN_VAL` SSM parameter and the container `Command` runs, as
the harness user on every boot:

```
if [ -n "$GH_TOKEN_VAL" ]; then
  printf "%s" "$GH_TOKEN_VAL" | gh auth login --with-token 2>&1 \
    || echo "[gh-auth] login failed (non-fatal)"
fi
exec hermes gateway
```

The `if [ -n ... ]` guard means an opt-out claw (`ENABLE_GH=false`) skips the
login entirely — `GH_TOKEN_VAL` is never injected, so there's no spurious
`[gh-auth]` failure logged. The login is **non-fatal** when enabled: if it
fails (rejected token, GitHub outage), the failure is logged to CloudWatch as
`[gh-auth] login failed (non-fatal)` and the gateway still starts — the Slack
bot is the claw's primary function; `gh` is secondary. The session persists in
`~/.config/gh` (on the EBS volume), and the entrypoint's `setup-env.sh` has
already seeded `GIT_CONFIG_GLOBAL` with the `gh auth git-credential` helper, so
HTTPS git operations authenticate via the same token.

**Verify it worked** (from a root exec session):

```bash
runuser -u harness -- gh auth status
```

A healthy boot shows `Logged in to github.com as <user>`. If instead you see
`not logged in`, check the logs for the `[gh-auth]` line — the token was
rejected (rotate it, see below) or GitHub was briefly unreachable (the next
task restart retries automatically).

**Rotating the token.** Update the `/bclaw/GH_TOKEN_VAL` SSM parameter in
the **AWS Console** (Systems Manager → Parameter Store → open the parameter →
**Edit** → paste the new PAT → Save), then force a new task so the boot
command re-runs the login:

```bash
aws ecs update-service --cluster "$CLAW_NAME" --service "$CLAW_NAME" \
  --force-new-deployment --region "$AWS_REGION"
```

See [harness docs → GitHub authentication](https://github.com/boldblackai/harness/blob/main/docs/github.md)
for creating a PAT and the scopes the claw's `gh`/git usage requires.

---

### Phase 7: Final report

Report to the user:

- Claw name, region, inference provider, and the single AZ the instance/task
  live in (with ARM64 confirmation)
- Stack name and key outputs (cluster, EBS volume ID, ASG name, SSM prefix)
- The SSM parameter locations (4 Slack + the provider key, plus `/bclaw/GH_TOKEN_VAL` if GitHub auth was enabled — values never displayed)
- The aws_ssm plugin is installed and its `secrets:` config merged (Phase 5); it resolves every `/bclaw/*` secret at gateway startup. Slack is connected (confirmed in Phase 5). To add or rotate a key, write the SSM param + force a new task (`aws ecs update-service --force-new-deployment`) — no template edit or redeploy.
- GitHub auth (if enabled) is automatic on boot from `/bclaw/GH_TOKEN_VAL` (Phase 6a) — verify with `runuser -u harness -- gh auth status` from an exec session; if disabled, `gh auth status` showing "not logged in" is expected
- How to tail logs: `aws logs tail "/ecs/${CLAW_NAME}" --follow --region "$AWS_REGION"`
- How to shell in: the `aws ecs execute-command` snippet from Phase 6
- How to tear down: point at the `teardown-bclaw` skill

---

## Notes

- **No derived image.** This deploys the signed upstream
  `ghcr.io/boldblackai/harness` image as-is. The upstream image's 4-way mount
  layout works directly via host bind-mounts on the EBS volume — no custom
  `Dockerfile` or `entrypoint.sh` is needed. Do not build a derived image.

- **Secrets live in SSM, not Secrets Manager.** Following the piranesi pattern,
  secrets are namespaced SecureString parameters (`/bclaw/KEY`) that the user
  writes. The `/bclaw/` namespace is hardcoded in the template so the
  deployer IAM policy can pin `parameter/bclaw/*`. They are not
  CloudFormation resources, so stack updates never clobber their values and
  they survive stack deletes. The teardown skill deletes them explicitly after
  user confirmation. SecureStrings are encrypted with a customer-managed KMS
  key (aliased `alias/${CLAW_NAME}-ssm`) created by the template — NOT the
  default `alias/aws/ssm`. The deployer policy pins `kms:Decrypt`/`kms:Encrypt`
  to this key via `kms:ResourceAliases`. Phase 3 tells the user to select this
  key (`alias/${CLAW_NAME}-ssm`) as the KMS Key ID when creating each
  SecureString in the console.

- **The `HARNESS_CLOUD_MODE=1` entrypoint behavior.** In cloud mode,
  `/entrypoint.sh` lets hermes self-seed `config.yaml` from env vars on
  first boot — it does **not** copy from any `/etc/harness/hermes-defaults/`
  directory (that path is referenced in older deploy docs but does not exist
  for hermes; only the `pi` agent has a `cp -rn` defaults seed). To seed a
  custom `system-prompt.md` or persona, write it directly into the EBS-mounted
  `/home/harness/.hermes/` via an exec session.

- **On-boot GitHub auth via the container `Command`.** The image's ENTRYPOINT
  is `[/tini, --, /entrypoint.sh]` (verified via `/proc/1` in a running
  container) — tini is PID 1 (signal forwarding + zombie reaping), and ECS
  `Command` overrides only CMD, not ENTRYPOINT, so tini and `/entrypoint.sh`
  always run. The task definition sets no explicit `EntryPoint` (it's baked
  into the image) and no `LinuxParameters.InitProcessEnabled` (tini is the
  init — a second ECS init layer would be redundant). The `Command` wrapper is
  `if [ -n "$GH_TOKEN_VAL" ]; then printf "%s" "$GH_TOKEN_VAL" | gh auth login --with-token 2>&1 || echo "[gh-auth] login failed (non-fatal)"; fi; exec hermes gateway`,
  which the entrypoint runs via its `exec "$@"`. The entrypoint runs first
  (sources `setup-env.sh` → routes `GIT_CONFIG_GLOBAL` into persisted
  `~/.config` and seeds the `gh auth git-credential` helper; seeds
  `config.yaml`), then `exec`s the wrapper as the harness user (uid 1000). The
  login is **non-fatal** — a failure (bad token, GitHub outage) is logged to
  CloudWatch and the gateway still starts. `GH_TOKEN_VAL` is an **optional**
  SSM param (`/bclaw/GH_TOKEN_VAL`), gated behind the `EnableGitHubKey` stack
  parameter (default `false`) and injected via `secrets[]` — it is the ONE
  secret still injected by CloudFormation, because the on-boot `gh auth login`
  runs before the aws_ssm plugin loads. (Every other secret — the Slack tokens
  and the inference-provider keys `OPENROUTER_API_KEY`/`ZAI_API_KEY`/
  `ANTHROPIC_API_KEY` — is resolved from SSM by the aws_ssm plugin at gateway
  startup, so they have no stack parameter and no `secrets[]` entry.) When
  GitHub auth is disabled, the `Command`'s `if [ -n "$GH_TOKEN_VAL" ]` guard
  skips the login entirely and no `GH_TOKEN_VAL` is injected. The secret is named
  `GH_TOKEN_VAL`, **not** `GH_TOKEN`, deliberately: when the reserved `GH_TOKEN`
  env var is present, `gh auth login --with-token` refuses to store the token
  (prints "the GH_TOKEN environment variable is being used", exits 1) — a gh
  safety feature. Injecting under a non-reserved name avoids the collision so
  gh stores the credential. Storing is **necessary**, not optional: the harness
  terminal/execute_code sandbox scrubs token-like env vars from its
  environment, so `gh`/git calls the agent makes find no env var — they rely on
  the stored credential in `~/.config/gh/hosts.yml` (on the EBS volume,
  persists across restarts). To rotate: update `/bclaw/GH_TOKEN_VAL` in the
  AWS console (Parameter Store → Edit, or `put-parameter --overwrite`) then
  `update-service --force-new-deployment` (the boot command re-runs on every
  task start). `printf` (not `echo`) is used so a token beginning with `-`
  isn't parsed as a flag, and `%s` avoids a trailing newline (gh trims
  whitespace anyway).

- **Host networking + the security group.** The task uses `NetworkMode: host`:
  the container shares the container instance's primary ENI, which carries the
  inbound-less security group and (via the subnet's `MapPublicIpOnLaunch` +
  the launch template's `AssociatePublicIpAddress`) a public IP for outbound to
  Slack (socket mode), ghcr.io (image pull), and SSM (`ssmmessages`, for ECS
  Exec). There is no NAT gateway and no task ENI — SG-per-task isolation is not
  used because the service is 1 task : 1 instance (a distinction without a
  difference at that ratio). The bot opens no inbound listener.

- **Persistent EBS volume + UserData wiring.** `EbsDataVolume` is a standalone
  `AWS::EC2::Volume` (gp3, encrypted, `DeletionPolicy/UpdateReplacePolicy:
  Retain`) tagged `Name=${ClawName}-data` and `ClawName=<claw>`, in the single
  AZ. It is NOT attached by CloudFormation (CFN cannot pre-attach to an
  ASG-managed instance); instead the launch template's UserData finds it by
  `Name` tag in the instance's AZ, attaches it (retrying through the
  `VolumeInUse` race when a predecessor instance is still detaching during ASG
  replacement), formats it if fresh (`mkfs.ext4 -L clawdata`), mounts it **by
  label** at `/data` (device names drift on Nitro/Graviton — `nvme1n1` vs the
  `/dev/sdf` attach hint — so the mount uses `LABEL=clawdata`, never the device
  name), creates the 4 subdirs with `chown 1000:1000`, and only THEN writes
  `ECS_CLUSTER` and restarts the ECS agent. Mounting and ECS registration are
  ordered so the task can never bind-mount an unmounted `/data` and silently
  land on the ephemeral root filesystem. Because the volume is standalone +
  retained + reattached by tag, data survives ASG instance replacement without
  any snapshot lifecycle. The harness user is uid/gid 1000; the subdirs are
  `chown`'d 1000:1000 by the UserData so the non-root gateway can write without
  any first-boot fixup.

- **Self-healing via the ASG.** `min=max=desired=1` means a failed or retired
  instance is replaced automatically; the replacement's UserData reattaches the
  same retained volume and re-registers, and ECS reschedules the task once the
  agent reconnects. This covers both hardware failure and AWS-initiated instance
  retirement (the latter is not covered by a standalone instance + a
  `RecoverInstance` alarm, which is why the ASG is used). No CloudWatch recovery
  alarm is needed — the ASG + EC2 health checks are the recovery.

- **`EnableExecuteCommand` cannot be toggled on a running service silently.**
  The template sets it at creation. If you ever need to re-enable it after a
  manual disable, you must force a new deployment
  (`aws ecs update-service --force-new-deployment ...`) for the SSM agent
  sidecar to re-inject.

- **AWS credentials.** See Prerequisites → "AWS credentials — the deployer IAM
  user" for creating the deployer principal, the `bclaw-deploy` policy, and the
  `.env` format. `.env` is gitignored; never commit it.

- **First-task image pull.** Initial task placement takes 2–3 minutes, most of
  it the ~500 MB image pull from `ghcr.io`. A transient
  `CannotPullContainerError` in service events is normal — ECS auto-retries.
  Persistent pull failures mean the task can't reach ghcr.io (the instance ENI
  needs outbound, which host networking + the public IP provide).

- **Stack updates revert un-passed parameters to template defaults.**
  `cloudformation deploy` applies the template's parameter `Default` to anything
  omitted from `--parameter-overrides` — it does not remember the prior stack's
  values. `DesiredCount` defaults to `1` (deliberately — a forgotten override
  keeps the claw running instead of scaling it to 0), but a count the user
  changed (e.g. scaled to 2) reverts unless re-passed. Before any stack update,
  capture the live count and re-pass it:

  ```bash
  DESIRED=$(aws ecs describe-services --cluster "$CLAW_NAME" --services "$CLAW_NAME" \
    --region "$AWS_REGION" --query 'services[0].desiredCount' --output text)
  # then add  DesiredCount="$DESIRED"  to --parameter-overrides on the deploy
  ```

  The provider-key and GitHub overrides are stricter — their default is `false`,
  so omitting them silently disables the feature (e.g. the gateway comes up
  with no model). Capture current params with `describe-stacks` and re-pass
  them, then verify the live task def matches intent.

- **Adding new SSM secrets.** To forward an additional secret into the
  gateway's env, just put it in SSM as a SecureString under `/bclaw/` (encrypted
  with the claw's CMK, `alias/${CLAW_NAME}-ssm`), then force a new task so the
  aws_ssm plugin resolves it at startup:
  ```bash
   aws ssm put-parameter --name "/bclaw/MY_API_KEY" \
     --type SecureString --key-id "alias/${CLAW_NAME}-ssm" \
     --value "<value>" --region "$AWS_REGION"
  aws ecs update-service --cluster "$CLAW_NAME" --service "$CLAW_NAME" \
    --force-new-deployment --region "$AWS_REGION"
  ```
  The leaf name becomes the env var (`/bclaw/MY_API_KEY` → `MY_API_KEY`;
  sub-paths flatten, e.g. `/bclaw/db/PASSWORD` → `DB_PASSWORD`). No
  `template.yaml` edit, no CloudFormation redeploy, no new stack parameter —
  the plugin already covers `parameter/bclaw/*`. Rotation is the same flow
  (`put-parameter --overwrite` + restart). This works for any secret consumed
  inside Hermes after the plugin loads (the Slack tokens, the provider keys,
  skill API keys).

  **Two exceptions** that CANNOT come from the plugin and need a `secrets[]`
  entry (a stack parameter + condition + the entry) instead:
  - **`GH_TOKEN_VAL`** (already wired) — the on-boot `gh auth login` runs in
    the container `Command` before `exec hermes gateway`, i.e. before the
    plugin loads, so it must be in the env at container start.
  - **Any secret consumed by the container `Command` itself** (pre-Hermes) —
    same reason. There are none today beyond `GH_TOKEN_VAL`.

  **Env-var blocklist caveat.** Hermes strips provider-credential names on its
  `_HERMES_PROVIDER_ENV_BLOCKLIST` from subprocess (terminal / execute_code)
  envs. For a key a *skill* uses, name it to avoid the blocklist (e.g.
  `OPENROUTER_IMAGE_API_KEY`, not `OPENROUTER_API_KEY`) and declare it in the
  skill's `required_environment_variables`.

- **Validating template edits.** After editing `template.yaml`, the built-in
  PyYAML linter (in `patch`/`write_file`) reports false-positive errors on
  CloudFormation intrinsic shorthand (`!Equals`, `!Sub`, `!If` — valid CFN, not
  valid plain YAML). Ignore those; instead validate with cfn-lint:
  `uvx cfn-lint .agents/skills/setup-bclaw/template.yaml` (run via
  `mise exec -- uvx cfn-lint ...`).
