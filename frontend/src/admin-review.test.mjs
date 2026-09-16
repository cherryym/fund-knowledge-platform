// Synthetic DOM and intercepted HTTP only: no backend, credentials, or business writes.
import test, { after, afterEach } from "node:test";
import assert from "node:assert/strict";
import Module, { createRequire } from "node:module";
import { resolve } from "node:path";
import { build } from "esbuild";

const require = createRequire(import.meta.url);
const { JSDOM } = require("jsdom");
const React = require("react"), { act } = React;
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: "http://localhost/#/documents", pretendToBeVisual: true,
});
const { window } = dom;
for (const key of ["window", "document", "HTMLElement", "HTMLDialogElement", "Element", "Node",
  "MutationObserver", "Event", "MouseEvent", "FormData", "sessionStorage", "localStorage"])
  globalThis[key] = key === "window" ? window : window[key];
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
globalThis.getComputedStyle = window.getComputedStyle.bind(window);
window.matchMedia = query => ({ matches: query.includes("reduce") && !query.includes("no-preference"), media: query,
  addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
window.HTMLDialogElement.prototype.close = function () { this.open = false; };
const { createRoot } = require("react-dom/client");
const gsap = require("gsap").gsap;
const { ScrollTrigger } = require("gsap/dist/ScrollTrigger");
const bundle = await build({
  stdin: { contents: `export * from './src/AdminReviewPanel'; export * from './src/PublishJobStatus';
    export * from './src/ResourceDetail'; export {AppContext} from './src/ui';
    export {setSession,clearSession} from './src/api';`, resolveDir: process.cwd(), loader: "tsx" },
  bundle: true, write: false, format: "cjs", platform: "node",
  external: ["react", "react/*", "react-dom", "react-dom/*", "gsap", "@gsap/react", "gsap/ScrollTrigger"],
  loader: { ".css": "empty" }, define: { "import.meta.env": "{}" },
});
const filename = resolve("src/__admin_review_test_bundle.cjs");
const compiled = new Module(filename);
compiled.filename = filename;
compiled.paths = Module._nodeModulePaths(resolve("src"));
const runtimeRequire = compiled.require.bind(compiled);
compiled.require = id => id === "gsap" ? gsap : id === "gsap/ScrollTrigger" ? { ScrollTrigger } : runtimeRequire(id);
compiled._compile(bundle.outputFiles[0].text, filename);
const ui = compiled.exports;

const hash = "a".repeat(64);
const reason = "已核对本版本文档及冻结来源，确认有效并同意发布";
const source = { id: "doc", space_id: "space", kind: "document", name: "合成资料", category: "未分类",
  revision: 3, owner_id: "owner", tags: [], deleted_at: null, active_release_id: null, active_version_id: null,
  access_epoch: 1, restricted: false, classification: "INTERNAL", suspended: false };
const draft = { id: "version", resource_id: "doc", revision: 4, author_id: "owner", title: "合成文稿", version_no: 1,
  state: "DRAFT", origin: "HUMAN", knowledge_type: "source", valid_from: null, valid_to: null,
  legal_status: "UNKNOWN", source_verified: false, content_sha256: null, applicability: {}, required_facts: [],
  blocks: [{ block_id: "block", ordinal: 0, block_type: "paragraph", data: { text: "合成原文可阅读编辑" }, locator: {}, citations: [] }] };
const confirmation = { mode: "ADMIN_CONFIRMED", version_id: "version", actor_id: "owner",
  confirmed_at: "2026-09-08T07:00:00Z", reason, independent_review: false, content_sha256: hash };
const queuedJob = { id: "publish-job", kind: "PUBLISH", state: "QUEUED", stage: "QUEUED", attempts: 0,
  error_code: null, result: null };
let root, component, props, requests = [], notices = [], refreshes = 0, reply;
let currentVersion = structuredClone(draft), currentResource = structuredClone(source), currentRecord = null;
const app = { space: { id: "space", name: "合成空间", roles: ["admin", "editor"] }, refresh: 0,
  me: { id: "owner", spaces: [], csrf_token: "synthetic-test-csrf" }, bump() {},
  notify(message) { notices.push(message); }, navigate() {}, ask() {}, openResource() {}, openVersion() {} };
const writes = () => requests.filter(row => row.method !== "GET");
const button = text => [...document.querySelectorAll("button")].find(el => el.textContent.trim() === text);
const admin = () => document.querySelector('section[aria-label="管理员确认发布"]');
const checkboxes = () => [...admin().querySelectorAll('input[type="checkbox"]')];
const textarea = () => admin().querySelector("textarea");
const defaultStatus = () => ({ version_id: currentVersion.id, can_confirm: true, current_sha256: hash, confirmation: currentRecord });
const response = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json", ETag: '"99"', ...headers },
});
const originalFetch = globalThis.fetch;
globalThis.fetch = async (input, options = {}) => {
  const url = new URL(String(input), "http://localhost");
  assert.equal(url.origin, "http://localhost", "all requests must be synthetic");
  const row = { path: url.pathname.replace(/^\/api\/v1/, ""), method: options.method ?? "GET",
    body: options.body ? JSON.parse(options.body) : undefined, headers: new Headers(options.headers), signal: options.signal };
  requests.push(row);
  const result = await reply(row);
  return result instanceof Response ? result : response(result);
};
function defaults(row) {
  if (row.method === "GET" && row.path === `/versions/${currentVersion.id}/admin-review`) return defaultStatus();
  if (row.method === "GET" && row.path === "/resources/doc") return currentResource;
  if (row.method === "GET" && row.path === "/resources/doc/versions") return { items: [currentVersion], next_cursor: null };
  if (row.method === "GET" && row.path === `/versions/${currentVersion.id}`) return currentVersion;
  if (row.method === "GET" && row.path.endsWith("/reviews")) return [];
  if (row.method === "GET" && row.path.endsWith("/content")) return new Response("<html><body>合成原件</body></html>", { headers: { "Content-Type": "text/html" } });
  throw new Error(`Unexpected synthetic request: ${row.method} ${row.path}`);
}
reply = defaults;
async function settle(ms = 20) { await act(async () => { await new Promise(done => setTimeout(done, ms)); }); }
async function render() {
  ui.setSession(app.me);
  await act(async () => root.render(React.createElement(ui.AppContext.Provider, { value: app }, React.createElement(component, props))));
  await settle();
}
async function mount(target = ui.AdminReviewPanel, value = {}) {
  component = target;
  props = target === ui.ResourceDetail ? { resource: source, initialTab: "review", close() {}, ...value }
    : { version: currentVersion, refresh() { refreshes++; }, ...value };
  root = createRoot(document.getElementById("root"));
  await render();
}
async function click(el) { assert.ok(el); await act(async () => el.click()); await settle(); }
async function fill(value) {
  const el = textarea(); assert.ok(el);
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set.call(el, value);
    el.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
}
async function consent(value = reason) {
  await fill(value);
  for (const el of checkboxes()) if (!el.checked) await click(el);
}
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }
async function submitDirectly(times = 1) {
  const form = admin().querySelector("form"); assert.ok(form);
  await act(async () => { for (let i = 0; i < times; i++) form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })); });
  await settle();
}
afterEach(async () => {
  if (root) { await act(async () => root.unmount()); root = undefined; }
  ui.clearSession(); requests = []; notices = []; refreshes = 0; reply = defaults;
  currentVersion = structuredClone(draft); currentResource = structuredClone(source); currentRecord = null;
  app.me.id = "owner"; app.refresh = 0; app.space.roles = ["admin", "editor"];
  sessionStorage.clear(); localStorage.clear();
});
after(() => { globalThis.fetch = originalFetch; ScrollTrigger.disable(); gsap.ticker.sleep(); window.close(); });

