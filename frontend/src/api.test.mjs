import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { build, transform } from "esbuild";
const source = await readFile(new URL("./api.ts", import.meta.url), "utf8");
const bundle = await build({stdin:{contents:source,loader:"ts",resolveDir:process.cwd()+"/src"},
  bundle:true,write:false,format:"esm",platform:"browser"});
const code = bundle.outputFiles[0].text;
const client = await import(
  `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`
);
async function loadTypescriptModule(filename) {
  const input = await readFile(new URL(filename, import.meta.url), "utf8");
  const compiled = await transform(input, { loader: "ts", format: "esm" });
  return import(
    "data:text/javascript;base64," +
      Buffer.from(compiled.code).toString("base64")
  );
}
const queue = await loadTypescriptModule("./uploadQueue.ts");
const templates = await loadTypescriptModule("./templateContent.ts");
const { resourceDisplayState } = await loadTypescriptModule("./resourcePresentation.ts");
test("latest draft or review status cannot inherit an older version's published badge", () => {
  const resource = { active_version_id: "v1", suspended: false, deleted_at: null };
  assert.equal(resourceDisplayState(resource, { id: "v2", state: "IN_REVIEW" }), "IN_REVIEW");
  assert.equal(resourceDisplayState(resource, { id: "v2", state: "DRAFT" }), "DRAFT");
  assert.equal(resourceDisplayState(resource, { id: "v1", state: "APPROVED" }), "PUBLISHED");
  assert.equal(resourceDisplayState({ ...resource, suspended: true }, { id: "v2", state: "DRAFT" }), "SUSPENDED");
});
const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;
afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.window = originalWindow;
  client.clearSession();
});
const json = (value, status = 200, headers = {}) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
test("cookie, CSRF, revision and idempotency are applied centrally", async () => {
  client.setSession({ csrf_token: "test-only-token" });
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/v1/resources/example");
    assert.equal(options.credentials, "include");
    assert.equal(options.headers.get("X-CSRF-Token"), "test-only-token");
    assert.equal(options.headers.get("If-Match"), '"7"');
    assert.match(options.headers.get("Idempotency-Key"), /^[a-z0-9-]{36}$/);
    assert.equal(options.body, '{"name":"updated"}');
    return json({ revision: 8 });
  };
  assert.deepEqual(
    await client.patch("/resources/example", { name: "updated" }, 7),
    { revision: 8 },
  );
});
test("explicit retry after network ambiguity reuses key; successful repeat gets a fresh key", async () => {
  const keys = [];
  let count = 0;
  globalThis.fetch = async (_, options) => {
    keys.push(options.headers.get("Idempotency-Key"));
    if (count++ === 0) throw new TypeError("network lost");
    return json({ id: "created" });
  };
  await assert.rejects(
    client.post("/resources", { name: "x" }),
    (error) => error.code === "NETWORK_ERROR",
  );
  await client.post("/resources", { name: "x" });
  await client.post("/resources", { name: "x" });
  assert.equal(keys[0], keys[1]);
  assert.notEqual(keys[1], keys[2]);
});
test("ETag from permissions GET is reused and conflict is surfaced without retry", async () => {
  let writes = 0;
  globalThis.fetch = async (_, options) => {
    if (options.method === "GET") return json([], 200, { ETag: '"11"' });
    writes++;
    assert.equal(options.headers.get("If-Match"), '"11"');
    return json(
      {
        code: "REVISION_CONFLICT",
        message: "conflict",
        trace_id: "trace-test",
      },
      412,
    );
  };
  await client.get("/spaces/x/members");
  await assert.rejects(
    client.api("/spaces/x/members", {
      method: "PUT",
      body: [],
      etagPath: "/spaces/x/members",
    }),
    (error) =>
      error.status === 412 &&
      error.message.includes("保留当前编辑") &&
      error.traceId === "trace-test",
  );
  assert.equal(writes, 1);
});
test("session expiry signals the application and session reset clears secrets and ETags", async () => {
  globalThis.window = new EventTarget();
  let expired = false;
  window.addEventListener("session-expired", () => {
    expired = true;
  });
  client.setSession({ csrf_token: "temporary-test-token" });
  globalThis.fetch = async () =>
    json({ code: "SESSION_EXPIRED", message: "expired" }, 401);
  await assert.rejects(client.get("/jobs"));
  assert.equal(expired, true);
  client.clearSession();
  globalThis.fetch = async (_, options) => {
    assert.equal(options.headers.has("X-CSRF-Token"), false);
    return json({});
  };
  await client.post("/auth/demo", { user_id: "test-user" });
});
test("pagination preserves the denominator and refuses a repeated cursor", async () => {
  const urls = [];
  let count = 0;
  globalThis.fetch = async (url) => {
    urls.push(url);
    return json(
      count++ === 0
        ? { items: [1], next_cursor: "next" }
        : { items: [2], next_cursor: null },
    );
  };
  assert.deepEqual(await client.allPages("/resources?space_id=x"), [1, 2]);
  assert.match(urls[1], /space_id=x&limit=100&cursor=next$/);
  globalThis.fetch = async () => json({ items: [], next_cursor: "same" });
  await assert.rejects(client.allPages("/resources"), /分页游标重复/);
});
test("chunk resume reuses only size and SHA-matching parts before sealing", async () => {
  const file = new File(["abcdefgh"], "test.txt");
  const firstHash = await client.sha256(file.slice(0, 4));
  const secondHash = await client.sha256(file.slice(4));
  const calls = [];
  const progress = [];
  const session = {
    id: "upload-test",
    state: "OPEN",
    part_size: 4,
    part_count: 2,
    completed_parts: [{ part_no: 1, size_bytes: 4, sha256: firstHash }],
  };
  globalThis.fetch = async (url, options) => {
    calls.push(url);
    if (options.method === "GET") return json(session);
    if (url.endsWith("/parts/2")) {
      assert.equal(await options.body.text(), "efgh");
      return json({ part_no: 2, size_bytes: 4, sha256: secondHash });
    }
    if (url.endsWith("/complete")) {
      const body = JSON.parse(options.body);
      assert.equal(body.parts.length, 2);
      assert.equal(body.parts[1].sha256, secondHash);
      return json({ id: "parse-job", state: "QUEUED" });
    }
    throw new Error("unexpected upload request");
  };
  assert.equal(
    (
      await client.sendParts(
        file,
        session,
        (n) => progress.push(n),
        new AbortController().signal,
      )
    ).id,
    "parse-job",
  );
  assert.equal(
    calls.some((url) => url.endsWith("/parts/1")),
    false,
  );
  assert.deepEqual(progress, [50, 100]);
});
test("aborted upload never seals the session", async () => {
  const controller = new AbortController();
  controller.abort();
  let sealed = false;
  globalThis.fetch = async (url) => {
    if (url.endsWith("/complete")) sealed = true;
    return json({
      id: "x",
      state: "OPEN",
      part_size: 4,
      part_count: 1,
      completed_parts: [],
    });
  };
  await assert.rejects(
    client.sendParts(
      new File(["test"], "test.txt"),
      { id: "x" },
      () => {},
      controller.signal,
    ),
    (error) => error.name === "AbortError",
  );
  assert.equal(sealed, false);
});
test("batch selection retains every file including PNG and JPG in input order", () => {
  const files = ["a.pdf", "b.PNG", "c.jpg", "d.jpeg"].map(
    (name) => new File(["synthetic"], name),
  );
  const result = queue.mergeUploadSelection([], files, false);
  assert.deepEqual(
    result.items.map((item) => item.filename),
    files.map((file) => file.name),
  );
  assert.equal(
    result.items.every((item) => item.phase === "queued"),
    true,
  );
  assert.equal(new Set(result.items.map((item) => item.id)).size, files.length);
});
test("an invalid batch item is retained with its own error without dropping valid neighbors", () => {
  const files = [
    new File(["ok"], "a.pdf"),
    new File(["no"], "unsafe.exe"),
    new File(["ok"], "c.png"),
  ];
  const result = queue.mergeUploadSelection([], files, false);
  assert.deepEqual(
    result.items.map((item) => item.phase),
    ["queued", "failed", "queued"],
  );
  assert.match(result.items[1].error, /当前支持/);
});
test("single-document replacement rejects multiple files rather than silently taking the first", () => {
  assert.throws(
    () =>
      queue.mergeUploadSelection(
        [],
        [new File(["a"], "a.pdf"), new File(["b"], "b.pdf")],
        true,
      ),
    /一次只接受一个/,
  );
});
test("restoring a queue omits file contents, preserves per-file sessions and reattaches selected files", () => {
  const files = [new File(["alpha"], "a.pdf"), new File(["beta"], "b.png")];
  const original = queue.mergeUploadSelection([], files, false).items;
  original[0].upload = { id: "upload-a", version_id: "version-a" };
  original[0].hash = "original-hash";
  original[0].progress = 50;
  const serialized = queue.serializeUploadQueue(original);
  assert.equal(serialized.includes("alpha"), false);
  assert.equal(serialized.includes('"file":'), false);
  const restored = queue.restoreUploadQueue(serialized);
  assert.equal(restored[0].phase, "waiting-file");
  assert.equal(restored[0].versionId, "version-a");
  const resumed = queue.mergeUploadSelection(restored, files, false);
  assert.equal(resumed.items.length, 2);
  assert.equal(resumed.items[0].upload.id, "upload-a");
  assert.equal(resumed.items[0].hash, "original-hash");
  assert.equal(resumed.items[0].file, files[0]);
});
test("duplicate file selection does not create duplicate queue resources", () => {
  const file = new File(["x"], "image.jpg", { lastModified: 100 });
  const initial = queue.mergeUploadSelection([], [file], false).items;
  const second = queue.mergeUploadSelection(initial, [file], false);
  assert.equal(second.items.length, 1);
  assert.equal(second.skipped, 1);
});
test("using a template copies actual blocks, citations and applicability without sharing mutable state", () => {
  const template = {
    blocks: [
      {
        block_id: "block-a",
        ordinal: 3,
        block_type: "step",
        data: {
          action: "核对费用",
          owner_role: "经办",
          output: "差异表",
          verification: "复核签字",
        },
        locator: {},
        citations: [
          { version_id: "source-a", block_id: "source-block", purpose: "RULE" },
        ],
      },
    ],
    applicability: {
      all: [{ field: "product_type", op: "eq", values: ["基金"] }],
    },
    required_facts: ["business_date"],
  };
  const body = templates.contentFromTemplate("新业务方案", "sop", template);
  assert.equal(body.blocks.length, 1);
  assert.equal(body.blocks[0].data.action, "核对费用");
  assert.deepEqual(body.blocks[0].citations, template.blocks[0].citations);
  assert.equal(body.blocks[0].ordinal, 0);
  assert.deepEqual(body.applicability, template.applicability);
  assert.deepEqual(body.required_facts, ["business_date"]);
  assert.equal(body.legal_status, "UNKNOWN");
  assert.equal(body.valid_from, null);
  body.blocks[0].data.action = "修改草稿";
  body.blocks[0].citations.length = 0;
  body.applicability.all[0].values[0] = "另一产品";
  assert.equal(template.blocks[0].data.action, "核对费用");
  assert.equal(template.blocks[0].citations.length, 1);
  assert.equal(template.applicability.all[0].values[0], "基金");
});
