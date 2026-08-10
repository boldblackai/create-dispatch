# bclaw

A Hermes Agent claw — a long-running gateway deployed as a Slack socket-mode
bot. It is outbound-only: no load balancer, no inbound ports. The Slack app
manifest lives in `slack-manifest.json`. The `agent_home/` overlay ships a
default `boldblackai/skills` marketplace tap so `hermes skills search` /
`install` can discover the public catalog after setup.

Runs on AWS ECS using the **EC2 launch type** — a single container instance in
an Auto Scaling Group (`min=max=desired=1`) with a **persistent EBS data
volume** for the claw's SQLite databases. Deployed and torn down via the agent
skills in `.agents/skills/`.


## Tooling

`mise` manages all tools (`aws-cli`, `jj`) — see `mise.toml`. Do not install
system-wide. Activate mise in every shell:

```bash
eval "$(/usr/local/bin/mise activate bash)" && mise trust
```


## Setup

The setup skill can't run until AWS credentials exist, and credentials need a
principal allowed to create + tear down the claw. Set up a dedicated
least-privilege deployer user rather than reusing a broad admin principal, then
run the skill.

### 1. Create the deployer IAM user

IAM → Users → Create user (e.g. `bclaw-deployer`) with programmatic access (an
access key).

### 2. Attach the `bclaw-deploy` policy and create the service role

Bclaw uses a **two-role** model so the deployer's long-lived access key is
never root-equivalent if it leaks:

- **`bclaw-deployer`** (the human identity, attached policy
  [`bclaw-deploy-policy.json`](./bclaw-deploy-policy.json)) — narrow powers:
  manage the CloudFormation stack, write/read SSM secrets, shell in, scale the
  service, debug the container instance, recover orphans during teardown, and
  manage **one** literal role (`bclaw-cfn-exec`).
- **`bclaw-cfn-exec`** (the CloudFormation service role, inline policy
  [`bclaw-cfn-exec-policy.json`](./bclaw-cfn-exec-policy.json), trust
  [`bclaw-cfn-exec-trust.json`](./bclaw-cfn-exec-trust.json)) — the broad
  infrastructure-create lifecycle (EC2/ASG/ECS/IAM/KMS/logs) that
  CloudFormation assumes during every deploy and the stack delete. It is
  **not** a stack resource (the stack cannot create the role it assumes to
  create itself), so the setup skill creates it idempotently in **Phase 0**
  before the first deploy, and the teardown skill deletes it last.

The deployer identity only ever passes `bclaw-cfn-exec` to CloudFormation
(`iam:PassRole` conditioned to `cloudformation.amazonaws.com`); it never
touches the infrastructure resources directly. Attach
[`bclaw-deploy-policy.json`](./bclaw-deploy-policy.json) to the user during
this onboarding step — the service role is created later, at deploy time.

#### Deployer policy — why some resources stay `Resource: "*"`

The pinning rule: **a resource can be pinned to a prefix only if its ARN is
name-based**. Resources whose ARNs use **AWS-assigned IDs** cannot be pinned by
ARN prefix — but if they support tags, they're constrained with
`aws:ResourceTag` conditions instead (the ABAC pattern). The deployer
statements below carry `Resource: "*"` with **no condition** — the hard AWS
constraints:

| Statement | Why it stays `*` (no condition) |
|---|---|
| `ReadOnlyDescribe` | Merged read-only bucket: every `Describe*`/`List*` action across EC2/ASG/Logs/SSM/ECS is List-type with no resource-level support (AWS requires `*`). All read-only; the sensitive create/delete/mutate actions live on `bclaw-cfn-exec`. |
| `ECSRead` | `DescribeTaskDefinition`/`DescribeTasks`/`ListTasks` operate on runtime-assigned IDs and task-definition families with no resource-level support — AWS requires `*`. Read-only (used for probing the live service). The sensitive `RegisterTaskDefinition` write lives on `bclaw-cfn-exec`. |
| `SSMMessages` | Amazon Message Gateway Service (`ssmmessages`) does not support resource-level permissions at all — AWS requires `Resource: "*"` for all four channel actions. Needed for `aws ecs execute-command` (ECS Exec) over the host's internet path. The sensitive `ecs:ExecuteCommand` itself IS scoped to bclaw tasks/cluster (`ECSExec`). |
| `CloudFormationGlobalMeta` | `GetTemplateSummary`/`ValidateTemplate` are account-global metadata calls with no resource ARN — AWS requires `*`. Read-only (the deploy command runs them before the change set). |