test("DRAFT confirmation requires both explicit checkboxes and at least eight non-padding characters", async () => {
  await mount();
  assert.equal(checkboxes().length, 2);
  assert.ok(checkboxes().every(el => !el.checked));
  assert.equal(textarea().value, "");
  assert.equal(button("确认有效并通过").disabled, true);
  await submitDirectly();
  await consent("   一二三四五六七   ");
  assert.equal(button("确认有效并通过").disabled, true);
  await submitDirectly();
  await fill("一二三四五六七八");
  assert.equal(button("确认有效并通过").disabled, false);
  await click(checkboxes()[1]);
  assert.equal(button("确认有效并通过").disabled, true);
  await submitDirectly();
  await click(checkboxes()[1]);
  await click(checkboxes()[0]);
  assert.equal(button("确认有效并通过").disabled, true);
  await submitDirectly();
  assert.deepEqual(writes(), []);
});

test("confirmation binds the GET hash and version.revision, sends exact attestation fields once, and does not publish", async () => {
  const pending = deferred();
  reply = row => row.method === "POST" ? pending.promise : defaults(row);
  await mount();
  await consent(`  ${reason}  `);
  await submitDirectly(2);
  assert.equal(writes().length, 1);
  const request = writes()[0];
  assert.equal(request.path, "/versions/version/admin-review");
  assert.equal(request.headers.get("If-Match"), '"4"', "use version.revision, not an unrelated cached ETag");
  assert.equal(request.headers.get("X-CSRF-Token"), "synthetic-test-csrf");
  assert.ok(request.headers.get("Idempotency-Key"));
  assert.deepEqual(request.body, { reviewed_sha256: hash, reason, confirm_valid_sources: true, acknowledge_not_independent: true });
  assert.ok(checkboxes().every(el => el.disabled));
  await act(async () => pending.resolve({ version_id: "version", state: "APPROVED", confirmation }));
  await settle();
  assert.equal(refreshes, 1);
  assert.match(admin().textContent, /最新管理员确认（非独立复核）/);
  assert.match(admin().textContent, /ADMIN_CONFIRMED/);
  assert.equal(writes().length, 1);
});

