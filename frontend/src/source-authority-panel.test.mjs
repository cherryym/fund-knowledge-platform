import assert from "node:assert/strict";
import { after, afterEach, beforeEach, test } from "node:test";
import { createRequire, Module } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const require = createRequire(import.meta.url);
const sourceDir = dirname(fileURLToPath(import.meta.url));
const { JSDOM } = require("jsdom");
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: "http://source-authority.test/", pretendToBeVisual: true,
});
for (const key of ["window", "document", "HTMLElement", "Element", "Node", "Event", "MouseEvent", "HTMLInputElement", "HTMLSelectElement", "HTMLTextAreaElement"])
  globalThis[key] = key === "window" ? dom.window : dom.window[key];
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
globalThis.getComputedStyle = window.getComputedStyle.bind(window);
window.matchMedia = media => ({ media, matches: media.includes("reduce"),
  addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });

let calls = [], unexpected = [], routes = new Map();
// No real API, URL or generation fallback, even when a test forgets a fixture.
const interceptedFetch = async (url, init = {}) => {
  const request = { url: String(url), ...init, method: init.method ?? "GET" };
  calls.push(request);
  const key = `${request.method} ${request.url}`;
  const allowed = /^GET \/api\/v1\/source-authority(?:\?space_id=[^&]+|\/suggestions\?space_id=[^&]+|\/[^/?]+)$/.test(key)
    || /^POST \/api\/v1\/source-authority(?:\/[^/?]+\/revoke)?$/.test(key)
    || /^GET \/api\/v1\/(?:resources|versions)(?:\?|\/)/.test(key)
    || /^GET \/api\/v1\/(?:spaces|libraries)$/.test(key);
  if (!allowed || !routes.has(key)) {
    unexpected.push(key);
    throw new Error(`Unmocked request blocked: ${key}`);
  }
  return routes.get(key)(request);
};
globalThis.fetch = dom.window.fetch = interceptedFetch;

const React = require("react");
const { act } = React;
const h = React.createElement;
const { createRoot } = require("react-dom/client");
const gsap = require("gsap").gsap;
const { ScrollTrigger } = require("gsap/dist/ScrollTrigger");
const compiled = await build({
  stdin: {
    contents: 'export { SourceAuthorityPanel } from "./SourceAuthorityPanel"; export { SettingsPage } from "./OperationsPages"; export { AppContext } from "./appContext"; export { setSession, clearSession } from "./api";',
    resolveDir: sourceDir, loader: "tsx",
  },
  bundle: true, platform: "node", format: "cjs", write: false, logLevel: "silent",
  external: ["react", "react-dom", "react-dom/*", "react/jsx-runtime", "gsap", "gsap/ScrollTrigger", "@gsap/react"],
  loader: { ".css": "empty" }, define: { "import.meta.env.DEV": "false" },
});
const runtime = new Module(resolve(sourceDir, "source-authority-test-runtime.cjs"));
runtime.filename = resolve(sourceDir, "source-authority-test-runtime.cjs");
runtime.paths = Module._nodeModulePaths(sourceDir);
const runtimeRequire = runtime.require.bind(runtime);
runtime.require = id => id === "gsap" ? gsap : id === "gsap/ScrollTrigger" ? { ScrollTrigger } : runtimeRequire(id);
runtime._compile(compiled.outputFiles[0].text, runtime.filename);
const { SourceAuthorityPanel, SettingsPage, AppContext, setSession, clearSession } = runtime.exports;
ScrollTrigger.disable(); gsap.ticker.sleep();

const json = (value, status = 200, headers = {}) => new Response(JSON.stringify(value), {
  status, headers: { "Content-Type": "application/json", ...headers },
});
const stub = (method, path, response) => routes.set(`${method} /api/v1${path}`,
  typeof response === "function" ? response : () => json(response));
