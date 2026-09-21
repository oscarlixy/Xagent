import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

async function assetFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map((entry) => {
      const target = path.join(directory, entry.name);
      return entry.isDirectory() ? assetFiles(target) : [target];
    }),
  );
  return nested.flat();
}

test("emitted client assets do not contain server credential sentinels", async () => {
  const credentials = [
    ["INTERNAL_API_TOKEN", process.env.INTERNAL_API_TOKEN],
    ["X_CLIENT_ID", process.env.X_CLIENT_ID],
    ["X_OAUTH_STATE_SECRET", process.env.X_OAUTH_STATE_SECRET],
  ];
  for (const [name, value] of credentials) {
    assert.ok(value, `${name} must be set to a leak sentinel for this test`);
  }

  const files = await assetFiles(path.join(process.cwd(), ".next", "static"));
  const leaks = [];
  for (const [name, value] of credentials) {
    for (const file of files) {
      if ((await readFile(file)).includes(Buffer.from(value))) {
        leaks.push(`${name}:${path.relative(process.cwd(), file)}`);
      }
    }
  }

  assert.deepEqual(leaks, [], `Server credentials leaked into: ${leaks.join(", ")}`);
});

test("digest rendering does not opt into unsafe HTML", async () => {
  const files = [
    path.join(process.cwd(), "src", "app", "digests", "[id]", "page.tsx"),
    path.join(process.cwd(), "src", "components", "DigestEventCard.tsx"),
  ];
  for (const file of files) {
    const source = await readFile(file, "utf8");
    assert.equal(source.includes("dangerouslySetInnerHTML"), false, file);
  }
});