test("IN_REVIEW self-review stays forbidden while admin confirmation enables the existing publish action and observes the job", async () => {
  currentVersion.state = "IN_REVIEW";
  currentVersion.content_sha256 = hash;
  const pendingJob = deferred();
  reply = row => {
    if (row.method === "POST" && row.path.endsWith("/admin-review")) {
      currentRecord = confirmation;
      currentVersion = { ...currentVersion, state: "APPROVED", revision: 5, source_verified: true };
      return { version_id: "version", state: "APPROVED", confirmation };
    }
    if (row.method === "POST" && row.path.endsWith("/publish")) return queuedJob;
    if (row.path === "/jobs/publish-job") return pendingJob.promise;
    return defaults(row);
  };
  await mount(ui.ResourceDetail);
  assert.equal(button("通过复核").disabled, true);
  assert.equal(button("退回修改").disabled, true);
  await click(button("通过复核"));
  await consent(); await click(button("确认有效并通过"));
  await settle();
  assert.ok(button("发布此版本"));
  assert.ok(document.querySelector('[aria-label="最新管理员确认（非独立复核）"]'));
  assert.deepEqual(writes().map(row => row.path), ["/versions/version/admin-review"]);
  await click(button("发布此版本"));
  assert.equal(writes()[1].headers.get("If-Match"), '"5"');
  assert.equal(writes()[1].body, undefined);
  assert.equal(button("发布此版本").disabled, true);
  assert.match(document.querySelector('[aria-label="发布任务状态"]').textContent, /尚未确认发布成功/);
  assert.ok(!notices.includes("此版本发布成功"));
  await act(async () => pendingJob.resolve({ ...queuedJob, state: "SUCCEEDED", stage: "COMPLETED" }));
  await settle();
  assert.match(document.querySelector('[aria-label="发布任务状态"]').textContent, /此版本发布成功/);
  assert.ok(notices.includes("此版本发布成功"));
  await click(button("渲染阅读"));
  await click(button("审核发布"));
  assert.equal(notices.filter(message => message === "此版本发布成功").length, 1,
    "returning to the tab must not notify or refresh the same completed job again");
  assert.equal(writes().length, 2);
  assert.ok(!requests.some(row => row.method === "POST" && row.path.endsWith("/reviews")));
});

for (const state of ["DRAFT", "IN_REVIEW", "APPROVED"]) test(`server can_confirm=false preserves the existing ${state} review page`, async () => {
  currentVersion.state = state;
  reply = row => row.path.endsWith("/admin-review") ? { ...defaultStatus(), can_confirm: false } : defaults(row);
  await mount(ui.ResourceDetail);
  assert.match(admin().textContent, /服务端未授予/);
  assert.equal(admin().querySelector("form"), null);
  assert.ok(button(state === "DRAFT" ? "提交复核" : state === "IN_REVIEW" ? "通过复核" : "发布此版本"));
  assert.ok(button("渲染阅读")); assert.ok(button("内容编辑"));
  assert.deepEqual(writes(), []);
});

for (const status of [404, 405, 501]) test(`old backend GET ${status} is a local notice and preserves review/read/edit`, async () => {
  reply = row => row.path.endsWith("/admin-review") ? response({ code: "NOT_FOUND" }, status) : defaults(row);
  await mount(ui.ResourceDetail);
  assert.match(admin().textContent, /当前后端暂不支持/);
  assert.equal(document.querySelector('[role="alert"]'), null);
  assert.ok(button("提交复核"));
  assert.match(document.body.textContent, /暂无独立人员复核记录/);
  await click(button("渲染阅读"));
  assert.match(document.body.textContent, /合成原文可阅读编辑/);
  assert.ok(button("内容编辑"));
  await click(button("内容编辑"));
  assert.ok(document.querySelector(".continuous-editor"));
  assert.deepEqual(writes(), []);
});

