import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { generate } from "../dist/generate.js";

test("optional Linear integration works in a renamed generated project", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "linear-generated-"));
  try {
    const targetDir = path.join(root, "sample");
    await generate({
      name: "sample",
      region: "us-west-2",
      targetDir,
      templateDir: path.resolve("template"),
    });
    const integration = path.join(targetDir, "integrations/linear");
    const template = JSON.parse(
      execFileSync("python3", [path.join(integration, "render_template.py")], { encoding: "utf8" }),
    );
    assert.equal(template.Parameters.ClawName.Default, "sample");
    assert.equal(
      template.Parameters.SigningSecretParameter.Default,
      "/sample/LINEAR_WEBHOOK_SIGNING_SECRET",
    );
    const config = await readFile(path.join(integration, "config.yaml"), "utf8");
    assert.match(config, /region: us-west-2/);
    assert.match(config, /LINEAR_CLIENT_SECRET: \/sample\/LINEAR_CLIENT_SECRET/);
    assert.doesNotMatch(config, /LINEAR_WEBHOOK_SIGNING_SECRET/);
    const defaultConfig = await readFile(path.join(targetDir, "agent_home/config.yaml"), "utf8");
    assert.doesNotMatch(defaultConfig, /linear-agent|LINEAR_/);
    const policy = JSON.parse(
      await readFile(path.join(targetDir, "sample-deploy-policy.json"), "utf8"),
    );
    assert.match(JSON.stringify(policy), /stack\/sample-linear\/\*/);
    execFileSync(
      "python3",
      ["-B", "-m", "unittest", "discover", "-s", path.join(integration, "tests")],
      { encoding: "utf8", stdio: "pipe" },
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
