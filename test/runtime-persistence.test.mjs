import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import yaml from "js-yaml";
import { generate } from "../dist/generate.js";

// Preserve CFN intrinsics so assertions can inspect the rendered resource tree.
const schema = yaml.DEFAULT_SCHEMA.extend([
  ...["Ref", "Sub", "GetAtt"].map(
    (tag) =>
      new yaml.Type(`!${tag}`, {
        kind: "scalar",
        construct: (value) => ({ [tag]: value }),
      }),
  ),
  ...["Equals", "If"].map(
    (tag) =>
      new yaml.Type(`!${tag}`, {
        kind: "sequence",
        construct: (value) => ({ [tag]: value }),
      }),
  ),
]);

test("generated runtime paths are writable mounts on retained storage", async () => {
  const dir = await mkdtemp(path.join(tmpdir(), "runtime-persistence-"));
  try {
    await generate({
      name: "sample",
      region: "us-west-2",
      targetDir: dir,
      templateDir: path.resolve("template"),
    });
    const text = await readFile(
      path.join(dir, ".agents/skills/setup-sample/template.yaml"),
      "utf8",
    );
    const { Resources: resources } = yaml.load(text, { schema });
    const task = resources.TaskDefinition.Properties;
    const container = task.ContainerDefinitions.find((c) => c.Name === "hermes");
    const env = Object.fromEntries(container.Environment.map((e) => [e.Name, e.Value]));
    const hosts = Object.fromEntries(task.Volumes.map((v) => [v.Name, v.Host.SourcePath]));
    const persistedPath = (containerPath) => {
      assert.equal(typeof containerPath, "string", "runtime env path must be explicit");
      const mount = container.MountPoints.filter(
        (m) => containerPath === m.ContainerPath || containerPath.startsWith(`${m.ContainerPath}/`),
      ).toSorted((a, b) => b.ContainerPath.length - a.ContainerPath.length)[0];
      assert.ok(mount, `${containerPath} must be mounted`);
      assert.equal(mount.ReadOnly, false);
      return hosts[mount.SourceVolume] + containerPath.slice(mount.ContainerPath.length);
    };
    assert.equal(persistedPath(env.UV_PYTHON_INSTALL_DIR), "/data/uv-python");
    assert.equal(persistedPath(env.UV_CACHE_DIR), "/data/uv-cache");
    assert.equal(persistedPath(env.MISE_CACHE_DIR), "/data/mise-data/cache");
    assert.equal(env.UV_PYTHON_INSTALL_DIR, "/home/harness/.local/share/uv/python");
    assert.equal(env.UV_CACHE_DIR, "/home/harness/.cache/uv");
    assert.equal(persistedPath(env.HERMES_HOME), "/data/hermes");
    assert.equal(resources.EbsDataVolume.DeletionPolicy, "Retain");
    assert.equal(resources.EbsDataVolume.UpdateReplacePolicy, "Retain");
    assert.ok(
      !container.MountPoints.some(
        (m) =>
          m.ContainerPath === "/home/harness" ||
          m.ContainerPath === "/home/harness/.local" ||
          m.ContainerPath === "/home/harness/.cache",
      ),
    );

    const boot = resources.LaunchTemplate.Properties.LaunchTemplateData.UserData["Fn::Base64"].Sub;
    execFileSync("bash", ["-n"], { input: boot });
    const mounted = boot.indexOf('mount "LABEL=$LABEL" "$MOUNT"');
    assert.ok(mounted >= 0, "retained volume must be mounted before directory preparation");
    const owned = boot.indexOf('chown -R 1000:1000 "$MOUNT"');
    const registered = boot.indexOf('echo "ECS_CLUSTER=');
    for (const folder of ["uv-python", "uv-cache"]) {
      const prepared = boot.indexOf(`"$MOUNT/${folder}"`);
      assert.ok(
        prepared > mounted && prepared < owned && owned < registered,
        `${folder} must be prepared and owned after mounting EBS and before ECS registration`,
      );
    }
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