test("local role names never replace service authorization and REJECTED never offers confirmation", async () => {
  app.space.roles = ["reader"];
  await mount();
  assert.ok(button("确认有效并通过"), "server can_confirm is authoritative even when local roles differ");
  currentVersion = { ...draft, state: "REJECTED" };
  props = { ...props, version: currentVersion }; await render();
  assert.equal(admin().querySelector("form"), null);
  assert.match(admin().textContent, /请先创建修订/);
});

for (const [status, code] of [[409, "REVIEW_HASH_MISMATCH"], [412, "REVISION_CONFLICT"], [403, "FORBIDDEN"], [422, "ADMIN_CONFIRMATION_REQUIRED"]])
  test(`POST ${status} ${code} keeps reason, clears consent, and never auto-replays or publishes`, async () => {
    reply = row => row.method === "POST" ? response({ code, message: "合成确认被拒绝" }, status) : defaults(row);
    await mount(); await consent(); await click(button("确认有效并通过"));
    assert.match(admin().textContent, new RegExp(code));
    assert.equal(textarea().value, reason);
    assert.ok(checkboxes().every(el => !el.checked && el.disabled));
    await submitDirectly();
    assert.equal(writes().length, 1);
    assert.equal(notices.length, 0);
    await click(button("重新读取版本"));
    assert.equal(refreshes, 1);
    assert.equal(writes().length, 1);
  });

test("ambiguous confirmation network loss requires reread instead of automatic replay", async () => {
  reply = row => { if (row.method === "POST") throw new TypeError("synthetic network loss"); return defaults(row); };
  await mount(); await consent(); await click(button("确认有效并通过"));
  assert.match(admin().textContent, /操作结果尚未确认/);
  assert.ok(button("重新读取版本"));
  await submitDirectly(); assert.equal(writes().length, 1);
  assert.equal(refreshes, 0); assert.equal(notices.length, 0);
});

for (const invalid of [
  { version_id: "other-version" }, { current_sha256: "" }, { can_confirm: "true" },
  { confirmation: { ...confirmation, independent_review: true } },
  { confirmation: { ...confirmation, content_sha256: "b".repeat(64) } },
]) test(`malformed GET data fails closed: ${JSON.stringify(invalid)}`, async () => {
  reply = () => ({ ...defaultStatus(), ...invalid });
  await mount();
  assert.match(admin().textContent, /不完整或与当前版本不一致/);
  assert.equal(admin().querySelector("form"), null);
  assert.equal(admin().querySelector('[aria-label="最新管理员确认（非独立复核）"]'), null);
  assert.deepEqual(writes(), []);
});

test("malformed successful POST response is not reported as a confirmation", async () => {
  reply = row => row.method === "POST" ? { version_id: "version", state: "APPROVED", confirmation: { ...confirmation, independent_review: true } } : defaults(row);
  await mount(); await consent(); await click(button("确认有效并通过"));
  assert.match(admin().textContent, /操作结果尚未确认/);
  assert.equal(refreshes, 0); assert.equal(notices.length, 0);
  assert.equal(admin().querySelector('[aria-label="最新管理员确认（非独立复核）"]'), null);
});

test("version revision and user changes reset explicit consent", async () => {
  await mount(); await consent();
  currentVersion = { ...draft, revision: 5 }; props = { ...props, version: currentVersion }; await render();
  assert.ok(checkboxes().every(el => !el.checked)); assert.equal(textarea().value, "");
  await consent(); app.me.id = "other-reader"; await render();
  assert.ok(checkboxes().every(el => !el.checked)); assert.equal(textarea().value, "");
  assert.deepEqual(writes(), []);
});

