# Persist uv runtimes and tool caches

**Date:** 2026-11-01

**Status:** Proposed

## Goal

Keep uv-managed Python installations available to retained worktrees after ECS
task or EC2 host replacement, and give mise a writable persistent cache.

## Problem

The generated deployment preserves worktrees on EBS but leaves uv-managed
interpreters in the task's writable layer. Virtualenv interpreter links can
therefore outlive their targets. uv's cache is also disposable, and mise's
default cache location can encounter a root-owned parent in the runtime image.

## Design

Add two host bind mounts on the existing retained EBS volume:

| Host path | Container path / environment variable |
|---|---|
| `/data/uv-python` | `/home/harness/.local/share/uv/python` (`UV_PYTHON_INSTALL_DIR`) |
| `/data/uv-cache` | `/home/harness/.cache/uv` (`UV_CACHE_DIR`) |

Set `MISE_CACHE_DIR` to `/home/harness/.local/share/mise/cache`, using the
existing mise data mount. Prepare the uv directories with uid/gid 1000 ownership
after EBS mounting and before ECS registration. Keep generated toolchain data
outside the agent-owned `.hermes` directory and avoid broad mounts that hide
image-provided files. No image, IAM, capacity, or networking change is needed.

## Migration and limits

Existing hosts need explicit directory preparation: launch-template updates do
not rerun UserData on a running instance. Preserve required interpreters before
stopping the old task and restore them at their original container paths before
mounting the new directories. Recreate virtualenvs whose interpreters cannot be
preserved. The generated README covers staging, ownership, verification, and
rollback limitations. Cache growth consumes existing EBS capacity.

This does not preserve system Python, arbitrary external interpreter paths,
or worktrees outside retained storage. Image upgrades can still introduce
runtime compatibility changes independently of persistence.

## Validation and acceptance

The regression test parses a renamed generated template, resolves each runtime
path through its writable mount to retained storage, and checks bootstrap
ownership/registration ordering and shell syntax. Golden tests check generated
output, and CloudFormation lint validates the proposed resources.

A local generated prototype is reconciled with the proposed template. The
maintainer integration project and corkboard are unavailable in this checkout;
live integration-cycle acceptance remains pending. Before accepting, validate
both existing-host migration and host replacement in that project, including a
retained worktree's interpreter and gateway health, and record the cycle on the
corkboard. Offline checks do not establish live replacement behavior.
