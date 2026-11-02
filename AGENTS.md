# AGENTS.md

## Rules

- Use a TDD workflow when writing code.
- Always redact AWS identifiers (account IDs, resource ARNs, instance IDs, etc.) in RFCs, PRs, PR comments, commit messages, and any other text checked into the repository. Use placeholders like `<account-id>`, `<resource-id>`, or a descriptive label instead.
- Never put links to the corkboard in code or commit messages (or any other text checked into the repository). The corkboard is a low-security, internal resource; reference it by name ("the corkboard") instead of embedding its URL.

## Documentation website

Built with [Zensical](https://zensical.org) from `docs/`. Config in
`zensical.toml`. Deploys to GitHub Pages via `.github/workflows/docs.yml`,
which also runs a build-only check on PRs that touch `docs/`, `zensical.toml`,
or the workflow itself (so doc build failures are caught before merge). The
published Pages tree is mirrored by the dispatch-site Amplify build and served
at `https://dispatch.boldblack.ai/docs/`. Build locally with
`uv tool install zensical==0.0.43 && zensical build --clean`. Zensical emits
relative links — keep it that way (no absolute-internal-link builds); the
mirror's relocatability depends on it.

## RFCs

Significant changes, architectural decisions, and new features should be proposed as RFCs in the `rfcs/` directory. RFCs use the format `rfcs/YYYY-MM-DD_short_title.md` with the following structure:

- `# Title` — short descriptive title
- `**Date:**` — proposal date (ISO format)
- `**Status:**` — `Proposed`, `Accepted`, `Implemented`, or `Rejected`
- `## Goal` — what the RFC aims to accomplish
- Remaining sections are free-form but typically include motivation, technical details, migration notes, and an implementation checklist
- When moving an RFC to `Implemented`, update `AGENTS.md` and `README.md` to reflect any new infrastructure, commands, or workflows introduced by the RFC. Also, replace the implementation checklist with implementation notes.

## Overview

This repo is `@boldblackai/create-dispatch`, an `npx`-distributed CLI that
generates a renamed skeleton of a Hermes Agent claw repo. Running
`npx @boldblackai/create-dispatch foo` produces a `foo/` directory whose contents
match the bundled `template/` snapshot except every lowercase `dispatch` reference
— file contents and file/directory names, including the SSM namespace, IAM
scopes, and KMS alias — is renamed to `foo`. A second literal token, `us-east-1`,
is substituted with the chosen AWS region (`--region`, default `us-east-1`) so
region-bearing static files — notably the deployer IAM policy's `kms:ViaService`,
which cannot use CloudFormation's `${AWS::Region}` — match the deploy region.
See `rfcs/2026-06-27_bclaw-cli-scaffolder.md` for the full design and
`rfcs/2026-07-15_region-substitution-token.md` for the region token.

- `template/` is the **source of truth** (a Hermes Agent claw repo snapshot)
  shipped inside the npm package; edit it directly.
- `src/` is the generator (`cli.ts` + `generate.ts`); `test/golden.test.mjs` is
  the correctness proof.

## Skills

Skills ship inside `template/.agents/skills/` and are consumed by the generated
claw, so their content must be **factual and present-tense** — instructional
steps and current-state facts only.

- **No historical or changelog narrative.** Do not write "previously gated
  on…", "was migrated from X to Y", "future template improvement: …", "the old
  behavior", or similar past/future-state framing in shipped skill files (the
  `SKILL.md`, plus shipped supporting files like `template.yaml` and
  `scripts/`).
- **No war-story framing.** Keep findings as stated facts, not discovery
  narrative ("a real deploy hit…", "near-miss from…", "bit a real deploy",
  "we discovered…").
- **Discovery narratives, "why" background, migration histories, and
  lessons-learned belong in the repo-level `references/` directory** (not
  inside `template/`). Those docs are for authors of this repo and do not ship
  in the generated claw. Never cite `references/` from shipped skill content —
  each skill must be self-contained **relative to what ships**: a skill may
  reference a sibling skill under `template/.agents/skills/` (the setup /
  manage / teardown skills always ship together in the generated claw, so
  cross-references between them are stable and preferred over duplicating
  shared procedure), but it must never depend on author-only docs, scripts, or
  state that won't be present in the generated repo.

## Layout: Generator Repo + Integration Repo

Since it doesn't make sense to deploy changes made in /workspace (its just templates + generator), we use an integration
repository instead (`/alt/integration`), which has AWS creds and represents a live, deployed dispatch agent we can make changes to.

