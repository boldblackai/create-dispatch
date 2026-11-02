---
name: teardown-dispatch
description: >
  Tears down a Hermes Agent dispatch on ECS (EC2 launch type) and all associated
  AWS resources. Follows a reverse-order sequence: scale to 0 → delete
  CloudFormation stack (VPC, EBS volume [retained], ASG, ECS, IAM) → delete
  retained EBS → delete orphaned VPC → delete SSM secrets → delete the
  CloudFormation service role (`dispatch-cfn-exec`). Use when asked to destroy,
  teardown, or decommission the dispatch. Companion to setup-dispatch.
---

# Teardown Harness ECS on EC2

Tears down a Hermes Agent claw on ECS (EC2 launch type). Follows the reverse
order of setup so dependencies delete cleanly without orphans. The
CloudFormation stack owns the VPC, the ASG + container instance, the ECS
service/task/cluster, the stack's IAM roles (exec, task, container instance +
instance profile), the log group, and the standalone EBS data volume. The EBS
volume is retained by the stack's `DeletionPolicy: Retain` and must be deleted
explicitly. SSM secrets are not stack-owned and are deleted separately. The
CloudFormation service role (`dispatch-cfn-exec`) is also not stack-owned — it is
created out-of-band in setup Phase 0 (the stack cannot create the role it
assumes to create itself), so it survives `delete-stack` and is deleted here as
the final step, after the stack is gone.

## Optional Linear stack dependency

