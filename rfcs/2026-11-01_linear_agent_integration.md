# Optional Linear agent integration

**Date:** 2026-11-01

**Status:** Proposed

## Goal

Ship a reusable Linear agent integration in generated projects, including its
deployment and lifecycle procedures, without changing the default Slack setup.

## Ownership and design

Dispatch owns deployment scaffolding, IAM policy scopes, mapped secrets,
persistent state, and setup/manage/teardown workflows. These are the integration
points needed by a Linear gateway adapter. Harness supplies the unchanged signed
runtime image; no container or Harness CLI change is required.

A signed Lambda receiver writes to SQS FIFO. A Hermes directory plugin polls
outbound, commits to a local SQLite inbox before acknowledging delivery, and
serializes work per session. Status-only requests bypass running work. Progress
messages carry findings; repeated liveness indicators are ephemeral. Restarted
in-flight work is reported as interrupted rather than automatically replayed.

## Generated project contract

- Plugin files stay in `integrations/linear/plugin/` until enabled. The base
  image default stays unchanged; enablement requires the documented compatible
  image upgrade.
- Names, secret paths, policies, and AWS regions follow generator substitutions.
- A separate stack attaches queue-consumer permissions to the main task role.
  Its receiver role can read only its signing parameter and send to its queue.
- Additional execution permissions are opt-in. Config merging preserves the
  existing enabled-plugin list and explicitly maps six gateway values.
- Teardown removes the side stack before its main-stack role dependency.

## Validation and acceptance

Offline tests cover protocol authentication, deduplication, interruption,
responsive status requests, and generated-project naming and region changes.
Golden tests reconcile generated content with the shipped template.

Live AWS/Linear acceptance remains pending. The maintainer integration project
and corkboard are unavailable in this checkout, so this proposal does not claim
completion of the live integration cycle. Before accepting, exercise the
generated setup/manage/teardown flow in the maintainer integration project,
record the cycle on the corkboard, and obtain maintainer confirmation.

## Implementation checklist

- [x] Ship optional plugin, receiver, renderer, policy, and config fragment.
- [x] Wire generated lifecycle skills and document removal order.
- [x] Add generated-project and offline protocol tests.
- [ ] Complete the maintainer live integration cycle and acceptance.