**Tag-conditioned statements** (ABAC — a tag condition restricts to the claw's
resources; `EC2InstanceOps` is additionally ARN-scoped to `instance/*`):

| Statement | Condition | What it protects |
|---|---|---|
| `EC2NetworkingManage` | `aws:ResourceTag/Name = bclaw*` | Teardown recovery: can only delete/mutate the claw's own VPC/subnets/route-tables/IGW/SG left orphaned by a `FORCE_DELETE_STACK`. Cannot touch any other networking. (Network **create** lives on `bclaw-cfn-exec`.) |
| `EC2InstanceOps` | `aws:ResourceTag/ClawName = bclaw` (ARN-scoped to `instance/*`) | `manage-bclaw` Mode 4: read the console output of, or terminate, the claw's own container instances. Cannot touch co-tenant instances. |
| `EC2DataVolumeManage` | `aws:ResourceTag/Name = bclaw-data` | Teardown Phase 3: delete the claw's own retained data volume. |
| `KMSUseKey` | `kms:ResourceAliases = alias/bclaw-ssm` (+ `kms:ViaService = ssm`) | Decrypt/Encrypt/ScheduleKeyDeletion only on the claw's own CMK, only via SSM. Cannot use any other key. |

ARN-pinned statements (no `*`): `CloudFormation` (`stack/bclaw/*`),
`ManageCfnExecRole` (`role/bclaw-cfn-exec` — create/delete the one service
role), `PassRoleToCfn` (`role/bclaw-cfn-exec` → `cloudformation.amazonaws.com`),
`ECSExec` (`cluster/bclaw`, `task/bclaw/*`), `ECSServiceManage` (`cluster/bclaw`,
`service/bclaw/*`), `LogsRead` (`log-group:/ecs/bclaw*`), `SSMSecrets`
(`parameter/bclaw/*`), and `DenyDirectSSMSession` (`task/bclaw/*`, a Deny).

#### Service role (`bclaw-cfn-exec`) — the infrastructure-create lifecycle

The service role's inline policy carries everything CloudFormation needs to
realize `template.yaml` and tear it back down. It is the natural home for the
powers that cannot be safely scoped on a human-held key:

- **EC2** networking/volume/launch-template create + tag-scoped delete, plus
  `ec2:RunInstances` + `CreateTags`
  (`EC2NetworkingCreate`/`EC2NetworkingManage`/`EC2DataVolumeCreate`/
  `EC2LaunchTemplateManage`). `RunInstances` is on cfn-exec (not the deployer)
  because the Auto Scaling Group pre-validates the launch template by
  simulating the instance launch, which requires the caller to be authorized
  for `ec2:RunInstances`; real launches still go through the Auto Scaling
  service-linked role.
- **Auto Scaling** group CRUD + the autoscaling service-linked role
  (`AutoScalingCreate`/`AutoScalingManage`/`CreateAutoScalingServiceLinkedRole`).
- **IAM** create/delete on `role/bclaw-*` and `instance-profile/bclaw-*`
  (`IAMRoles`/`IAMInstanceProfiles`) — the stack's exec/task/instance roles.