- `/workspace` (no aws access): the `create-dispatch` project, it creates project skeletons from `template/`
- `/alt/integration` (aws access via mise): a project created from `create-dispatch`; we edit and iterate on THIS repo, and
integrate ("port back") changes back into `/workspace/template/` once we verify they work.

### Workflow conventions

- **RFCs live in `/workspace/rfcs/`** (the generator project), never in the
  iteration repo. They describe changes to the template that ships in the npm
  package.
- **Prototype edits happen in `/alt/integration`** so they can be confirmed
  live against a real working test project before being ported back.

### Integration cycles

Working through a change to dispatch templates (skills, policies, CFN, etc), such as implementing a proposed RFC, goes through what
is known as an "Integration Cycle". We always start a cycle by creating an integration journal and applying/testing our changes into `/alt/integration`.

Once I (and only I) confirm the changes work in the `/alt/integration` project (this requires a deploy or a possible regeneration), we can integrate our changes back into
the `create-dispatch` templates under `/workspace` and run the golden test. We can use the integration cycle journal to help us integrate our changes.

#### Port-back: diff `/alt/integration` against `template/`

The golden test only verifies that `/workspace/template/` reproduces byte-for-byte
into a freshly-generated cluster — it does **not** verify that every edit made
in `/alt/integration` was actually carried back. A file changed live but missed
in the port-back is silently invisible to the golden test, because the check is
`template/`↔generated, not `template/`↔`/alt/integration`.

Before finishing a port-back, run a recursive diff of the two trees and
**reconcile every deviation**:

```bash
diff -rq /workspace/template /alt/integration --exclude=.git --exclude=.agents/skills --exclude=node_modules --exclude=dist
```

- Any other deviation is a **missed port-back item**. Either fix it (carry the
  edit into `template/`) or **flag it to the user** with the specific file +
  the nature of the divergence before considering the port-back complete.

`/workspace/AGENTS.md` itself, `/workspace/rfcs/`, and `/workspace/references/`
are generator-repo-only (no `template/` equivalent) — those are not divergences.
The `/workspace/` root files (`package.json`, `src/`, etc.) likewise. The check
is scoped to what a generated cluster inherits from `template/`.

#### Integration cycle journal Format

To aid in porting back changes, keep a journal of issues we encountered during an integration cycle. The journal lives in the `journal:create-dispatch:` namespace on the **corkboard** (DokuWiki), as a page like `journal:create-dispatch:YYYY-MM-DD_short_title`, authored in DokuWiki syntax — Markdown renders as literal text, so use wiki markup (see the corkboard skill) — in journal-style append-only format:

- `====== Title ======` — short descriptive title (page H1)
- `**Date:**` — date (ISO format)
- `**Status:**` — `In Progress`, `Done`
- `===== Entries =====` — list of entries (H2)

ONLY add issues, do not talk about plans or implementation details (the rfc is for that, just link to it).

Once the port-back is complete and the golden test passes, **set the journal's status to `Done` and leave it in place** — it now lives permanently in the `journal:create-dispatch:` namespace as a record of the cycle, so do not delete it.

## Tool Versions

This project uses `mise.toml` as the single source of truth for all tool and
language versions. Do not install tools globally or via ad-hoc commands — use
mise instead.

- Activate mise before running commands: `eval "$(mise activate bash)"`
- Install all declared tools: `mise install` (then `mise trust` on a fresh clone)
- Run one-off commands with correct versions: `mise exec -- <command>`

`mise.toml` pins every tool (`node`, `uv`, `github:rhysd/actionlint`, plus
`oxlint` + `oxfmt` via the `oxc-project/oxc` github backend — see Linting).
pnpm is **not** a mise tool — it is managed by corepack via the `packageManager`
field (see Package Manager). If you change a tool version, update `mise.toml`
and run `mise install` to apply.

## Package Manager

This project uses **pnpm**, declared via the `packageManager` field in
`package.json`. Do not use npm or yarn.

- Install dependencies: `pnpm install`
- Add a dependency: `pnpm add <pkg>` (updates `pnpm-lock.yaml`)
- Add a dev dependency: `pnpm add -D <pkg>`
- Run a script: `pnpm <script>` or `pnpm run <script>`
- In CI or to enforce the lockfile: `pnpm install --frozen-lockfile`

The pnpm version is pinned in `package.json` (`"packageManager": "pnpm@11.9.0"`)
and read automatically by corepack. `pnpm-lock.yaml` is committed and is the
source of truth for the dependency tree; `.pnpm-store/` is a cache and is
gitignored.

