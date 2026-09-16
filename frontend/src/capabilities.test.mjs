import assert from "node:assert/strict";
import { after, afterEach, beforeEach, test } from "node:test";
import { createRequire, Module } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import { build } from "esbuild";

const require = createRequire(import.meta.url), sourceDir = dirname(fileURLToPath(import.meta.url));
const { JSDOM } = require("jsdom");
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: "http://capabilities.test/", pretendToBeVisual: true });
for (const key of ["window", "document", "HTMLElement", "Element", "Node", "Event", "MouseEvent", "HTMLInputElement", "HTMLSelectElement", "HTMLTextAreaElement"])
  globalThis[key] = key === "window" ? dom.window : dom.window[key];
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
globalThis.getComputedStyle = window.getComputedStyle.bind(window);
window.matchMedia = media => ({ media, matches: media.includes("reduce"), addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
let calls = [], routes = new Map(), unexpected = [], copied = [], downloads = [], blobs = new Map(), opened = [], bumps = 0;
Object.defineProperty(navigator, "clipboard", { configurable: true, value: { async writeText(value) { copied.push(value); } } });
URL.createObjectURL = blob => { const id = `blob:capability-test/${blobs.size}`; blobs.set(id, blob); return id; };
URL.revokeObjectURL = () => {};
dom.window.HTMLAnchorElement.prototype.click = function () { downloads.push({ name: this.download, blob: blobs.get(this.href) }); };
const interceptedFetch = async (url, init = {}) => {
  const call = { ...init, url: String(url), method: init.method ?? "GET", headers: new Headers(init.headers) };
  calls.push(call);
  const key = `${call.method} ${call.url}`;
  const allowed = /^\/api\/v1\/(?:capabilities|capability-versions|capability-runs|agent-access|resources|versions)(?:\/|\?|$)/.test(call.url);
  if (!allowed || !routes.has(key)) { unexpected.push(key); throw new Error(`Unmocked request blocked: ${key}`); }
  return routes.get(key)(call);
};
globalThis.fetch = window.fetch = interceptedFetch;
const React = require("react"), { act } = React, h = React.createElement;
const { createRoot } = require("react-dom/client");
const gsap = require("gsap").gsap, { ScrollTrigger } = require("gsap/dist/ScrollTrigger");
const compiled = await build({ stdin: { contents: 'export { CapabilitiesPage } from "./CapabilitiesPage"; export { AppContext } from "./appContext"; export { setSession, clearSession } from "./api"; export { definitionErrors, parseCapabilityValues, capabilityStarter } from "./CapabilitiesShared"; export { definitionFromKnowledge } from "./CapabilitiesEditor";', resolveDir: sourceDir, loader: "tsx" },
  bundle: true, platform: "node", format: "cjs", write: false, logLevel: "silent", loader: { ".css": "empty" }, define: { "import.meta.env.DEV": "false" },
  external: ["react", "react-dom", "react-dom/*", "react/jsx-runtime", "gsap", "gsap/ScrollTrigger", "@gsap/react"],
  plugins: [{ name: "isolate-unchanged-legacy-page", setup(builder) { builder.onLoad({ filter: /ResourcesPage\.tsx$/ }, () => ({
    loader: "tsx", contents: 'export function ResourcesPage({kind}) { return <div data-legacy-kind={kind}>旧模板原入口</div>; }',
  })); } }],
});
const runtime = new Module(resolve(sourceDir, "capabilities-test-runtime.cjs"));
runtime.filename = resolve(sourceDir, "capabilities-test-runtime.cjs"); runtime.paths = Module._nodeModulePaths(sourceDir);
const runtimeRequire = runtime.require.bind(runtime);
runtime.require = id => id === "gsap" ? gsap : id === "gsap/ScrollTrigger" ? { ScrollTrigger } : runtimeRequire(id);
runtime._compile(compiled.outputFiles[0].text, runtime.filename);
const { CapabilitiesPage, AppContext, setSession, clearSession, capabilityStarter, definitionErrors, parseCapabilityValues, definitionFromKnowledge } = runtime.exports;
ScrollTrigger.disable(); gsap.ticker.sleep();

const sourceId = "11111111-1111-4111-8111-111111111111";
const json = (value, status = 200, headers = {}) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json", ...headers } });
const stub = (method, path, value) => routes.set(`${method} /api/v1${path}`, typeof value === "function" ? value : () => json(value));
const fail = (status, message = "合成接口错误") => () => json({ code: `SYNTHETIC_${status}`, message }, status);
const page = items => ({ items, next_cursor: null });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const definition = (overrides = {}) => ({ ...capabilityStarter(), ...overrides });
const detail = (overrides = {}) => ({ resource_id: "cap-1", space_id: "space-a", version_id: "cap-v1", revision: 3, version_no: 1, state: "DRAFT",
  name: "合成工作流", definition: definition(), manifest_sha256: "a".repeat(64), content_sha256: "b".repeat(64), source_bindings: [],
  permissions: { can_edit: true, can_trial: true, can_run: false, can_export: true }, notes: ["合成能力说明"], ...overrides });
const catalog = (items = [detail()], can_edit = true) => ({ items, legacy_items: [{ resource_id: "legacy", name: "保留的旧模板" }], can_edit, notes: ["合成目录说明"] });
const source = (overrides = {}) => ({ id: "knowledge-1", space_id: "space-a", kind: "knowledge", name: "已发布合成知识", category: "合成分类",
  active_version_id: sourceId, active_release_id: "release-1", revision: 1, access_epoch: 1, suspended: false, deleted_at: null, ...overrides });
const sourceVersion = (overrides = {}) => ({ id: sourceId, resource_id: "knowledge-1", version_no: 2, state: "APPROVED", title: "已发布合成知识", revision: 4,
  content_sha256: "c".repeat(64), blocks: [{ block_id: "step-source", ordinal: 1, block_type: "step", locator: {}, citations: [],
    data: { action: "核对源文中的实际条件", owner_role: "复核人员", output: "保留可核对的成果", verification: "按原文逐项核对" } }], ...overrides });
const run = (overrides = {}) => ({ id: "run-1", space_id: "space-a", owner_id: "user-a", version_id: "cap-v1", capability_name: "合成运行",
  revision: 5, state: "WAITING_AGENT", mode: "trial", inputs: { task_goal: "合成目标" }, manifest_sha256: "a".repeat(64), created_at: "2026-09-12T00:00:00Z", updated_at: "2026-09-12T00:00:00Z",
  steps: definition().steps.map(step => ({ id: step.id, title: step.title, kind: step.kind, state: "PENDING", outputs: {}, note: "", reported_by: null, reviewed_by: null })),
  deliverables: ["合成工作底稿"], notes: ["收到报告不代表外部业务已执行"], ...overrides });
const runSummary = value => Object.fromEntries(["id", "space_id", "version_id", "capability_name", "revision", "state", "mode", "created_at", "updated_at"].map(key => [key, value[key]]));
const next = (overrides = {}) => ({ run_id: "run-1", revision: 5, state: "WAITING_AGENT", inputs: run().inputs, previous_outputs: {},
  ready_steps: [definition().steps[0]], waiting_human_steps: [], notes: ["合成下一步"], ...overrides });
const sourceBinding = () => ({ version_id: sourceId, resource_id: "knowledge-1", space_id: "space-a", revision: 4, access_epoch: 1,
  content_sha256: "c".repeat(64), title: "合成来源", block_ids: ["b", "b1"] });
const sourceRecord = (overrides = {}) => ({ resource_id: "knowledge-1", version_id: sourceId, block_id: "b1", ordinal: 1,
  title: "合成来源", text: "完整原文", locator: { label: "第 3 页" }, content_sha256: "d".repeat(64), legal_status: "UNKNOWN", ...overrides });
const access = (overrides = {}) => ({ id: "access-1", name: "合成接入", space_id: "space-a", scopes: ["capabilities:read"], expires_at: "2099-01-01T00:00:00Z", revoked_at: null, created_at: "2026-09-12T00:00:00Z", revision: 1, ...overrides });
const accessList = (items = [], can_create = true) => ({ items, can_create, base_url: "https://fundkb.invalid/api/v1", notes: ["接入状态未经验证"] });
const token = "synthetic-one-time-token-not-real";
let root, currentApp;
const app = (overrides = {}) => ({ me: { id: "user-a", display_name: "合成用户", csrf_token: "synthetic-csrf", is_admin: false },
  space: { id: "space-a", name: "合成知识库甲", revision: 1, roles: ["editor"] }, refresh: 0,
  bump: () => { bumps++; }, notify() {}, navigate() {}, ask() {}, openVersion() {}, openResource: (...args) => opened.push(args), ...overrides });
const body = () => document.body.textContent;
const writes = () => calls.filter(call => call.method !== "GET");
const button = (name, within = document) => { const value = [...within.querySelectorAll("button")].find(item => item.textContent.trim() === name); assert.ok(value, `Missing button: ${name}`); return value; };
const click = value => act(async () => value.click());
const field = label => document.querySelector(`[aria-label="${label}"]`);
const submit = label => act(async () => document.querySelector(`form[aria-label="${label}"]`).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
async function change(element, value) { assert.ok(element); const proto = element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  await act(async () => { Object.getOwnPropertyDescriptor(proto, "value").set.call(element, value); element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? "change" : "input", { bubbles: true })); }); }
async function mount(value = app()) { currentApp = value; setSession(value.me); root = createRoot(document.getElementById("root")); await act(async () => root.render(h(AppContext.Provider, { value }, h(CapabilitiesPage)))); }
async function switchApp(value) { currentApp = value; setSession(value.me); await act(async () => root.render(h(AppContext.Provider, { value }, h(CapabilitiesPage)))); }
async function unmount() { if (root) await act(async () => root.unmount()); root = null; }
async function edit() { await click(button("查看能力")); await click(button("编辑能力定义")); }
async function openRun() { await click(button("运行记录")); const item = document.querySelector('.capabilities-run-list button'); assert.ok(item); await click(item); }
async function newAccess({ confirm = true } = {}) { await click(button("Agent接入")); await click(button("创建 Agent 接入凭据")); await change(field("凭据名称"), "合成接入");
  await change(field("凭据有效期"), "2099-01-01T12:00"); if (confirm) await click(field("确认知识库范围和有效期")); }
function installCreate() { stub("POST", "/capabilities", call => { const payload = JSON.parse(call.body), value = detail({ resource_id: "cap-new", version_id: "cap-new-v1", name: payload.definition.name, definition: payload.definition });
  // Canonicalize object key order recursively like the server, preserving all nested keys.
  const order = item => Array.isArray(item) ? item.map(order) : item && typeof item === "object" ? Object.fromEntries(Object.keys(item).sort().map(key => [key, order(item[key])])) : item;
  value.definition = order(payload.definition);
  stub("GET", "/capability-versions/cap-new-v1", value); stub("GET", "/capabilities?space_id=space-a", catalog([value])); return json(value, 201); }); }
function installAccessCreate() { stub("POST", "/agent-access", call => { const payload = JSON.parse(call.body), value = access({ name: payload.name, scopes: payload.scopes, expires_at: payload.expires_at });
  stub("GET", "/agent-access?space_id=space-a", accessList([value])); return json({ access: value, token }, 201); }); }
function installRun(value, upcoming = next()) { stub("GET", "/capability-runs?space_id=space-a", { items: [runSummary(value)] }); stub("GET", "/capability-runs/run-1", value);
  stub("GET", "/capability-runs/run-1/next", { ...upcoming, inputs: value.inputs,
    previous_outputs: Object.fromEntries(value.steps.filter(step => ["REPORTED", "ACCEPTED"].includes(step.state)).map(step => [step.id, step.outputs])) }); }

beforeEach(() => {
  clearSession(); window.localStorage.clear(); window.sessionStorage.clear(); calls = []; routes = new Map(); unexpected = []; copied = []; downloads = []; blobs = new Map(); opened = []; bumps = 0;
  stub("GET", "/capabilities?space_id=space-a", catalog()); stub("GET", "/capabilities?space_id=space-b", catalog([], false));
  stub("GET", "/capabilities/starter?space_id=space-a", { definition: definition(), state: "EXAMPLE_NOT_SAVED" });
  stub("GET", "/capability-versions/cap-v1", detail()); stub("GET", "/capabilities/cap-1", detail());
  stub("GET", "/resources/cap-1", source({ id: "cap-1", kind: "template" }));
  stub("GET", "/resources?space_id=space-a&limit=100", page([source()]));
  stub("GET", "/resources/knowledge-1", source()); stub("GET", `/versions/${sourceId}`, sourceVersion());
  stub("GET", "/resources/knowledge-1/versions?limit=100", page([sourceVersion()]));
  installRun(run());
  stub("GET", "/agent-access?space_id=space-a", accessList()); stub("GET", "/agent-access?space_id=space-b", accessList([], false));
});
afterEach(async () => {
  await unmount(); clearSession();
  assert.deepEqual(unexpected, [], "All requests use explicit synthetic responses; native network is blocked");
  assert.ok(calls.every(call => !/\/(models|connections|threads|jobs|auth)(?:\/|\?|$)/.test(call.url)));
  assert.equal(globalThis.fetch, interceptedFetch); assert.equal(window.fetch, interceptedFetch);
  assert.equal(window.localStorage.length, 0); assert.equal(window.sessionStorage.length, 0); assert.equal(bumps, 0);
});
after(() => { ScrollTrigger.disable(); gsap.globalTimeline.clear(); gsap.ticker.sleep(); dom.window.close(); });

test("catalog reads all visible capability cards, is lazy about runs/access, and preserves an explicit old-template entry", async () => {
  stub("GET", "/capabilities?space_id=space-a", catalog(Array.from({ length: 257 }, (_, i) => detail({ resource_id: `cap-${i}` }))));
  await mount(); assert.equal(document.querySelectorAll(".capabilities-card").length, 257);
  assert.ok(calls.every(call => call.url === "/api/v1/capabilities?space_id=space-a")); assert.equal(writes().length, 0);
  await click(button("旧方案模板")); assert.ok(document.querySelector('[data-legacy-kind="template"]'));
});
test("Application keeps the templates route and changes only its lazy component, nav label and mount", () => {
  const value = readFileSync(resolve(sourceDir, "Application.tsx"), "utf8");
  assert.match(value, /\["templates", "Agent能力", FileText\]/); assert.match(value, /page === "templates" && <CapabilitiesPage/);
  assert.match(value, /const CapabilitiesPage = lazy/); assert.match(value, /page === "documents" && <ResourcesPage kind="document"/);
});
test("server can_edit=false prevents starter creation even for an admin-shaped client", async () => {
  stub("GET", "/capabilities?space_id=space-a", catalog([detail()], false)); await mount(app({ me: { ...app().me, is_admin: true } }));
  assert.equal(button("沉淀新能力").disabled, true); await click(button("沉淀新能力")); assert.equal(writes().length, 0);
});
for (const status of [401, 403, 404, 503]) test(`catalog ${status} has no writable stale data`, async () => {
  stub("GET", "/capabilities?space_id=space-a", fail(status)); await mount(); assert.ok(document.querySelector('[role="alert"]'));
  assert.equal(document.querySelectorAll(".capabilities-card").length, 0); assert.equal(writes().length, 0);
});
test("a wrong-space capability response is never rendered", async () => {
  stub("GET", "/capabilities?space_id=space-a", catalog([detail({ space_id: "secret", name: "SECRET NAME" })])); await mount();
  assert.doesNotMatch(body(), /SECRET NAME/); assert.ok(document.querySelector('[role="alert"]'));
});
test("starter is fetched from the server, and one explicit click creates DRAFT without publishing", async () => {
  installCreate(); await mount(); await click(button("沉淀新能力"));
  assert.equal(writes().length, 0); assert.ok(calls.some(call => call.url.includes("/capabilities/starter?")));
  await click(button("用工作底稿创建草稿")); assert.equal(writes().length, 1);
  assert.deepEqual(JSON.parse(writes()[0].body), { space_id: "space-a", definition: definition() });
  assert.equal(writes()[0].headers.get("X-CSRF-Token"), "synthetic-csrf"); assert.ok(writes()[0].headers.get("Idempotency-Key"));
  assert.ok(field("能力详情")); assert.match(body(), /草稿/);
});
test("starter failures never cause a hardcoded fallback or automatic POST", async () => {
  stub("GET", "/capabilities/starter?space_id=space-a", fail(503)); await mount(); await click(button("沉淀新能力"));
  assert.equal(button("用工作底稿创建草稿").disabled, true); assert.equal(writes().length, 0);
});
test("visual editor maintains fields and dependency cards; raw JSON is only an advanced option", async () => {
  await mount(); await edit(); assert.ok(field("能力定义编辑器")); assert.ok(field("工作流步骤与依赖"));
  assert.equal(document.querySelector("details.capabilities-advanced").open, false);
  await click(button("添加输入要素")); assert.ok(field("输入要素 2 类型")); assert.ok(field("步骤 1 指导"));
  await change(field("步骤 1 标识"), "gather"); assert.match(field("工作流步骤与依赖").textContent, /整理输入与依据/);
  await click(button("载入当前定义 JSON")); const draft = JSON.parse(field("高级定义 JSON").value);
  assert.deepEqual(draft.steps[1].depends_on, ["gather"]); assert.equal(writes().length, 0);
});
test("definition validation rejects missing checks, duplicated fields, cycles and nonhuman financial risk", () => {
  assert.deepEqual(definitionErrors(definition()), []);
  const variants = [definition({ triggers: [] }), definition({ deliverables: [] }), definition({ steps: [] }),
    definition({ inputs: [definition().inputs[0], definition().inputs[0]] }),
    definition({ steps: definition().steps.map((step, i) => i ? step : { ...step, depends_on: ["review"] }) }),
    definition({ steps: definition().steps.map((step, i) => i ? step : { ...step, risk: "financial_action" }) }),
    definition({ steps: definition().steps.map((step, i) => i ? step : { ...step, checks: [] }) }),
    definition({ steps: definition().steps.map((step, i) => i ? step : { ...step, id: "x".repeat(65) }) })];
  for (const value of variants) assert.ok(definitionErrors(value).length);
});
test("typed inputs preserve false, zero, dates and JSON; invalid required values do not become defaults", () => {
  const fields = ["string", "number", "integer", "boolean", "date", "json"].map(type => ({ key: type, label: type, type, required: true, description: "" }));
  const parsed = parseCapabilityValues(fields, { string: "内容", number: "0", integer: "4", boolean: "false", date: "2026-09-12", json: '{"a":0}' });
  assert.deepEqual(parsed.errors, []); assert.deepEqual(parsed.values, { string: "内容", number: 0, integer: 4, boolean: false, date: "2026-09-12", json: { a: 0 } });
  assert.equal(parseCapabilityValues(fields, {}).errors.length, 6);
  assert.ok(parseCapabilityValues(fields, { string: " ", number: "Infinity", integer: "1.5", boolean: "maybe", date: "2026-02-30", json: "bad" }).errors.length >= 6);
});
test("saving a draft uses version revision, accepts canonical JSON ordering and never changes a published version", async () => {
  stub("PUT", "/capabilities/cap-1", call => { const payload = JSON.parse(call.body), result = detail({ revision: 4, definition: payload.definition, manifest_sha256: "d".repeat(64) });
    stub("GET", "/capability-versions/cap-v1", result); stub("GET", "/capabilities?space_id=space-a", catalog([result])); return json(result); });
  await mount(); await edit(); await change(field("能力名称"), "调整后的定义"); await submit("能力定义编辑器");
  assert.equal(writes().length, 1); assert.equal(writes()[0].method, "PUT"); assert.equal(writes()[0].headers.get("If-Match"), '"3"');
  assert.equal(JSON.parse(writes()[0].body).version_id, "cap-v1"); assert.equal(JSON.parse(writes()[0].body).definition.name, "调整后的定义");
});
test("a published capability offers COPY revision and preserves the original version body", async () => {
  const published = detail({ state: "APPROVED", permissions: { ...detail().permissions, can_edit: false, can_run: true } });
  stub("GET", "/capabilities?space_id=space-a", catalog([published])); stub("GET", "/capability-versions/cap-v1", published);
  stub("POST", "/resources/cap-1/versions", call => { assert.equal(JSON.parse(call.body).base_version_id, "cap-v1"); return json({ id: "copy-v2", resource_id: "cap-1", state: "DRAFT", base_version_id: "cap-v1", origin: "COPY" }, 201); });
  stub("GET", "/capability-versions/copy-v2", detail({ version_id: "copy-v2", version_no: 2 }));
  await mount(); await click(button("查看能力")); assert.equal(button("编辑能力定义").disabled, true);
  await click(button("新建 COPY 草稿修订")); await change(field("修订原因"), "补充核对要求"); await submit("新建能力修订");
  assert.equal(writes().length, 1); assert.equal(writes()[0].url, "/api/v1/resources/cap-1/versions");
});
for (const status of [409, 412]) test(`draft ${status} preserves edits and requires reread without automatic overwrite`, async () => {
  stub("PUT", "/capabilities/cap-1", fail(status)); await mount(); await edit(); await change(field("能力名称"), "保留编辑");
  await submit("能力定义编辑器"); assert.equal(writes().length, 1); assert.equal(field("能力名称").value, "保留编辑");
  assert.equal(button("保存草稿").disabled, true); await submit("能力定义编辑器"); assert.equal(writes().length, 1);
});
test("published knowledge imports only structured step content and binds its exact published version", async () => {
  await mount(); await click(button("沉淀新能力")); const select = field("选择已发布知识搜索").closest("label").querySelector("select");
  await change(select, "knowledge-1"); await click(button("带入已发布知识步骤"));
  assert.equal(field("步骤 1 指导").value, "核对源文中的实际条件\n原文责任角色：复核人员");
  assert.match(body(), /暂按人工核对配置/); assert.equal(field("步骤 1 处理者").value, "human");
  assert.match(body(), new RegExp(sourceId)); assert.equal(writes().length, 0);
});
test("knowledge without structured steps clearly requires supplementation and creates nothing", async () => {
  stub("GET", `/versions/${sourceId}`, sourceVersion({ blocks: [{ block_type: "paragraph", data: { text: "不得从这段文本编造流程" } }] }));
  await mount(); await click(button("沉淀新能力")); await change(field("选择已发布知识搜索").closest("label").querySelector("select"), "knowledge-1");
  await click(button("带入已发布知识步骤")); assert.match(body(), /没有结构化步骤/); assert.equal(field("能力定义编辑器"), null); assert.equal(writes().length, 0);
  assert.throws(() => definitionFromKnowledge(sourceVersion({ blocks: [] })), /没有结构化步骤/);
});
test("source publication changes before import cannot turn a draft into an approved capability source", async () => {
  stub("GET", `/versions/${sourceId}`, sourceVersion({ state: "DRAFT" })); await mount(); await click(button("沉淀新能力"));
  await change(field("选择已发布知识搜索").closest("label").querySelector("select"), "knowledge-1"); await click(button("带入已发布知识步骤"));
  assert.match(body(), /先通过原审核流程/); assert.equal(writes().length, 0);
});
test("skill export is explicit, read-only and keeps all three payload files without installing anything", async () => {
  stub("GET", "/capability-versions/cap-v1/skill", { filename: "SKILL.md", skill_markdown: "# Synthetic skill", workflow_json: { complete: [1, 2, 3] }, connector_markdown: "Configure your own Agent" });
  await mount(); await click(button("查看能力")); assert.equal(downloads.length, 0);
  await click(button("导出技能包")); assert.equal(downloads.length, 0); assert.match(body(), /导出不等于安装/);
  await click(button("下载 SKILL.md")); await click(button("下载 workflow.json")); await click(button("下载接入说明"));
  assert.deepEqual(downloads.map(item => item.name), ["SKILL.md", "workflow.json", "connector.md"]);
  assert.deepEqual(JSON.parse(await downloads[1].blob.text()), { complete: [1, 2, 3] }); assert.equal(writes().length, 0);
});
test("export permissions are server-controlled and a late export cannot appear in a different space", async () => {
  const pending = deferred(); stub("GET", "/capability-versions/cap-v1/skill", () => pending.promise);
  await mount(); await click(button("查看能力")); await click(button("导出技能包"));
  const request = calls.at(-1); await switchApp(app({ space: { ...app().space, id: "space-b" } })); assert.equal(request.signal.aborted, true);
  await act(async () => pending.resolve(json({ filename: "SKILL.md", skill_markdown: "OLD EXPORT", workflow_json: {}, connector_markdown: "x" })));
  assert.doesNotMatch(body(), /OLD EXPORT|技能包已准备/); assert.equal(downloads.length, 0);
});
test("trial requires typed inputs and sends the frozen version, mode and actual values once", async () => {
  stub("POST", "/capability-runs", call => json(run({ inputs: JSON.parse(call.body).inputs }), 201));
  await mount(); await click(button("查看能力")); await click(button("试运行能力")); await submit("能力试运行输入"); assert.equal(writes().length, 0);
  await change(field("本次工作目标"), "实际输入目标"); await submit("能力试运行输入");
  assert.deepEqual(JSON.parse(writes()[0].body), { version_id: "cap-v1", mode: "trial", inputs: { task_goal: "实际输入目标" } });
  assert.ok(field("能力运行详情")); assert.equal(writes().length, 1);
});
test("changed trial definition is rejected before POST", async () => {
  await mount(); await click(button("查看能力")); await click(button("试运行能力")); await change(field("本次工作目标"), "目标");
  stub("GET", "/capability-versions/cap-v1", detail({ revision: 4 })); await submit("能力试运行输入");
  assert.equal(writes().length, 0); assert.match(body(), /能力版本已变化/);
});
test("run list accepts compact summaries and only explicit next reads expose report controls", async () => {
  await mount(); await openRun(); assert.equal(field("步骤报告表单"), null); assert.equal(writes().length, 0);
  await click(button("读取下一步")); assert.ok(field("步骤报告表单")); assert.match(body(), /手动回传步骤报告/);
  assert.ok(calls.some(call => call.url.endsWith("/run-1/next"))); assert.equal(writes().length, 0);
});
test("step reports validate outputs, use exact ETag and do not assert external financial execution", async () => {
  stub("POST", "/capability-runs/run-1/steps/prepare", call => { const payload = JSON.parse(call.body), value = run({ revision: 6, state: "WAITING_HUMAN",
    steps: run().steps.map(item => item.id === "prepare" ? { ...item, state: "REPORTED", outputs: payload.outputs, note: payload.note, reported_by: "user-a", report_channel: "web_user" } : item) });
    installRun(value, next({ revision: 6, state: "WAITING_HUMAN", ready_steps: [], waiting_human_steps: [definition().steps[1]] })); return json(value); });
  await mount(); await openRun(); await click(button("读取下一步")); await change(field("步骤说明"), "实际回传说明"); await submit("步骤报告表单"); assert.equal(writes().length, 0);
  await change(field("工作底稿"), "完整底稿正文与定位"); await submit("步骤报告表单"); assert.equal(writes().length, 1);
  assert.equal(writes()[0].headers.get("If-Match"), '"5"'); assert.deepEqual(JSON.parse(writes()[0].body), { status: "reported", outputs: { workpaper: "完整底稿正文与定位" }, note: "实际回传说明" });
  assert.match(body(), /网页手动回传/); assert.match(body(), /收到报告不证明外部动作已执行/); assert.match(body(), /待人工核对/);
});
test("blocked and failed reports require an explanation without fabricating required outputs", async () => {
  stub("POST", "/capability-runs/run-1/steps/prepare", call => { const payload = JSON.parse(call.body), value = run({ revision: 6, state: "BLOCKED" }); installRun(value); return json(value); });
  await mount(); await openRun(); await click(button("读取下一步")); await change(field("报告状态"), "blocked"); await submit("步骤报告表单"); assert.equal(writes().length, 0);
  await change(field("步骤说明"), "缺少授权资料"); await submit("步骤报告表单"); assert.deepEqual(JSON.parse(writes()[0].body), { status: "blocked", outputs: {}, note: "缺少授权资料" });
});
test("human review requires explicit acknowledgment and typed human outputs, using only the review endpoint", async () => {
  const def = definition(); def.steps[1].outputs = [{ key: "accepted_count", label: "核对数量", type: "integer", required: true, description: "真实核对数量" }];
  stub("GET", "/capability-versions/cap-v1", detail({ definition: def }));
  const value = run({ state: "WAITING_HUMAN", steps: run().steps.map(item => item.id === "prepare" ? { ...item, state: "REPORTED" } : item) });
  installRun(value, next({ state: "WAITING_HUMAN", ready_steps: [], waiting_human_steps: [def.steps[1]] }));
  stub("POST", "/capability-runs/run-1/review", call => { const result = run({ revision: 6, state: "COMPLETED", steps: value.steps.map(item => item.id === "review" ? { ...item, state: "ACCEPTED", reviewed_by: "user-a", outputs: JSON.parse(call.body).outputs } : item) }); installRun(result); return json(result); });
  await mount(); await openRun(); await click(button("读取下一步")); await change(field("步骤说明"), "已检查实际产物"); await submit("人工核对步骤"); assert.equal(writes().length, 0);
  await click(field("已核对实际交付物")); await submit("人工核对步骤"); assert.equal(writes().length, 0);
  await change(field("核对数量"), "3"); await click(field("已核对实际交付物")); await submit("人工核对步骤");
  assert.deepEqual(JSON.parse(writes()[0].body), { step_id: "review", decision: "accept", note: "已检查实际产物", outputs: { accepted_count: 3 } });
  assert.equal(writes()[0].headers.get("If-Match"), '"5"'); assert.match(body(), /定义流程已完成/); assert.match(body(), /不认证资金、过账、交易执行/);
});
for (const status of [409, 412]) test(`run ${status} prevents stale report replay until reread`, async () => {
  stub("POST", "/capability-runs/run-1/steps/prepare", fail(status)); await mount(); await openRun(); await click(button("读取下一步"));
  await change(field("工作底稿"), "正文"); await change(field("步骤说明"), "说明"); await submit("步骤报告表单");
  assert.equal(writes().length, 1); assert.equal(button("回传步骤报告").disabled, true); await submit("步骤报告表单"); assert.equal(writes().length, 1);
});
test("cancel records a reason and does not claim it undoes external tool actions", async () => {
  stub("POST", "/capability-runs/run-1/cancel", () => { const result = run({ revision: 6, state: "CANCELLED" }); installRun(result); return json(result); });
  await mount(); await openRun(); await click(button("取消运行")); await submit("取消能力运行"); assert.equal(writes().length, 0);
  assert.match(body(), /不会撤销外部系统已经发生的动作/); await change(field("取消运行原因"), "用户停止本次指导"); await submit("取消能力运行");
  assert.deepEqual(JSON.parse(writes()[0].body), { reason: "用户停止本次指导" }); assert.equal(writes()[0].headers.get("If-Match"), '"5"'); assert.match(body(), /已取消/);
});
test("next cannot return a human step through the agent reporting channel or a foreign step", async () => {
  stub("GET", "/capability-runs/run-1/next", next({ ready_steps: [definition().steps[1]] })); await mount(); await openRun(); await click(button("读取下一步"));
  assert.match(body(), /步骤类型不一致/); assert.equal(field("步骤报告表单"), null); assert.equal(writes().length, 0);
});
test("source reads show full bound text and locators, rejecting any foreign version without rendering it", async () => {
  const def = definition({ source_version_ids: [sourceId] }); stub("GET", "/capability-versions/cap-v1", detail({ definition: def, source_bindings: [sourceBinding()] }));
  stub("GET", "/capability-runs/run-1/sources", { run_id: "run-1", scope: "reference", source_bindings: [sourceBinding()], notes: ["资料辅助"], records: [sourceRecord({ text: "完整原文 <img src=x>" })] });
  await mount(); await openRun(); await click(button("读取绑定来源")); assert.match(body(), /完整原文/); assert.match(body(), /第 3 页/); assert.equal(document.querySelectorAll("img").length, 0);
  stub("GET", "/capability-runs/run-1/sources", { run_id: "run-1", scope: "reference", notes: [], records: [{ version_id: "foreign", block_id: "b1", title: "SECRET TITLE", text: "SECRET BODY" }] });
  await click(button("读取绑定来源")); assert.doesNotMatch(body(), /SECRET TITLE|SECRET BODY/); assert.match(body(), /来源响应与本次绑定版本不一致/);
});
test("cross-owner run detail and a changed frozen manifest are blocked", async () => {
  stub("GET", "/capability-runs/run-1", run({ owner_id: "someone-else", capability_name: "SECRET RUN" }));
  await mount(); await openRun(); assert.doesNotMatch(body(), /SECRET RUN/); assert.equal(field("步骤报告表单"), null);
});
test("Agent access opens only a metadata list and never creates credentials or reads external provider configuration", async () => {
  await mount(); await click(button("Agent接入")); assert.match(body(), /尚未连接 Agent/); assert.equal(writes().length, 0);
  assert.equal(document.querySelectorAll('input[type="password"]').length, 0); assert.ok(calls.some(call => call.url === "/api/v1/agent-access?space_id=space-a"));
});
test("can_create=false prevents credential issuance regardless of local role", async () => {
  stub("GET", "/agent-access?space_id=space-a", accessList([], false)); await mount(app({ me: { ...app().me, is_admin: true } }));
  await click(button("Agent接入")); assert.equal(button("创建 Agent 接入凭据").disabled, true); await click(button("创建 Agent 接入凭据")); assert.equal(writes().length, 0);
});
test("credential creation requires explicit scope/library/expiry confirmation, uses request_id and omits generic Idempotency-Key", async () => {
  installAccessCreate(); await mount(); await newAccess({ confirm: false }); await submit("创建 Agent 接入凭据"); assert.equal(writes().length, 0);
  await click(field("确认知识库范围和有效期")); await click(field("领取运行并回传步骤")); assert.equal(field("确认知识库范围和有效期").checked, false);
  await submit("创建 Agent 接入凭据"); assert.equal(writes().length, 0); await click(field("确认知识库范围和有效期")); await submit("创建 Agent 接入凭据");
  assert.equal(writes().length, 1); const request = writes()[0], payload = JSON.parse(request.body);
  assert.match(payload.request_id, /^[a-f\d-]{36}$/); assert.equal(payload.space_id, "space-a"); assert.match(payload.expires_at, /Z$/);
  assert.deepEqual(payload.scopes, ["capabilities:read", "runs:write"]); assert.equal(request.headers.get("Idempotency-Key"), null); assert.equal(request.headers.get("X-CSRF-Token"), "synthetic-csrf");
  assert.equal(field("一次性明文").type, "password"); assert.equal(field("一次性明文").value, token); assert.equal(copied.length, 0);
  await click(button("复制一次性凭据")); assert.deepEqual(copied, [token]); await click(button("已保存，清除本页明文")); assert.equal(field("一次性明文"), null);
});
for (const kind of ["tab", "space", "user", "session"]) test(`credential plaintext clears on ${kind} change and is never restored from storage`, async () => {
  installAccessCreate(); await mount(); await newAccess(); await submit("创建 Agent 接入凭据"); assert.equal(field("一次性明文").value, token);
  if (kind === "tab") await click(button("能力目录"));
  if (kind === "space") await switchApp(app({ space: { ...app().space, id: "space-b" } }));
  if (kind === "user") await switchApp(app({ me: { ...app().me, id: "user-b" } }));
  if (kind === "session") await act(async () => window.dispatchEvent(new Event("session-expired")));
  assert.equal(field("一次性明文"), null); assert.doesNotMatch(body(), new RegExp(token));
  assert.equal(window.localStorage.length, 0); assert.equal(window.sessionStorage.length, 0);
});
for (const status of [409, 422, 500, 200]) test(`uncertain credential response ${status} never replays creation or shows server error secrets`, async () => {
  stub("POST", "/agent-access", () => json({ message: token, token, access: access() }, status)); await mount(); await newAccess(); await submit("创建 Agent 接入凭据");
  assert.equal(writes().length, 1); assert.equal(field("一次性明文"), null); assert.doesNotMatch(body(), new RegExp(token));
  assert.equal(button("确认创建一次性凭据").disabled, true); await submit("创建 Agent 接入凭据"); assert.equal(writes().length, 1);
  const before = calls.length; await click(button("只查询凭据列表")); assert.ok(calls.slice(before).every(call => call.method === "GET")); assert.equal(writes().length, 1);
});
test("credential network ambiguity and duplicate synchronous submits result in one attempt only", async () => {
  const pending = deferred(); stub("POST", "/agent-access", () => pending.promise); await mount(); await newAccess();
  await act(async () => { for (let i = 0; i < 3; i++) field("创建 Agent 接入凭据").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
  assert.equal(writes().length, 1); await act(async () => pending.resolve(json({}, 503))); assert.equal(button("确认创建一次性凭据").disabled, true);
});
test("late credential success after page navigation cannot display or copy plaintext", async () => {
  const pending = deferred(); stub("POST", "/agent-access", () => pending.promise); await mount(); await newAccess(); await submit("创建 Agent 接入凭据");
  const request = writes()[0], payload = JSON.parse(request.body); await click(button("能力目录")); assert.equal(request.signal.aborted, true);
  await act(async () => pending.resolve(json({ access: access({ expires_at: payload.expires_at }), token }, 201)));
  assert.equal(field("一次性明文"), null); assert.deepEqual(copied, []); assert.equal(downloads.length, 0);
});
test("credential revocation uses metadata ETag and normal idempotency without mutating capabilities", async () => {
  stub("GET", "/agent-access?space_id=space-a", accessList([access()]));
  stub("POST", "/agent-access/access-1/revoke", call => { const value = access({ revision: 2, revoked_at: "2026-09-12T01:00:00Z" }); stub("GET", "/agent-access?space_id=space-a", accessList([value])); return json(value); });
  await mount(); await click(button("Agent接入")); await click(button("撤销接入凭据")); await change(field("凭据撤销原因"), "停止该接入"); await submit("撤销接入凭据");
  assert.equal(writes().length, 1); assert.equal(writes()[0].headers.get("If-Match"), '"1"'); assert.ok(writes()[0].headers.get("Idempotency-Key"));
  assert.deepEqual(JSON.parse(writes()[0].body), { reason: "停止该接入" }); assert.match(body(), /已撤销/);
});
test("credential source code has no console or persistent token storage path", () => {
  const text = readFileSync(resolve(sourceDir, "CapabilitiesAccess.tsx"), "utf8"); assert.doesNotMatch(text, /console\.|localStorage|sessionStorage/);
  assert.match(text, /request_id: crypto.randomUUID/); assert.match(text, /response.status !== 201/);
});
test("late capability data cannot populate another space or resurrect an editor", async () => {
  const pending = deferred(); stub("GET", "/capability-versions/cap-v1", () => pending.promise); await mount(); await click(button("查看能力"));
  const old = calls.at(-1); await switchApp(app({ space: { ...app().space, id: "space-b" } })); assert.equal(old.signal.aborted, true);
  await act(async () => pending.resolve(json(detail({ name: "OLD SECRET" })))); assert.doesNotMatch(body(), /OLD SECRET/); assert.equal(field("能力定义编辑器"), null);
});
test("run preflight is aborted on identity change and cannot emit a late step POST", async () => {
  await mount(); await openRun(); await click(button("读取下一步")); await change(field("工作底稿"), "底稿"); await change(field("步骤说明"), "说明");
  const pending = deferred(); stub("GET", "/capability-runs/run-1", () => pending.promise); await submit("步骤报告表单");
  const request = calls.at(-1); await switchApp(app({ me: { ...app().me, id: "user-b" } })); assert.equal(request.signal.aborted, true);
  await act(async () => pending.resolve(json(run()))); assert.equal(writes().length, 0);
});

test("advanced JSON rejects hidden extra fields without applying or submitting them", async () => {
  await mount(); await edit(); await change(field("高级定义 JSON"), JSON.stringify({ ...definition(), arbitrary_shell: "do not execute" }));
  await click(button("应用 JSON 到编辑器")); assert.match(body(), /包含不支持的字段/); assert.equal(writes().length, 0);
  assert.ok(definitionErrors(definition({ inputs: [{ ...definition().inputs[0], secret: "extra" }] })).length);
  assert.ok(definitionErrors(definition({ source_version_ids: ["../agent-access"] })).length);
});
test("source binding changes block draft save before PUT and preserve the edited definition", async () => {
  await mount(); await edit(); await change(field("能力名称"), "来源变化时保留");
  stub("GET", "/capability-versions/cap-v1", detail({ source_bindings: [{ version_id: sourceId, access_epoch: 2 }] }));
  await submit("能力定义编辑器"); assert.equal(writes().length, 0); assert.match(body(), /来源绑定已变化/); assert.equal(field("能力名称").value, "来源变化时保留");
});
test("expired source fencing withdraws previously loaded source text and step controls", async () => {
  stub("GET", "/capability-versions/cap-v1", detail({ definition: definition({ source_version_ids: [sourceId] }), source_bindings: [sourceBinding()] }));
  stub("GET", "/capability-runs/run-1/sources", { run_id: "run-1", scope: "reference", source_bindings: [sourceBinding()], notes: [], records: [sourceRecord({ block_id: "b", text: "PREVIOUS_SOURCE_BODY" })] });
  await mount(); await openRun(); await click(button("读取绑定来源")); assert.match(body(), /PREVIOUS_SOURCE_BODY/);
  stub("GET", "/capability-runs/run-1/next", () => json({ code: "CAPABILITY_SOURCE_CHANGED", message: "来源已变化" }, 409));
  await click(button("读取下一步")); assert.doesNotMatch(body(), /PREVIOUS_SOURCE_BODY/); assert.equal(field("步骤报告表单"), null);
});
test("a genuine network error during credential creation cannot be retried or reflected as a secret", async () => {
  stub("POST", "/agent-access", () => { throw new Error(token); }); await mount(); await newAccess(); await submit("创建 Agent 接入凭据");
  assert.equal(writes().length, 1); assert.doesNotMatch(body(), new RegExp(token)); await submit("创建 Agent 接入凭据"); assert.equal(writes().length, 1);
});
test("a malformed credential receipt cannot reveal a token for another space", async () => {
  stub("POST", "/agent-access", call => json({ access: access({ expires_at: JSON.parse(call.body).expires_at, space_id: "other-space" }), token }, 201));
  await mount(); await newAccess(); await submit("创建 Agent 接入凭据"); assert.equal(field("一次性明文"), null); assert.doesNotMatch(body(), new RegExp(token));
});
for (const status of [401, 403]) test(`credential create ${status} clears the scope without reading a secret-bearing error body`, async () => {
  stub("POST", "/agent-access", () => json({ token, message: token }, status)); await mount(); await newAccess(); await submit("创建 Agent 接入凭据");
  assert.equal(field("一次性明文"), null); assert.doesNotMatch(body(), new RegExp(token)); assert.match(body(), /旧数据与一次性凭据已清除/);
});
test("expired validity or an empty permission set cannot issue an Agent credential", async () => {
  await mount(); await newAccess(); await change(field("凭据有效期"), "2000-01-01T00:00"); await click(field("确认知识库范围和有效期"));
  await submit("创建 Agent 接入凭据"); assert.equal(writes().length, 0);
  await change(field("凭据有效期"), "2099-01-01T00:00"); await click(field("读取能力定义")); await click(field("确认知识库范围和有效期"));
  await submit("创建 Agent 接入凭据"); assert.equal(writes().length, 0);
});
test("credential revoke conflicts are not silently replayed", async () => {
  stub("GET", "/agent-access?space_id=space-a", accessList([access()])); stub("POST", "/agent-access/access-1/revoke", fail(412, token));
  await mount(); await click(button("Agent接入")); await click(button("撤销接入凭据")); await change(field("凭据撤销原因"), "停止接入"); await submit("撤销接入凭据");
  assert.equal(writes().length, 1); assert.doesNotMatch(body(), new RegExp(token)); assert.match(body(), /凭据状态已变化/); assert.equal(document.querySelector('form[aria-label="撤销接入凭据"]'), null);
});
test("a failed download permission check never leaves a prepared skill payload on screen", async () => {
  stub("GET", "/capability-versions/cap-v1/skill", fail(403)); await mount(); await click(button("查看能力")); await click(button("导出技能包"));
  assert.equal(field("可下载技能包"), null); assert.equal(downloads.length, 0); assert.match(body(), /权限已失效/);
});
test("dependency checkboxes actually update the definition and a user-created cycle blocks saving", async () => {
  await mount(); await edit();
  const dependency = field("步骤 2").querySelector('.capabilities-dependencies input[type="checkbox"]');
  assert.equal(dependency.checked, true); await click(dependency); assert.equal(dependency.checked, false);
  await click(button("载入当前定义 JSON")); assert.deepEqual(JSON.parse(field("高级定义 JSON").value).steps[1].depends_on, []);
  await click(dependency); assert.equal(dependency.checked, true);
  await click(field("步骤 1").querySelector('.capabilities-dependencies input[type="checkbox"]'));
  await submit("能力定义编辑器"); assert.equal(writes().length, 0); assert.match(body(), /步骤依赖存在循环/);
});
test("source editor binds an exact published version through the picker without any automatic write", async () => {
  await mount(); await edit(); await click(button("添加来源版本"));
  const picker = field("选择资料搜索").closest(".form-stack");
  await change(picker.querySelectorAll("select")[0], "knowledge-1"); await change(picker.querySelectorAll("select")[1], sourceId);
  await click(button("绑定所选来源")); await click(button("载入当前定义 JSON"));
  assert.deepEqual(JSON.parse(field("高级定义 JSON").value).source_version_ids, [sourceId]); assert.equal(writes().length, 0);
});
test("a mismatched resource/version picker response cannot be attached as a source", async () => {
  stub("GET", "/resources/knowledge-1/versions?limit=100", page([sourceVersion({ resource_id: "foreign-resource" })]));
  await mount(); await edit(); await click(button("添加来源版本"));
  const picker = field("选择资料搜索").closest(".form-stack");
  await change(picker.querySelectorAll("select")[0], "knowledge-1"); await change(picker.querySelectorAll("select")[1], sourceId);
  await click(button("绑定所选来源")); assert.match(body(), /请选择当前知识库已发布的确切来源版本/);
  await click(button("载入当前定义 JSON")); assert.deepEqual(JSON.parse(field("高级定义 JSON").value).source_version_ids, []); assert.equal(writes().length, 0);
});

test("contract: starter declares read_bound_sources and arbitrary user tools remain valid", () => {
  assert.deepEqual(capabilityStarter().steps[0].required_tools, ["read_bound_sources"]);
  const custom = definition(); custom.steps[0].required_tools = ["my_agent.read_local", "custom-tool"];
  assert.deepEqual(definitionErrors(custom), []);
});
test("contract: reported outputs can be submitted with an empty optional note", async () => {
  stub("POST", "/capability-runs/run-1/steps/prepare", () => { const value = run({ revision: 6, state: "WAITING_HUMAN" }); installRun(value); return json(value); });
  await mount(); await openRun(); await click(button("读取下一步")); await change(field("工作底稿"), "只有实际产物也可报告");
  await submit("步骤报告表单"); assert.equal(writes().length, 1);
  assert.deepEqual(JSON.parse(writes()[0].body), { status: "reported", outputs: { workpaper: "只有实际产物也可报告" }, note: "" });
});
test("contract: blocked reports are not held up by incomplete typed success outputs", async () => {
  const def = definition(); def.steps[0].outputs[0].type = "json";
  stub("GET", "/capability-versions/cap-v1", detail({ definition: def })); installRun(run(), next({ ready_steps: [def.steps[0]] }));
  stub("POST", "/capability-runs/run-1/steps/prepare", () => { const value = run({ revision: 6, state: "BLOCKED" }); installRun(value); return json(value); });
  await mount(); await openRun(); await click(button("读取下一步")); await change(field("工作底稿"), "unfinished JSON");
  await change(field("报告状态"), "blocked"); assert.equal(field("工作底稿"), null);
  await change(field("步骤说明"), "暂缺材料"); await submit("步骤报告表单");
  assert.deepEqual(JSON.parse(writes()[0].body), { status: "blocked", outputs: {}, note: "暂缺材料" });
});
test("contract: next exposes actual inputs and prior reports without mixing output_fields with output values", async () => {
  const def = definition();
  const value = run({ state: "WAITING_HUMAN", inputs: { task_goal: "本次真实填写的目标" }, steps: run().steps.map((row, i) => ({
    ...def.steps[i], output_fields: def.steps[i].outputs, ...row,
    ...(i === 0 ? { state: "REPORTED", outputs: { workpaper: "PREVIOUS_REAL_OUTPUT" }, report_channel: "agent_token", reported_by: "user-a" } : {}),
  })) });
  installRun(value, next({ state: "WAITING_HUMAN", ready_steps: [], waiting_human_steps: [def.steps[1]] }));
  await mount(); await openRun(); assert.match(field("本次运行输入").textContent, /本次真实填写的目标/);
  await click(button("读取下一步")); assert.match(field("前序步骤产物").textContent, /PREVIOUS_REAL_OUTPUT/);
  assert.ok(field("人工核对步骤")); assert.equal(writes().length, 0);
});
test("contract: mismatched output_fields are rejected before guiding or reporting a step", async () => {
  installRun(run({ steps: run().steps.map((row, i) => i ? row : { ...row, output_fields: [] }) }));
  await mount(); await openRun(); assert.match(body(), /output_fields 与冻结能力定义不一致/); assert.equal(field("步骤报告表单"), null);
});
for (const change of [{ inputs: { task_goal: "OTHER INPUT" } }, { previous_outputs: { foreign: { data: "OTHER OUTPUT" } } }])
  test(`contract: next rejects mismatched ${Object.keys(change)[0]}`, async () => {
    stub("GET", "/capability-runs/run-1/next", next(change)); await mount(); await openRun(); await click(button("读取下一步"));
    assert.equal(field("步骤报告表单"), null); assert.match(body(), /运行状态已变化/); assert.doesNotMatch(body(), /OTHER INPUT|OTHER OUTPUT/);
  });
test("contract: source records retain block hashes and legal status separately from whole-version bindings", async () => {
  stub("GET", "/capability-versions/cap-v1", detail({ definition: definition({ source_version_ids: [sourceId] }), source_bindings: [sourceBinding()] }));
  stub("GET", "/capability-runs/run-1/sources", { run_id: "run-1", scope: "reference", source_bindings: [sourceBinding()], notes: [], records: [sourceRecord()] });
  await mount(); await openRun(); await click(button("读取绑定来源"));
  assert.match(field("运行绑定来源").textContent, /未确认/); assert.match(body(), /原文块摘要（非整份版本摘要）/);
  assert.ok(body().includes("d".repeat(64))); assert.equal(writes().length, 0);
});
test("contract: missing bound-version source records cannot masquerade as a complete source read", async () => {
  stub("GET", "/capability-versions/cap-v1", detail({ definition: definition({ source_version_ids: [sourceId] }), source_bindings: [sourceBinding()] }));
  stub("GET", "/capability-runs/run-1/sources", { run_id: "run-1", scope: "reference", source_bindings: [sourceBinding()], notes: [], records: [] });
  await mount(); await openRun(); await click(button("读取绑定来源")); assert.equal(field("运行绑定来源"), null); assert.match(body(), /来源响应与本次绑定版本不一致/);
});
test("contract: historical approved source selection reaches the server release check without requiring the current pointer", async () => {
  stub("GET", "/resources/knowledge-1", source({ active_version_id: "newer-version" }));
  stub("PUT", "/capabilities/cap-1", call => { const result = detail({ revision: 4, definition: JSON.parse(call.body).definition, source_bindings: [sourceBinding()] });
    stub("GET", "/capability-versions/cap-v1", result); stub("GET", "/capabilities?space_id=space-a", catalog([result])); return json(result); });
  await mount(); await edit(); await click(button("添加来源版本")); const picker = field("选择资料搜索").closest(".form-stack");
  await change(picker.querySelectorAll("select")[0], "knowledge-1"); await change(picker.querySelectorAll("select")[1], sourceId); await click(button("绑定所选来源"));
  await submit("能力定义编辑器"); assert.equal(writes().length, 1); assert.deepEqual(JSON.parse(writes()[0].body).definition.source_version_ids, [sourceId]);
});
test("contract: owner can revoke a credential after losing content access but cannot create or reopen content", async () => {
  stub("GET", "/capabilities?space_id=space-a", fail(404)); stub("GET", "/agent-access?space_id=space-a", accessList([access()], false));
  stub("POST", "/agent-access/access-1/revoke", () => { const value = access({ revision: 2, revoked_at: "2026-09-13T00:00:00Z" }); stub("GET", "/agent-access?space_id=space-a", accessList([value], false)); return json(value); });
  await mount(); await click(button("Agent接入")); assert.equal(button("创建 Agent 接入凭据").disabled, true);
  await click(button("撤销接入凭据")); await change(field("凭据撤销原因"), "已退出该库"); await submit("撤销接入凭据");
  assert.equal(writes().length, 1); await click(button("能力目录")); assert.match(body(), /权限已失效/);
});
test("contract: credential and Agent labels are bounded before any one-time creation or run POST", async () => {
  await mount(); await newAccess(); await change(field("凭据名称"), "名".repeat(201)); await click(field("确认知识库范围和有效期"));
  await submit("创建 Agent 接入凭据"); assert.equal(writes().length, 0); assert.match(body(), /不能超过 200/);
  await click(button("能力目录")); await click(button("查看能力")); await click(button("试运行能力"));
  await change(field("本次工作目标"), "目标"); await change(field("Agent 标记"), "a".repeat(201)); await submit("能力试运行输入");
  assert.equal(writes().length, 0); assert.match(body(), /Agent 标记最多 200/);
});
for (const workflow_json of [null, [], "{}"])
  test(`contract: export rejects a non-object workflow_json (${JSON.stringify(workflow_json)})`, async () => {
    stub("GET", "/capability-versions/cap-v1/skill", { filename: "SKILL.md", skill_markdown: "# Skill", workflow_json, connector_markdown: "Instructions" });
    await mount(); await click(button("查看能力")); await click(button("导出技能包")); assert.equal(field("可下载技能包"), null); assert.equal(downloads.length, 0);
  });