const failed = (status, message = "合成请求失败") => () => json({ code: `SYNTHETIC_${status}`, message }, status);
const page = items => ({ items, next_cursor: null });
const deferred = () => {
  let resolvePromise;
  const promise = new Promise(resolve => { resolvePromise = resolve; });
  // Intentionally ignores AbortSignal to verify the component's late-result guards.
  return { promise, resolve: resolvePromise };
};
const record = (overrides = {}) => ({
  id: "relation-a", space_id: "space-a", revision: 1, state: "ACTIVE",
  predecessor_version_id: "old-v1", successor_version_id: "new-v1", evidence_version_id: "evidence-v1",
  predecessor_title: "合成旧规则", successor_title: "合成后继规则", evidence_title: "合成替代公告",
  effective_from: "2026-01-01", scope: "full", reason: "合成替代条款说明",
  created_at: "2026-09-12T01:00:00Z", created_by: "other-admin", revoked_at: null, revoke_reason: null,
  validation_state: "VALID", validation_note: "合成记录校验说明", ...overrides,
});
const listing = (items = [record()], can_manage = true) => ({ items, can_manage, notes: ["合成服务端说明"] });
const resource = (id, overrides = {}) => ({
  id, space_id: "space-a", kind: "document", name: `合成文档 ${id}`, category: "合成分类",
  revision: 2, access_epoch: 3, suspended: false, deleted_at: null, tags: [], ...overrides,
});
const version = (resourceId, no = 1, overrides = {}) => ({
  id: `${resourceId}-v${no}`, resource_id: resourceId, version_no: no, title: `合成 ${resourceId} V${no}`,
  revision: 4, state: "APPROVED", content_sha256: `synthetic-hash-${resourceId}-${no}`,
  blocks: [], legal_status: "UNKNOWN", source_verified: false, ...overrides,
});
let root, currentApp, currentComponent, opened, notifications, bumps;
const app = (overrides = {}) => ({
  me: { id: "synthetic-user", display_name: "合成操作人", csrf_token: "synthetic-csrf", is_admin: false },
  space: { id: "space-a", name: "合成空间甲", roles: ["admin"], revision: 1 }, refresh: 0,
  openResource: (...args) => opened.push(args), notify: message => notifications.push(message),
  bump: () => { bumps++; }, navigate() {}, openVersion() { throw new Error("Use guarded exact-version navigation"); }, ...overrides,
});
const body = () => document.body.textContent;
const writes = () => calls.filter(call => call.method !== "GET");
const listReads = () => calls.filter(call => /^\/api\/v1\/source-authority\?/.test(call.url));
const button = (name, within = document) => {
  const element = [...within.querySelectorAll("button")].find(item => item.textContent.trim() === name);
  assert.ok(element, `Missing button: ${name}`);
  return element;
};
const click = element => act(async () => element.click());
const form = type => document.querySelector(`form[aria-label="${type === "revoke" ? "撤销事实" : "确认替代事实"}表单"]`);
const submit = (type = "create") => act(async () => form(type).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
async function change(element, value) {
  assert.ok(element, "Input must exist");
  const prototype = element instanceof HTMLSelectElement ? HTMLSelectElement.prototype
    : element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, "value").set;
  await act(async () => { setter.call(element, value); element.dispatchEvent(new Event(
    element instanceof HTMLSelectElement ? "change" : "input", { bubbles: true })); });
}
const picker = index => form().querySelectorAll("section")[index];
async function choose(index, resourceId, versionId = `${resourceId}-v1`) {
  await change(picker(index).querySelectorAll("select")[0], resourceId);
  if (versionId) await change(picker(index).querySelectorAll("select")[1], versionId);
}
async function fillCreate({ scope = "full", reason = "合成替代条款说明", date = "2026-01-01", confirm = true } = {}) {
  await click(button("手动登记"));
  await choose(0, "old"); await choose(1, "new"); await choose(2, "evidence");
  await change(form().querySelector('input[type="date"]'), date);
  await change(form().querySelectorAll("select")[6], scope);
  await change(form().querySelector("textarea"), reason);
  if (confirm) await click(form().querySelector('input[type="checkbox"]'));
}
async function fillRevoke(reason = "合成撤销原因") {
  await click(button("撤销事实"));
  await change(form("revoke").querySelector("textarea"), reason);
  await click(form("revoke").querySelector('input[type="checkbox"]'));
}
async function mount(value = app(), component = SourceAuthorityPanel, strict = false) {
  currentApp = value; currentComponent = component;
  setSession(value.me);
  root = createRoot(document.getElementById("root"));
  await act(async () => root.render(h(AppContext.Provider, { value }, strict ? h(React.StrictMode, null, h(component)) : h(component))));
}
async function switchApp(value) {
  currentApp = value; setSession(value.me);
  await act(async () => root.render(h(AppContext.Provider, { value }, h(currentComponent))));
}
async function unmount() { if (root) await act(async () => root.unmount()); root = null; }
function createStub() {
  stub("POST", "/source-authority", request => {
    const { expected_sources, ...fields } = JSON.parse(request.body);
    const value = record(fields); // Bindings are input-only; the record never needs to echo them.
    stub("GET", "/source-authority?space_id=space-a", listing([value]));
    return json(value, 201);
  });
}
beforeEach(() => {
  clearSession(); window.localStorage.clear(); window.sessionStorage.clear();
  calls = []; unexpected = []; routes = new Map(); opened = []; notifications = []; bumps = 0;
  stub("GET", "/source-authority?space_id=space-a", listing([record({ effective_from: "2025-01-01" })]));
  stub("GET", "/source-authority/suggestions?space_id=space-a", listing([]));
  stub("GET", "/source-authority/suggestions?space_id=space-b", listing([], false));
  stub("GET", "/source-authority?space_id=space-b", listing([], false));
  stub("GET", "/source-authority/relation-a", () => json(record({ revision: 4 }), 200, { ETag: '"99"' }));
  const resources = ["old", "new", "evidence", "alternate"].map(id => resource(id));
  stub("GET", "/resources?space_id=space-a&limit=100", page(resources));
  stub("GET", "/resources?space_id=space-b&limit=100", page([]));
  for (const item of resources) {
    stub("GET", `/resources/${item.id}`, item);
    stub("GET", `/resources/${item.id}/versions?limit=100`, page([version(item.id, 2), version(item.id, 1)]));
    for (const no of [1, 2]) stub("GET", `/versions/${item.id}-v${no}`, version(item.id, no));
  }
});
afterEach(async () => {
  await unmount(); clearSession();
  assert.deepEqual(unexpected, [], "All network requests must have explicit mocks");
  assert.ok(writes().every(request => /^\/api\/v1\/source-authority(?:\/[^/?]+\/revoke)?$/.test(request.url)));
  assert.ok(calls.every(request => !/\/(?:models|connections|runs|threads|auth\/demo)(?:\/|\?|$)/.test(request.url)));
  assert.equal(globalThis.fetch, interceptedFetch); assert.equal(window.fetch, interceptedFetch);
  assert.equal(bumps, 0, "No document/Wiki/model cache invalidation");
  assert.equal(window.localStorage.length, 0); assert.equal(window.sessionStorage.length, 0);
});
after(() => { ScrollTrigger.disable(); gsap.globalTimeline.clear(); gsap.ticker.sleep(); dom.window.close(); });

test("opening only reads the current visible relationships, including all 257 records and plain untrusted notes", async () => {
  stub("GET", "/source-authority?space_id=space-a", { ...listing(Array.from({ length: 257 }, (_, i) => record({ id: `r-${i}` }))),
    notes: ['<img src="https://invalid.test/tracker">', "https://invalid.test/extra"] });
  await mount(app(), SourceAuthorityPanel, true);
  assert.equal(document.querySelectorAll(".source-authority-record").length, 257);
  assert.match(body(), /当前可见 257 条关系/);
  assert.match(body(), /不代表整份法规现行有效/); assert.match(body(), /不等于独立专家审核/);
  assert.equal(document.querySelectorAll("img,a,iframe").length, 0);
  assert.ok(calls.every(request => /^\/api\/v1\/source-authority(?:\/suggestions)?\?space_id=space-a$/.test(request.url)));
  assert.equal(writes().length, 0);
});

test("only can_manage grants write controls, even when the client context claims platform admin", async () => {
  stub("GET", "/source-authority?space_id=space-a", listing([record()], false));
  await mount(app({ me: { ...app().me, is_admin: true } }));
  assert.equal(button("手动登记").disabled, true); assert.equal(button("撤销事实").disabled, true);
  await click(button("手动登记")); await click(button("撤销事实"));
  assert.equal(document.querySelectorAll("form,input,select").length, 0);
  assert.match(body(), /只有空间管理员/); assert.equal(writes().length, 0);
});

test("empty relations, stale/unavailable checks, partial scope, revocation and future dates remain distinct", async () => {
  stub("GET", "/source-authority?space_id=space-a", listing([
    record({ id: "future", effective_from: "9999-01-01" }),
    record({ id: "partial", scope: "partial", validation_state: "STALE" }),
    record({ id: "unavailable", validation_state: "UNAVAILABLE" }),
    record({ id: "unknown", validation_state: undefined }),
    record({ id: "revoked", state: "REVOKED", revoke_reason: "保留撤销历史", revoked_at: "2026-09-12T02:00:00Z" }),
  ]));
  await mount();
  assert.match(body(), /未来日期 · 尚不适用/); assert.match(body(), /不能据此整份排除旧文/);
  assert.match(body(), /来源不可用 · 待复核/); assert.match(body(), /记录待复核/);
  assert.match(body(), /保留撤销历史/); assert.match(body(), /other-admin/);
  assert.equal([...document.querySelectorAll("button")].filter(item => item.textContent === "撤销事实").length, 4);
  stub("GET", "/source-authority?space_id=space-a", listing([]));
  await click(button("刷新关系"));
  assert.match(body(), /没有已登记关系不代表法规现行有效/);
});

for (const [name, value] of [
  ["missing permission", { items: [], notes: [] }], ["truthy permission", { items: [], can_manage: "true", notes: [] }],
  ["wrong space", listing([record({ space_id: "space-secret", predecessor_title: "SECRET TITLE" })])],
  ["duplicate identities", listing([record(), record()])], ["missing revision", listing([record({ revision: undefined })])],
]) test(`malformed list fails closed: ${name}`, async () => {
  stub("GET", "/source-authority?space_id=space-a", value); await mount();
  assert.ok(document.querySelector('[role="alert"]')); assert.equal(document.querySelectorAll("form,.source-authority-record").length, 0);
  assert.doesNotMatch(body(), /SECRET TITLE/); assert.equal(writes().length, 0);
});