Before deleting the main CloudFormation stack, check whether
`dispatch-linear` exists. If present, follow the
[integration removal procedure](../../../integrations/linear/README.md#operations-and-removal):
disable webhook delivery, stop the consumer, preserve required queue data,
then delete the Linear stack and wait for completion. Its consumer IAM policy
references the main task role, and the receiver uses the main KMS key. Remove
the supplemental execution-role policy only after successful side-stack
deletion. Treat access or lookup errors as errors, not as proof the stack is
absent. Continue main-stack teardown only after this dependency is removed.


## Prerequisites

Before starting, ensure the shell has `mise` and AWS credentials loaded
(same as setup):

```bash
eval "$(/usr/local/bin/mise activate bash)" \
  && mise trust /workspace \
  && cd /workspace
```

All `aws` commands in this skill assume this shell state.

**Confirm intent.** Use `ask_user_question` to confirm the user wants to
destroy the claw. This is destructive and irreversible — all EBS data
(sessions, memories, skills, the SQLite databases, gh credentials)
will be lost unless the user backs it up first.

Also collect the **claw name** (default `dispatch`) and **region** (default
`us-east-1`) via `ask_user_question`. Store as:

```bash
CLAW_NAME=<user-provided>
AWS_REGION=<user-provided>
```

## Teardown Sequence

Follow these phases **in order**. Each phase has a gate that must be satisfied
before proceeding.

---

### Phase 0: Pre-flight — confirm AWS access

**Gate: `aws sts get-caller-identity` succeeds.**

Confirm the shell's AWS credentials are live and identify the principal that
will run the deletes:

```bash
aws sts get-caller-identity --query 'Arn' --output text
```

Teardown is self-correcting: a failed delete leaves the resource behind and
Phase 7's verification catches any leftovers, so real permission gaps surface
as the phases run rather than up front.

---

### Phase 1: Scale the service to 0

**Gate: user confirmed intent.**

Stop the running task before touching the stack, so the gateway shuts down
cleanly (flushes session state) instead of being killed mid-write.

```bash
aws ecs update-service \
  --cluster "$CLAW_NAME" \
  --service "$CLAW_NAME" \
  --desired-count 0 \
  --region "$AWS_REGION"
```

Wait for the service to reach 0 running tasks:

```bash
# services-inactive waits for status INACTIVE (only set by DeleteService);
# scale-to-0 only drains runningCount. Poll that directly instead.
for i in $(seq 1 30); do
  RUNNING=$(aws ecs describe-services \
    --cluster "$CLAW_NAME" \
    --services "$CLAW_NAME" \
    --region "$AWS_REGION" \
    --query 'services[0].runningCount' --output text)
  echo "[$((i*10))s] runningCount=$RUNNING"
  [ "$RUNNING" = "0" ] && break
  sleep 10
done
[ "$RUNNING" = "0" ] || echo "still draining — fall through to stop-task"
```

If the wait times out (a task stuck in `STOPPING`), force it:

```bash
aws ecs stop-task \
  --cluster "$CLAW_NAME" \
  --task "$(aws ecs list-tasks --cluster "$CLAW_NAME" --region "$AWS_REGION" \
    --query 'taskArns[0]' --output text)" \
  --region "$AWS_REGION"
```

---

### Phase 2: Delete the CloudFormation stack

**Gate: service is at 0 running tasks.**

Delete the stack. This removes the service, task definition family's inactive
revisions, cluster, IAM roles (exec + task + container instance), instance
profile, log group, launch template, Auto Scaling Group (terminating the
container instance), security group, subnet, route table, internet gateway, and
VPC. The standalone EBS volume is left behind (subject to the retain policy —
see Phase 3).

Delete the stack, passing `--role-arn dispatch-cfn-exec` so CloudFormation assumes
the service role (the same role the deploys use) to perform the deletions. The
service role is itself deleted in Phase 6, so it must be passed here while it
still exists and is still needed:

```bash
aws cloudformation delete-stack \
  --stack-name "$CLAW_NAME" \
  --role-arn arn:aws:iam::$(aws sts get-caller-identity \
    --query 'Account' --output text):role/${CLAW_NAME}-cfn-exec \
  --region "$AWS_REGION"
```

Wait for `DELETE_COMPLETE`:

```bash
aws cloudformation wait stack-delete-complete \
  --stack-name "$CLAW_NAME" \
  --region "$AWS_REGION"
```

**Common delete failures and how to force-clean them:**

- **`DELETE_FAILED`: CloudFormation can't confirm deletion of a resource.**
  The stack delete can fail not because a resource still exists, but because
  CloudFormation's handler can't confirm a resource that's already going away.
  Two manifestations on this stack:
  - **ASG / instance still terminating.** Deleting the Auto Scaling Group
    begins terminating the container instance; the handler can time out before
    EC2 confirms termination. Verify directly with
    `aws autoscaling describe-auto-scaling-groups` (should show no instances)
    and `aws ec2 describe-instances` (instance `shutting-down`/`terminated`).
    If they're empty/gone but the stack is stuck on handler confirmation,
    re-run `delete-stack --deletion-mode FORCE_DELETE_STACK`.
  - **IAM roles (`NoSuchEntity`).** The exec/task/container-instance roles
    (`dispatch-*`) can be deleted out from under the handler (e.g. a prior partial
    teardown), so the handler 404s confirming a resource that no longer exists.
    Verify with `aws iam get-role` for each `dispatch-*` role (returns
    `NoSuchEntity` if gone).

  In both cases the resources are confirmed gone via direct CLI, but the stack
  is stuck on handler confirmation. Fix: re-run `delete-stack` with
  `--deletion-mode FORCE_DELETE_STACK` (requires `cloudformation:DeleteStack`
  with the force capability). This skips resources that fail and continues
  deleting the rest.

- **EBS volume retained.** `EbsDataVolume` has `DeletionPolicy: Retain` to
  protect data, so the stack delete leaves it behind. Phase 3 deletes it
  explicitly after the user confirms. Capture its ID first:

  ```bash
  EBS_ID=$(aws ec2 describe-volumes \
    --region "$AWS_REGION" \
    --filters "Name=tag:Name,Values=${CLAW_NAME}-data" \
    --query 'Volumes[0].VolumeId' --output text)
  echo "EBS to delete (Phase 3): $EBS_ID"
  ```

  > The container instance attaches the data volume at runtime (via the
  > UserData, not a CloudFormation `VolumeAttachment`), and volumes attached via
  > the EC2 API default to `DeleteOnTermination=false`. So when the ASG
  > terminates the instance during stack delete, the volume is **detached but
  > kept** — it becomes `available` and is left for Phase 3. If you see it still
  > `in-use` while the instance is mid-termination, Phase 3 waits for it to
  > detach (or force-detaches).

---

### Phase 3: Delete the retained EBS volume

**Gate: stack is `DELETE_COMPLETE`; user confirms data loss is OK.**

The EBS volume was retained by the stack to protect data. Now delete it. First
confirm it is no longer attached (the instance termination during stack delete
should have detached it; if not, force-detach):

```bash
STATE=$(aws ec2 describe-volumes --region "$AWS_REGION" \
  --volume-ids "$EBS_ID" --query 'Volumes[0].State' --output text)
echo "volume state: $STATE"
```

If `in-use`, the instance may still be terminating — wait for it, or
force-detach (the deployer policy scopes `ec2:DetachVolume` by
`aws:ResourceTag/Name: dispatch-data`, which this volume carries):

```bash
aws ec2 detach-volume --volume-id "$EBS_ID" --region "$AWS_REGION" --force || true
aws ec2 wait volume-available --volume-id "$EBS_ID" --region "$AWS_REGION"
```

Then delete the volume:

```bash
aws ec2 delete-volume \
  --volume-id "$EBS_ID" \
  --region "$AWS_REGION"
```

> **Skip this phase** if the user wants to keep the EBS data (e.g. for a future
> redeploy). The volume survives indefinitely; note its ID so a future setup
> can find + reattach it by tag.

> **EBS delete permissions are tag-conditioned, not absent.** The deployer
> policy scopes `ec2:DeleteVolume` via `aws:ResourceTag/Name: dispatch-data`, so
> volumes tagged `Name=dispatch-data` (all of them, regardless of which stack
> version created them) are within scope. Try the `delete-volume` command
> directly. If it fails with `AccessDeniedException`, note the volume IDs in
> the final report for manual console cleanup. To delete multiple retained
> orphan volumes (from several stack updates), loop over all IDs returned by
> the `describe-volumes` tag query in Phase 2.

---

### Phase 4: Delete the orphaned VPC and networking

**Gate: stack is `DELETE_COMPLETE` (Phase 2); EBS handled (Phase 3, done or skipped).**

The VPC, subnets, route table, internet gateway, and security group are
stack-owned, so a clean stack delete removes them. They survive only when the
stack delete skipped them — the `FORCE_DELETE_STACK` recovery path skips any
resource whose delete fails (a timed-out handler, or a dependency still
detaching). Once Phase 2 (stack) and Phase 3 (EBS volume) are handled, the only
remaining orphans are the VPC and its networking.

Find the VPC by its `Name` tag:

```bash
VPC_ID=$(aws ec2 describe-vpcs \
  --region "$AWS_REGION" \
  --filters "Name=tag:Name,Values=${CLAW_NAME}-vpc" \
  --query 'Vpcs[].VpcId' --output text)

if [ -z "$VPC_ID" ]; then
  echo "vpc: gone (stack delete cleaned it up)"
else
  echo "vpc: ORPHANED ($VPC_ID) — removing"
fi
```

If `VPC_ID` is empty, the stack delete handled the VPC — skip the rest of this
phase. Otherwise, delete the VPC's dependencies in dependency order, then the
VPC. Each delete is tag-scoped to the claw's resources and tolerates resources
that are already gone:

```bash
# Subnets — deleting a subnet clears its route-table associations
for s in $(aws ec2 describe-subnets --region "$AWS_REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" \
    --query 'Subnets[].SubnetId' --output text); do
  aws ec2 delete-subnet --subnet-id "$s" --region "$AWS_REGION" \
    && echo "subnet deleted: $s" || echo "subnet left: $s"
done

# Custom route table (the VPC's main route table is removed with the VPC)
for rt in $(aws ec2 describe-route-tables --region "$AWS_REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" \
    --query 'RouteTables[?Associations[0].Main!=`true`].RouteTableId' --output text); do
  aws ec2 delete-route-table --route-table-id "$rt" --region "$AWS_REGION" \
    && echo "route table deleted: $rt" || echo "route table left: $rt"
done

# Internet gateway — detach, then delete
for igw in $(aws ec2 describe-internet-gateways --region "$AWS_REGION" \
    --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
    --query 'InternetGateways[].InternetGatewayId' --output text); do
  aws ec2 detach-internet-gateway --internet-gateway-id "$igw" \
    --vpc-id "$VPC_ID" --region "$AWS_REGION" || true
  aws ec2 delete-internet-gateway --internet-gateway-id "$igw" \
    --region "$AWS_REGION" \
    && echo "internet gateway deleted: $igw" || echo "internet gateway left: $igw"
done

# Security group (skip the VPC default — it is removed with the VPC)
for sg in $(aws ec2 describe-security-groups --region "$AWS_REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" \
    --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text); do
  aws ec2 delete-security-group --group-id "$sg" --region "$AWS_REGION" \
    && echo "security group deleted: $sg" || echo "security group left: $sg"
done

# The VPC
aws ec2 delete-vpc --vpc-id "$VPC_ID" --region "$AWS_REGION" \
  && echo "vpc deleted: $VPC_ID" || echo "vpc STILL EXISTS: $VPC_ID"
```

> **A lingering network interface blocks the chain.** `delete-subnet` fails
> with a dependency error if any ENI remains in it. With host networking there
> is no task ENI (the task shares the instance's ENI), and the ASG terminates
> the container instance during stack delete — so a stuck ENI is rare and
> transient (a still-terminating instance). Wait and re-run the sweep. The
> deployer policy does not grant `ec2:DeleteNetworkInterface`; if an ENI truly
> won't clear, delete it from the console.

> **The deployer can delete only claw-tagged networking.** Every resource
> deleted above carries a `Name=dispatch*` tag, which is what the policy's
> `EC2NetworkingManage` statement (`aws:ResourceTag/Name: dispatch*`) keys on. The
> read-only `Describe*` calls are unscoped, so finding the VPC always works;
> the deletes succeed only against the claw's own resources.

---

### Phase 5: Delete the SSM secrets

**Gate: stack deleted (Phase 2); EBS handled (Phase 3, skipped or done).**

The SSM parameters are not stack-owned, so they survive the stack delete. Delete
them now. `ssm delete-parameter` is immediate and irreversible (no recovery
window — that only applies to Secrets Manager).

> **The deployer policy grants the deletes directly.** Its `SSMSecrets`
> statement allows `ssm:DeleteParameter` on
> `arn:aws:ssm:*:*:parameter/dispatch/*`, so the deletes below succeed without a
> console fallback. Only fall back to console cleanup if a delete fails with
> `AccessDeniedException` for a param outside that ARN scope.

```bash
# Delete ALL params under /dispatch/ — covers whichever provider key
# (OPENROUTER_API_KEY | ANTHROPIC_API_KEY | ZAI_API_KEY) this deploy used.
# The namespace is one-claw-per-account, so this is the full secret set.
aws ssm describe-parameters \
  --parameter-filters "Key=Name,Option=BeginsWith,Values=/dispatch/" \
  --region "$AWS_REGION" \
  --query 'Parameters[].Name' --output text | tr '\t' '\n' | while read -r p; do
  [ -n "$p" ] || continue
  if aws ssm delete-parameter --name "$p" --region "$AWS_REGION"; then
    echo "deleted: $p"
  else
    echo "(already gone: $p)"
  fi
done
```

Verify the namespace is empty:

```bash
aws ssm describe-parameters \
  --parameter-filters "Key=Name,Option=BeginsWith,Values=/dispatch/" \
  --region "$AWS_REGION" \
  --query 'Parameters[].Name' --output table
```

Expected: an empty list.

---

### Phase 6: Delete the CloudFormation service role

**Gate: stack is `DELETE_COMPLETE` (Phase 2).**

`dispatch-cfn-exec` is the role CloudFormation assumed during every deploy and the
Phase 2 stack delete. It is not stack-owned, so `delete-stack` leaves it behind
— it must be removed explicitly, and **last** (it was needed during the stack
delete, and the deployer's only IAM-create powers now target this one literal
role). The deployer policy's `ManageCfnExecRole` statement grants the deletes.

**IAM refuses to delete a role that still has an inline policy attached**, so
delete the inline execution policy *first*, then the role:

```bash
aws iam delete-role-policy \
  --role-name "${CLAW_NAME}-cfn-exec" \
  --policy-name "${CLAW_NAME}-cfn-exec"
aws iam delete-role \
  --role-name "${CLAW_NAME}-cfn-exec"
```

If `delete-role` fails with `DeleteConflict: Cannot delete entity, must delete
policies first`, the inline policy was still attached — run the
`delete-role-policy` line again, then retry `delete-role`. If the role is
already gone, both calls return `NoSuchEntity`, which is fine.

---

### Phase 7: Final verification and report

Confirm nothing is left under the claw's name:

```bash
# No remaining stacks
aws cloudformation describe-stacks \
  --stack-name "$CLAW_NAME" \
  --region "$AWS_REGION" 2>&1 | grep -q "does not exist" \
  && echo "stack: gone" || echo "stack: STILL EXISTS"

# No remaining EBS volumes
aws ec2 describe-volumes --region "$AWS_REGION" \
  --filters "Name=tag:Name,Values=${CLAW_NAME}-data" \
  --query 'Volumes[].VolumeId' --output text | grep -q . \
  && echo "ebs: STILL EXISTS" || echo "ebs: gone"

# No remaining SSM params
aws ssm describe-parameters \
  --parameter-filters "Key=Name,Option=BeginsWith,Values=/dispatch/" \
  --region "$AWS_REGION" --query 'Parameters[].Name' --output text | grep -q . \
  && echo "ssm: STILL EXISTS" || echo "ssm: gone"

# No remaining CloudFormation service role
aws iam get-role --role-name "${CLAW_NAME}-cfn-exec" 2>&1 | grep -q "NoSuchEntity" \
  && echo "cfn-exec role: gone" || echo "cfn-exec role: STILL EXISTS"
```

Report to the user:
- Stack status (gone)
- EBS volume status (gone, or retained ID if they chose to keep it)
- SSM parameters status (gone)
- CloudFormation service role status (gone)
- Reminder that the Slack app config (bot/app tokens, allowed users, home
  channel) still exists on the Slack side and can be reused for a future deploy

---

## Notes

- **EBS `DeletionPolicy: Retain` is deliberate.** The setup template retains
  the volume on stack delete to protect sessions/memories/skills/the SQLite
  databases from accidental loss. This teardown skill deletes it explicitly in
  Phase 3 only after the user confirms. If you want stack-delete to also nuke
  the volume, change `DeletionPolicy` to `Delete` in `template.yaml` — but the
  retain default is safer.

- **The volume is attached by UserData, not by CloudFormation.** Because the
  container instance is ASG-managed (not a CFN `AWS::EC2::Instance`), the data
  volume is a standalone retained resource the instance attaches at runtime via
  its UserData. CFN therefore has no `VolumeAttachment` to delete — on stack
  delete the ASG terminates the instance (which auto-detaches the
  `DeleteOnTermination=false` volume), and Phase 3 deletes the now-orphaned
  volume directly.

- **Task definition revisions are not deleted by `delete-stack`.** ECS keeps
  inactive task definition revisions. `delete-stack` removes the service but
  the task definition family's old revisions linger (harmless, no cost). They
  can be cleaned with `aws ecs deregister-task-definition` per revision, but
  it's not necessary for a clean teardown.

- **SSM has no recovery for `delete-parameter`.** Unlike Secrets Manager
  (7-day recovery window by default), `ssm delete-parameter` is immediate and
  irreversible. If the user might redeploy, have them record the values before
  Phase 5 — they'll need to re-write them during setup.

- **The SSM namespace is hardcoded (`/dispatch/`), not derived from `ClawName`.** Secrets live at
  `/dispatch/<KEY>` so the deployer's IAM policy can be scoped to
  `parameter/dispatch/*` (see the setup skill's Phase 0). With one claw per
  account, Phase 5 deleting `/dispatch/*` removes the account's entire secret
  set — correct for a full teardown.

- **Order matters.** Always scale to 0 (Phase 1) before deleting the stack
  (Phase 2). Deleting the stack while the task is running leaves the container
  mid-write against the EBS-backed bind-mounts; while the volume is retained
  either way, a clean task stop flushes SQLite WAL before the volume is
  detached.
