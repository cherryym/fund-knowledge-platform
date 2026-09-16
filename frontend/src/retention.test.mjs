import assert from "node:assert/strict";
import { after, afterEach, test } from "node:test";
import { createRequire, Module } from "node:module";
import { dirname, resolve } from "node:path";
import { build } from "esbuild";
const require = createRequire(import.meta.url);
const { JSDOM } = require("jsdom");
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: "http://localhost/" });
for (const key of ["window", "document", "HTMLElement", "Element", "Node", "Event", "MouseEvent", "HTMLInputElement", "HTMLTextAreaElement"])
  globalThis[key] = key === "window" ? dom.window : dom.window[key];
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const React = require("react");
const { act } = React;
const h = React.createElement;
const { createRoot } = require("react-dom/client");
const compiled = await build({ stdin: { contents: 'export { RetentionPanel, PurgeEligibilityPanel } from "./RetentionPanel"; export { AppContext } from "./appContext"; export { setSession, clearSession } from "./api";', resolveDir: resolve("src"), loader: "tsx" },
  bundle: true, platform: "node", format: "cjs", external: ["react", "react-dom", "react-dom/*", "react/jsx-runtime"],
  loader: { ".css": "empty" }, write: false, logLevel: "silent" });
const module = new Module(resolve("src/retention-test-runtime.cjs"));
module.filename = resolve("src/retention-test-runtime.cjs");
module.paths = Module._nodeModulePaths(dirname(module.filename));
module._compile(compiled.outputFiles[0].text, module.filename);
const { RetentionPanel, PurgeEligibilityPanel, AppContext, setSession, clearSession } = module.exports;
const originalFetch = globalThis.fetch;
const context = { me: { id: "owner", csrf_token: "synthetic-csrf" }, space: { id: "space-a", name: "合成库", roles: ["editor"] }, refresh: 0 };
const policy = (overrides = {}) => ({ space_id: "space-a", revision: 0, status: "UNCONFIGURED", configured: false,
  approved: false, retention_days: 2, suggested_retention_days: 2, purge_allowed_roles: ["admin", "owner"],
  approved_by: null, approved_at: null, approval_expires_at: null, automatic_purge: false,
  permissions: { can_configure: true, can_request_purge: false, can_manage_preservation: true }, ...overrides });
const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
let root, calls = [], handler;
async function mount(element = h(RetentionPanel), app = context) {
  globalThis.fetch = async (url, init) => { calls.push({ url, ...init }); return handler(url, init); };
  setSession(context.me);
  root = createRoot(document.getElementById("root"));
  await act(async () => root.render(h(AppContext.Provider, { value: app }, element)));
}
const click = element => act(async () => element.dispatchEvent(new MouseEvent("click", { bubbles: true })));
const submit = () => act(async () => document.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
async function reason(value = "用户已确认保留2天") {
  const element = document.querySelector("textarea");
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
  await act(async () => { setter.call(element, value); element.dispatchEvent(new Event("input", { bubbles: true })); });
}
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  root = null; calls = []; handler = null; clearSession(); globalThis.fetch = originalFetch;
});
after(() => dom.window.close());

test("no required props; two-day suggestion, unapproved status and legal limits are visible", async () => {
  handler = () => json(policy());
  await mount();
  assert.match(document.body.textContent, /删除时间起保留 2 天/);
  assert.match(document.body.textContent, /尚未配置/);
  assert.match(document.body.textContent, /既有法定要求、法律保全和更长保留期限优先/);
  assert.equal(document.querySelector('input[type="number"]').value, "2");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "GET");
});

test("server permissions prevent editor self-elevation and forced form submission", async () => {
  handler = () => json(policy({ permissions: { can_configure: false, can_request_purge: false, can_manage_preservation: false } }));
  await mount();
  assert.ok([...document.querySelectorAll("fieldset")].every(item => item.disabled));
  assert.equal(document.querySelector('button[type="submit"]').disabled, true);
  await submit();
  assert.equal(calls.length, 1);
});

