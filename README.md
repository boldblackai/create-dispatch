# @boldblackai/create-dispatch

Create a repository for your own dispatch agent, deployed to your own AWS account.

## What is a dispatch agent?

A dispatch agent is an opinionated deployment of [hermes-agent](https://hermes-agent.nousresearch.com/) configured as a
long-running ["claw"](https://www.cnet.com/tech/services-and-software/claw-ai-explainer-openclaw-nvidia/) within your Slack workspace.

Create, customize and deploy as many as you'd like. Each generated repository corresponds to one specific long-running agent and Slack application/user.

For example, you could generate a `@swe-pal` for a "Devin" type experience: code reviews, pull requests, etc. Or, a `@reporter` that posts reports at scheduled times to configured channels.

## How it works

1. Generate your dispatch agent repository

    npx @boldblackai/create-dispatch swe-pal

2. Follow the instructions in the README to create the IAM user and policy to get the `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` for `.env`. This also
walks you through creating and installing the Slack app into your workspace to get the `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN` you'll need later.

3. The generated repository is a set of skills, so open up `swe-pal` in your favorite harness ([Pi](https://boldblackai.github.io/harness/agents/pi/), [Hermes](https://boldblackai.github.io/harness/agents/hermes/), [OpenCode](https://boldblackai.github.io/harness/agents/opencode/))

4. Run the `/setup-dispatch` skill. This will prompt you for an inference provider, it supports [OpenRouter](https://openrouter.ai/), [ZAI](https://z.ai/subscribe), and [Anthropic](https://www.anthropic.com/) out of the box, but trivial
to use any that [hermes-agent already supports](https://hermes-agent.nousresearch.com/docs/integrations/providers/).

5. To manage it (update running image version, update skills/SOUL.md, etc) you can use the `/manage-dispatch` skill.

6. To uninstall it, run the `/teardown-dispatch` skill.

## What you get

- [hermes-agent](https://hermes-agent.nousresearch.com/docs) running on AWS ECS (EC2 launch type) — a single container instance in an Auto Scaling Group with a persistent EBS data volume — via our [hardened](https://harness.boldblack.ai/docs/security/) [harness](https://github.com/boldblackai/harness) Docker image.

- GitHub & Slack integration

- SQLite-backed persistent state on a retained gp3 EBS volume (local block storage — SQLite WAL is unsafe on NFS).

## Usage

```bash
npx @boldblackai/create-dispatch <name>
# or equivalently
npm init @boldblackai/dispatch <name>
```

If no name is given (and stdin is a TTY), you'll be prompted for one.

`<name>` must match `^[a-zA-Z]([a-zA-Z0-9-]*[a-zA-Z0-9])?$` and be 1–59 characters. It becomes
the CloudFormation stack name, IAM role prefix (`<name>-exec`, `<name>-task`,
`<name>-instance`), ECS cluster/service, log group, SSM namespace (`/<name>/`),
KMS alias (`alias/<name>-ssm`), and EBS volume tag (`<name>-data`). The
59-char ceiling keeps the `-exec`/`-task`/`-instance` role suffixes under
IAM's 64-char role-name limit. A name containing the literal region token
`us-east-1` is rejected (it would be corrupted by region substitution).

You can also pass the AWS region the claw will deploy into. It is substituted
into the generated claw (notably the deployer IAM policy's `kms:ViaService`,
which is a static JSON that can't use CloudFormation's `${AWS::Region}`):

```bash
npx @boldblackai/create-dispatch <name> --region us-west-2
```

`--region` must match `^[a-z]{2}(-gov)?-[a-z]+-[0-9]+$` (any AWS region,
including GovCloud/China) and defaults to `us-east-1`. If omitted and stdin
is a TTY you'll be prompted; otherwise the default is used silently.

### Options

- `--region <region>` — AWS region to bake into the claw (default `us-east-1`). Substituted into the deployer IAM policy's `kms:ViaService` so the claw works in that region.
- `--force` — generate into a non-empty target directory, merging with existing files (default: refuse).
- `--version`, `-V` — print the version.
- `--help`, `-h` — show this help.

## Development

```bash
pnpm install          # installs deps + builds dist/ (prepare)
pnpm build            # tsc
pnpm exec tsc --noEmit  # typecheck only
pnpm lint             # oxlint .
pnpm format:check     # oxfmt --check (CI gate)
pnpm test             # tsc && node --test (golden test)
```

### Publishing

Releases are automated via npm OIDC trusted publishing. Open a release PR
titled `release v<version>`; merging it triggers `tag-on-merge.yml` which
tags, publishes to npm (with provenance), and creates the GitHub release.

## License

MIT