## Linting and Formatting

This project uses **oxlint** for linting and **oxfmt** for formatting
TypeScript/JavaScript. Both are **mise tools** (declared in `mise.toml`), not
npm devDependencies. Do not introduce eslint, prettier, biome, or
editor-specific config.

- Lint: `pnpm lint` (runs `oxlint .`) — the CI gate.
- Auto-fix lint issues: `pnpm lint:fix` (runs `oxlint --fix .`).
- Format: `pnpm format` (runs `oxfmt --write .`); `pnpm format:check`
  (runs `oxfmt --check .`) is the read-only CI gate.

oxlint config lives in `.oxlintrc.json`; oxfmt config lives in `.oxfmtrc.jsonc`.
Both tools ship as per-platform archives inside the single `oxc-project/oxc`
GitHub release (tag `apps_v<N>`, which bundles matching oxlint + oxfmt versions —
e.g. `apps_v1.74.0` carries oxlint 1.74.0 and oxfmt 0.59.0). A distinct
`[tool_alias]` per binary is required so each gets its own install directory;
`asset_pattern` per platform (under `[tools.<alias>.platforms]`) selects the
right archive since `matching` is broken in the installed mise rev. When you
bump them, change the single `version = "apps_v<N>"` line for both aliases in
`mise.toml` (keep them on the same release) and run `mise install`. Both
configs ignore `dist/` and `template/` so the compiled output and bundled
snapshot are never linted/formatted; oxfmt additionally ignores the
hand-curated tool-config dotfiles (`.oxfmtrc.jsonc`, `.oxlintrc.json`) and
disables `experimentalSortPackageJson` to preserve `package.json`'s deliberate
field order.

Before finishing any code change, run `pnpm lint`.

## TypeScript

This project is written in TypeScript and compiled with `tsc` (no babel/swc).
Configuration lives in `tsconfig.json`; `strict` mode is on — do not disable it
or weaken individual strict flags.

- Build (compile + emit declarations): `pnpm build`
- Typecheck only, no emit: `pnpm exec tsc --noEmit`
- Source lives in `src/` and compiles to `dist/`; do not add TypeScript outside
  `src/`. `template/` lives outside `src/` on purpose so it is never compiled.

## GitHub Actions

All third-party action references in `.github/workflows/` must be pinned to their
full 40-character commit SHA with a version tag in a trailing comment:

```yaml
uses: owner/repo@<sha> # <tag>
```

Never use tag-only references (e.g. `actions/checkout@v5`). When adding or
updating an action, resolve the tag to a SHA using
`gh api repos/OWNER/REPO/commits/TAG --jq '.sha'` or
`git ls-remote https://github.com/OWNER/REPO.git refs/tags/TAG`. Local composite
actions (`./.github/actions/*`) are exempt.

## GitHub Actions Linting

This project lints `.github/workflows/*.yml` with **actionlint**. The tool is
installed via mise (`"github:rhysd/actionlint" = "v1.7.12"` in `mise.toml`), not
via pnpm — activate mise before running it. actionlint also runs shellcheck on
every `run:` block.

Configuration lives in `.actionlint.yaml`. New workflow files are discovered
automatically — no file arguments needed. CI gets actionlint from `mise.toml`
via the `jdx/mise-action` step (alongside oxlint/oxfmt), so there is no longer a
separate in-workflow version to keep in sync — `mise.toml` is the single source.
When the version changes, update `mise.toml` first and run `mise install`.

## Markdown Linting

This project lints Markdown with **markdownlint-cli2**. Configuration lives in
`.markdownlint-cli2.jsonc`.

- Lint Markdown only: `pnpm lint:md`
- Lint everything (Markdown + JS/TS + actions): `pnpm lint`

The config enables all default rules (`default: true`) and configures/disables
specific ones — each with an inline comment explaining why. Do not disable a
rule to silence a violation; either fix the Markdown or, if the disable is
genuinely justified, add it with a comment naming the concrete reason.
`template/` and `dist/` are in the `ignores` array (they are bundled/compiled
output, not hand-authored) — add generated paths there rather than letting them
fail lint.

## Runtime storage

The generated ECS template has four agent-state mounts plus two narrow EBS
mounts for uv-managed Python and its cache. `MISE_CACHE_DIR` lives inside the
existing mise data mount. Keep environment paths, task volumes, bootstrap
ownership, and the generated README migration procedure consistent.
`test/runtime-persistence.test.mjs` parses a renamed generated template and
checks this storage contract and bootstrap ordering; `pnpm test` includes it.