for (const status of [401, 403, 404, 500]) test(`list error ${status} removes old data and only manual refresh restores controls`, async () => {
  await mount(); await fillCreate();
  stub("GET", "/source-authority?space_id=space-a", failed(status));
  await click(button("刷新关系"));
  assert.equal(document.querySelectorAll("form,.source-authority-record").length, 0);
  assert.ok(document.querySelector('[role="alert"]')); assert.equal(writes().length, 0);
  stub("GET", "/source-authority?space_id=space-a", listing([], false));
  await click(button("刷新关系")); assert.equal(button("手动登记").disabled, true);
});

test("create requires exact versions, date, scope, nonblank reason and the explicit evidence acknowledgment", async () => {
  await mount(); await fillCreate({ confirm: false });
  assert.equal(button("提交确认").disabled, true); await submit(); assert.equal(writes().length, 0);
  await click(form().querySelector('input[type="checkbox"]'));
  assert.equal(button("提交确认").disabled, false);
  for (const [selector, empty, restore] of [['textarea', "   ", "合成替代条款说明"], ['input[type="date"]', "", "2026-01-01"]]) {
    await change(form().querySelector(selector), empty); await click(form().querySelector('input[type="checkbox"]'));
    await submit(); assert.equal(writes().length, 0); assert.equal(button("提交确认").disabled, true);
    await change(form().querySelector(selector), restore);
  }
  await change(form().querySelectorAll("select")[6], ""); await click(form().querySelector('input[type="checkbox"]'));
  await submit(); assert.equal(writes().length, 0);
  assert.equal(document.querySelectorAll('input[type="text"]').length, 0, "No manual UUID field");
});

