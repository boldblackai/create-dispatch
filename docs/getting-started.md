# Getting started

Generate a repository for your dispatch agent, deploy it to AWS, and connect it
to Slack. End to end this takes about 30 minutes, most of it waiting on
CloudFormation.

## Prerequisites

- **Node.js 18+** — to run the generator (`npx`).
- **An AWS account** — the agent runs on ECS (EC2 launch type) in a region of
  your choice (default `us-east-1`). You will create a dedicated least-privilege
  deployer IAM user during setup.
- **A Slack workspace where you can install apps** — the agent runs as a
  socket-mode bot, so it needs no inbound URLs or load balancer.
- **A harness** — the generated repo is a set of skills, opened in
  [Pi](https://github.com/boldblackai/harness), [Hermes](https://github.com/boldblackai/harness),
  or [OpenCode](https://github.com/boldblackai/harness) (see
  [harness docs](https://harness.boldblack.ai/docs/)).

## 1. Generate the repo

```bash
npx @boldblackai/create-dispatch swe-pal
```

This creates a `swe-pal/` directory: a renamed snapshot of the dispatch
template. Every `dispatch` reference — file contents and file/directory names,
including the SSM namespace, IAM scopes, and KMS alias — now reads `swe-pal`.

You can also pass the AWS region the agent will deploy into:

```bash
npx @boldblackai/create-dispatch swe-pal --region us-west-2
```

The region is substituted into the generated claw (notably the deployer IAM
policy's `kms:ViaService`, which is a static JSON that cannot use
CloudFormation's `${AWS::Region}`). All CLI flags are covered in the
[CLI reference](cli.md).

## 2. Create the deployer IAM user

The setup skill cannot run until AWS credentials exist, and credentials need a
principal allowed to create and tear down the agent. Create a dedicated
least-privilege deployer user rather than reusing a broad admin principal.

The generated repo's README walks through this in detail. In short:

1. IAM → Users → Create user (e.g. `dispatch-deployer`) with programmatic
   access.
2. Attach the repo's `dispatch-deploy-policy.json` policy to the user.
3. Put the access key in the repo's `.env` (gitignored).

The agent uses a **two-role model** so a leaked deployer key is never
root-equivalent — see [Security model](security.md) for how the split works.

## 3. Create the Slack app

The agent runs as a Slack **socket-mode** bot: it makes an outbound WebSocket
connection to Slack, so there is no inbound URL to host. The manifest
(`slack-manifest.json`) fully defines the app — name, slash commands, OAuth
scopes, event subscriptions, and socket mode.

1. Go to <https://api.slack.com/apps> → **Create New App** → **From an app
   manifest**, pick your workspace, and paste the manifest.
2. Generate an app-level token (`xapp-`, scope `connections:write`) — this
   becomes the `SLACK_APP_TOKEN` secret.
3. Install the app to the workspace and copy the bot token (`xoxb-`) — this
   becomes the `SLACK_BOT_TOKEN` secret.
4. Copy your Slack user ID and a home channel ID for the allow-list and home
   channel secrets.

## 4. Gather the secrets

The agent resolves its secrets at startup from SSM Parameter Store
SecureStrings under the `/dispatch/` namespace (renamed to your agent's name).
Secrets are not CloudFormation resources — they survive stack updates and
deletes. [Concepts: secrets](secrets.md) covers the full list and the KMS
key requirement.

At minimum: the four Slack values, plus one inference-provider API key
(OpenRouter, Anthropic, or Z.AI).

## 5. Run the setup skill

Open the generated repo in your harness and run the `/setup-dispatch` skill. It
follows a gated sequence:

1. Create the CloudFormation service role (`dispatch-cfn-exec`).
2. Probe one ARM64 availability zone.
3. Deploy CloudFormation (VPC, persistent EBS volume, single-instance Auto
   Scaling Group, ECS service at `DesiredCount 0` on first deploy).
4. Write the SSM secrets.
5. Scale to 1.
6. Overlay `agent_home/`, install the `aws_ssm` secret-source plugin, and
   merge its secrets config.
7. Restart and verify.

It will prompt for an inference provider — [OpenRouter](https://openrouter.ai/),
[ZAI](https://z.ai/subscribe), and [Anthropic](https://www.anthropic.com/) are
supported out of the box, and any provider
[hermes-agent already supports](https://hermes-agent.nousresearch.com/docs/integrations/providers/)
works too.

When the skill finishes, your agent is live in Slack. Talk to it the way you
would talk to any colleague: `@swe-pal can you review PR 42?`

## What you get

- [hermes-agent](https://hermes-agent.nousresearch.com/docs) running on AWS ECS
  (EC2 launch type) — a single container instance in an Auto Scaling Group with
  a persistent EBS data volume — via the hardened
  [harness](https://github.com/boldblackai/harness) Docker image.
- GitHub and Slack integration.
- SQLite-backed persistent state on a retained gp3 EBS volume (local block
  storage — SQLite WAL is unsafe on NFS).

## Next steps

- [Architecture](architecture.md) — what got built on AWS and why it is shaped
  that way.
- [Upgrading](upgrading.md) — roll the running agent onto a new harness image
  tag.
- [Concepts: skills](skills.md) — how to change the agent's skills, memories,
  and system prompt without a redeploy.
