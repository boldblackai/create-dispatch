# Linear agent sessions

Generated projects include an optional integration in `integrations/linear/`.
It connects a running gateway to mentions, issue delegation, and follow-up
messages in one Linear workspace. Enable it using the generated integration
README after completing the base [setup](getting-started.md).

The Lambda webhook receiver verifies signatures, event freshness, and workspace
and app identity before writing to an encrypted SQS FIFO queue. The gateway
polls outbound, preserving its existing network configuration. A durable SQLite
inbox on retained EBS deduplicates deliveries and records progress. Explicit
Linear user IDs authorize requests and Stop signals.

The plugin sends meaningful interim findings and temporary liveness activities.
Standalone `/status` requests read recorded progress without waiting for a
running model call. Its prompt asks the agent to report verified existing fixes
immediately and reuse evidence; these instructions do not guarantee task latency.

## Enablement

The default Slack deployment does not enable Linear. The plugin targets Hermes
v0.22.2 in Harness `hermes-1.12.0`; use the generated manage skill to select and
verify that image before installation. The generated guide covers:

1. Creating a private Linear app and storing mapped SSM SecureStrings.
2. Adding scoped deployment permissions and deploying the separate receiver stack.
3. Overlaying plugin files and merging config while preserving other plugins.
4. Verifying replies, status, Stop, restart recovery, and authorization on a
   disposable issue.

No custom image is built. Issue-management tools and repository credentials are
configured independently. The integration does not add Linear MCP authentication.

## Operations

Monitor receiver errors, queue age, and dead-letter messages. Queue and inbox
contents include issue context; include them in your retention policy. The local
inbox has no automatic pruning. Interrupted runs require a new request so tools
with side effects are not silently replayed after a restart.

Disable webhooks and remove the Linear side stack before
[tearing down](teardown.md) the main gateway stack, because its consumer policy
references the main task role. The generated guide covers config removal,
secret rotation, and the exact deletion order.
