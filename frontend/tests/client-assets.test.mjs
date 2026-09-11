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

test("emitted client assets do not contain the internal API token", async () => {
  const token = process.env.INTERNAL_API_TOKEN;
  assert.ok(token, "INTERNAL_API_TOKEN must be set to a leak sentinel for this test");

  const files = await assetFiles(path.join(process.cwd(), ".next", "static"));
  const leakedFiles = [];
  for (const file of files) {
    if ((await readFile(file)).includes(Buffer.from(token))) leakedFiles.push(path.relative(process.cwd(), file));
  }

  assert.deepEqual(leakedFiles, [], `Internal API token leaked into: ${leakedFiles.join(", ")}`);
});
