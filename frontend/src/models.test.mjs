// Reproducible: node --test src/models.test.mjs, using project dependencies only.
// HTTP, native dialog and layout are test stand-ins; no provider call/browser opens.
import test, { after, afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFile, access } from "node:fs/promises";
import Module, { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { build, transform } from "esbuild";
const require = createRequire(import.meta.url);
const domainCode = await transform(
  await readFile(new URL("./models.types.ts", import.meta.url), "utf8"),
  { loader: "ts", format: "esm" },
);
const domain = await import(
  "data:text/javascript;base64," +
    Buffer.from(domainCode.code).toString("base64")
);
const spaceId = "00000000-0000-4000-8000-000000000101";
const otherSpaceId = "00000000-0000-4000-8000-000000000102";
const fixtureProvider = {
  id: "openai",
  name: "测试提供商",
  kind: "direct",
  protocol: "responses",
  base_url: "https://models.example/v1",
  icon: "openai",
  models: [
    {
      id: "test-flagship",
      name: "测试旗舰模型",
      brand: "openai",
      flagship: true,
      source_url: "https://docs.example/models",
    },
  ],
  verified_at: null,
  source_url: "https://docs.example/models",
};
const fixtureConnection = {
  id: "connection-test",
  owner_user_id: "user-test",
  space_id: spaceId,
  name: "测试连接",
  provider_id: "openai",
  protocol: "responses",
  base_url: "https://models.example/v1",
  enabled: true,
  credential_mode: "encrypted",
  api_key_env: null,
  credential_present: true,
  allow_document_transfer: false,
  revision: 7,
  status: "UNVERIFIED",
  last_error_code: null,
  last_synced_at: null,
  models: [
    {
      id: "test-flagship",
      name: "测试旗舰模型",
      brand: "openai",
      flagship: true,
      source: "preset",
    },
  ],
};
const draft = (overrides) => ({
  name: "测试连接",
  provider_id: "openai",
  base_url: "https://models.example/v1",
  protocol: "responses",
  enabled: true,
  credential_mode: "encrypted",
  api_key_env: "",
  allow_document_transfer: false,
  custom_models: [],
  ...overrides,
});

test("all manifest brands including BAAI resolve to real local SVG files; unknown brands cannot become URLs", async () => {
  const manifest = JSON.parse(
    await readFile(
      new URL("../public/provider-icons/manifest.json", import.meta.url),
      "utf8",
    ),
  );
  assert.equal(manifest.version, "1.95.0");
  for (const brand of Object.keys(manifest.icons)) {
    assert.equal(
      domain.providerIconPath(brand),
      "/provider-icons/" + brand + ".svg",
    );
    await access(
      new URL("../public" + domain.providerIconPath(brand), import.meta.url),
    );
  }
  assert.equal(
    domain.providerIconPath("https://external.example/logo.svg"),
    null,
  );
  assert.equal(domain.providerIconPath("../../private"), null);
  assert.equal(domain.providerIconPath("__proto__"), null);
});
test("configured credentials and synced catalogs never imply a successful model probe", () => {
  assert.equal(
    domain.connectionState(fixtureConnection).label,
    "已配置 · 未验证",
  );
  assert.equal(
    domain.connectionState({ ...fixtureConnection, status: "SYNCED" }).label,
    "已同步 · 未验证",
  );
  assert.equal(
    domain.connectionState({ ...fixtureConnection, status: "TESTED" }).label,
    "连接探测通过",
  );
  assert.equal(
    domain.connectionState({ ...fixtureConnection, credential_present: false })
      .label,
    "未配置",
  );
  assert.equal(
    domain.connectionState({ ...fixtureConnection, enabled: false }).label,
    "已停用",
  );
});
test("connection payload keeps encrypted input write-only and uses the exact current space", () => {
  const body = domain.connectionWrite(draft(), spaceId, "test-only-secret");
  assert.equal(body.api_key, "test-only-secret");
  assert.equal(body.space_id, spaceId);
  assert.equal(body.allow_document_transfer, false);
  assert.equal("api_key_env" in body, false);
  assert.equal("credential_ciphertext" in body, false);
  assert.equal("credential_present" in body, false);
  const existing = domain.connectionWrite(
    draft(),
    spaceId,
    "",
    fixtureConnection,
  );
  assert.equal("api_key" in existing, false);
  assert.equal("space_id" in existing, false);
});
test("env/none modes never send leftover secrets, and empty encrypted drafts must remain disabled", () => {
  const env = domain.connectionWrite(
    draft({ credential_mode: "env", api_key_env: "FKB_TEST_MODEL_KEY" }),
    spaceId,
    "ignored-test-secret",
  );
  assert.equal(env.api_key_env, "FKB_TEST_MODEL_KEY");
  assert.equal("api_key" in env, false);
  assert.throws(
    () =>
      domain.connectionWrite(
        draft({ credential_mode: "env", api_key_env: "SHARED_PRIVATE_KEY" }),
        spaceId,
        "",
      ),
    /FKB_/,
  );
  assert.throws(() => domain.connectionWrite(draft(), spaceId, ""), /API 密钥/);
  assert.equal(
    "api_key" in domain.connectionWrite(draft({ enabled: false }), spaceId, ""),
    false,
  );
  const local = domain.connectionWrite(
    draft({
      credential_mode: "none",
      base_url: "http://127.0.0.1:11434",
      protocol: "ollama",
    }),
    spaceId,
    "ignored-test-secret",
  );
  assert.equal("api_key" in local, false);
  assert.equal("api_key_env" in local, false);
});
test("target or protocol changes require credential re-entry and invalid model IDs fail early", () => {
  assert.throws(
    () =>
      domain.connectionWrite(
        draft({ protocol: "openai" }),
        spaceId,
        "",
        fixtureConnection,
      ),
    /重新输入/,
  );
  assert.throws(
    () =>
      domain.connectionWrite(
        draft({ base_url: "https://other.example/v1" }),
        spaceId,
        "",
        fixtureConnection,
      ),
    /重新输入/,
  );
  assert.throws(
    () =>
      domain.connectionWrite(
        draft({ base_url: "https://models.example/v2" }),
        spaceId,
        "",
        fixtureConnection,
      ),
    /重新输入/,
  );
  assert.throws(
    () =>
      domain.connectionWrite(
        draft({ base_url: "https://user:secret@models.example" }),
        spaceId,
        "test",
      ),
    /基础地址/,
  );
  assert.throws(
    () =>
      domain.connectionWrite(
        draft({
          custom_models: [{ id: "错误 ID", name: "名称", brand: "openai" }],
        }),
        spaceId,
        "test",
      ),
    /模型 ID/,
  );
});
test("public connection state drops accidental secret/ciphertext fields, including nested model fields", () => {
  const publicValue = domain.publicConnection({
    ...fixtureConnection,
    api_key: "hidden-plaintext",
    credential_ciphertext: "hidden-ciphertext",
    models: [{ ...fixtureConnection.models[0], api_key: "nested-hidden" }],
  });
  assert.equal(JSON.stringify(publicValue).includes("hidden-"), false);
  assert.equal(
    domain.redactConnectionError(
      new Error("unexpected test-only-secret"),
      "test-only-secret",
    ).message,
    "unexpected [密钥已隐藏]",
  );
});
test("model selection compares both connection and model; gateway source does not replace model brand", () => {
  const options = [
    {
      connection_id: "gateway",
      connection_name: "中转连接",
      provider_id: "openrouter",
      kind: "gateway",
      protocol: "openai",
      model_id: "anthropic/test",
      model_name: "测试模型",
      brand: "anthropic",
      flagship: true,
      configured: true,
      allow_document_transfer: true,
    },
  ];
  assert.equal(
    domain.sameModel(
      { connection_id: "a", model_id: "same" },
      { connection_id: "b", model_id: "same" },
    ),
    false,
  );
  assert.equal(
    domain.filterModelOptions(options, "Anthropic", "gateway", true).length,
    1,
  );
  assert.equal(domain.filterModelOptions(options, "", "local", true).length, 0);
});

const { JSDOM } = require("jsdom");
const dom = new JSDOM(
  '<!doctype html><html><body><div id="root"></div></body></html>',
  { url: "http://localhost/", pretendToBeVisual: true },
);
const { window } = dom;
for (const key of [
  "window",
  "document",
  "HTMLElement",
  "HTMLDialogElement",
  "Element",
  "Node",
  "MutationObserver",
  "Event",
  "MouseEvent",
  "FormData",
])
  globalThis[key] = key === "window" ? window : window[key];
globalThis.getComputedStyle = window.getComputedStyle.bind(window);
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
window.matchMedia = (query) => ({
  media: query,
  matches: query.includes("reduce") && !query.includes("no-preference"),
  addListener() {},
  removeListener() {},
  addEventListener() {},
  removeEventListener() {},
});
window.HTMLDialogElement.prototype.showModal = function () {
  this.open = true;
  this.querySelector("input,button")?.focus();
};
window.HTMLDialogElement.prototype.close = function () {
  this.open = false;
};
const storageWrites = [];
window.Storage.prototype.setItem = function (key, value) {
  storageWrites.push([key, value]);
};
const React = require("react");
const { act } = React;
const h = React.createElement;
const { createRoot } = require("react-dom/client");
const gsap = require("gsap").gsap;
const { ScrollTrigger } = require("gsap/dist/ScrollTrigger");
const compiled = await build({
  stdin: {
    contents:
      'export { ModelPicker } from "./ModelPicker"; export { ModelsPage } from "./ModelsPage"; export { BrandIcon } from "./BrandIcon"; export { AppContext } from "./ui"; export { setSession, clearSession } from "./api";',
    resolveDir: resolve("src"),
    loader: "tsx",
  },
  bundle: true,
  platform: "node",
  format: "cjs",
  external: [
    "react",
    "react-dom",
    "react-dom/*",
    "react/jsx-runtime",
    "gsap",
    "gsap/ScrollTrigger",
    "@gsap/react",
  ],
  loader: { ".css": "empty" },
  write: false,
  logLevel: "silent",
});
const module = new Module(resolve("src/models-test-runtime.cjs"));
module.filename = resolve("src/models-test-runtime.cjs");
module.paths = Module._nodeModulePaths(dirname(module.filename));
const runtimeRequire = module.require.bind(module);
module.require = (id) => id === "gsap" ? gsap : id === "gsap/ScrollTrigger" ? { ScrollTrigger } : runtimeRequire(id);
module._compile(compiled.outputFiles[0].text, module.filename);
const {
  ModelsPage,
  ModelPicker,
  BrandIcon,
  AppContext,
  setSession,
  clearSession,
} = module.exports;
const json = (value, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
const originalFetch = globalThis.fetch;
let root;
let calls = [];
let serverConnections = [];
let options = [];
let action;
const context = {
  me: {
    id: "user-test",
    display_name: "测试管理员",
    csrf_token: "test-csrf",
    spaces: [],
  },
  space: { id: spaceId, name: "测试空间", revision: 1, roles: ["admin"] },
  refresh: 0,
  bump() {},
  notify() {},
  navigate() {},
  openResource() {},
  openVersion() {},
  ask() {},
};
function backend() {
  globalThis.fetch = async (url, init) => {
    const path = String(url).replace("/api/v1", "");
    calls.push({ path, ...init });
    if (path === "/model-providers")
      return json({ items: [fixtureProvider], live_verified: false });
    if (path.startsWith("/model-options?"))
      return json({ items: options, default: null });
    if ((path === "/model-connections" && init.method === "GET") || path.startsWith("/model-connections?"))
      return json({ items: serverConnections });
    if (action) return action(path, init);
    throw new Error("Unexpected test HTTP request: " + path);
  };
}
async function mount(element, overrides = {}) {
  backend();
  setSession(context.me);
  root = createRoot(document.getElementById("root"));
  await act(async () =>
    root.render(
      h(AppContext.Provider, { value: { ...context, ...overrides } }, element),
    ),
  );
}
const click = (element) => {
  assert.ok(element, "expected clickable element");
  return act(async () =>
    element.dispatchEvent(
      new window.MouseEvent("click", { bubbles: true, cancelable: true }),
    ),
  );
};
const byText = (text, scope = document) =>
  Array.from(scope.querySelectorAll("button")).find(
    (button) => button.textContent.trim() === text,
  );
const submit = (form) =>
  act(async () =>
    form.dispatchEvent(
      new window.Event("submit", { bubbles: true, cancelable: true }),
    ),
  );
afterEach(async () => {
  if (root) {
    await act(async () => root.unmount());
    root = undefined;
  }
  calls = [];
  serverConnections = [];
  options = [];
  action = undefined;
  storageWrites.length = 0;
  clearSession();
  globalThis.fetch = originalFetch;
});
after(() => {
  // This test process owns its plugin instance; production components only
  // release their own triggers. Stop the closed jsdom host's global timer here.
  try { assert.equal(ScrollTrigger.getAll().length, 0); }
  finally { ScrollTrigger.disable(); gsap.ticker.sleep(); window.close(); }
});

test("BrandIcon uses real local img and unknown provider uses a standard CPU SVG", async () => {
  await mount(
    h(
      "div",
      null,
      h(BrandIcon, { brand: "gemini" }),
      h(BrandIcon, { brand: "custom-vendor" }),
    ),
  );
  assert.equal(
    document.querySelector("img").getAttribute("src"),
    "/provider-icons/gemini.svg",
  );
  assert.ok(document.querySelector("svg.model-brand-fallback"));
  assert.equal(document.querySelectorAll("img").length, 1);
});
test("ModelPicker uses spaceId override, separates gateway/brand and blocks unconsented transfer", async () => {
  const selected = [];
  options = [
    {
      connection_id: "gateway-a",
      connection_name: "中转 A",
      provider_id: "openrouter",
      kind: "gateway",
      protocol: "openai",
      model_id: "anthropic/test-a",
      model_name: "允许传输的模型",
      brand: "anthropic",
      configured: true,
      flagship: true,
      allow_document_transfer: true,
    },
    {
      connection_id: "gateway-b",
      connection_name: "中转 B",
      provider_id: "openrouter",
      kind: "gateway",
      protocol: "openai",
      model_id: "openai/test-b",
      model_name: "未授权模型",
      brand: "openai",
      configured: true,
      allow_document_transfer: false,
    },
  ];
  await mount(
    h(ModelPicker, {
      value: null,
      onChange: (value) => selected.push(value),
      spaceId: otherSpaceId,
      requireTransfer: true,
    }),
  );
  assert.ok(
    calls.some((call) => call.path.includes("space_id=" + otherSpaceId)),
  );
  await click(document.querySelector('[role="combobox"]'));
  const choices = Array.from(document.querySelectorAll('[role="option"]'));
  assert.equal(choices.length, 2);
  assert.equal(choices[1].disabled, true);
  assert.match(choices[0].textContent, /Anthropic/);
  assert.match(choices[0].textContent, /OpenRouter/);
  await click(choices[0]);
  assert.deepEqual(selected, [
    { connection_id: "gateway-a", model_id: "anthropic/test-a" },
  ]);
});
test("disabled ModelPicker does not open or mutate the selection", async () => {
  let changed = 0;
  await mount(
    h(ModelPicker, { value: null, onChange: () => changed++, disabled: true }),
  );
  assert.equal(document.querySelector('[role="combobox"]').disabled, true);
  await click(document.querySelector('[role="combobox"]'));
  assert.equal(document.querySelector("dialog"), null);
  assert.equal(changed, 0);
});
test("creating a connection posts encrypted-mode write input with cookie/CSRF, never persists or echoes the key", async () => {
  let createdBody;
  action = async (path, init) => {
    assert.equal(path, "/model-connections");
    assert.equal(init.method, "POST");
    createdBody = JSON.parse(init.body);
    assert.equal(init.credentials, "include");
    assert.equal(init.headers.get("X-CSRF-Token"), "test-csrf");
    assert.ok(init.headers.get("Idempotency-Key"));
    serverConnections = [
      {
        ...fixtureConnection,
        name: createdBody.name,
        api_key: "unexpected-secret-from-server",
        credential_ciphertext: "unexpected-ciphertext",
      },
    ];
    return json(serverConnections[0], 201);
  };
  await mount(h(ModelsPage));
  await click(byText("添加连接"));
  const password = document.querySelector('input[type="password"]');
  password.value = "SYNTHETIC_MODEL_KEY_NOT_REAL";
  await submit(document.querySelector("dialog form"));
  assert.equal(createdBody.api_key, "SYNTHETIC_MODEL_KEY_NOT_REAL");
  assert.equal(createdBody.allow_document_transfer, false);
  assert.equal(storageWrites.length, 0);
  assert.equal(
    document.body.textContent.includes("SYNTHETIC_MODEL_KEY_NOT_REAL"),
    false,
  );
  assert.equal(
    document.body.textContent.includes("unexpected-secret-from-server"),
    false,
  );
  assert.equal(
    calls.some((call) => /\/test$|\/sync-models$/.test(call.path)),
    false,
  );
});
test("editing preserves a stored credential without sending an empty key, and uses If-Match", async () => {
  serverConnections = [structuredClone(fixtureConnection)];
  let body;
  action = async (path, init) => {
    assert.equal(path, "/model-connections/connection-test");
    assert.equal(init.method, "PATCH");
    assert.equal(init.headers.get("If-Match"), '"7"');
    body = JSON.parse(init.body);
    serverConnections = [{ ...fixtureConnection, ...body, revision: 8 }];
    return json(serverConnections[0]);
  };
  await mount(h(ModelsPage));
  await click(byText("编辑连接"));
  assert.equal(document.querySelector('input[type="password"]').value, "");
  await submit(document.querySelector("dialog form"));
  assert.equal("api_key" in body, false);
  assert.equal("credential_ciphertext" in body, false);
});
test("save errors redact the submitted key and clear the password input", async () => {
  action = async () =>
    json(
      {
        code: "TEST_ERROR",
        message: "rejected SYNTHETIC_FAILURE_KEY",
        trace_id: "test",
      },
      400,
    );
  await mount(h(ModelsPage));
  await click(byText("添加连接"));
  document.querySelector('input[type="password"]').value =
    "SYNTHETIC_FAILURE_KEY";
  await submit(document.querySelector("dialog form"));
  assert.equal(document.querySelector('input[type="password"]').value, "");
  assert.equal(
    document.body.textContent.includes("SYNTHETIC_FAILURE_KEY"),
    false,
  );
  assert.match(document.body.textContent, /密钥已隐藏/);
});
test("model sync returns a standard Job and polls before refreshing synced models", async () => {
  serverConnections = [structuredClone(fixtureConnection)];
  let polled = 0;
  const job = {
    id: "job-sync",
    kind: "COMPILE",
    state: "QUEUED",
    stage: "MODEL_SYNC",
    attempts: 0,
    error_code: null,
    result: null,
  };
  action = async (path, init) => {
    if (path === "/model-connections/connection-test/sync-models") {
      assert.equal(init.method, "POST");
      return json(job, 202);
    }
    if (path === "/jobs/job-sync") {
      polled++;
      serverConnections = [
        {
          ...fixtureConnection,
          status: "SYNCED",
          last_synced_at: "2026-01-01T00:00:00Z",
          models: [{ ...fixtureConnection.models[0], source: "synced" }],
        },
      ];
      return json({
        ...job,
        state: "SUCCEEDED",
        result: {
          connection_id: "connection-test",
          model_count: 1,
          status: "SYNCED",
        },
      });
    }
    throw new Error("unexpected operation " + path);
  };
  await mount(h(ModelsPage));
  await click(byText("同步模型"));
  await act(async () => {});
  assert.ok(polled > 0);
  assert.match(document.body.textContent, /账号已同步/);
  assert.match(document.body.textContent, /已同步 · 未验证/);
});
test("enabled connection removal uses DELETE with revision and refreshes it out of the list", async () => {
  serverConnections = [structuredClone(fixtureConnection)];
  let removed = false;
  action = async (path, init) => {
    assert.equal(path, "/model-connections/connection-test");
    assert.equal(init.method, "DELETE");
    assert.equal(init.headers.get("If-Match"), '"7"');
    removed = true;
    serverConnections = [];
    return new Response(null, { status: 204 });
  };
  await mount(h(ModelsPage));
  await click(byText("停用"));
  const dialog = document.querySelector("dialog");
  assert.ok(byText("移除并清除凭据配置", dialog));
  assert.match(dialog.textContent, /再次使用时需新建连接并配置凭据/);
  assert.equal(removed, false, "opening confirmation must not delete the connection");
  await submit(document.querySelector("dialog form"));
  assert.equal(removed, true);
  assert.equal(serverConnections.length, 0);
  assert.equal(document.querySelectorAll(".model-connection-row").length, 0);
  assert.match(document.body.textContent, /还没有配置模型连接/);
});
test("disabled connection can be removed without re-enabling it and other connections remain", async () => {
  const disabledConnection = {
    ...structuredClone(fixtureConnection),
    id: "disabled-connection",
    name: "待移除的禁用连接",
    enabled: false,
    credential_present: false,
    status: "DISABLED",
    revision: 11,
    models: [],
  };
  serverConnections = [disabledConnection, structuredClone(fixtureConnection)];
  let removed = false;
  action = async (path, init) => {
    assert.equal(path, "/model-connections/disabled-connection");
    assert.equal(init.method, "DELETE");
    assert.equal(init.headers.get("If-Match"), '"11"');
    removed = true;
    serverConnections = serverConnections.filter(connection => connection.id !== disabledConnection.id);
    return new Response(null, { status: 204 });
  };
  await mount(h(ModelsPage));
  const removeButton = byText("移除连接");
  assert.ok(removeButton, "disabled connections must still expose a removal action");
  assert.equal(removeButton.disabled, false);
  assert.equal(byText("停用"), undefined);
  assert.equal(byText("同步模型").disabled, true);
  assert.equal(byText("测试连接").disabled, true);
  await click(removeButton);
  const dialog = document.querySelector("dialog");
  assert.equal(dialog.querySelector("h2").textContent, "移除模型连接");
  assert.ok(byText("移除并清除凭据配置", dialog));
  assert.match(dialog.textContent, /再次使用时需新建连接并配置凭据/);
  assert.equal(removed, false);
  await submit(dialog.querySelector("form"));
  assert.equal(removed, true);
  assert.deepEqual(serverConnections.map(connection => connection.id), [fixtureConnection.id]);
  assert.deepEqual(Array.from(document.querySelectorAll(".model-connection-title strong"), element => element.textContent), [fixtureConnection.name]);
  assert.equal(calls.some(call => call.method === "PATCH"), false, "removal must not re-enable the connection first");
});
test("connection test sends only the chosen model id and polls the probe Job", async () => {
  serverConnections = [structuredClone(fixtureConnection)];
  let probeBody;
  let polled = 0;
  const job = {
    id: "job-probe",
    kind: "COMPILE",
    state: "QUEUED",
    stage: "MODEL_TEST",
    attempts: 0,
    error_code: null,
    result: null,
  };
  action = async (path, init) => {
    if (path === "/model-connections/connection-test/test") {
      probeBody = JSON.parse(init.body);
      return json(job, 202);
    }
    if (path === "/jobs/job-probe") {
      polled++;
      serverConnections = [{ ...fixtureConnection, status: "TESTED" }];
      return json({
        ...job,
        state: "SUCCEEDED",
        result: { connection_id: "connection-test", status: "TESTED" },
      });
    }
    throw new Error("Unexpected probe request: " + path);
  };
  await mount(h(ModelsPage));
  await click(byText("测试连接"));
  document.querySelector('select[name="model_id"]').value = "test-flagship";
  await submit(document.querySelector("dialog form"));
  await act(async () => {});
  assert.deepEqual(probeBody, { model_id: "test-flagship" });
  assert.ok(polled > 0);
  assert.match(document.body.textContent, /连接探测通过/);
  assert.match(document.body.textContent, /不代表专业准确性/);
});
test("space readers can manage their own connections without space-admin rights", async () => {
  serverConnections = [structuredClone(fixtureConnection)];
  await mount(h(ModelsPage), {
    space: { ...context.space, roles: ["reader"] },
  });
  for (const text of ["添加连接", "编辑连接", "同步模型", "测试连接", "停用"])
    assert.equal(byText(text).disabled, false, text);
  serverConnections = [{ ...fixtureConnection, enabled: false, status: "DISABLED" }];
  await click(byText("刷新"));
  assert.ok(byText("移除连接"));
  assert.equal(byText("移除连接").disabled, false);
  await click(byText("移除连接"));
  assert.ok(document.querySelector("dialog"));
  assert.equal(
    calls.every((call) => call.method === "GET"),
    true,
  );
});

test("OAuth writes never forward secrets or custom endpoints and URLs stay official", () => {
  const body = domain.connectionWrite(draft({provider_id: "chatgpt-codex", base_url: "https://evil.invalid",
    credential_mode: "chatgpt_oauth"}), spaceId, "SYNTHETIC_SHOULD_DROP");
  assert.equal(body.protocol, "codex_app_server");
  assert.equal(body.base_url, "");
  assert.equal("api_key" in body, false);
  assert.equal("api_key_env" in body, false);
  assert.equal(domain.safeOAuthUrl("https://auth.openai.com/codex/device"), "https://auth.openai.com/codex/device");
  for (const url of ["javascript:alert(1)", "https://auth.openai.com.evil.invalid", "https://evil@auth.openai.com", "https://auth.openai.com:bad"])
    assert.equal(domain.safeOAuthUrl(url), undefined);
});

test("unconfigured OAuth runtime shows honest disabled state and never starts login automatically", async () => {
  const caps = {auth: false, model_list: false, inference: false, auth_blocked_reason: "CODEX_BRIDGE_DISABLED",
    inference_blocked_reason: "CODEX_TEXT_ISOLATION_UNVERIFIED", browser_callback_reachable: false, live_verified: false};
  serverConnections = [{...fixtureConnection, provider_id: "chatgpt-codex", protocol: "codex_app_server",
    credential_mode: "chatgpt_oauth", credential_present: false, models: [], capabilities: caps}];
  action = async (path, init) => {
    assert.equal(path, "/model-connections/connection-test/oauth");
    assert.equal(init.method, "GET");
    return json({connection_id: "connection-test", owner_user_id: "user-test", connection_revision: 7,
      oauth_revision: 1, auth_epoch: 0, state: "SIGNED_OUT", attempt_id: null, active_job_id: null,
      account: null, checked_at: null, last_error_code: null, capabilities: caps});
  };
  await mount(h(ModelsPage));
  await act(async () => {});
  assert.equal(byText("使用设备码登录").disabled, true);
  assert.equal(byText("测试连接").disabled, true);
  assert.match(document.body.textContent, /登录适配未就绪/);
  assert.match(document.body.textContent, /推理暂不可用/);
  assert.ok(calls.every(call => call.method === "GET"));
});

test("Codex picker option stays blocked even when an account is configured", async () => {
  options = [{connection_id: "codex-test", connection_name: "My Pro", provider_id: "chatgpt-codex",
    kind: "subscription", protocol: "codex_app_server", model_id: "synthetic", model_name: "Synthetic",
    brand: "openai", configured: true, selectable: false, allow_document_transfer: true,
    blocked_reason: "CODEX_TEXT_ISOLATION_UNVERIFIED"}];
  await mount(h(ModelPicker, {value: null, onChange() {throw new Error("Blocked option selected");}}));
  await click(document.querySelector('[role="combobox"]'));
  assert.equal(document.querySelector('[role="option"]').disabled, true);
  assert.match(document.querySelector('[role="option"]').title, /严格文本隔离/);
  assert.equal(document.querySelector(".model-option-state").textContent, "服务不可用");
  assert.match(document.querySelector(".model-option-unavailable").textContent, /严格文本隔离/);
});

test("server-approved Codex options can be selected and switched without running a model", async () => {
  options = ["a", "b"].map(id => ({connection_id:"codex-ready", connection_name:"本人订阅连接",
    provider_id:"chatgpt-codex", kind:"subscription", protocol:"codex_app_server", model_id:`synthetic-${id}`,
    model_name:`合成订阅模型 ${id}`, brand:"openai", configured:true, selectable:true,
    allow_document_transfer:true, blocked_reason:null}));
  const selected=[];
  function ControlledPicker() {
    const [value,setValue]=React.useState(null);
    return h(ModelPicker,{value,requireTransfer:true,onChange(next){selected.push(next);setValue(next);}});
  }
  await mount(h(ControlledPicker));
  await click(document.querySelector('[role="combobox"]'));
  assert.ok([...document.querySelectorAll('[role="option"]')].every(e=>!e.disabled));
  await click(document.querySelector('[role="option"]'));
  assert.deepEqual(selected[0],{connection_id:"codex-ready",model_id:"synthetic-a"});
  assert.equal(document.querySelector("dialog"),null);
  assert.match(document.querySelector('[role="combobox"]').textContent,/合成订阅模型 a/);
  await click(document.querySelector('[role="combobox"]'));
  assert.equal(document.querySelector('[role="option"]').getAttribute("aria-selected"),"true");
  await click(document.querySelectorAll('[role="option"]')[1]);
  assert.deepEqual(selected[1],{connection_id:"codex-ready",model_id:"synthetic-b"});
  assert.match(document.querySelector('[role="combobox"]').textContent,/合成订阅模型 b/);
  assert.ok(calls.every(call=>call.method==="GET"));
});

test("Codex without explicit server readiness stays unavailable instead of showing choose", async () => {
  options=[{connection_id:"codex-unknown",connection_name:"状态未确认",provider_id:"chatgpt-codex",
    kind:"subscription",protocol:"codex_app_server",model_id:"synthetic",model_name:"未知可用状态模型",
    brand:"openai",configured:true,allow_document_transfer:true}];
  await mount(h(ModelPicker,{value:null,onChange(){throw new Error("unknown model selected");}}));
  await click(document.querySelector('[role="combobox"]'));
  assert.equal(document.querySelector('[role="option"]').disabled,true);
  assert.equal(document.querySelector(".model-option-state").textContent,"状态待确认");
  assert.match(document.querySelector(".model-option-unavailable").textContent,/刷新/);
});

test("ready subscription still cannot transfer documents without connection consent", async () => {
  options=[{connection_id:"codex-no-transfer",connection_name:"禁止外发连接",provider_id:"chatgpt-codex",
    kind:"subscription",protocol:"codex_app_server",model_id:"synthetic",model_name:"已就绪订阅模型",
    brand:"openai",configured:true,selectable:true,allow_document_transfer:false}];
  await mount(h(ModelPicker,{value:null,requireTransfer:true,onChange(){throw new Error("unconsented choice");}}));
  await click(document.querySelector('[role="combobox"]'));
  assert.equal(document.querySelector('[role="option"]').disabled,true);
  assert.equal(document.querySelector(".model-option-state").textContent,"未授权资料传输");
  assert.ok(calls.every(call=>call.method==="GET"));
});

test("already-authenticated OAuth mount does not cause endless metadata reloads", async () => {
  const caps = {auth:true, model_list:true, inference:false, auth_blocked_reason:null,
    inference_blocked_reason:"CODEX_TEXT_ISOLATION_UNVERIFIED", browser_callback_reachable:true, live_verified:false};
  serverConnections = [{...fixtureConnection, provider_id:"chatgpt-codex", protocol:"codex_app_server",
    credential_mode:"chatgpt_oauth", credential_present:true, models:[], capabilities:caps}];
  action = async () => json({connection_id:"connection-test", owner_user_id:"user-test", connection_revision:7,
    oauth_revision:3, auth_epoch:1, state:"AUTHENTICATED", attempt_id:null, active_job_id:null,
    account:{type:"chatgpt",plan_type:"pro"}, checked_at:null, last_error_code:null, capabilities:caps});
  let reloads = 0;
  await mount(h(ModelsPage), {bump(){reloads += 1;}});
  await act(async () => {});
  assert.equal(reloads, 0);
  assert.match(document.body.textContent, /已登录.*pro/);
  assert.ok(calls.every(call => call.method === "GET"));
});

test("long connection names keep a separate selected state and all model records", async () => {
  const longName = "ChatGPT / Codex 官方登录 · 运营知识库长名称回归验证".repeat(4);
  const models = Array.from({length: 7}, (_, index) => ({
    ...fixtureConnection.models[0], id: `synthetic-${index}-` + "long-model-id-".repeat(12),
    name: "合成模型长名称".repeat(12) + index,
  }));
  serverConnections = [{...fixtureConnection, name: longName, models},
    {...fixtureConnection, id: "second-connection", name: "另一个合成连接"}];
  await mount(h(ModelsPage));
  const selected = document.querySelector('.model-connection-row[aria-pressed="true"]');
  assert.equal(selected.querySelector(".model-connection-title strong").textContent, longName);
  assert.equal(selected.querySelector(":scope > .model-state").textContent, "已配置 · 未验证");
  assert.equal(selected.querySelector(".model-connection-title .model-state"), null);
  assert.equal(document.querySelector(".models-connection-detail h2").textContent, longName);
  assert.equal(document.querySelectorAll(".models-account-model").length, 7);
  assert.deepEqual([...document.querySelectorAll(".models-account-model .model-id")].map(e => e.textContent), models.map(m => m.id));
  const metadata = [...document.querySelectorAll(".model-connection-metadata > div")];
  assert.equal(metadata.length, 5);
  assert.ok(metadata.every(e => e.querySelector("dt") && e.querySelector("dd")));
  await click(document.querySelectorAll(".model-connection-row")[1]);
  assert.equal(document.querySelector('.model-connection-row[aria-pressed="true"] .model-connection-title strong').textContent, "另一个合成连接");
  assert.ok(calls.every(call => call.method === "GET"));
});

test("model surfaces override button nowrap and reserve content padding without clipping", async () => {
  serverConnections = [structuredClone(fixtureConnection)];
  await mount(h(ModelsPage));
  const style = document.createElement("style");
  style.textContent = 'button { white-space: nowrap; }\n' + await readFile(new URL("./models-ui.css", import.meta.url), "utf8");
  document.head.appendChild(style);
  try {
    const css = selector => getComputedStyle(document.querySelector(selector));
    assert.equal(css(".model-connection-row").display, "grid");
    assert.equal(css(".model-connection-row").whiteSpace, "normal");
    assert.equal(css(".model-connection-title").whiteSpace, "normal");
    assert.equal(css(".model-connection-row > .model-state").gridColumn, "2");
    assert.equal(css(".model-connection-row > .model-state").whiteSpace, "normal");
    assert.equal(css(".models-page-body").backgroundColor, "rgb(255, 255, 255)");
    assert.equal(css(".models-connection-detail").padding, "24px");
    assert.equal(css(".models-connection-detail").overflowWrap, "anywhere");
    assert.equal(css(".models-connection-detail > header").flexWrap, "wrap");
    assert.equal(css(".models-account-model").display, "grid");
    assert.equal(css(".models-account-model > div").minWidth, "0");
    assert.equal(css(".models-account-model > .model-state").gridColumn, "2");
    // This guards CSS structure only; real geometry is verified in the browser.
    for (const selector of [".models-page-body", ".models-connection-detail", ".models-account-model"])
      assert.ok(!["hidden", "clip"].includes(css(selector).overflow));
  } finally {
    style.remove();
  }
});