- **`iam:PassRole`** on `role/bclaw-*` + `instance-profile/bclaw-*` (`PassRole`,
  resource-scoped, **not** service-conditioned — `iam:PassedToService`-conditioning
  breaks this stack's Auto Scaling launch-template validation; the resource scope
  is the boundary, and cfn-exec can only pass the stack's own `bclaw-*` roles).
- **ECS** cluster/service/task-definition write + describe (`ECSWrite`/`ECSDescribe`).
- **KMS** key lifecycle — `CreateKey`/`CreateAlias`/`DeleteAlias`/`PutKeyPolicy`/
  `EnableKeyRotation`/`DescribeKey` (`KMSLifecycle`, `Resource: "*"`). The
  key-management actions cannot be alias-scoped: KMS evaluates the `PutKeyPolicy`
  capability at `CreateKey` time, before the alias exists, so an alias condition
  would block key creation.
- **Logs** group create/delete/retention + tag actions (`LogsLifecycle`).
- **SSM** public AMI-id resolution (`SSMPublicEcsAmi`) + read-only describes
  (`ReadOnlyDescribe`).

Because `bclaw-cfn-exec` is assumable **only** by `cloudformation.amazonaws.com`
(its trust policy) and the deployer's only `iam:PassRole` for it is conditioned
to that same service, none of these broad powers are reachable by the
human-held key — closing the privilege-escalation chains a leaked deployer key
otherwise opens.

#### Notes

- **Shell-in (ECS Exec) permissions are on the deployer.** `ECSExec` grants
  `ecs:ExecuteCommand` scoped to the bclaw cluster + tasks; `SSMMessages`
  grants the four `ssmmessages:*` channel actions (`ssmmessages` cannot be
  resource-scoped). Needed by setup (Phases 6–7), teardown, and `manage-bclaw`.
  AWS additionally recommends **denying** `ssm:StartSession` on ECS tasks
  (`DenyDirectSSMSession`): sessions via `ecs:ExecuteCommand` are logged;
  direct SSM sessions bypass ECS Exec logging and consume the session quota.
- **The deployer's `iam:PassRole` is `PassedToService`-conditioned**
  (`PassRoleToCfn`: `bclaw-cfn-exec` → `cloudformation.amazonaws.com` only). The
  service role's `iam:PassRole` (`PassRole`) is **resource-scoped** to
  `bclaw-*` roles/instance-profiles but **not** service-conditioned —
  `iam:PassedToService`-conditioning breaks this stack's Auto Scaling
  launch-template validation (the same constraint that forces cfn-exec's
  KMS key-management actions to be unconditional). The boundary is the resource
  scope: cfn-exec can only pass the stack's own `bclaw-*` roles, and cfn-exec
  is itself assumable only by `cloudformation.amazonaws.com`.
- **No `iam:SimulatePrincipalPolicy`.** The deployer policy intentionally omits
  it (a leaked key should not be able to probe its own scope). Permission gaps
  surface as the exact `is not authorized to perform` error at deploy time. If
  you need the policy simulator to verify a permission split, run it from a
  separate admin identity.
- **`sts:DecodeAuthorizationMessage` is not granted.** To decode an encoded
  denial message, use a separate admin identity (the deployer key deliberately
  cannot).

### 3. Put the access key in `.env`

Create `.env` (gitignored) with the access key from step 1:

```
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
```

`mise` loads `.env` via the `[env] _.file` entry in `mise.toml`. Activate mise
in every shell that runs `aws`:

```bash
eval "$(/usr/local/bin/mise activate bash)" \
  && mise trust /workspace \
  && cd /workspace
```

Verify:

```bash
aws sts get-caller-identity --query 'Account' --output text
```


## Slack app

The claw runs as a Slack **socket-mode** bot — it makes an outbound WebSocket
connection to Slack, so there is no inbound URL to host (the
`hermes-agent.local` request URLs in `slack-manifest.json` are placeholders,
ignored under socket mode). The manifest fully defines the app: name (`bclaw`),
the slash commands, OAuth scopes, event subscriptions, bot user, and
interactivity. Socket mode is on by default in the manifest.

### Create the app

1. Go to https://api.slack.com/apps → **Create New App** → **From an app
   manifest**.
2. Pick the workspace.
3. Paste the contents of [`slack-manifest.json`](./slack-manifest.json) →
   **Create**.

The app comes up with socket mode on, every slash command registered, the bot
user `bclaw`, OAuth scopes, and event subscriptions already configured —
nothing to toggle by hand.

### App-level token (socket mode auth)

Socket mode authenticates the outbound WebSocket with an app-level token, which
Slack generates separately (it can't live in the manifest):

- **Basic Information** → **App-Level Tokens** → **Generate Token and Scope**.
- Name it (e.g. `socket`), add the **`connections:write`** scope → **Generate**.
- Copy the `xapp-` token → this becomes `/bclaw/SLACK_APP_TOKEN`.

### Install to the workspace

- **OAuth & Permissions** → **Install to Workspace** → authorize.
- Copy the **Bot User OAuth Token** (`xoxb-`) → this becomes
  `/bclaw/SLACK_BOT_TOKEN`.

### Channel + user IDs

- **Your user ID** (for `/bclaw/SLACK_ALLOWED_USERS`): in Slack, click your
  profile → **Copy member ID**. Comma-separate multiple IDs.
- **Home channel ID** (for `/bclaw/SLACK_HOME_CHANNEL`): right-click the
  channel → **Copy link**, take the trailing ID.
- If the home channel is **private**, invite the bot with `/invite @bclaw` so
  it can read and post there (public channels are covered by its `channels:*`
  scopes once installed).

## Secrets you'll need

The claw needs SSM SecureString parameters under the `/bclaw/` namespace. The
scaffolder renames `bclaw` to your claw name everywhere — SSM paths, the IAM
scope, and the CloudFormation template — so the namespace matches the claw
name. It is a literal in the template (not derived from the `ClawName` parameter
at deploy time), which is what lets the IAM policy pin it to a fixed ARN prefix.
You'll enter them in the AWS console during the setup skill (Phase 3) — they're
not CloudFormation resources, so they survive stack updates and deletes.

A Hermes secret-source plugin (`aws_ssm`, from
[boldblackai/hermes-aws-ssm-secret-source](https://github.com/boldblackai/hermes-aws-ssm-secret-source),
installed during setup) resolves every `/bclaw/*` parameter into the gateway's
environment at startup, so adding or rotating a key is just an SSM write + task
restart — no template edit, no redeploy. The only secret NOT resolved by the
plugin is the optional GitHub token (`GH_TOKEN_VAL`): the on-boot `gh auth
login` runs before Hermes starts, so it is injected the CloudFormation way (a
stack parameter gates it). Gather the values beforehand:

### Slack (required)

| SSM key | What it is | Where to find it |
|---|---|---|
| `/bclaw/SLACK_BOT_TOKEN` | Slack bot OAuth token (`xoxb-`) | Slack app → OAuth & Permissions → Bot User OAuth Token |
| `/bclaw/SLACK_APP_TOKEN` | Slack app-level token (`xapp-`, enables socket mode) | Slack app → Basic Information → App-Level Tokens |
| `/bclaw/SLACK_ALLOWED_USERS` | Comma-separated Slack user IDs allowed to use the bot | Slack profile → "Copy member ID" |
| `/bclaw/SLACK_HOME_CHANNEL` | Slack channel ID the bot treats as home | Right-click channel → "Copy link", take the trailing ID |

### Optional: GitHub authentication

`/bclaw/GH_TOKEN_VAL` is the ONE secret still injected via CloudFormation
(`secrets[]` + the `EnableGitHubKey` stack parameter, Phase 1 step 3), because
the on-boot `gh auth login --with-token` runs before Hermes (and the aws_ssm
plugin) start. Enable it only if the agent should make authenticated
`gh`/HTTPS-git calls; when disabled the login is skipped and no token is
injected.

| SSM key | What it is | Where to find it |
|---|---|---|
| `/bclaw/GH_TOKEN_VAL` | GitHub PAT — on-boot `gh auth login` (see setup skill Phase 6a). Named `*_VAL`, not `GH_TOKEN`, to avoid `gh`'s reserved env var | https://github.com/settings/tokens |

### Inference-provider key (create at least one)

| SSM key | What it is | Where to find it |
|---|---|---|
| `/bclaw/OPENROUTER_API_KEY` | OpenRouter API key (recommended) | https://openrouter.ai/keys |
| `/bclaw/ANTHROPIC_API_KEY` | Anthropic (direct Claude API) | https://console.anthropic.com/ |
| `/bclaw/ZAI_API_KEY` | Z.AI / Zhipu (GLM) | https://z.ai/manage-apikey/apikey-list |

The aws_ssm plugin resolves every provider key present in SSM, so you can
create more than one if the gateway uses multiple providers. Create at least the
one matching the provider you chose in Phase 1.

Each parameter is a **SecureString** and must be encrypted with the claw's own
KMS key (alias `alias/bclaw-ssm`), created by the setup skill's CloudFormation
stack in Phase 2 — **not** the default `alias/aws/ssm`. The deployer IAM policy
pins `kms:Decrypt`/`kms:Encrypt` to `alias/bclaw-ssm` via
`kms:ResourceAliases`, so the claw can only decrypt parameters this key
encrypted. A parameter left under the default SSM key fails to decrypt, so the
aws_ssm plugin can't resolve it and the gateway runs without it. In the
console's KMS key picker, type `alias/bclaw-ssm` (substituting your claw name) —
it resolves to the key the stack just created.


## Deploy

### 4. Run the setup skill

It follows a gated sequence: create the CloudFormation service role
(`bclaw-cfn-exec`) → probe one ARM64 AZ → deploy CloudFormation (VPC, EBS
volume, EC2 container instance + Auto Scaling Group, ECS service at DesiredCount
0 on the first deploy) → write SSM secrets → scale to 1 → overlay
`agent_home/` + install the aws_ssm plugin + merge its secrets config →
restart → verify.

`.agents/skills/setup-bclaw/SKILL.md`

Permissions are not pre-checked — if the deployer principal is missing an
action, CloudFormation surfaces the exact `is not authorized to perform` error
at deploy time (Phase 2). Fix any gap in the policy from step 2 and re-run.


## Tear down

`.agents/skills/teardown-bclaw/SKILL.md`