test("explicit readOnly overrides server management ability", async () => {
  handler = () => json(policy());
  await mount(h(RetentionPanel, { readOnly: true }));
  assert.ok([...document.querySelectorAll("fieldset")].every(item => item.disabled));
  await submit();
  assert.equal(calls.length, 1);
});

test("approval writes correct ETag CSRF and role list then shows success without purge", async () => {
  handler = (url, init) => init.method === "GET" ? json(policy()) : json(policy({ revision: 1, status: "APPROVED", approved: true, configured: true, backfilled_count: 3 }));
  await mount();
  await click(document.querySelector('input[type="checkbox"]'));
  await reason();
  assert.equal(document.querySelector('button[type="submit"]').disabled, false);
  await submit();
  const write = calls.find(call => call.method === "PUT");
  assert.equal(write.headers.get("If-Match"), '"0"');
  assert.equal(write.headers.get("X-CSRF-Token"), "synthetic-csrf");
  assert.ok(write.headers.get("Idempotency-Key"));
  assert.equal(write.credentials, "include");
  assert.deepEqual(JSON.parse(write.body), { retention_days: 2, approved: true, purge_allowed_roles: ["admin", "owner"],
    reason: "用户已确认保留2天", backfill_trash: true, approval_expires_at: null });
  assert.match(document.body.textContent, /保存成功.*3 条/);
  assert.ok(calls.every(call => !String(call.url).includes("/purge")));
});

test("unapproved save remains unapproved and conflict blocks stale overwrite", async () => {
  handler = (url, init) => init.method === "GET" ? json(policy()) : json({ code: "REVISION_CONFLICT", message: "版本冲突" }, 412);
  await mount();
  await reason("保留当前审批草稿");
  await submit();
  assert.match(document.querySelector('[role="alert"]').textContent, /重新读取/);
  assert.equal(document.querySelector('button[type="submit"]').disabled, true);
  assert.equal(JSON.parse(calls[1].body).approved, false);
  assert.equal(document.querySelector("textarea").value, "保留当前审批草稿");
  await submit();
  assert.equal(calls.length, 2);
});

test("space switch ignores stale response and uses new server permissions", async () => {
  let finish;
  handler = url => String(url).includes("space-a") ? new Promise(resolve => { finish = resolve; }) : json(policy({ space_id: "space-b", permissions: { can_configure: false } }));
  await mount();
  await act(async () => root.render(h(AppContext.Provider, { value: { ...context, space: { id: "space-b" } } }, h(RetentionPanel))));
  await act(async () => finish(json(policy())));
  assert.equal(document.querySelector('button[type="submit"]').disabled, true);
  assert.match(document.body.textContent, /你只有查看权限/);
});

test("purge preflight lists concrete reasons and expiry; callback invalidates during refresh", async () => {
  const values = [];
  handler = () => json({ resource_id: "resource-a", eligible: false, can_request_purge: true, deleted_at: "2026-09-08T00:00:00Z",
    expiry: "2026-09-10T00:00:00Z", legal_hold: true, checked_at: "2026-09-08T01:00:00Z",
    reasons: [{ code: "LEGAL_HOLD", message: "资源处于法律保全中" }, { code: "RETENTION_NOT_EXPIRED", message: "删除后的保留期尚未届满" }] });
  await mount(h(PurgeEligibilityPanel, { resourceId: "resource-a", onEligibility: value => values.push(value) }));
  assert.match(document.body.textContent, /最早到期时间/);
  assert.match(document.body.textContent, /法律保全中/);
  assert.match(document.body.textContent, /保留期尚未届满/);
  assert.equal(values[0], null);
  assert.equal(values.at(-1).eligible, false);
  assert.ok(calls.every(call => call.method === "GET"));
});

test("failed preflight clears stale eligibility and displays server error", async () => {
  const values = [];
  handler = () => json({ code: "NOT_FOUND", message: "对象不存在或不可访问" }, 404);
  await mount(h(PurgeEligibilityPanel, { resourceId: "missing", onEligibility: value => values.push(value) }));
  assert.equal(values.at(-1), null);
  assert.match(document.querySelector('[role="alert"]').textContent, /不可访问/);
});