test("late GET and POST responses cannot attach confirmation to a different version", async () => {
  const lateGet = deferred(), latePost = deferred();
  reply = row => row.path === "/versions/version/admin-review" ? lateGet.promise : defaults(row);
  await mount();
  currentVersion = { ...draft, id: "next-version" }; props = { ...props, version: currentVersion }; await render();
  await act(async () => lateGet.resolve({ version_id: "version", can_confirm: true, current_sha256: hash, confirmation }));
  await settle();
  assert.equal(admin().querySelector('[aria-label="最新管理员确认（非独立复核）"]'), null);
  reply = row => row.method === "POST" ? latePost.promise : defaults(row);
  await consent(); await click(button("确认有效并通过"));
  currentVersion = { ...draft, id: "third-version" }; props = { ...props, version: currentVersion }; await render();
  await act(async () => latePost.resolve({ version_id: "next-version", state: "APPROVED",
    confirmation: { ...confirmation, version_id: "next-version" } }));
  await settle();
  assert.equal(admin().querySelector('[aria-label="最新管理员确认（非独立复核）"]'), null);
  assert.equal(refreshes, 0); assert.equal(notices.length, 0);
  assert.ok(checkboxes().every(el => !el.checked));
});

test("APPROVED Wiki reading retains UNKNOWN and the frozen historical warning alongside the latest admin record", async () => {
  currentRecord = confirmation;
  currentResource = { ...source, kind: "knowledge", tags: ["unverified-sources"] };
  currentVersion = { ...draft, state: "APPROVED", knowledge_type: "wiki", content_sha256: hash,
    blocks: [{ ...draft.blocks[0], block_type: "warning", data: { text: "未核验来源：构建时来源尚未核验，此历史警告保留。" } }] };
  const before = structuredClone({ currentVersion, currentResource });
  await mount(ui.ResourceDetail, { resource: currentResource, initialTab: "content" });
  assert.match(document.body.textContent, /未核验来源：构建时来源尚未核验/);
  const record = document.querySelector('[aria-label="最新管理员确认（非独立复核）"]');
  assert.ok(record); assert.match(record.textContent, /owner/); assert.match(record.textContent, /正式效力须按原独立流程核验/);
  assert.match(document.querySelector(".content-meta").textContent, /未确认/);
  assert.equal(document.querySelector('input[type="checkbox"]'), null);
  assert.deepEqual({ currentVersion, currentResource }, before);
  await click(button("审核发布"));
  assert.ok(button("发布此版本")); assert.equal(admin().querySelector("form"), null);
  assert.deepEqual(writes(), []);
});

function JobHarness({ initial = queuedJob, onSettled = () => {} }) {
  const [job, setJob] = React.useState(initial);
  return React.createElement(ui.PublishJobStatus, { job, onUpdate: setJob, onSettled });
}
for (const [state, text] of [["SUCCEEDED", "此版本发布成功"], ["FAILED", "发布失败"], ["CANCELLED", "发布已取消"]])
  test(`publish observation reports ${state} only from the returned job and issues no mutations`, async () => {
    let settled = 0;
    reply = row => { assert.equal(row.path, "/jobs/publish-job"); return { ...queuedJob, state, error_code: state === "FAILED" ? "PUBLISH_SOURCE_CHANGED" : null }; };
    await mount(JobHarness, { onSettled(job) { settled++; assert.equal(job.state, state); } });
    assert.match(document.body.textContent, new RegExp(text));
    assert.equal(settled, 1); assert.deepEqual(writes(), []);
    if (state !== "SUCCEEDED") assert.doesNotMatch(document.body.textContent, /此版本发布成功/);
  });

test("publish observation pauses on read error and manual retry is GET-only", async () => {
  let count = 0;
  reply = () => ++count === 1 ? response({ code: "TEMPORARY_FAILURE", message: "合成读取失败" }, 503)
    : { ...queuedJob, state: "SUCCEEDED" };
  await mount(JobHarness);
  assert.match(document.body.textContent, /合成读取失败/);
  assert.doesNotMatch(document.body.textContent, /此版本发布成功/);
  assert.equal(count, 1);
  await click(button("重试"));
  assert.equal(count, 2); assert.match(document.body.textContent, /此版本发布成功/);
  assert.deepEqual(writes(), []);
});

test("publish observation rejects a mismatched job and aborts pending GET on unmount", async () => {
  reply = () => ({ ...queuedJob, id: "wrong-job", state: "SUCCEEDED" });
  await mount(JobHarness);
  assert.match(document.body.textContent, /发布任务状态无效/);
  assert.doesNotMatch(document.body.textContent, /此版本发布成功/);
  const pending = deferred();
  reply = () => pending.promise;
  await click(button("重试"));
  const request = requests.at(-1);
  await act(async () => root.unmount()); root = undefined;
  assert.equal(request.signal.aborted, true);
  await act(async () => pending.resolve({ ...queuedJob, state: "SUCCEEDED" }));
  assert.equal(document.body.textContent, "");
  assert.deepEqual(writes(), []);
});