test("creation sends the chosen older versions, trimmed reason, CSRF and idempotency; then refreshes without modifying source content", async () => {
  createStub(); await mount(); await fillCreate({ scope: "partial", reason: "  合成替代条款说明  " });
  const before = listReads().length;
  await submit();
  assert.equal(writes().length, 1);
  const request = writes()[0];
  assert.deepEqual(JSON.parse(request.body), { space_id: "space-a", predecessor_version_id: "old-v1", successor_version_id: "new-v1",
    evidence_version_id: "evidence-v1", effective_from: "2026-01-01", scope: "partial", reason: "合成替代条款说明" });
  assert.equal(request.headers.get("X-CSRF-Token"), "synthetic-csrf");
  assert.ok(request.headers.get("Idempotency-Key")); assert.equal(request.headers.get("If-Match"), null);
  assert.equal(request.credentials, "include");
  assert.equal(listReads().length, before + 1); assert.equal(form(), null);
  assert.match(body(), /替代事实已确认/); assert.match(body(), /部分替代/);
  assert.ok(calls.filter(item => /\/versions\//.test(item.url)).every(item => item.method === "GET"));
});

test("changing a resource clears its old version and acknowledgment; changing a version also clears acknowledgment", async () => {
  await mount(); await fillCreate();
  await choose(0, "alternate", "");
  assert.equal(picker(0).querySelectorAll("select")[1].value, "");
  assert.equal(form().querySelector('input[type="checkbox"]').checked, false);
  await submit(); assert.equal(writes().length, 0);
  await change(picker(0).querySelectorAll("select")[1], "alternate-v1");
  await click(form().querySelector('input[type="checkbox"]')); assert.equal(button("提交确认").disabled, false);
  await change(picker(0).querySelectorAll("select")[1], "alternate-v2");
  assert.equal(form().querySelector('input[type="checkbox"]').checked, false); assert.equal(button("提交确认").disabled, true);
});

test("the same predecessor and successor version is rejected, but evidence may be the successor document", async () => {
  createStub(); await mount(); await fillCreate(); await choose(1, "old");
  await click(form().querySelector('input[type="checkbox"]')); await submit();
  assert.equal(writes().length, 0); assert.match(body(), /不能选择同一个版本/);
  await choose(1, "new"); await choose(2, "new"); await click(form().querySelector('input[type="checkbox"]'));
  await submit(); assert.equal(JSON.parse(writes()[0].body).evidence_version_id, "new-v1");
});

test("knowledge pages and a mismatched resource/version cannot serve as document evidence", async () => {
  stub("GET", "/resources?space_id=space-a&limit=100", page([resource("old"), resource("evidence", { kind: "knowledge" })]));
  await mount(); await click(button("手动登记")); await choose(2, "evidence");
  assert.match(body(), /知识页不能代替证据原件/); assert.equal(button("提交确认").disabled, true);
  stub("GET", "/resources/old/versions?limit=100", page([version("old", 1, { resource_id: "another" })]));
  await choose(0, "old"); assert.equal(button("提交确认").disabled, true); assert.equal(writes().length, 0);
});

test("existing pickers traverse every resource/version page, retaining older exact versions", async () => {
  stub("GET", "/resources?space_id=space-a&limit=100", { items: [resource("old")], next_cursor: "page-2" });
  stub("GET", "/resources?space_id=space-a&limit=100&cursor=page-2", page([resource("new"), resource("evidence")]));
  stub("GET", "/resources/old/versions?limit=100", { items: [version("old", 2)], next_cursor: "older" });
  stub("GET", "/resources/old/versions?limit=100&cursor=older", page([version("old", 1)]));
  await mount(); await fillCreate();
  assert.equal(picker(0).querySelectorAll("select")[1].value, "old-v1");
  assert.equal(button("提交确认").disabled, false); assert.equal(writes().length, 0);
});

for (const [endpoint, changed] of [["old", { revision: 8 }], ["new", { content_sha256: "changed" }], ["evidence", { revision: 9 }]])
  test(`preflight source change clears all selections without writing: ${endpoint}`, async () => {
    await mount(); await fillCreate(); stub("GET", `/versions/${endpoint}-v1`, version(endpoint, 1, changed));
    await submit(); assert.equal(writes().length, 0); assert.match(body(), /来源版本、内容或权限已变化/);
    assert.equal(form().querySelector('input[type="checkbox"]').checked, false);
    for (let index = 0; index < 3; index++) assert.equal(picker(index).querySelectorAll("select")[0].value, "");
  });

test("changed access epoch and a cross-space resource are rejected at the source boundary", async () => {
  await mount(); await fillCreate();
  stub("GET", "/resources/old", resource("old", { access_epoch: 4 })); await submit();
  assert.equal(writes().length, 0); assert.match(body(), /权限已变化/);
  await click(button("取消确认")); await fillCreate();
  stub("GET", "/resources/old", resource("old", { space_id: "space-secret" })); await submit();
  assert.equal(writes().length, 0); assert.match(body(), /当前空间/); assert.doesNotMatch(body(), /space-secret/);
});

for (const status of [401, 403, 404]) test(`source permission failure ${status} clears record titles, form and choices`, async () => {
  await mount(); await fillCreate(); stub("GET", "/versions/evidence-v1", failed(status, "HIDDEN DETAILS"));
  await submit(); assert.equal(writes().length, 0);
  assert.equal(document.querySelectorAll("form,.source-authority-record").length, 0);
  assert.doesNotMatch(body(), /合成旧规则|HIDDEN DETAILS/); assert.match(body(), /已清除旧结果和选择/);
});

for (const status of [403, 404]) test(`write permission failure ${status} removes all management and readable stale data`, async () => {
  stub("POST", "/source-authority", failed(status)); await mount(); await fillCreate(); await submit();
  assert.equal(writes().length, 1); assert.equal(document.querySelectorAll("form,.source-authority-record").length, 0);
  assert.match(body(), /已清除旧结果和选择/);
});

for (const status of [409, 412, 422]) test(`create conflict or validation failure ${status} cannot reuse an old evidence confirmation`, async () => {
  stub("POST", "/source-authority", failed(status)); await mount(); await fillCreate(); await submit();
  assert.equal(writes().length, 1); assert.equal(button("提交确认").disabled, true);
  assert.equal(form().querySelector('input[type="checkbox"]').checked, false);
  await submit(); assert.equal(writes().length, 1);
});

test("ambiguous network failure retains the explicit retry key and never automatically replays a creation", async () => {
  stub("POST", "/source-authority", () => { throw new Error("synthetic offline"); });
  await mount(); await fillCreate(); await submit();
  assert.equal(writes().length, 1); assert.match(body(), /操作结果尚未确认/);
  assert.equal(form().querySelector("textarea").value, "合成替代条款说明");
  createStub(); await submit(); assert.equal(writes().length, 2);
  assert.equal(writes()[0].headers.get("Idempotency-Key"), writes()[1].headers.get("Idempotency-Key"));
});

test("synchronous duplicate submits share one creation and stay locked while preflight or POST is pending", async () => {
  const pending = deferred(); stub("POST", "/source-authority", () => pending.promise);
  await mount(); await fillCreate();
  await act(async () => { for (let index = 0; index < 3; index++) form().dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
  assert.equal(writes().length, 1); assert.equal(form().querySelector("fieldset").disabled, true);
  await act(async () => pending.resolve(json(record(), 201)));
  assert.equal(form(), null);
});

test("a successful HTTP response with mismatched source data is not reported as success or submitted twice", async () => {
  stub("POST", "/source-authority", () => json(record({ successor_version_id: "unrelated" }), 201));
  await mount(); await fillCreate(); await submit();
  assert.doesNotMatch(body(), /替代事实已确认/); assert.match(body(), /回执与提交内容不一致/);
  assert.equal(button("提交确认").disabled, true); await submit(); assert.equal(writes().length, 1);
});

test("revoke reads the record, requires a reason and confirmation, and uses its revision rather than a cached ETag", async () => {
  stub("POST", "/source-authority/relation-a/revoke", request => {
    const value = record({ revision: 5, state: "REVOKED", revoke_reason: JSON.parse(request.body).reason });
    stub("GET", "/source-authority?space_id=space-a", listing([value])); return json(value);
  });
  await mount(); await click(button("撤销事实")); await submit("revoke"); assert.equal(writes().length, 0);
  await change(form("revoke").querySelector("textarea"), "  合成撤销原因  ");
  await submit("revoke"); assert.equal(writes().length, 0);
  await click(form("revoke").querySelector('input[type="checkbox"]')); await submit("revoke");
  const request = writes()[0]; assert.deepEqual(JSON.parse(request.body), { reason: "合成撤销原因" });
  assert.equal(request.headers.get("If-Match"), '"4"'); assert.ok(request.headers.get("Idempotency-Key"));
  assert.equal(request.headers.get("X-CSRF-Token"), "synthetic-csrf");
  assert.equal(form("revoke"), null); assert.match(body(), /治理记录已停用并保留历史/); assert.match(body(), /合成撤销原因/);
});

test("412 preserves the revoke reason, blocks stale resubmission, and requires re-reading and re-confirming the new revision", async () => {
  stub("POST", "/source-authority/relation-a/revoke", failed(412));
  await mount(); await fillRevoke(); await submit("revoke");
  assert.equal(writes().length, 1); assert.equal(form("revoke").querySelector("textarea").value, "合成撤销原因");
  assert.match(body(), /记录已变化/); assert.equal(button("确认撤销").disabled, true);
  await submit("revoke"); assert.equal(writes().length, 1);
  stub("GET", "/source-authority/relation-a", () => json(record({ revision: 7 }), 200, { ETag: '"7"' }));
  await click(button("重新读取记录")); assert.equal(button("确认撤销").disabled, true);
  stub("POST", "/source-authority/relation-a/revoke", request => json(record({ revision: 8, state: "REVOKED", revoke_reason: JSON.parse(request.body).reason })));
  await click(form("revoke").querySelector('input[type="checkbox"]')); await submit("revoke");
  assert.equal(writes().length, 2); assert.equal(writes()[1].headers.get("If-Match"), '"7"');
  assert.notEqual(writes()[0].headers.get("Idempotency-Key"), writes()[1].headers.get("Idempotency-Key"));
});

for (const status of [403, 404, 503]) test(`revoke detail error ${status} cannot submit and never reuses the list revision`, async () => {
  stub("GET", "/source-authority/relation-a", failed(status)); await mount(); await click(button("撤销事实"));
  if (status === 503) { assert.equal(button("确认撤销").disabled, true); await submit("revoke"); }
  else assert.equal(form("revoke"), null);
  assert.equal(writes().length, 0); assert.ok(document.querySelector('[role="alert"]'));
});

test("an already revoked detail cannot be revoked again and cancellation never writes", async () => {
  stub("GET", "/source-authority/relation-a", record({ state: "REVOKED" }));
  await mount(); await click(button("撤销事实")); assert.match(body(), /记录已撤销，无需重复提交/);
  assert.equal(button("确认撤销").disabled, true); await click(button("取消撤销")); assert.equal(form("revoke"), null);
  await click(button("手动登记")); await click(button("取消确认")); assert.equal(form(), null); assert.equal(writes().length, 0);
});

test("revoke confirmation shows the freshly read dates, scope, reason and exact endpoints", async () => {
  stub("GET", "/source-authority/relation-a", record({ scope: "partial", effective_from: "2027-02-03",
    reason: "最新修订的替代范围说明", evidence_version_id: "evidence-v2" }));
  await mount(); await click(button("撤销事实"));
  const text = form("revoke").textContent;
  assert.match(text, /2027-02-03/); assert.match(text, /部分替代/);
  assert.match(text, /最新修订的替代范围说明/); assert.match(text, /evidence-v2/);
});

test("a malformed revoke receipt is not reported as success and needs a fresh record before another submission", async () => {
  stub("POST", "/source-authority/relation-a/revoke", () => json(record({ revision: 5, state: "ACTIVE" })));
  await mount(); await fillRevoke(); await submit("revoke");
  assert.equal(button("确认撤销").disabled, true); assert.doesNotMatch(body(), /治理记录已停用并保留历史/);
  await submit("revoke"); assert.equal(writes().length, 1);
  stub("GET", "/source-authority/relation-a", record({ revision: 5, state: "REVOKED" }));
  await click(button("重新读取记录")); assert.equal(button("确认撤销").disabled, true);
  assert.match(body(), /记录已撤销，无需重复提交/);
});

test("a failed refresh after a committed create reports the write separately and hides unverified list controls", async () => {
  stub("POST", "/source-authority", () => {
    stub("GET", "/source-authority?space_id=space-a", failed(503)); return json(record(), 201);
  });
  await mount(); await fillCreate(); await submit();
  assert.match(body(), /替代事实已确认/); assert.match(body(), /合成请求失败/);
  assert.equal(document.querySelectorAll("form,.source-authority-record").length, 0);
  assert.equal(writes().length, 1);
});

test("refresh after an access failure does not expose the old records while a new read is pending", async () => {
  await mount(); stub("GET", "/versions/old-v1", failed(404)); await click(button("合成旧规则"));
  const pending = deferred(); stub("GET", "/source-authority?space_id=space-a", () => pending.promise);
  await click(button("刷新关系")); assert.equal(document.querySelectorAll(".source-authority-record").length, 0);
  await act(async () => pending.resolve(json(listing([], false))));
  assert.equal(button("手动登记").disabled, true);
});

test("a late list response cannot paint titles or admin permissions after switching spaces", async () => {
  const pending = deferred(); stub("GET", "/source-authority?space_id=space-a", () => pending.promise);
  await mount(); const old = listReads()[0];
  await switchApp(app({ space: { id: "space-b", name: "合成空间乙", roles: ["reader"], revision: 1 } }));
  assert.equal(old.signal.aborted, true);
  await act(async () => pending.resolve(json(listing([record({ predecessor_title: "LATE PRIVATE TITLE" })]))));
  assert.doesNotMatch(body(), /LATE PRIVATE TITLE|合成空间甲/); assert.equal(button("手动登记").disabled, true);
});

for (const contextChange of ["space", "user", "permission", "revision", "refresh"]) test(`identity ${contextChange} change clears the full form and selected sources`, async () => {
  await mount(); await fillCreate();
  const value = app();
  if (contextChange === "space") value.space = { ...value.space, id: "space-b", name: "合成空间乙" };
  if (contextChange === "user") value.me = { ...value.me, id: "another-user", csrf_token: "other-synthetic-csrf" };
  if (contextChange === "permission") value.space = { ...value.space, roles: ["reader"] };
  if (contextChange === "revision") value.space = { ...value.space, revision: 2 };
  if (contextChange === "refresh") value.refresh = 1;
  await switchApp(value); assert.equal(form(), null); assert.equal(writes().length, 0);
});

test("pending preflight is aborted by a space change and cannot issue a POST even if all responses arrive late", async () => {
  await mount(); await fillCreate(); const pending = deferred();
  stub("GET", "/versions/old-v1", () => pending.promise);
  await submit(); const request = calls.filter(item => item.url.endsWith("/versions/old-v1")).at(-1);
  await switchApp(app({ space: { id: "space-b", name: "合成空间乙", revision: 1, roles: ["reader"] } }));
  assert.equal(request.signal.aborted, true);
  await act(async () => pending.resolve(json(version("old")))); assert.equal(writes().length, 0); assert.equal(form(), null);
});

test("pending creation is aborted on space change; a late success cannot show a toast or refresh the new space", async () => {
  const pending = deferred(); stub("POST", "/source-authority", () => pending.promise);
  await mount(); await fillCreate(); await submit();
  await switchApp(app({ space: { id: "space-b", name: "合成空间乙", revision: 1, roles: ["reader"] } }));
  const reads = listReads().length; assert.equal(writes()[0].signal.aborted, true);
  await act(async () => pending.resolve(json(record(), 201)));
  assert.equal(listReads().length, reads); assert.doesNotMatch(body(), /替代事实已确认/); assert.deepEqual(notifications, []);
});

test("late revoke failure after an identity change cannot attach an error to the new scope", async () => {
  const pending = deferred(); stub("POST", "/source-authority/relation-a/revoke", () => pending.promise);
  await mount(); await fillRevoke(); await submit("revoke");
  await switchApp(app({ me: { ...app().me, id: "new-user" } }));
  assert.equal(writes()[0].signal.aborted, true);
  await act(async () => pending.resolve(json({ message: "OLD USER ERROR" }, 500)));
  assert.doesNotMatch(body(), /OLD USER ERROR/); assert.equal(form("revoke"), null);
});

test("session expiry removes data immediately and aborts pending navigation before a late response", async () => {
  await mount(); const pending = deferred(); stub("GET", "/versions/old-v1", () => pending.promise);
  await click(button("合成旧规则"));
  await act(async () => window.dispatchEvent(new Event("session-expired")));
  assert.equal(document.querySelectorAll(".source-authority-record").length, 0);
  await act(async () => pending.resolve(json(version("old")))); assert.deepEqual(opened, []);
  assert.match(body(), /已清除旧结果和选择/);
});

test("opening a source rechecks its resource and opens the exact version without jumping to latest", async () => {
  await mount(); await click(button("合成旧规则"));
  assert.equal(opened.length, 1); assert.equal(opened[0][0].id, "old");
  assert.deepEqual(opened[0].slice(1), ["preview", "old-v1", undefined]); assert.equal(writes().length, 0);
});

test("a changed source selection aborts opening the previously selected source", async () => {
  await mount(); await fillCreate(); const pending = deferred(); stub("GET", "/versions/old-v1", () => pending.promise);
  await click(button("核对所选旧规则文档")); await choose(0, "alternate");
  await act(async () => pending.resolve(json(version("old")))); assert.deepEqual(opened, []);
});

test("an unmounted panel aborts pending reads and ignores late failures", async () => {
  const pending = deferred(); stub("GET", "/source-authority?space_id=space-a", () => pending.promise);
  await mount(); const request = calls[0]; await unmount(); assert.equal(request.signal.aborted, true);
  await act(async () => pending.resolve(json({ message: "OLD ERROR" }, 500))); assert.doesNotMatch(body(), /OLD ERROR/);
});

test("Settings adds the rules tab; its data is only requested after explicit tab selection", async () => {
  stub("GET", "/spaces", []); stub("GET", "/libraries", { items: [] });
  await mount(app(), SettingsPage); assert.equal(listReads().length, 0);
  await click(button("规则效力")); assert.equal(listReads().length, 1);
  assert.ok(document.querySelector('.source-authority-panel')); assert.equal(writes().length, 0);
  await click(button("知识库管理")); assert.equal(document.querySelector('.source-authority-panel'), null);
});

const binding = (id, overrides = {}) => ({ version_id: `${id}-v1`, revision: 4, access_epoch: 3,
  content_sha256: ({ old: "a", new: "b", evidence: "c", alternate: "d" }[id] ?? "e").repeat(64), ...overrides });
const suggestion = (overrides = {}) => ({
  id: "suggestion-ready", space_id: "space-a", status: "READY", origin: "source_text",
  existing_record_id: null, existing_validation_state: null,
  predecessor_version_id: "old-v1", successor_version_id: "new-v1", evidence_version_id: "evidence-v1",
  predecessor_title: "合成旧规则", successor_title: "合成后继规则", evidence_title: "合成替代公告",
  effective_from: "2026-01-01", scope: "full", reason: "从公告原文带入的替代说明",
  missing_fields: [], expected_sources: [binding("old"), binding("new"), binding("evidence")],
  excerpts: [{ version_id: "evidence-v1", block_id: "evidence-block", text: "合成公告：旧规则自指定日期被替代。", locator: { label: "公告第二条" } }],
  ...overrides,
});
const suggestionReads = () => calls.filter(call => call.url.includes("/source-authority/suggestions?"));
const prefill = () => document.querySelector('form[aria-label="预填替代事实表单"]');
const confirmPrefill = () => act(async () => prefill().dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
const suggestionsStub = (items = [suggestion()], canManage = true) => stub("GET", "/source-authority/suggestions?space_id=space-a", listing(items, canManage));
async function openReady(value = suggestion()) {
  suggestionsStub([value]); await mount(); await click(button("核对替代建议"));
}
const prefillSource = index => prefill().querySelectorAll(".source-authority-prefill-source")[index];
async function choosePrefill(index, resourceId, versionId = `${resourceId}-v1`) {
  await change(prefillSource(index).querySelectorAll("select")[0], resourceId);
  await change(prefillSource(index).querySelectorAll("select")[1], versionId);
}

test("prefill: page reads suggestions by default; READY opens all fields with no selectors, attestation checkbox or POST", async () => {
  suggestionsStub(); await mount(app(), SourceAuthorityPanel, true);
  assert.ok(suggestionReads().length >= 1); assert.equal(prefill(), null);
  await click(button("核对替代建议"));
  assert.equal(prefill().querySelectorAll("input,textarea,select").length, 0);
  for (const expected of ["合成旧规则", "合成后继规则", "合成替代公告", "2026-01-01", "全部替代", "从公告原文带入的替代说明", "合成公告", "公告第二条"])
    assert.ok(prefill().textContent.includes(expected), expected);
  assert.equal(button("确认替代事实").disabled, false);
  assert.equal(calls.filter(request => /\/(resources|versions)(?:\/|\?)/.test(request.url)).length, 0);
  assert.equal(writes().length, 0); assert.match(prefill().textContent, /尚未由你确认/);
});

test("prefill: one explicit confirmation posts original fields and exact server bindings, accepting a response with no binding echo", async () => {
  createStub(); await openReady(); await click(button("确认替代事实"));
  assert.equal(writes().length, 1);
  const request = writes()[0], payload = JSON.parse(request.body);
  assert.deepEqual(payload, { space_id: "space-a", predecessor_version_id: "old-v1", successor_version_id: "new-v1",
    evidence_version_id: "evidence-v1", effective_from: "2026-01-01", scope: "full", reason: "从公告原文带入的替代说明",
    expected_sources: suggestion().expected_sources });
  assert.ok(request.headers.get("Idempotency-Key")); assert.equal(request.headers.get("X-CSRF-Token"), "synthetic-csrf");
  assert.equal(request.headers.get("If-Match"), null);
  assert.match(body(), /替代事实已确认/); assert.equal(prefill(), null);
  assert.equal(suggestionReads().length, 2); assert.equal(listReads().length, 3);
});

test("prefill: READY wins over earlier incomplete and confirmed items, with lightweight selection retaining every suggestion", async () => {
  const confirmed = suggestion({ id: "confirmed", status: "CONFIRMED", origin: "registered_fact", existing_record_id: "relation-a", existing_validation_state: "VALID" });
  suggestionsStub([suggestion({ id: "incomplete", status: "NEEDS_INPUT", effective_from: null, missing_fields: ["effective_from"] }), confirmed, suggestion()]);
  await mount(); await click(button("核对替代建议"));
  const select = document.querySelector('[aria-label="选择替代建议"]');
  assert.equal(select.value, "suggestion-ready"); assert.equal(select.options.length, 4);
  await change(select, "incomplete"); assert.ok(prefill().querySelector('input[type="date"]'));
  await change(select, "confirmed"); assert.match(prefill().textContent, /已确认，无需重复提交/);
  assert.equal(writes().length, 0);
});

test("prefill: the actual no-READY shape (one CONFIRMED and five NEEDS_INPUT) defaults to complete confirmed information", async () => {
  const incomplete = Array.from({ length: 5 }, (_, index) => suggestion({ id: `incomplete-${index}`, status: "NEEDS_INPUT",
    predecessor_version_id: null, missing_fields: ["predecessor_version_id"], expected_sources: [binding("new"), binding("evidence")] }));
  suggestionsStub([...incomplete, suggestion({ id: "confirmed", status: "CONFIRMED", origin: "registered_fact",
    existing_record_id: "relation-a", existing_validation_state: "VALID" })]);
  await mount(); await click(button("查看已确认信息"));
  assert.equal(document.querySelector('[aria-label="选择替代建议"]').value, "confirmed");
  assert.equal(document.querySelector('[aria-label="选择替代建议"]').options.length, 7);
  assert.equal(prefill().querySelectorAll("input,select,textarea").length, 0);
  assert.match(prefill().textContent, /合成旧规则/); assert.match(prefill().textContent, /合成后继规则/);
  assert.match(prefill().textContent, /2026-01-01/); assert.match(prefill().textContent, /公告第二条/);
  assert.equal(button("已确认，无需重复提交").disabled, true); await confirmPrefill(); assert.equal(writes().length, 0);
});

test("prefill: clicking an existing record fetches fresh fields including reason, revision, operator and date, without duplication", async () => {
  stub("GET", "/source-authority/relation-a", record({ effective_from: "2027-02-03", scope: "partial", revision: 8,
    created_by: "fresh-admin", reason: "最新确认说明", evidence_version_id: "evidence-v2" }));
  await mount(); await click(button("查看已确认要素"));
  for (const expected of ["2027-02-03", "部分替代", "fresh-admin", "最新确认说明", "evidence-v2", "8"])
    assert.ok(prefill().textContent.includes(expected), expected);
  assert.equal(prefill().querySelectorAll("input,select,textarea").length, 0);
  await confirmPrefill(); assert.equal(writes().length, 0);
});

test("prefill: stale confirmed records stay view-only, and stale suggestion excerpts cannot replace a newer record's evidence", async () => {
  suggestionsStub([suggestion({ status: "CONFIRMED", existing_record_id: "relation-a", existing_validation_state: "STALE" })]);
  stub("GET", "/source-authority/relation-a", record({ validation_state: "STALE", evidence_version_id: "evidence-v2" }));
  await mount(); await click(button("查看已确认信息"));
  assert.match(prefill().textContent, /来源仍需复核/); assert.doesNotMatch(prefill().textContent, /合成公告：/);
  await confirmPrefill(); assert.equal(writes().length, 0);
});

test("prefill: NEEDS_INPUT asks only for the missing predecessor version and keeps all known fields", async () => {
  createStub(); await openReady(suggestion({ status: "NEEDS_INPUT", predecessor_version_id: null,
    missing_fields: ["predecessor_version_id"], expected_sources: [binding("new"), binding("evidence")] }));
  assert.equal(prefill().querySelectorAll("select").length, 2);
  assert.equal(prefill().querySelectorAll('input[type="date"],textarea').length, 0);
  assert.match(prefill().textContent, /2026-01-01/); assert.match(prefill().textContent, /合成后继规则/);
  assert.equal(button("确认替代事实").disabled, true);
  await choosePrefill(0, "old"); assert.equal(prefill().querySelectorAll("select").length, 0);
  await click(button("确认替代事实"));
  const payload = JSON.parse(writes()[0].body);
  assert.equal(payload.predecessor_version_id, "old-v1"); assert.equal(payload.successor_version_id, "new-v1");
  assert.equal(payload.effective_from, "2026-01-01");
  assert.equal("expected_sources" in payload, false, "Do not send a stale partial binding set after an explicit source completion");
});

test("prefill: missing date and scope alone are editable; no upload/source date is substituted", async () => {
  createStub(); await openReady(suggestion({ status: "NEEDS_INPUT", effective_from: null, scope: null, missing_fields: ["effective_from", "scope"] }));
  const date = prefill().querySelector('input[type="date"]'); assert.equal(date.value, "");
  assert.equal(prefill().querySelectorAll("select").length, 1);
  assert.equal(prefill().querySelectorAll('input[placeholder="按资料名称筛选"]').length, 0);
  await confirmPrefill(); assert.equal(writes().length, 0);
  await change(date, "2030-02-03"); await change(prefill().querySelector("select"), "partial");
  await click(button("确认替代事实"));
  assert.equal(JSON.parse(writes()[0].body).effective_from, "2030-02-03");
  assert.equal(JSON.parse(writes()[0].body).scope, "partial");
  assert.deepEqual(JSON.parse(writes()[0].body).expected_sources, suggestion().expected_sources);
});

test("prefill: explicit date/scope/reason adjustments keep source bindings and do not write until confirmation", async () => {
  createStub(); await openReady();
  await click(button("修改日期")); await change(prefill().querySelector('input[type="date"]'), "2030-01-01");
  await click(button("修改范围")); await change(prefill().querySelector("select"), "partial");
  await click(button("修改说明")); await change(prefill().querySelector("textarea"), "  用户修正的部分范围  ");
  assert.equal(writes().length, 0); await click(button("确认替代事实"));
  const payload = JSON.parse(writes()[0].body);
  assert.equal(payload.reason, "用户修正的部分范围"); assert.equal(payload.scope, "partial");
  assert.equal(payload.effective_from, "2030-01-01"); assert.deepEqual(payload.expected_sources, suggestion().expected_sources);
});

test("prefill: changing one source opens only its picker, removes unrelated excerpts and never forwards the old binding set", async () => {
  createStub(); await openReady(); await click(button("更换替代证据文档"));
  assert.equal(prefill().querySelectorAll("select").length, 2); assert.equal(button("确认替代事实").disabled, true);
  await choosePrefill(2, "alternate");
  assert.doesNotMatch(prefill().textContent, /合成公告：/); assert.match(prefill().textContent, /来源已手动调整/);
  assert.equal(prefill().querySelectorAll("select").length, 0);
  await click(button("确认替代事实")); const payload = JSON.parse(writes()[0].body);
  assert.equal(payload.evidence_version_id, "alternate-v1"); assert.equal("expected_sources" in payload, false);
  assert.equal(payload.predecessor_version_id, "old-v1");
});

test("prefill: cancelling a source change preserves the original choice and original optimistic binding", async () => {
  createStub(); await openReady(); await click(button("更换旧规则文档"));
  await change(prefillSource(0).querySelectorAll("select")[0], "alternate");
  await click(button("保留原旧规则文档")); assert.equal(button("确认替代事实").disabled, false);
  await click(button("确认替代事实"));
  assert.equal(JSON.parse(writes()[0].body).predecessor_version_id, "old-v1");
  assert.deepEqual(JSON.parse(writes()[0].body).expected_sources, suggestion().expected_sources);
});

for (const status of [409, 412]) test(`prefill: stale ${status} preserves visible fields, withdraws excerpts and reloads prefill instead of three blank selectors`, async () => {
  stub("POST", "/source-authority", () => json({ code: "SOURCE_AUTHORITY_PREFILL_STALE", message: "建议绑定已变化" }, status));
  await openReady(); await click(button("修改说明")); await change(prefill().querySelector("textarea"), "用户调整的说明");
  await click(button("确认替代事实")); assert.equal(writes().length, 1);
  assert.equal(button("确认替代事实").disabled, true); assert.equal(prefill().querySelectorAll("select").length, 0);
  assert.equal(prefill().querySelector("textarea").value, "用户调整的说明");
  assert.match(prefill().textContent, /合成旧规则/); assert.doesNotMatch(prefill().textContent, /合成公告：/);
  await confirmPrefill(); assert.equal(writes().length, 1);
  const fresh = suggestion({ reason: "刷新后的说明", expected_sources: [binding("old", { revision: 5 }), binding("new"), binding("evidence")] });
  suggestionsStub([fresh]); stub("GET", "/versions/old-v1", version("old", 1, { revision: 5 }));
  await click(button("重新读取建议"));
  assert.match(prefill().textContent, /刷新后的说明/); assert.equal(prefill().querySelectorAll("input,textarea,select").length, 0);
  assert.equal(writes().length, 1); createStub(); await click(button("确认替代事实"));
  assert.deepEqual(JSON.parse(writes()[1].body).expected_sources, fresh.expected_sources);
});

test("prefill: a changed current source revision fails preflight without posting or emptying the form", async () => {
  await openReady(); stub("GET", "/versions/new-v1", version("new", 1, { revision: 20 }));
  await click(button("确认替代事实")); assert.equal(writes().length, 0);
  assert.match(prefill().textContent, /建议来源版本或权限已变化/);
  assert.equal(button("确认替代事实").disabled, true); assert.equal(prefill().querySelectorAll("select").length, 0);
});

test("prefill: expected hashes are not replaced by a null stored version hash; the server still checks the original content binding", async () => {
  await openReady(); stub("GET", "/versions/old-v1", version("old", 1, { content_sha256: null }));
  stub("POST", "/source-authority", request => {
    assert.equal(JSON.parse(request.body).expected_sources[0].content_sha256, binding("old").content_sha256);
    return json({ code: "SOURCE_AUTHORITY_PREFILL_STALE", message: "实际内容摘要已变化" }, 409);
  });
  await click(button("确认替代事实")); assert.equal(writes().length, 1);
  assert.match(prefill().textContent, /实际内容摘要已变化/); assert.equal(button("确认替代事实").disabled, true);
});

test("prefill: a fact confirmed by someone else after opening is shown as existing, without a duplicate POST", async () => {
  await openReady(); stub("GET", "/source-authority?space_id=space-a", listing([record()]));
  await click(button("确认替代事实")); assert.equal(writes().length, 0);
  assert.match(prefill().textContent, /已确认，无需重复提交/);
});

test("prefill: a duplicate READY projection becomes a confirmed read-only entry from the current relationship list", async () => {
  suggestionsStub(); stub("GET", "/source-authority?space_id=space-a", listing([record()]));
  await mount(); await click(button("查看已确认信息"));
  await confirmPrefill(); assert.equal(writes().length, 0); assert.match(prefill().textContent, /已确认，无需重复提交/);
});

test("prefill: manual duplicate confirmation is also blocked and can open the existing fact", async () => {
  stub("GET", "/source-authority?space_id=space-a", listing([record()]));
  await mount(); await fillCreate(); assert.equal(button("提交确认").disabled, true);
  await submit(); assert.equal(writes().length, 0);
  await click(button("查看已确认要素", form())); assert.match(prefill().textContent, /已确认，无需重复提交/);
});

test("prefill: can_manage=false permits viewing but no editing, confirmation, manual entry or revocation", async () => {
  suggestionsStub([suggestion()], false); await mount(app({ me: { ...app().me, is_admin: true } }));
  await click(button("核对替代建议"));
  assert.equal(prefill().querySelectorAll("input,select,textarea").length, 0);
  assert.equal(button("确认替代事实").disabled, true); assert.equal(button("手动登记").disabled, true); assert.equal(button("撤销事实").disabled, true);
  await confirmPrefill(); assert.equal(writes().length, 0);
});

test("prefill: loss of management in the fresh preflight clears the entire old scope without a POST", async () => {
  await openReady(); stub("GET", "/source-authority?space_id=space-a", listing([], false));
  await click(button("确认替代事实")); assert.equal(writes().length, 0); assert.equal(prefill(), null);
  assert.doesNotMatch(body(), /合成旧规则/); assert.match(body(), /已清除旧结果和选择/);
});

for (const status of [401, 403, 404]) test(`prefill: suggestions permission error ${status} clears prefilled fields and existing records`, async () => {
  await openReady(); stub("GET", "/source-authority/suggestions?space_id=space-a", failed(status, "HIDDEN SUGGESTION"));
  await click(button("刷新关系")); assert.equal(prefill(), null); assert.equal(document.querySelectorAll(".source-authority-record").length, 0);
  assert.doesNotMatch(body(), /HIDDEN SUGGESTION|合成旧规则/); assert.equal(writes().length, 0);
});

test("prefill: a suggestions transport error never opens a blank form automatically and leaves the existing revoke flow available", async () => {
  stub("GET", "/source-authority/suggestions?space_id=space-a", failed(503));
  await mount(); assert.equal(prefill(), null); assert.equal(form(), null);
  assert.equal(button("核对替代建议").disabled, true); assert.equal(button("撤销事实").disabled, false);
  await fillRevoke(); assert.equal(button("确认撤销").disabled, false); assert.equal(writes().length, 0);
});

for (const [name, value] of [
  ["wrong space", suggestion({ space_id: "private", predecessor_title: "SECRET TITLE" })],
  ["foreign excerpt", suggestion({ excerpts: [{ version_id: "secret-version", block_id: "secret-block", text: "SECRET TEXT", locator: {} }] })],
  ["partial malformed hash", suggestion({ expected_sources: [binding("old", { content_sha256: "" })] })],
  ["foreign binding", suggestion({ expected_sources: [binding("alternate")] })],
  ["unknown status", suggestion({ status: "AUTO_APPROVED" })],
]) test(`prefill: malformed suggestions cannot be confirmed or leak mismatched fields: ${name}`, async () => {
  suggestionsStub([value]); await mount(); assert.equal(button("核对替代建议").disabled, true);
  assert.equal(prefill(), null); assert.ok(document.querySelector('[role="alert"]'));
  assert.doesNotMatch(body(), /SECRET TITLE|SECRET TEXT|secret-version/); assert.equal(writes().length, 0);
});

test("prefill: a READY item missing complete source bindings is blocked instead of downgrading to unbound creation", async () => {
  await openReady(suggestion({ expected_sources: [binding("old"), binding("new")] }));
  assert.match(prefill().textContent, /来源绑定尚不完整/); assert.equal(button("确认替代事实").disabled, true);
  await confirmPrefill(); assert.equal(writes().length, 0);
});

test("prefill: repeated clicks cannot create twice, even before the first preflight resolves", async () => {
  await openReady(); const pending = deferred(); stub("POST", "/source-authority", () => pending.promise);
  await act(async () => { for (let index = 0; index < 3; index++) prefill().dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
  assert.equal(writes().length, 1);
  await act(async () => pending.resolve(json(record({ reason: suggestion().reason }), 201))); assert.equal(prefill(), null);
});

test("prefill: ambiguous errors keep the original payload binding and idempotency key for an explicit retry", async () => {
  await openReady(); stub("POST", "/source-authority", () => { throw new Error("offline"); });
  await click(button("确认替代事实")); assert.equal(writes().length, 1);
  createStub(); await click(button("确认替代事实")); assert.equal(writes().length, 2);
  assert.equal(writes()[0].body, writes()[1].body);
  assert.equal(writes()[0].headers.get("Idempotency-Key"), writes()[1].headers.get("Idempotency-Key"));
});

test("prefill: a malformed successful receipt is locked, not counted as a successful confirmation", async () => {
  await openReady(); stub("POST", "/source-authority", () => json(record({ reason: suggestion().reason, successor_version_id: "wrong" }), 201));
  await click(button("确认替代事实")); assert.doesNotMatch(body(), /替代事实已确认/);
  assert.equal(button("确认替代事实").disabled, true); await confirmPrefill(); assert.equal(writes().length, 1);
});

test("prefill: a late suggestion read cannot populate another space or grant its management permission", async () => {
  const pending = deferred(); stub("GET", "/source-authority/suggestions?space_id=space-a", () => pending.promise);
  await mount(); const request = suggestionReads()[0];
  await switchApp(app({ space: { ...app().space, id: "space-b", name: "合成空间乙" } }));
  assert.equal(request.signal.aborted, true);
  await act(async () => pending.resolve(json(listing([suggestion({ predecessor_title: "LATE SECRET" })]))));
  assert.doesNotMatch(body(), /LATE SECRET/); assert.equal(button("手动登记").disabled, true);
});

for (const kind of ["user", "space", "permission"]) test(`prefill: ${kind} switch clears text edits and aborts preflight before any POST`, async () => {
  await openReady(); await click(button("修改说明")); await change(prefill().querySelector("textarea"), "OLD PRIVATE TEXT");
  const pending = deferred(); stub("GET", "/versions/old-v1", () => pending.promise); await click(button("确认替代事实"));
  const request = calls.filter(call => call.url.endsWith("/versions/old-v1")).at(-1);
  const value = app();
  if (kind === "user") value.me = { ...value.me, id: "other-user" };
  if (kind === "space") value.space = { ...value.space, id: "space-b" };
  if (kind === "permission") value.space = { ...value.space, roles: ["reader"] };
  await switchApp(value); assert.equal(request.signal.aborted, true);
  await act(async () => pending.resolve(json(version("old"))));
  assert.equal(prefill(), null); assert.doesNotMatch(body(), /OLD PRIVATE TEXT/); assert.equal(writes().length, 0);
});

test("prefill: changing the selected suggestion aborts a pending POST and its late success cannot clear the new selection", async () => {
  suggestionsStub([suggestion(), suggestion({ id: "second", effective_from: "2028-01-01" })]); await mount(); await click(button("核对替代建议"));
  const pending = deferred(); stub("POST", "/source-authority", () => pending.promise); await click(button("确认替代事实"));
  await change(document.querySelector('[aria-label="选择替代建议"]'), "second"); assert.equal(writes()[0].signal.aborted, true);
  const reads = suggestionReads().length;
  await act(async () => pending.resolve(json(record({ reason: suggestion().reason }), 201)));
  assert.match(prefill().textContent, /2028-01-01/); assert.equal(suggestionReads().length, reads);
  assert.doesNotMatch(body(), /替代事实已确认/);
});

test("prefill: evidence excerpts remain plain text and navigation targets the exact returned version and block", async () => {
  await openReady(suggestion({ excerpts: [{ version_id: "evidence-v1", block_id: "exact-block",
    text: '<img src="https://invalid.test/pixel"> [link](javascript:alert(1))', locator: { label: "原件第 3 页" } }] }));
  assert.equal(document.querySelectorAll("img,iframe,a").length, 0);
  await click(button("定位此段原文")); assert.deepEqual(opened[0].slice(1), ["content", "evidence-v1", "exact-block"]);
  assert.equal(writes().length, 0);
});
