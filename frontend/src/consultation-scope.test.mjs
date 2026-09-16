// node --test src/consultation-scope.test.mjs
// Real page, picker, API serialization and React hooks; synthetic DOM/data only.
// All fetches are intercepted. Nothing is written to disk or sent to a service.
import test, { beforeEach, afterEach, after } from "node:test";
import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import Module, { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import { qwen, bge, profiles, selection, frozenSelection, retrievalKey } from "./retrievalProfiles.test-fixtures.mjs";

const require = createRequire(import.meta.url);
const { JSDOM } = require("jsdom");
const dom = new JSDOM('<!doctype html><div id="root"></div>', {
  url: "http://consultation.test/",
  pretendToBeVisual: true,
});
const { window } = dom;
for (const key of [
  "window", "document", "HTMLElement", "HTMLDialogElement", "Element",
  "Node", "MutationObserver", "Event", "MouseEvent", "FormData",
]) globalThis[key] = key === "window" ? window : window[key];
globalThis.getComputedStyle = window.getComputedStyle.bind(window);
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
window.matchMedia = (media) => ({
  media, matches: media.includes("reduce") && !media.includes("no-preference"),
  addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
});
window.HTMLElement.prototype.scrollIntoView = function () {};
window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
window.HTMLDialogElement.prototype.close = function () { this.open = false; };
const clipboard = [];
Object.defineProperty(globalThis, "navigator", { configurable: true, value: window.navigator });
Object.defineProperty(window.navigator, "clipboard", {
  value: { async writeText(value) { clipboard.push(value); } },
});
const React = require("react");
const { act } = React;
const h = React.createElement;
const { createRoot } = require("react-dom/client");
const gsap = require("gsap").gsap;
const { ScrollTrigger } = require("gsap/dist/ScrollTrigger");
const srcDir = dirname(fileURLToPath(import.meta.url));
const performanceMeasurements = [];
const measureBaseline = process.env.CONSULTATION_PERF_BASELINE === "1";
const measuredSource = process.env.CONSULTATION_PERF_SOURCE ?? join(srcDir, "ConsultationPage.tsx");
globalThis.__consultationRenderCounts = {};
const compiled = await build({
  stdin: {
    contents: 'export { ConsultationPage } from "./ConsultationPage"; export { RetrievalPanel } from "./RetrievalPanel"; export { AppContext } from "./ui"; export { setSession, clearSession } from "./api";',
    resolveDir: srcDir,
    loader: "tsx",
  },
  bundle: true, platform: "node", format: "cjs",
  external: ["react", "react-dom", "react-dom/*", "react/jsx-runtime", "gsap", "gsap/ScrollTrigger", "@gsap/react"],
  loader: { ".css": "empty" }, write: false, logLevel: "silent",
  plugins: [{ name: "count-consultation-renders", setup(builder) {
    builder.onLoad({ filter: /ConsultationPage\.tsx$/ }, async ({ path }) => ({
      loader: "tsx", contents: (await readFile(measuredSource, "utf8")).replace(
        /(function (AnswerView|RunStatus|AnswerPreview)\([^]*?\)\s*\{)/g,
        (match, opening, name) => `${opening}\nglobalThis.__consultationRenderCounts["${name}"] = (globalThis.__consultationRenderCounts["${name}"] ?? 0) + 1;`),
    }));
  } }],
});
const runtime = new Module(join(srcDir, "consultation-scope-test-runtime.cjs"));
runtime.filename = join(srcDir, "consultation-scope-test-runtime.cjs");
runtime.paths = Module._nodeModulePaths(srcDir);
const runtimeRequire = runtime.require.bind(runtime);
runtime.require = (id) => id === "gsap" ? gsap
  : id === "gsap/ScrollTrigger" ? { ScrollTrigger } : runtimeRequire(id);
runtime._compile(compiled.outputFiles[0].text, runtime.filename);
const { ConsultationPage, RetrievalPanel, AppContext, setSession, clearSession } = runtime.exports;

const referenceNote = "可使用待复核资料，结果仅供参考、不代表现行制度。";
const fixtureThread = { id: "thread-fixture", space_id: "space-fixture", title: "合成历史咨询", created_at: "2026-09-08T00:00:00Z" };
const fixtureSource = {
  id: "resource-fixture", name: "合成来源", active_version_id: "version-fixture",
};
const fixtureCitation = {
  id: "E1", resource_id: fixtureSource.id, version_id: fixtureSource.active_version_id,
  block_id: "block-fixture", source_title: fixtureSource.name, excerpt: "合成原文摘录",
  locator: { label: "合成段落 1" }, content_sha256: "synthetic-hash",
};
function answer(overrides = {}) {
  return {
    run_id: "run-fixture", status: "NEEDS_CLARIFICATION", review_status: "REQUIRES_EXPERT",
    mode: "answer", summary: "合成回答摘要", scope: { product_type: "合成产品" },
    facts: [{ name: "已知事实", value: "合成值", origin: "USER", certainty: "CONFIRMED" }],
    missing_facts: [{ field: "fund_label", question: "请补充基金标识", why_needed: "用于核对适用范围" }],
    claims: [{ id: "C1", text: "合成依据与解答", evidence_ids: ["E1"] }],
    citations: [fixtureCitation], solution: null, limitations: ["合成适用限制"],
    required_sources: ["合成待补资料"], generated_at: "2026-09-08T00:00:00Z", ...overrides,
  };
}
function run(overrides = {}) {
  return {
    id: "run-fixture", thread_id: fixtureThread.id, job_id: "job-fixture",
    state: "COMPLETED", invalidated: false, error_code: null, question: "合成问题",
    answer_scope: "formal", answer: answer(), created_at: "2026-09-08T00:00:00Z",
    model_snapshot: { execution_mode: "deterministic_clarification", model_invoked: false, evidence_count: 0 },
    ...overrides,
  };
}
function diagnostic(overrides = {}) {
  return {
    scope: "formal", basis: "current_access", observed_at: "2026-09-08T06:30:00Z",
    message: "当前可访问资料中没有符合本轮范围的匹配证据。",
    note: "按当前访问权限重新计算，仅供定位资格排除原因。",
    visible_resource_count: 7, eligible_resource_count: 2, excluded_resource_count: 5,
    reasons: [
      { code: "LEGAL_STATUS_UNDETERMINED", label: "效力状态尚未确定", count: 3 },
      { code: "NO_MATCHING_ELIGIBLE_EVIDENCE", label: "未找到匹配的合格证据" },
    ],
    next_step: "核对资料适用范围后，手动发起新的提问。", ...overrides,
  };
}
function zeroEvidenceRun(overrides = {}) {
  return run({
    answer_scope_origin: "explicit",
    answer: answer({ status: "INSUFFICIENT_EVIDENCE", claims: [], citations: [] }),
    evidence_diagnostic: diagnostic(), ...overrides,
  });
}
function modelOption(id, overrides = {}) {
  return {
    connection_id: `connection-${id}`, connection_name: `合成连接 ${id}`, provider_id: "openai",
    kind: "direct", protocol: "responses", model_id: id, model_name: `合成模型 ${id}`,
    brand: "openai", configured: true, enabled: true, selectable: true,
    allow_document_transfer: true, ...overrides,
  };
}
const openedVersions = [];
const context = {
  me: { id: "user-fixture", display_name: "合成用户", csrf_token: "synthetic-csrf", spaces: [] },
  space: { id: fixtureThread.space_id, name: "合成空间", revision: 1, roles: ["admin"] },
  refresh: 0, notify() {}, bump() {}, navigate() {}, openResource() {}, ask() {},
  openVersion(...args) { openedVersions.push(args); },
};
let root;
let threads;
let storedRuns;
let calls;
let unexpected;
let options;
let systemStatus;
let respondToRun;
let intercept;
const originalFetch = globalThis.fetch;
const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { "Content-Type": "application/json" },
});

beforeEach(() => {
  window.localStorage.clear();
  threads = [];
  storedRuns = [];
  calls = [];
  unexpected = [];
  options = [];
  systemStatus = { answer_scopes: ["formal", "reference"], model: { provider: "http", configured: true } };
  respondToRun = undefined;
  intercept = undefined;
  clipboard.length = 0;
  openedVersions.length = 0;
  globalThis.fetch = async (url, init) => {
    // No fallback to real fetch, even for an unexpected request.
    if (typeof url !== "string" || !url.startsWith("/api/v1/")) {
      unexpected.push(String(url));
      throw new Error("External requests are forbidden in this fixture");
    }
    const path = new URL(url, window.location.origin).pathname.slice("/api/v1".length);
    const call = { path, url, signal: init.signal, method: init.method, body: init.body ? JSON.parse(init.body) : undefined };
    calls.push(call);
    const custom = intercept?.(call);
    if (custom !== undefined) return custom;
    if (init.method === "GET") {
      if (path === "/threads") return json({ items: threads, next_cursor: null });
      if (threads.some((t) => path === `/threads/${t.id}`))
        return json({ items: storedRuns.filter((r) => path === `/threads/${r.thread_id}`), next_cursor: null });
      if (path === "/resources") return json({ items: [fixtureSource], next_cursor: null });
      if (path === "/system/status") return json(systemStatus);
      if (path === "/model-options") return json({ items: options, default: null });
      if (path === "/retrieval/profiles") return json({ default_profile_id: null, items: [], enabled: false });
      if (/^\/runs\/[^/]+\/events$/.test(path)) {
        const existing = storedRuns.find(run => path === `/runs/${run.id}/events`);
        return new Response(`event: stage\ndata: ${JSON.stringify({ run_id: path.split("/")[2], state: existing?.state ?? "RUNNING", stage: "queued" })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } });
      }
      const existing = storedRuns.find((r) => path === `/runs/${r.id}`);
      if (existing) return json(existing);
    }
    if (init.method === "POST") {
      if (path === "/threads") {
        const next = { ...fixtureThread, ...call.body, id: `thread-${threads.length + 1}` };
        threads.push(next);
        return json(next);
      }
      if (/^\/threads\/[^/]+\/runs$/.test(path)) {
        const next = respondToRun?.(call) ?? run({
          id: `run-${storedRuns.length + 1}`, thread_id: path.split("/")[2],
          question: call.body.question, answer_scope: call.body.answer_scope,
        });
        storedRuns.push(next);
        return json(next);
      }
    }
    unexpected.push(`${init.method} ${path}`);
    throw new Error("Unexpected synthetic request: " + path);
  };
});
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  root = undefined;
  clearSession();
  globalThis.fetch = originalFetch;
  assert.deepEqual(unexpected, [], "all requests must be handled by synthetic fixtures");
  for (const { body } of sentRuns()) {
    assert.ok(["reference", "formal"].includes(body.answer_scope), "every run request must have an explicit scope");
    assert.equal("answer_scope_origin" in body, false, "scope provenance is server-derived");
    assert.equal("evidence_diagnostic" in body, false, "current diagnostics must never be sent back as run data");
    assert.equal(body.reasoning_strategy, "model_first");
    assert.equal(body.require_model, true, "the composer must not silently request extractive-only answers");
  }
});
after(() => {
  ScrollTrigger.disable();
  gsap.ticker.sleep();
  window.close();
});
after(async () => {
  if (process.env.CONSULTATION_PERF_REPORT) await writeFile(process.env.CONSULTATION_PERF_REPORT,
    JSON.stringify({ observed_at: new Date().toISOString(), kind: measureBaseline ? "before" : "after",
      source_sha256: createHash("sha256").update(await readFile(measuredSource)).digest("hex"),
      real_model_calls: 0, measurements: performanceMeasurements }, null, 2) + "\n");
});

async function mount(props = {}) {
  setSession(context.me);
  root = createRoot(document.getElementById("root"));
  await act(async () => root.render(h(AppContext.Provider, { value: context }, h(ConsultationPage, props))));
}
async function refreshApp(refresh) {
  await act(async () => root.render(h(AppContext.Provider, { value: { ...context, refresh } }, h(ConsultationPage))));
}
async function history(runs) {
  threads = [fixtureThread];
  storedRuns = runs;
  await mount();
  await click(document.querySelector(".history-item > button"));
}
async function click(element) {
  assert.ok(element, "expected clickable element");
  await act(async () => element.click());
}
const byText = (text, scope = document) => [...scope.querySelectorAll("button")]
  .find((button) => button.textContent.trim() === text);
const modelTrigger = () => document.querySelector('[aria-label="选择答疑模型"]');
const statusRefresh = () => document.querySelector('[aria-label="刷新答疑服务状态"]');
const scopePicker = () => document.querySelector(".consultation-scope-selector select");
const questionInput = () => document.querySelector('[aria-label="咨询问题"]');
const messages = () => [...document.querySelectorAll(".consultation-run")];
const sentRuns = () => calls.filter((c) => c.method === "POST" && c.path.endsWith("/runs"));
const sentThreads = () => calls.filter((c) => c.method === "POST" && c.path === "/threads");
const sendButton = () => document.querySelector('[aria-label="发送问题"]');
async function submitWithoutButton() {
  for (const modifier of [null, "ctrlKey", "metaKey"]) {
    await act(async () => questionInput().dispatchEvent(new window.KeyboardEvent("keydown", {
      key: "Enter", ...(modifier ? { [modifier]: true } : {}), bubbles: true, cancelable: true,
    })));
  }
  await act(async () => questionInput().form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })));
}
async function assertReferenceBlocked(text = "合成待就绪问题") {
  await change(questionInput(), text);
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(sendButton().disabled, true);
  await click(sendButton());
  await submitWithoutButton();
  assert.equal(sentThreads().length, 0, "a blocked question must not create an empty thread");
  assert.equal(sentRuns().length, 0);
  assert.equal(questionInput().value, text);
}
async function change(element, value) {
  assert.ok(element, "expected editable element");
  const prototype = element.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype
    : element.tagName === "SELECT" ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(prototype, "value").set.call(element, value);
    element.dispatchEvent(new window.Event(element.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
  });
}
async function send(text = "合成新问题") {
  await change(questionInput(), text);
  await click(document.querySelector('[aria-label="发送问题"]'));
  assert.ok(sentRuns().length > 0, "expected a serialized run request");
}

const preferenceKey = (userId = context.me.id) => `fund-kb:consultation-model:v1:${encodeURIComponent(userId)}`;
const retrievalPicker = () => document.querySelector('[aria-label="选择检索方案"]');
const retrievalPreference = (value = context) => retrievalKey(value.me.id, value.space.id);
function dualConsultation(items = [qwen, bge], defaultId = "qwen") {
  intercept = ({ method, path }) => method === "GET" && path === "/retrieval/profiles"
    ? json(profiles(items, defaultId)) : undefined;
}

test("new consultation submits the server Qwen default explicitly; metadata alone never generates", async () => {
  dualConsultation([bge, qwen]);
  await mount();
  assert.equal(retrievalPicker().value, "qwen");
  assert.equal(window.localStorage.getItem(retrievalPreference()), null);
  assert.equal(sentRuns().length, 0); assert.equal(sentThreads().length, 0);
  await send();
  assert.deepEqual(sentRuns()[0].body.retrieval_selection, selection(qwen));
});

test("the server can change its default without an implicit hardcoded Qwen fallback", async () => {
  dualConsultation([qwen, bge], "bge"); await mount(); await send();
  assert.equal(retrievalPicker().value, "bge");
  assert.deepEqual(sentRuns()[0].body.retrieval_selection, selection(bge));
});

test("retrieval preferences persist only an ID across remounts and new conversations", async () => {
  dualConsultation(); await mount(); await change(retrievalPicker(), "bge");
  assert.equal(window.localStorage.getItem(retrievalPreference()), "bge");
  assert.equal(window.localStorage.length, 1);
  await remount(); assert.equal(retrievalPicker().value, "bge");
  await click(byText("新建咨询")); assert.equal(retrievalPicker().value, "bge");
  await send(); assert.deepEqual(sentRuns()[0].body.retrieval_selection, selection(bge));
  await click(byText("恢复默认")); await remount();
  assert.equal(retrievalPicker().value, "qwen");
  assert.equal(window.localStorage.getItem(retrievalPreference()), null);
});

test("retrieval preferences isolate both user and space and restore each prior choice", async () => {
  dualConsultation(); await mount(); await change(retrievalPicker(), "bge");
  const otherSpace = { ...context, space: { ...context.space, id: "space-two" } };
  await renderContext(otherSpace); assert.equal(retrievalPicker().value, "qwen");
  await change(retrievalPicker(), "qwen");
  const otherUser = { ...context, me: { ...context.me, id: "user-two" } };
  await renderContext(otherUser); assert.equal(retrievalPicker().value, "qwen");
  await change(retrievalPicker(), "qwen");
  await renderContext(context); assert.equal(retrievalPicker().value, "bge");
  assert.equal(window.localStorage.getItem(retrievalPreference()), "bge");
  assert.equal(window.localStorage.getItem(retrievalPreference(otherSpace)), "qwen");
  assert.equal(window.localStorage.getItem(retrievalPreference(otherUser)), "qwen");
  assert.equal(sentRuns().length, 0);
});

for (const unavailable of ["removed", "not-indexed", "model-missing"]) test(`saved ${unavailable} retrieval preference blocks every submit path without falling back`, async () => {
  const items = unavailable === "removed" ? [qwen] : [qwen, { ...bge, available: false,
    ...(unavailable === "model-missing" ? { model_ready: false } : { index_ready: false }) }];
  dualConsultation(items);
  window.localStorage.setItem(retrievalPreference(), "bge");
  await mount(); await assertReferenceBlocked();
  assert.equal(retrievalPicker().value, "bge");
  assert.equal(window.localStorage.getItem(retrievalPreference()), "bge");
  await click(byText("恢复默认"));
  assert.equal(sentRuns().length, 0);
  await click(sendButton());
  assert.deepEqual(sentRuns()[0].body.retrieval_selection, selection(qwen));
});

for (const code of [404, 501]) test(`legacy profiles HTTP ${code} permits unselected legacy requests but preserves a saved unavailable ID`, async () => {
  intercept = ({ path }) => path === "/retrieval/profiles" ? json({}, code) : undefined;
  await mount(); await send();
  assert.equal("retrieval_selection" in sentRuns()[0].body, false);
  await act(async () => root.unmount()); root = undefined;
  window.localStorage.setItem(retrievalPreference(), "bge");
  calls = []; await mount(); await assertReferenceBlocked();
  assert.equal(retrievalPicker().value, "bge");
});

for (const code of [401, 403, 500]) test(`profiles HTTP ${code} never downgrades to a legacy omitted selection`, async () => {
  intercept = ({ path }) => path === "/retrieval/profiles" ? json({}, code) : undefined;
  await mount(); await assertReferenceBlocked();
  assert.match(document.body.textContent, /检索方案读取失败/);
});

for (const [label, payload] of [
  ["null", null], ["missing enabled", { items: [] }],
  ["invalid fingerprint", profiles([{ ...qwen, fingerprint: "invalid" }, bge])],
  ["missing dimensions", profiles([{ ...qwen, dimensions: undefined }, bge])],
  ["string availability", profiles([{ ...qwen, available: "true" }, bge])],
  ["string readiness", profiles([{ ...qwen, can_index: "false" }, bge])],
  ["duplicate ID", profiles([qwen, qwen])],
  ["default not in registry", profiles([bge])],
]) test(`malformed profile registry fails closed: ${label}`, async () => {
  intercept = ({ path }) => path === "/retrieval/profiles" ? json(payload) : undefined;
  await mount(); await assertReferenceBlocked();
});

test("a pending profiles read gates thread creation and becoming ready never auto-submits", async () => {
  let resolveProfiles;
  intercept = ({ path }) => path === "/retrieval/profiles" ? new Promise(resolve => { resolveProfiles = resolve; }) : undefined;
  await mount(); await assertReferenceBlocked();
  await act(async () => resolveProfiles(json(profiles())));
  assert.equal(sentThreads().length, 0); assert.equal(sentRuns().length, 0);
  await click(sendButton());
  assert.deepEqual(sentRuns()[0].body.retrieval_selection, selection(qwen));
});

test("ID-only preferences refresh the fingerprint from the server rather than browser storage", async () => {
  window.localStorage.setItem(retrievalPreference(), "bge");
  const newer = { ...bge, fingerprint: "c".repeat(64) };
  dualConsultation([qwen, newer]); await mount(); await send();
  assert.deepEqual(sentRuns()[0].body.retrieval_selection, selection(newer));
  assert.equal(window.localStorage.getItem(retrievalPreference()), "bge");
});

test("submission freezes retrieval ID and fingerprint before delayed thread creation and later preference updates", async () => {
  let resolveThread; let registered = profiles();
  intercept = ({ path, method }) => path === "/retrieval/profiles" ? json(registered)
    : method === "POST" && path === "/threads" ? new Promise(resolve => { resolveThread = resolve; }) : undefined;
  await mount(); await change(questionInput(), "冻结检索选择"); await click(sendButton());
  assert.equal(retrievalPicker().disabled, true);
  const key = retrievalPreference();
  await act(async () => {
    window.localStorage.setItem(key, "bge");
    window.dispatchEvent(new window.StorageEvent("storage", { key, newValue: "bge" }));
  });
  registered = profiles([{ ...qwen, fingerprint: "d".repeat(64) }, bge]);
  await refreshApp(1); await submitWithoutButton();
  await act(async () => { threads = [fixtureThread]; resolveThread(json(fixtureThread)); });
  assert.equal(sentRuns().length, 1);
  assert.deepEqual(sentRuns()[0].body.retrieval_selection, selection(qwen));
  assert.equal(retrievalPicker().value, "bge");
});

test("history uses each returned frozen selection and never backfills legacy metadata from the picker", async () => {
  dualConsultation();
  await history([
    run({ id: "bge-history", model_snapshot: { retrieval_selection: frozenSelection(bge) } }),
    run({ id: "qwen-history", model_snapshot: { retrieval_selection: frozenSelection(qwen) } }), run({ id: "legacy-history" }),
  ]);
  assert.equal(retrievalPicker().value, "qwen");
  const rowText = () => messages().map(row => row.querySelector(".retrieval-scheme-used").textContent);
  const before = rowText();
  assert.match(before[0], /BAAI\/bge-m3/); assert.match(before[1], /Qwen\/Qwen3/);
  assert.match(before[2], /未记录/);
  assert.match(messages()[0].querySelector(".consultation-execution-details").textContent, new RegExp(bge.fingerprint));
  await change(retrievalPicker(), "bge"); assert.deepEqual(rowText(), before);
  await click(byText("补充事实", messages()[1])); await click(sendButton());
  assert.deepEqual(sentRuns()[0].body.retrieval_selection, selection(bge));
  assert.equal(sentRuns()[0].body.parent_run_id, "qwen-history");
});

for (const boundary of ["user", "space", "unmount"]) test(`a delayed thread response cannot create a run after ${boundary} changes`, async () => {
  let resolveThread;
  intercept = ({ path, method }) => path === "/retrieval/profiles" ? json(profiles())
    : method === "POST" && path === "/threads" ? new Promise(resolve => { resolveThread = resolve; }) : undefined;
  await mount(); await change(questionInput(), "原身份问题"); await click(sendButton());
  const signal = sentThreads()[0].signal;
  if (boundary === "unmount") { await act(async () => root.unmount()); root = undefined; }
  else await renderContext(boundary === "user"
    ? { ...context, me: { ...context.me, id: "changed-user" } }
    : { ...context, space: { ...context.space, id: "changed-space" } });
  assert.equal(signal.aborted, true);
  await act(async () => resolveThread(json(fixtureThread)));
  assert.equal(sentRuns().length, 0);
  assert.equal(messages().length, 0);
});

test("a pending old profile response cannot replace the new identity's server default", async () => {
  let resolveOld; let reads = 0;
  intercept = ({ path }) => path === "/retrieval/profiles"
    ? ++reads === 1 ? new Promise(resolve => { resolveOld = resolve; }) : json(profiles([qwen, bge], "bge")) : undefined;
  await mount();
  const signal = calls.find(call => call.path === "/retrieval/profiles").signal;
  await renderContext({ ...context, space: { ...context.space, id: "space-new" } });
  await act(async () => resolveOld(json(profiles())));
  assert.equal(signal.aborted, true);
  assert.equal(retrievalPicker().value, "bge");
});

test("settings selection is reused by consultation, and consultation selection is reused by settings", async () => {
  intercept = ({ path, url }) => {
    if (path === "/retrieval/profiles") return json(profiles());
    if (path === "/retrieval/status") {
      const item = new URL(url, window.location.origin).searchParams.get("profile_id") === "qwen" ? qwen : bge;
      return json({ space_id: context.space.id, mode: "hybrid", enabled: true,
        retrieval_selection: frozenSelection(item), vector: { backend: "qdrant", mode: "remote", available: true, status: "ready" },
        embedding: { mode: "transformers", model: item.model, dimensions: item.dimensions },
        coverage: {}, permissions: { can_index: true }, active_job: null, notes: [] });
    }
  };
  await mount();
  await act(async () => root.render(h(AppContext.Provider, { value: context }, h(RetrievalPanel))));
  await change(retrievalPicker(), "bge");
  await renderContext(context); assert.equal(retrievalPicker().value, "bge");
  await send(); assert.deepEqual(sentRuns()[0].body.retrieval_selection, selection(bge));
  await change(retrievalPicker(), "qwen");
  await act(async () => root.render(h(AppContext.Provider, { value: context }, h(RetrievalPanel))));
  assert.equal(retrievalPicker().value, "qwen");
  assert.equal(sentRuns().length, 1);
});

test("blocked browser storage retains an ID across navigation and explicitly clearing it restores defaults", async () => {
  dualConsultation();
  const originalSet = window.Storage.prototype.setItem;
  const originalRemove = window.Storage.prototype.removeItem;
  window.Storage.prototype.setItem = () => { throw new Error("storage blocked"); };
  window.Storage.prototype.removeItem = () => { throw new Error("storage blocked"); };
  try {
    await mount(); await change(retrievalPicker(), "bge"); await remount();
    assert.equal(retrievalPicker().value, "bge");
    await click(byText("恢复默认")); await remount();
    assert.equal(retrievalPicker().value, "qwen");
    assert.equal(sentRuns().length, 0);
  } finally {
    window.Storage.prototype.setItem = originalSet;
    window.Storage.prototype.removeItem = originalRemove;
    window.dispatchEvent(new window.StorageEvent("storage", { key: retrievalPreference() }));
  }
});

async function chooseModel(index = 0) {
  await click(modelTrigger());
  await click(document.querySelectorAll('[role="option"]')[index]);
}
async function remount() {
  await act(async () => root.unmount());
  root = undefined;
  await mount();
}
async function renderContext(value) {
  setSession(value.me);
  await act(async () => root.render(h(AppContext.Provider, { value }, h(ConsultationPage))));
}
async function enter(overrides = {}, timeStamp) {
  const event = new window.KeyboardEvent("keydown", {key:"Enter",bubbles:true,cancelable:true,...overrides});
  if (timeStamp !== undefined) Object.defineProperty(event,"timeStamp",{value:timeStamp});
  await act(async () => questionInput().dispatchEvent(event));
  return event;
}

test("the chosen model survives page remounts, new conversations and a different library", async () => {
  options = [modelOption("first"), modelOption("second")];
  await mount();
  await chooseModel();
  assert.deepEqual(JSON.parse(window.localStorage.getItem(preferenceKey())), {
    version:1, connection_id:"connection-first", model_id:"first",
  });
  await remount();
  assert.match(modelTrigger().title,/合成模型 first/);
  await click(byText("新建咨询"));
  assert.match(modelTrigger().title,/合成模型 first/);
  await renderContext({...context,space:{...context.space,id:"another-library"}});
  assert.match(modelTrigger().title,/合成模型 first/);
  assert.equal(sentThreads().length,0);
  assert.equal(sentRuns().length,0);
});

test("a saved model loads on a fresh page and is the model serialized by Enter", async () => {
  options = [modelOption("saved")];
  window.localStorage.setItem(preferenceKey(),JSON.stringify({version:1,connection_id:"connection-saved",model_id:"saved"}));
  await mount();
  assert.match(modelTrigger().title,/合成模型 saved/);
  await change(questionInput(),"合成回车发送问题");
  const event = await enter();
  assert.equal(event.defaultPrevented,true);
  assert.equal(sentThreads().length,1);
  assert.equal(sentRuns().length,1);
  assert.deepEqual(sentRuns()[0].body.model_selection,{connection_id:"connection-saved",model_id:"saved"});
  assert.equal(sentRuns()[0].body.question,"合成回车发送问题");
  assert.equal(questionInput().value,"");
  assert.match(modelTrigger().title,/合成模型 saved/);
});

test("switching the model replaces the preference; clearing it survives another remount", async () => {
  options = [modelOption("first"),modelOption("second")];
  await mount();
  await chooseModel();
  await chooseModel(1);
  await remount();
  assert.match(modelTrigger().title,/合成模型 second/);
  await click(modelTrigger());
  await click(byText("清除模型选择"));
  assert.equal(window.localStorage.getItem(preferenceKey()),null);
  await remount();
  assert.equal(modelTrigger().getAttribute("data-state"),"idle");
  assert.equal(sentRuns().length,0);
});

test("model preferences are isolated by user even when the component stays mounted", async () => {
  options = [modelOption("first"),modelOption("second")];
  await mount();
  await chooseModel();
  const other = {...context,me:{...context.me,id:"another-user"}};
  await renderContext(other);
  assert.equal(modelTrigger().getAttribute("data-state"),"idle");
  await chooseModel(1);
  assert.equal(JSON.parse(window.localStorage.getItem(preferenceKey(other.me.id))).model_id,"second");
  assert.equal(JSON.parse(window.localStorage.getItem(preferenceKey())).model_id,"first");
  await renderContext(context);
  assert.match(modelTrigger().title,/合成模型 first/);
  assert.equal(sentRuns().length,0);
});

test("a removed or unapproved saved model is retained as unavailable, never silently replaced", async () => {
  options = [modelOption("first"),modelOption("second")];
  await mount();
  await chooseModel();
  options = [modelOption("second")];
  await remount();
  assert.equal(modelTrigger().getAttribute("data-state"),"unavailable");
  assert.match(modelTrigger().title,/之前选择的模型暂不可用/);
  assert.equal(JSON.parse(window.localStorage.getItem(preferenceKey())).model_id,"first");
  options = [modelOption("first",{allow_document_transfer:false})];
  await refreshApp(1);
  assert.equal(modelTrigger().getAttribute("data-state"),"unavailable");
  await click(modelTrigger());
  assert.equal(document.querySelector('[role="option"]').disabled,true);
  assert.equal(sentRuns().length,0);
});

for (const stored of ["invalid JSON","null",JSON.stringify({version:2,connection_id:"x",model_id:"y"}),JSON.stringify({version:1,connection_id:{},model_id:"y"})]) {
  test(`malformed model preference does not break the composer: ${stored}`,async()=>{
    window.localStorage.setItem(preferenceKey(),stored);
    await mount();
    assert.equal(modelTrigger().getAttribute("data-state"),"idle");
    assert.equal(sentRuns().length,0);
  });
}

test("a newer selection in another tab updates the model without submitting", async () => {
  options = [modelOption("first"),modelOption("second")];
  await mount();
  await chooseModel();
  await act(async()=>{
    window.localStorage.setItem(preferenceKey(),JSON.stringify({version:1,connection_id:"connection-second",model_id:"second"}));
    window.dispatchEvent(new window.StorageEvent("storage",{key:preferenceKey(),storageArea:window.localStorage}));
  });
  assert.match(modelTrigger().title,/合成模型 second/);
  await act(async()=>{
    window.localStorage.removeItem(preferenceKey());
    window.dispatchEvent(new window.StorageEvent("storage",{key:preferenceKey(),storageArea:window.localStorage}));
  });
  assert.equal(modelTrigger().getAttribute("data-state"),"idle");
  assert.equal(sentRuns().length,0);
});

test("blocked browser storage still preserves selection during same-session navigation", async () => {
  options = [modelOption("first")];
  await mount();
  const descriptor=Object.getOwnPropertyDescriptor(window,"localStorage");
  const restricted={...context,me:{...context.me,id:"storage-restricted-user"}};
  try {
    Object.defineProperty(window,"localStorage",{configurable:true,get(){throw new Error("storage unavailable");}});
    await renderContext(restricted);
    await chooseModel();
    assert.match(modelTrigger().title,/合成模型 first/);
    await act(async()=>root.render(h(AppContext.Provider,{value:restricted},h("div",null,"other menu"))));
    await renderContext(restricted);
    assert.match(modelTrigger().title,/合成模型 first/);
    assert.equal(sentRuns().length,0);
  } finally {Object.defineProperty(window,"localStorage",descriptor);}
});

test("Shift+Enter remains a newline and holding Enter never submits",async()=>{
  await mount();
  await change(questionInput(),"合成多行问题");
  assert.equal((await enter({shiftKey:true})).defaultPrevented,false);
  assert.equal((await enter({altKey:true})).defaultPrevented,false);
  assert.equal((await enter({repeat:true})).defaultPrevented,true);
  assert.equal(sentThreads().length,0);
  assert.equal(sentRuns().length,0);
  assert.equal(questionInput().value,"合成多行问题");
});

test("IME composition and keyCode 229 cannot send a question",async()=>{
  await mount();
  await change(questionInput(),"合成中文输入");
  await act(async()=>questionInput().dispatchEvent(new window.CompositionEvent("compositionstart",{bubbles:true})));
  await enter();
  await enter({isComposing:true});
  const end = new window.CompositionEvent("compositionend",{bubbles:true});
  await act(async()=>questionInput().dispatchEvent(end));
  await enter({},end.timeStamp+1);
  await enter({keyCode:229},end.timeStamp+100);
  assert.equal(sentThreads().length,0);
  assert.equal(sentRuns().length,0);
  await enter({},end.timeStamp+150);
  assert.equal(sentRuns().length,1,"a separate Enter after composition sends normally");
});

test("Enter shares the synchronous in-flight guard with the arrow and form",async()=>{
  let resolveThread;
  intercept=({method,path})=>method==="POST"&&path==="/threads" ? new Promise(resolve=>{resolveThread=resolve;}) : undefined;
  await mount();
  await change(questionInput(),"合成防重复问题");
  await act(async()=>{
    questionInput().dispatchEvent(new window.KeyboardEvent("keydown",{key:"Enter",bubbles:true,cancelable:true}));
    questionInput().dispatchEvent(new window.KeyboardEvent("keydown",{key:"Enter",bubbles:true,cancelable:true}));
  });
  await enter();
  assert.equal(sentThreads().length,1);
  assert.equal(sentRuns().length,0);
  await act(async()=>{threads=[fixtureThread];resolveThread(json(fixtureThread));});
  assert.equal(sentRuns().length,1);
});

test("a cleared live textarea cannot submit stale React question state",async()=>{
  await mount();
  await change(questionInput(),"旧草稿，不应提交");
  Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,"value").set.call(questionInput(),"");
  await submitWithoutButton();
  assert.equal(sentThreads().length,0);
  assert.equal(sentRuns().length,0);
});

test("form submission snapshots the current visible draft, not a delayed previous render",async()=>{
  await mount();
  await change(questionInput(),"旧草稿");
  Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,"value").set.call(questionInput(),"当前可见的新草稿");
  await act(async()=>questionInput().form.dispatchEvent(new window.Event("submit",{bubbles:true,cancelable:true})));
  assert.equal(sentRuns().length,1);
  assert.equal(sentRuns()[0].body.question,"当前可见的新草稿");
  assert.equal(sentThreads()[0].body.title,"当前可见的新草稿");
});

test("compact composer removes all scope controls but submits an explicit reference scope", async () => {
  await mount();
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  const composer = document.querySelector(".composer-wrap");
  assert.equal(composer.querySelector(".consultation-scope-selector, .consultation-model-select"), null);
  assert.doesNotMatch(composer.textContent, /答疑范围|正式业务答疑|当前选择用于下一次提问/);
  assert.match(composer.textContent, /回答仅供参考/);
  assert.equal(document.querySelector(".composer-service-state"), null);
  await send();
  assert.equal(sentRuns()[0].body.answer_scope, "reference");
  assert.equal(sentRuns()[0].body.mode, "auto");
  assert.equal("model_selection" in sentRuns()[0].body, false);
  assert.match(messages()[0].textContent, /资料辅助答疑/);
  assert.ok(messages()[0].textContent.includes(referenceNote));
});

test("new questions and new conversations use reference without a scope selection section", async () => {
  await mount();

  await change(document.querySelector('[aria-label="回答方式"]'), "solution");
  await send();
  assert.equal(sentRuns()[0].body.answer_scope, "reference");
  assert.equal(sentRuns()[0].body.mode, "solution");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.match(messages()[0].querySelector(".consultation-run-scope").textContent, /资料辅助答疑/);

  await click(byText("新建咨询"));
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(messages().length, 0);
});

test("history retains each run's scope and legacy metadata when the next question changes", async () => {
  await history([
    run({ id: "formal", answer_scope: "formal" }),
    run({ id: "reference", answer_scope: "reference" }),
    run({ id: "legacy", answer_scope: undefined, model_snapshot: undefined }),
  ]);
  const labels = () => messages().map((m) => m.querySelector(".consultation-run-scope").textContent);
  const expected = ["正式业务答疑", "资料辅助答疑", "历史答疑范围未记录"];
  assert.deepEqual(labels(), expected);
  await change(questionInput(), "下一条合成问题");
  assert.deepEqual(labels(), expected);

  assert.deepEqual(labels(), expected);
  assert.equal(sentRuns().length, 0);
});

test("a rejected first run preserves the chosen scope and question for retry after thread creation", async () => {
  let rejected = false;
  intercept = ({ method, path }) => {
    if (method === "POST" && path.endsWith("/runs") && !rejected) {
      rejected = true;
      return json({ code: "SYNTHETIC_REJECTION", message: "合成请求拒绝" }, 403);
    }
  };
  await mount();

  await send("合成保留问题");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(questionInput().value, "合成保留问题");
  assert.equal(messages().length, 0);
  assert.match(document.querySelector('[role="alert"]').textContent, /合成请求拒绝/);
  await click(document.querySelector('[aria-label="发送问题"]'));
  assert.deepEqual(sentRuns().map((c) => c.body.answer_scope), ["reference", "reference"]);
  assert.equal(calls.filter((c) => c.method === "POST" && c.path === "/threads").length, 1);
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
});

for (const parentScope of ["reference", "formal", undefined]) {
  for (const actionLabel of ["补充事实", "填写缺失事实"]) {
    test(`${actionLabel} inherits ${parentScope ?? "legacy formal default"} without a scope picker`, async () => {
      await history([run({ answer_scope: parentScope })]);
      const expectedScope = parentScope ?? "formal";

      await click(byText(actionLabel, messages()[0]));
      assert.equal(scopePicker(), null, "scope controls are removed from the composer");
      assert.equal(scopePicker(), null, "scope controls are removed from the composer");
      if (!parentScope) assert.match(document.querySelector(".followup-label").textContent, /历史答疑范围未记录/);
      if (actionLabel === "填写缺失事实") {
        await change(document.querySelector(".context-panel input"), "合成基金标识");
      }
      await send("合成补充问题");
      const body = sentRuns()[0].body;
      assert.equal(body.answer_scope, expectedScope);
      assert.equal(body.parent_run_id, "run-fixture");
      if (actionLabel === "填写缺失事实") {
        assert.match(body.question, /补充事实：\nfund_label：合成基金标识/);
        assert.equal(body.context.fund_label, "合成基金标识");
      }
      assert.equal(scopePicker(), null, "scope controls are removed from the composer");
      assert.equal(scopePicker(), null, "scope controls are removed from the composer");
    });
  }
}

test("cancelling a follow-up restores the new-question scope and removes the parent from the payload", async () => {
  await history([run({ answer_scope: "formal" })]);
  await click(byText("补充事实", messages()[0]));
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  await click(document.querySelector('[aria-label="取消补充模式"]'));
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  await send();
  assert.equal("parent_run_id" in sentRuns()[0].body, false);
  assert.equal(sentRuns()[0].body.answer_scope, "reference");
});

test("execution status comes only from the invocation boolean, with zero/missing evidence kept distinct", async () => {
  await history([
    run({ id: "reference", answer_scope: "reference", model_snapshot: { model_id: "model-reference", execution_mode: "reference_grounded", model_invoked: true, evidence_count: 3 } }),
    run({ id: "http", model_snapshot: { model_id: "model-http", execution_mode: "http_grounded", model_invoked: true, evidence_count: 2 } }),
    run({ id: "clarification", model_snapshot: { model_id: "selected-only", execution_mode: "deterministic_clarification", model_invoked: false, evidence_count: 0 } }),
    run({ id: "extractive", model_snapshot: { execution_mode: "extractive", model_invoked: false, evidence_count: 1 } }),
    run({ id: "legacy", model_snapshot: { model_id: "legacy-selected", execution_mode: "http_grounded" } }),
    run({ id: "missing", model_snapshot: null }),
  ]);
  assert.deepEqual(messages().map((m) => m.querySelector(".consultation-invocation-state").textContent), [
    "模型已调用", "模型已调用", "未调用模型", "未调用模型", "历史调用状态未记录", "历史调用状态未记录",
  ]);
  for (const [index, mode] of ["资料辅助解答", "证据约束解答", "规则澄清", "证据摘录"].entries())
    assert.ok(messages()[index].textContent.includes(`执行方式：${mode}`));
  assert.match(messages()[0].textContent, /调用模型：model-reference/);
  assert.match(messages()[2].textContent, /所选模型：selected-only/);
  assert.match(messages()[2].textContent, /依据数量：0/);
  assert.match(messages()[4].textContent, /所选模型：legacy-selected/);
  assert.match(messages()[4].textContent, /依据数量未记录/);
  assert.match(messages()[5].textContent, /执行方式未记录/);
  for (const message of messages()) {
    assert.match(message.querySelector(".response-heading").textContent, /处理完成/);
    assert.doesNotMatch(message.textContent, /生成成功/);
  }
  assert.match(messages()[2].textContent, /需要补充事实/);
});

test("unknown execution modes and malformed/missing telemetry never imply a call or invented evidence", async () => {
  await history([
    run({ id: "missing", model_snapshot: { model_id: "model-only" } }),
    run({ id: "null", model_snapshot: { model_invoked: null, evidence_count: null } }),
    run({ id: "string", model_snapshot: { model_invoked: "true", execution_mode: "future_mode", evidence_count: "2" } }),
    run({ id: "negative", model_snapshot: { model_invoked: false, evidence_count: -1 } }),
    run({ id: "fractional", model_snapshot: { model_invoked: false, evidence_count: 0.5 } }),
  ]);
  for (const message of messages()) {
    assert.doesNotMatch(message.querySelector(".response-heading").textContent, /模型已调用/);
    assert.match(message.textContent, /依据数量未记录/);
  }
  assert.match(messages()[2].textContent, /未知方式（future_mode）/);
});

test("actual ModelPicker preserves transfer/capability gates and choosing a model does not invoke it", async () => {
  options = [
    modelOption("allowed"),
    modelOption("no-transfer", { allow_document_transfer: false }),
    modelOption("unconfigured", { configured: false }),
    modelOption("oauth", { protocol: "codex_app_server" }),
    modelOption("oauth-unknown", { protocol: "codex_app_server", selectable: undefined }),
    modelOption("blocked", { selectable: false }),
  ];
  await history([run({ model_snapshot: { model_id: "historical-selected" } })]);
  await click(modelTrigger());
  const buttons = [...document.querySelectorAll('[role="option"]')];
  assert.deepEqual(buttons.map((button) => button.disabled), [false, true, true, false, true, true]);
  await click(buttons[3]);
  assert.equal(sentRuns().length, 0);
  assert.match(modelTrigger().title, /合成模型 oauth/);
  assert.equal(modelTrigger().getAttribute("data-state"), "selected");
  assert.match(messages()[0].querySelector(".response-heading").textContent, /历史调用状态未记录/);
  assert.doesNotMatch(messages()[0].textContent, /调用模型：oauth/);
  await send();
  assert.deepEqual(sentRuns()[0].body.model_selection, { connection_id: "connection-oauth", model_id: "oauth" });
  assert.equal(sentRuns()[0].body.answer_scope, "reference");
  assert.match(messages()[1].querySelector(".response-heading").textContent, /未调用模型/);
});

test("answer rendering, source navigation, solution structure, and copied reference warning are preserved", async () => {
  await history([run({
    answer_scope: "reference", invalidated: true,
    answer: answer({ solution: {
      goal: "合成处理方案", preconditions: ["合成前提"], materials: ["合成材料"],
      steps: [{ id: "S1", action: "合成步骤", owner_role: "合成角色", inputs: ["合成输入"], output: "合成输出", verification: "合成验证", evidence_ids: ["E1"], depends_on: [] }],
      branches: [{ condition: "合成条件", action: "合成分支处理" }],
      completion_checks: ["合成完成检查"], escalation: ["合成升级处理"],
    } }),
  })]);
  const message = messages()[0];
  for (const text of ["合成回答摘要", "合成依据与解答", "合成原文摘录", "合成处理方案", "合成步骤", "合成完成检查", "合成适用限制", "合成待补资料", "来源已发生变化"])
    assert.ok(message.textContent.includes(text), text);
  await click(message.querySelector(".citation-chip"));
  await click(message.querySelector(".source-card"));
  assert.deepEqual(openedVersions, [["version-fixture", "block-fixture"], ["version-fixture", "block-fixture"]]);
  await click(message.querySelector('[aria-label="复制回答与引用"]'));
  assert.ok(clipboard[0].startsWith(`资料辅助答疑\n${referenceNote}`));
  assert.match(clipboard[0], /合成回答摘要[\s\S]*合成依据与解答[\s\S]*合成来源 · 合成段落 1 · version-fixture \/ block-fixture/);
  for (const label of ["有帮助", "问题反馈", "补充事实", "提交问题单", "填写缺失事实"])
    assert.ok(byText(label, message));
});

test("attachments and keyboard submit keep their existing payload while adding reference scope", async () => {
  await mount({ initialResource: fixtureSource });
  assert.match(document.querySelector(".attachment-chips").textContent, /合成来源/);
  await change(questionInput(), "合成快捷键问题");
  await act(async () => questionInput().dispatchEvent(new window.KeyboardEvent("keydown", {
    key: "Enter", ctrlKey: true, bubbles: true, cancelable: true,
  })));
  assert.equal(sentRuns().length, 1);
  assert.equal(sentRuns()[0].body.answer_scope, "reference");
  assert.deepEqual(sentRuns()[0].body.attachment_version_ids, ["version-fixture"]);
});

test("Wiki Markdown is a full readable answer, not a JSON success gate, and citations stay interactive", async () => {
  const markdown = "# FOF估值\n\n这是 **完整说明**。[E1]\n\n| 情况 | 处理 |\n|---|---|\n| 有净值 | 核对日期 |\n\nC < R。\n\n<script>alert(1)</script>\n\n![禁止加载](https://tracker.invalid/pixel)\n\n末尾完整内容。";
  await history([run({ answer_scope: "reference", answer: answer({format: "wiki_markdown",
    narrative_markdown: markdown, grounding_status: "SOURCE_LINKED", claims: [], solution: null,
    server_notice: "本系统未执行交易或审批。", quality_warnings: [{code: "BUSINESS_VERIFICATION_REQUIRED", message: "业务判断待复核。"}]}) })]);
  const panel = messages()[0].querySelector('[aria-label="完整综合答复"]');
  assert.ok(panel.querySelector('h1') && panel.querySelector('table') && panel.querySelector('strong'));
  assert.match(panel.textContent, /C < R/);
  assert.match(panel.textContent, /末尾完整内容/);
  assert.equal(panel.querySelector('script,img,iframe'), null);
  await click(panel.querySelector('[data-evidence-id="E1"]'));
  assert.deepEqual(openedVersions, [["version-fixture", "block-fixture"]]);
  await click(messages()[0].querySelector('[aria-label="复制回答与引用"]'));
  assert.ok(clipboard[0].includes(markdown) && clipboard[0].includes("业务判断待复核"));
  assert.match(messages()[0].textContent, /模型综合答复/);
});

test("unmatched Wiki narrative stays visible and unverified without invented source links", async () => {
  await history([run({answer: answer({format: "wiki_markdown", narrative_markdown: '普通答复 [E9999]。{"未闭合":',
    grounding_status: "UNVERIFIED", citations: [], claims: [], quality_warnings: []})})]);
  const panel = messages()[0].querySelector('[aria-label="完整综合答复"]');
  assert.match(panel.textContent, /普通答复/);
  assert.equal(panel.querySelector('[data-evidence-id]'), null);
  assert.match(messages()[0].textContent, /未绑定可核对的本地引用/);
  assert.doesNotMatch(messages()[0].textContent, /有据可循/);
});

test("Wiki range and grouped citations link the registered paragraphs without expanding arbitrary numbers", async () => {
  await history([run({answer: answer({format: "wiki_markdown", narrative_markdown:
    "[E1–E3]，表格引用 E1-E2，未绑定 E99999–E999999999999999999999。`E1–E3`", grounding_status:"SOURCE_LINKED",
    claims:[], citations:[fixtureCitation,{...fixtureCitation,id:"E2",block_id:"block-2"},{...fixtureCitation,id:"E3",block_id:"block-3"}], quality_warnings:[]})})]);
  const panel=messages()[0].querySelector('[aria-label="完整综合答复"]');
  assert.equal(panel.querySelector('[data-evidence-ids="E1,E2,E3"]').textContent,"E1–E3");
  assert.equal(panel.querySelector('[data-evidence-ids="E1,E2"]').textContent,"E1-E2");
  assert.equal(panel.querySelector('code button'),null);
  assert.equal(panel.querySelectorAll('button').length,2);
  await click(panel.querySelector('[data-evidence-ids="E1,E2,E3"]'));
  assert.deepEqual(openedVersions,[["version-fixture","block-fixture"]]);
  assert.equal(messages()[0].querySelectorAll('.wiki-answer-source-group .source-card').length,3);
});

test("unlimited Wiki waiting shows cancellation and real loaded-page counts without a countdown", async () => {
  await history([run({state: "RUNNING", phase: "READING_WIKI_PAGES", answer: null, model_snapshot: {
    model_invoked: true, execution_mode: "wiki_reading", wiki_reading: {catalog_pages: 1008, loaded_pages: 8,
      loaded_blocks: 120, loaded_characters: 30000, page_titles: ["合成完整知识页"], full_text_loaded: true},
    last_request: {phase: "wiki_notes", state: "waiting", attempt: 1, limits: {total_seconds: null,
      read_idle_seconds: null, connect_seconds: 10, max_output_tokens: 16384}}
  }})]);
  assert.match(messages()[0].textContent, /无固定思考时限，可随时取消/);
  assert.match(messages()[0].textContent, /1008 页 · 已加载全文：8 页 \/ 120 段/);
  assert.ok(byText("取消", messages()[0]));
  assert.doesNotMatch(messages()[0].textContent, /null 秒|本阶段上限：90/);
});

test("scoped reading exposes real batch progress without claiming the entire handbook was read", async () => {
  await history([run({state:"RUNNING",phase:"READING_WIKI_PAGES",answer:null,model_snapshot:{
    model_invoked:true,execution_mode:"wiki_reading",
    wiki_reading:{catalog_pages:1013,loaded_pages:4,loaded_blocks:86,loaded_characters:4300,
      page_titles:["合成手册"],full_text_loaded:false,scoped_source_pages:1,full_source_pages:0,source_sections:3},
    reading_progress:{stage:"reading_sections",current_batch:2,total_batches:3,completed_batches:1}
  }})]);
  const progress=messages()[0].querySelector('.consultation-reading-progress');
  assert.ok(progress);
  assert.match(progress.textContent,/已完成 1 \/ 3 批.*当前第 2 批/s);
  assert.equal(progress.querySelector('progress').value,1);
  assert.equal(progress.querySelector('progress').max,3);
  assert.match(messages()[0].textContent,/已按相关章节读取/);
  assert.doesNotMatch(messages()[0].textContent,/已加载全文：4/);
  assert.match(messages()[0].textContent,/不是整本已读/);
});

test("unknown or invalid batch totals never fabricate a percentage", async () => {
  await history([run({state:"RUNNING",answer:null,model_snapshot:{model_invoked:true,
    reading_progress:{stage:"loading_sections",current_batch:0,total_batches:0,completed_batches:0}}})]);
  const progress=messages()[0].querySelector('.consultation-reading-progress');
  assert.match(progress.textContent,/正在定位完整知识页与原文小节/);
  assert.equal(progress.querySelector('progress'),null);
  assert.doesNotMatch(progress.textContent,/0 \/ 0|NaN|100%/);
});

test("completed or cancelled runs do not retain a live processing progress bar", async () => {
  await history([run({state:"CANCELLED",answer:null,model_snapshot:{model_invoked:true,
    reading_progress:{stage:"reading_sections",current_batch:2,total_batches:4,completed_batches:1}}})]);
  assert.equal(messages()[0].querySelector('.consultation-reading-progress'),null);
});

test("polling replaces pending telemetry with the server's actual execution result", async () => {
  const pending = run({ state: "QUEUED", answer: null, answer_scope: "reference", model_snapshot: { model_invoked: false } });
  const completed = run({ answer_scope: "reference", model_snapshot: { model_id: "polled-model", model_invoked: true, execution_mode: "reference_grounded", evidence_count: 4 } });
  intercept = ({ method, path }) => method === "GET" && path === `/runs/${pending.id}` ? json(completed) : undefined;
  await history([pending]);
  assert.ok(calls.some((c) => c.path === `/runs/${pending.id}`));
  assert.match(messages()[0].querySelector(".response-heading").textContent, /模型已调用[\s\S]*处理完成/);
  assert.match(messages()[0].textContent, /依据数量：4/);
  assert.equal(document.querySelector(".pending-answer"), null);
});

test("failed tasks retain retry and never turn an error into successful generation", async () => {
  const failed = run({ state: "FAILED", answer: null, error_code: "SYNTHETIC_FAILURE" });
  intercept = ({ method, path }) => method === "POST" && path === `/jobs/${failed.job_id}/retry` ? json({}) : undefined;
  await history([failed]);
  assert.match(messages()[0].textContent, /未能完成本次咨询：SYNTHETIC_FAILURE/);
  await click(byText("重试本次任务"));
  assert.ok(calls.some((c) => c.path === `/jobs/${failed.job_id}/retry` && c.method === "POST"));
  assert.match(messages()[0].textContent, /未调用模型/);
  assert.doesNotMatch(messages()[0].textContent, /处理完成|生成成功/);
});

test("cancelled pending tasks keep the cancel action and final server state", async () => {
  const pending = run({ state: "RUNNING", answer: null });
  let cancelled = false;
  intercept = ({ method, path }) => {
    if (method === "POST" && path === `/jobs/${pending.job_id}/cancel`) {
      cancelled = true;
      return json({});
    }
    if (method === "GET" && path === `/runs/${pending.id}`)
      return json({ ...pending, state: cancelled ? "CANCELLED" : "RUNNING" });
  };
  await history([pending]);
  await click(byText("取消", messages()[0]));
  assert.equal(cancelled, true);
  assert.match(messages()[0].textContent, /本次咨询已取消/);
  assert.equal(document.querySelector(".pending-answer"), null);
});

test("diagnostic styling stays page-local and the shared picker retains transfer validation", async () => {
  const source = await readFile(new URL("./ConsultationPage.tsx", import.meta.url), "utf8");
  const css = await readFile(new URL("./consultation-scope.css", import.meta.url), "utf8");
  assert.match(source, /import "\.\/consultation-scope\.css"/);
  assert.match(source, /<ModelPicker[^>]*requireTransfer/);
  assert.equal((source.match(/setInterval\(/g) ?? []).length, 0, "the page delegates to one scoped polling owner");
  for (const block of css.split("}").filter((part) => part.trim())) {
    const selectors = block.split("{")[0].trim().split(",");
    for (const selector of selectors) assert.ok(selector.trim().startsWith(".consultation-layout "));
  }
});

test("model selection is an icon immediately after business context, not another form section", async () => {
  await mount();
  const trigger = modelTrigger();
  assert.ok(trigger);
  assert.equal(trigger.type, "button");
  assert.equal(trigger.textContent, "");
  assert.equal(trigger.getAttribute("aria-haspopup"), "dialog");
  assert.equal(trigger.getAttribute("aria-expanded"), "false");
  assert.ok(trigger.querySelector(".model-brand-icon"));
  assert.equal(trigger.parentElement.previousElementSibling.textContent.trim(), "业务背景");
  assert.equal(trigger.closest(".inline-actions").parentElement.tagName, "FOOTER");
  assert.equal(document.querySelector(".consultation-model-select, .model-field-label, .model-picker-hint"), null);
  assert.equal(questionInput().rows, 2);
  assert.match(document.getElementById(trigger.getAttribute("aria-describedby")).textContent, /未选择(?:个人)?模型.*不会自动发起调用/);
  assert.ok(calls.every((call) => call.method === "GET"));
});

test("icon picker switches brand and accessible description and clears without submitting", async () => {
  options = [modelOption("first"), modelOption("second", { brand: "anthropic" })];
  await mount();
  await click(modelTrigger());
  await click(document.querySelectorAll('[role="option"]')[0]);
  assert.match(modelTrigger().title, /合成模型 first/);
  assert.equal(modelTrigger().querySelector("img").getAttribute("src"), "/provider-icons/openai.svg");
  await click(modelTrigger());
  await click(document.querySelectorAll('[role="option"]')[1]);
  assert.match(modelTrigger().title, /合成模型 second/);
  assert.equal(modelTrigger().querySelector("img").getAttribute("src"), "/provider-icons/anthropic.svg");
  assert.equal(modelTrigger().textContent, "");
  await click(modelTrigger());
  await click(byText("清除模型选择"));
  assert.equal(modelTrigger().getAttribute("data-state"), "idle");
  assert.ok(modelTrigger().querySelector("svg.model-brand-fallback"));
  assert.equal(document.querySelector("dialog"), null);
  assert.equal(sentRuns().length, 0);
  assert.equal(sentThreads().length, 0);
});

test("an unavailable selected icon is explained and remains inspectable without a full-width field", async () => {
  options = [modelOption("selected")];
  await mount();
  await click(modelTrigger());
  await click(document.querySelector('[role="option"]'));
  options = [modelOption("selected", { allow_document_transfer: false })];
  await refreshApp(1);
  assert.equal(modelTrigger().getAttribute("data-state"), "unavailable");
  assert.match(modelTrigger().title, /明确授权/);
  await click(modelTrigger());
  assert.equal(document.querySelector('[role="option"]').disabled, true);
  assert.equal(sentRuns().length, 0);
  assert.equal(sentThreads().length, 0);
});

test("failed model-list loading is visible on the icon and can be retried inside the picker", async () => {
  intercept = ({ method, path }) => method === "GET" && path === "/model-options"
    ? json({ code: "SYNTHETIC_PICKER_FAILURE", message: "合成模型列表读取失败" }, 503) : undefined;
  await mount();
  assert.equal(modelTrigger().getAttribute("data-state"), "unavailable");
  assert.match(modelTrigger().title, /模型列表读取失败/);
  assert.equal(document.querySelector(".question-composer .model-field-label"), null);
  await click(modelTrigger());
  assert.match(document.querySelector("dialog").textContent, /合成模型列表读取失败/);
  intercept = undefined;
  options = [modelOption("recovered")];
  await click(document.querySelector('[aria-label="刷新可选模型"]'));
  await click(document.querySelector('[role="option"]'));
  assert.equal(modelTrigger().getAttribute("data-state"), "selected");
  assert.match(modelTrigger().title, /合成模型 recovered/);
  assert.equal(sentRuns().length, 0);
  assert.equal(sentThreads().length, 0);
});

test("execution metadata starts folded while model invocation, answer and warnings remain visible", async () => {
  await history([run({ answer_scope: "reference", model_snapshot: { model_invoked: true, evidence_count: 1 } })]);
  const message = messages()[0];
  const details = message.querySelector(".consultation-run-details");
  assert.equal(details.open, false);
  assert.equal(details.querySelector("summary").textContent, "执行记录");
  assert.equal(message.querySelector(".consultation-invocation-state").closest("details"), null);
  assert.equal(message.querySelector(".answer-body").closest("details"), null);
  assert.match(message.querySelector(".answer-body").textContent, /合成回答摘要|合成适用限制/);
  assert.match(details.textContent, /资料辅助答疑|依据数量：1/);
});

test("chat geometry fills remaining height and is isolated from the other workspaces", async () => {
  const css = await readFile(new URL("./consultation-layout.css", import.meta.url), "utf8");
  const rule = (selector) => css.slice(css.indexOf(selector + " {"), css.indexOf("}", css.indexOf(selector + " {")) + 1);
  assert.match(rule(".consultation-workspace"), /height: calc\(100dvh - var\(--app-topbar-height, 64px\)\)/);
  assert.match(rule(".consultation-workspace .conversation-scroll"), /flex: 1 1 0/);
  assert.match(rule(".consultation-workspace .conversation-scroll"), /min-height: 0/);
  assert.match(rule(".consultation-workspace .conversation-history"), /overflow: auto/);
  assert.match(rule(".consultation-workspace .composer-wrap"), /border: 0/);
  assert.match(rule(".consultation-workspace .question-composer textarea"), /height: 64px/);
  assert.match(css, /@media \(max-width: 600px\)/);
  assert.match(css, /grid-template-rows: auto minmax\(0, 1fr\)/);
  assert.match(css, /\.conversation-history \{ height: 72px; padding: 8px 12px/);
  assert.match(css, /\.history-item > button:first-child > span \{[^}]*white-space: nowrap; text-overflow: ellipsis/);
  assert.doesNotMatch(css, /(?:^|[;{])\s*zoom\s*:|transform:\s*scale/);
  assert.doesNotMatch(css, /\.app-shell\s*\{|\.main-content\s*\{|\.topbar\s*\{/);
});

test("chat shares the page light field and document sidebar tone while keeping reading surfaces opaque", async () => {
  const css = await readFile(new URL("./consultation-layout.css", import.meta.url), "utf8");
  const documents = await readFile(new URL("./document-center.css", import.meta.url), "utf8");
  const block = (text, selector) => text.slice(text.indexOf(selector + " {"), text.indexOf("}", text.indexOf(selector + " {")) + 1);
  assert.match(block(css, ".consultation-workspace"), /background: transparent/);
  assert.match(block(css, ".app-shell[data-page] .page-heading.consultation-heading"), /background: transparent/);
  const background = rule => rule.match(/background:\s*([^;]+);/)?.[1];
  assert.equal(background(block(css, ".app-shell .consultation-workspace .conversation-history")),
    background(block(documents, ".document-center .document-classification-panel")));
  for (const selector of [".conversation-main", ".composer-wrap", ".question-composer"])
    assert.equal(background(block(css, ".consultation-workspace " + selector)), "#fff");
});

test("narrow history labels retain full accessible text and a title despite visual ellipsis", async () => {
  const title = "合成的长咨询标题，需要完整保留以便识别".repeat(5);
  threads = [{ ...fixtureThread, title }];
  await mount();
  const entry = document.querySelector(".history-item > button");
  assert.equal(entry.title, title);
  assert.ok(entry.textContent.includes(title));
  assert.ok(entry.querySelector("small").textContent);
});

test("missing capabilities keep reference selected and block all entry points before creating a thread", async () => {
  systemStatus = { model: { provider: "http", configured: true } };
  await mount({ initialResource: fixtureSource });
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.match(document.querySelector(".composer-service-state").textContent, /答疑服务暂未就绪/);
  assert.ok(statusRefresh());
  await assertReferenceBlocked();
  assert.match(document.querySelector('[role="alert"]').textContent, /不会自动改用正式业务答疑/);
  systemStatus = { ...systemStatus, answer_scopes: ["formal", "reference"] };
  await click(statusRefresh());
  await change(document.querySelector('[aria-label="回答方式"]'), "solution");
  await send("合成恢复后问题");
  assert.deepEqual(sentRuns()[0].body, {
    question: "合成恢复后问题", mode: "solution", answer_scope: "reference", context: {}, attachment_version_ids: ["version-fixture"],
    reasoning_strategy: "model_first", require_model: true,
  });
  assert.equal(document.querySelector('[role="alert"]'), null);
  assert.match(messages()[0].textContent, /处理完成/);
  assert.match(messages()[0].querySelector(".consultation-run-scope").textContent, /资料辅助答疑/);
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(calls.filter((c) => c.path === "/system/status").length, 2);
});

for (const [label, scopes] of [
  ["formal-only", ["formal"]], ["empty", []], ["null", null],
  ["string", "formal,reference"], ["object", { reference: true }], ["other capability", ["reference_pending"]],
]) {
  test(`${label} capability cannot silently downgrade the fixed reference scope`, async () => {
    systemStatus = { answer_scopes: scopes, model: { provider: "http", configured: true, live_model_verified: true } };
    await mount();
    assert.equal(scopePicker(), null, "scope controls are removed from the composer");
    await assertReferenceBlocked();
    systemStatus = { ...systemStatus, answer_scopes: ["reference"] };
    await click(statusRefresh());
    await send();
    assert.equal(sentRuns()[0].body.answer_scope, "reference");
  });
}

test("reference capability must come from top-level system status, not selected/configured model metadata", async () => {
  systemStatus = { model: { provider: "http", configured: true, live_model_verified: true, answer_scopes: ["formal", "reference"] } };
  options = [modelOption("selected-model", { answer_scopes: ["formal", "reference"] })];
  await mount();
  await click(modelTrigger());
  await click(document.querySelector('[role="option"]'));
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  await assertReferenceBlocked();
  systemStatus = { ...systemStatus, answer_scopes: ["reference"] };
  await click(statusRefresh());
  await send();
  assert.equal(sentRuns()[0].body.answer_scope, "reference");
  assert.deepEqual(sentRuns()[0].body.model_selection, { connection_id: "connection-selected-model", model_id: "selected-model" });
});

test("manual status reload enables the preserved reference preference without selecting again or submitting", async () => {
  systemStatus = { model: { provider: "http", configured: true } };
  await mount();
  await change(questionInput(), "合成待发送问题");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  systemStatus = { ...systemStatus, answer_scopes: ["formal", "reference"] };
  assert.equal(sendButton().disabled, true, "server-side capability changes need an actual status reload");
  await click(statusRefresh());
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(questionInput().value, "合成待发送问题");
  assert.equal(sentRuns().length, 0);
  assert.equal(calls.filter((c) => c.method === "POST").length, 0);
  assert.equal(calls.filter((c) => c.path === "/system/status").length, 2);
  assert.equal(document.querySelector(".composer-service-state"), null);
  await click(document.querySelector('[aria-label="发送问题"]'));
  assert.equal(sentRuns()[0].body.answer_scope, "reference");
});

test("initial pending status blocks reference before any thread POST and readiness never auto-submits", async () => {
  let resolveStatus;
  intercept = ({ method, path }) => method === "GET" && path === "/system/status"
    ? new Promise((resolve) => { resolveStatus = resolve; }) : undefined;
  await mount();
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(statusRefresh().disabled, true);
  await assertReferenceBlocked("合成状态未到达问题");
  await act(async () => resolveStatus(json(systemStatus)));
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(sentRuns().length, 0);
  assert.equal(sentThreads().length, 0, "capability arrival cannot submit the waiting question");
  assert.equal(sendButton().disabled, false);
  await click(sendButton());
  assert.equal(sentRuns()[0].body.answer_scope, "reference");
});

test("failed status load blocks reference without changing it and recovers through existing reload", async () => {
  intercept = ({ method, path }) => method === "GET" && path === "/system/status"
    ? json({ code: "SYNTHETIC_STATUS_FAILURE", message: "合成状态读取失败" }, 503) : undefined;
  await mount();
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.match(document.querySelector(".composer-service-state").textContent, /无法读取答疑服务状态/);
  await assertReferenceBlocked();
  intercept = undefined;
  await click(statusRefresh());
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.doesNotMatch(document.body.textContent, /合成状态读取失败/);
  assert.equal(sentRuns().length, 0);
  await click(sendButton());
  assert.equal(sentRuns()[0].body.answer_scope, "reference");
  assert.equal(document.querySelector('[role="alert"]'), null);
});

test("a formal parent survives capability loss and recovery without exposing a scope control", async () => {
  await history([run({ answer_scope: "formal" })]);
  await click(byText("补充事实", messages()[0]));

  systemStatus = { ...systemStatus, answer_scopes: undefined };
  await refreshApp(1);
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  systemStatus = { ...systemStatus, answer_scopes: ["formal", "reference"] };
  await refreshApp(2);
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  await send();
  assert.equal(sentRuns()[0].body.answer_scope, "formal");
});

for (const actionLabel of ["补充事实", "填写缺失事实"]) {
  test(`${actionLabel} for a reference parent cannot downgrade through click, keyboard or form submission`, async () => {
    systemStatus = { ...systemStatus, answer_scopes: undefined };
    await history([run({ answer_scope: "reference" })]);
    await click(byText(actionLabel, messages()[0]));
    assert.equal(scopePicker(), null, "scope controls are removed from the composer");
    assert.equal(scopePicker(), null, "scope controls are removed from the composer");
    assert.match(document.querySelector(".composer-service-state").textContent, /答疑服务暂未就绪/);
    await change(questionInput(), "合成参考父轮补充");
    const sendButton = document.querySelector('[aria-label="发送问题"]');
    assert.equal(sendButton.disabled, true);
    await click(sendButton);
    await act(async () => questionInput().dispatchEvent(new window.KeyboardEvent("keydown", {
      key: "Enter", ctrlKey: true, bubbles: true, cancelable: true,
    })));
    await act(async () => questionInput().form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })));
    assert.equal(calls.filter((c) => c.method === "POST").length, 0);
    assert.equal(questionInput().value, "合成参考父轮补充");
    assert.match(document.querySelector('[role="alert"]').textContent, /不能降级为正式业务答疑/);
    systemStatus = { ...systemStatus, answer_scopes: ["formal", "reference"] };
    await click(statusRefresh());
    assert.equal(scopePicker(), null, "scope controls are removed from the composer");
    assert.equal(scopePicker(), null, "scope controls are removed from the composer");
    assert.equal(sendButton.disabled, false);
    await click(sendButton);
    assert.equal(sentRuns().length, 1);
    assert.equal(sentRuns()[0].body.answer_scope, "reference");
    assert.equal(sentRuns()[0].body.parent_run_id, "run-fixture");
  });
}

for (const parentScope of ["formal", undefined]) {
  test(`${parentScope ?? "legacy"} parent remains locked and explicitly sends formal without reference readiness`, async () => {
    systemStatus = { ...systemStatus, answer_scopes: undefined };
    await history([run({ answer_scope: parentScope })]);
    await click(byText("填写缺失事实", messages()[0]));
    assert.equal(scopePicker(), null, "scope controls are removed from the composer");
    assert.equal(scopePicker(), null, "scope controls are removed from the composer");
    await change(document.querySelector(".context-panel input"), "合成旧接口基金标识");
    await send();
    assert.equal(sentRuns()[0].body.answer_scope, "formal");
    assert.equal(sentRuns()[0].body.parent_run_id, "run-fixture");
    assert.equal(sentRuns()[0].body.context.fund_label, "合成旧接口基金标识");
  });
}

test("reference parent remains reference and blocks submission if a refreshed service loses capability", async () => {
  await history([run({ answer_scope: "reference" })]);
  await click(byText("补充事实", messages()[0]));
  assert.equal(document.querySelector('[aria-label="发送问题"]').disabled, false);
  systemStatus = { ...systemStatus, answer_scopes: ["formal"] };
  await refreshApp(1);
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(document.querySelector('[aria-label="发送问题"]').disabled, true);
  assert.match(messages()[0].querySelector(".consultation-run-scope").textContent, /资料辅助答疑/);
  assert.equal(sentRuns().length, 0);
  await click(document.querySelector('[aria-label="取消补充模式"]'));
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(sendButton().disabled, true);
  systemStatus = { ...systemStatus, answer_scopes: ["formal", "reference"] };
  await click(statusRefresh());
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
});

for (const statusState of ["pending", "error"]) {
  test(`a formal follow-up can be submitted while capability status is ${statusState}`, async () => {
    let resolveStatus;
    options = [modelOption("followup")];
    intercept = ({ method, path }) => method === "GET" && path === "/system/status"
      ? statusState === "pending"
        ? new Promise((resolve) => { resolveStatus = resolve; })
        : json({ code: "SYNTHETIC_STATUS_FAILURE", message: "合成状态读取失败" }, 503)
      : undefined;
    await history([run({ answer_scope: "formal" })]);
    await click(modelTrigger());
    await click(document.querySelector('[role="option"]'));
    await click(byText("补充事实", messages()[0]));
    await send();
    assert.equal(sentThreads().length, 0);
    assert.equal(sentRuns()[0].body.answer_scope, "formal");
    if (resolveStatus) await act(async () => resolveStatus(json(systemStatus)));
    assert.equal(sentRuns().length, 1, "status recovery must not rerun a completed question");
  });
}

test("an old server rejecting explicit scope does not trigger an omitted-scope fallback or an automatic retry", async () => {
  intercept = ({ method, path }) => method === "POST" && path.endsWith("/runs")
    ? json({ code: "EXTRA_FIELD", message: "合成旧接口拒绝范围字段" }, 422) : undefined;
  await mount();

  await send("合成接口拒绝问题");
  assert.equal(sentThreads().length, 1);
  assert.equal(sentRuns().length, 1);
  assert.equal(sentRuns()[0].body.answer_scope, "reference");
  assert.equal(questionInput().value, "合成接口拒绝问题");
  assert.equal(scopePicker(), null, "scope controls are removed from the composer");
  assert.match(document.querySelector('[role="alert"]').textContent, /合成旧接口拒绝范围字段/);
});

for (const initialScope of ["reference"]) {
  test(`${initialScope} and the full payload are fixed before delayed thread creation despite later UI/capability changes`, async () => {
    let resolveThread;
    options = [modelOption("first"), modelOption("later")];
    intercept = ({ method, path }) => method === "POST" && path === "/threads"
      ? new Promise((resolve) => { resolveThread = resolve; }) : undefined;
    await mount({ initialResource: fixtureSource });

    await click(modelTrigger());
    await click(document.querySelectorAll('[role="option"]')[0]);
    await change(questionInput(), "合成提交时问题");
    await click(sendButton());
    assert.equal(sentThreads().length, 1);
    assert.equal(sentRuns().length, 0);
    assert.equal(scopePicker(), null, "scope controls are removed from the composer");
    assert.equal(sendButton().disabled, true);
    assert.equal(byText("新建咨询").disabled, true);
    // Synthetic change events also exercise the handler behind the disabled UI.

    await change(questionInput(), "合成后续输入");
    await change(document.querySelector('[aria-label="回答方式"]'), "solution");
    await click(document.querySelector('[aria-label="移除附件"]'));
    await click(modelTrigger());
    assert.equal(modelTrigger().disabled, true, "model selection cannot change during submission");
    assert.equal(modelTrigger().getAttribute("data-state"), "selected", "busy must not imply a broken model connection");
    assert.equal(document.querySelector('[role="option"]'), null);
    systemStatus = { ...systemStatus, answer_scopes: ["formal"] };
    await refreshApp(1);
    assert.equal(scopePicker(), null, "scope controls are removed from the composer");
    await submitWithoutButton();
    assert.equal(sentThreads().length, 1, "busy keyboard/form events cannot create duplicate threads");
    assert.equal(sentRuns().length, 0);
    await act(async () => {
      threads = [fixtureThread];
      resolveThread(json(fixtureThread));
    });
    assert.equal(sentRuns().length, 1);
    assert.deepEqual(sentRuns()[0].body, {
      question: "合成提交时问题", mode: "auto", answer_scope: initialScope, context: {},
      reasoning_strategy: "model_first", require_model: true,
      attachment_version_ids: ["version-fixture"],
      model_selection: { connection_id: "connection-first", model_id: "first" },
    });
    assert.equal(scopePicker(), null, "scope controls are removed from the composer");
    assert.match(messages()[0].querySelector(".consultation-run-scope").textContent,
      initialScope === "reference" ? /资料辅助答疑/ : /正式业务答疑/);
  });
}

for (const parentScope of ["reference", "formal", undefined]) {
  test(`${parentScope ?? "legacy"} parent and explicit scope remain bound during a delayed follow-up`, async () => {
    let resolveRun;
    await history([
      run({ answer_scope: parentScope }),
      run({ id: "other-parent", answer_scope: parentScope === "reference" ? "formal" : "reference" }),
    ]);
    await click(byText("补充事实", messages()[0]));
    intercept = ({ method, path }) => method === "POST" && path.endsWith("/runs")
      ? new Promise((resolve) => { resolveRun = resolve; }) : undefined;
    await change(questionInput(), "合成锁定父轮补充");
    await click(sendButton());
    const expected = parentScope ?? "formal";
    assert.equal(sentThreads().length, 0);
    assert.equal(sentRuns().length, 1);
    assert.equal(sentRuns()[0].body.answer_scope, expected);
    assert.equal(sentRuns()[0].body.parent_run_id, "run-fixture");
    assert.equal(scopePicker(), null, "scope controls are removed from the composer");
    assert.equal(document.querySelector('[aria-label="取消补充模式"]').disabled, true);
    for (const label of ["补充事实", "填写缺失事实"]) {
      const otherParentButton = byText(label, messages()[1]);
      assert.equal(otherParentButton.disabled, true);
      await click(otherParentButton);
    }

    await submitWithoutButton();
    assert.equal(sentRuns().length, 1);
    await act(async () => resolveRun(json(run({ id: "followup", answer_scope: expected }))));
    assert.equal(sentRuns()[0].body.answer_scope, expected);
    assert.equal(sentRuns()[0].body.parent_run_id, "run-fixture");
  });
}

test("a pending run blocks click, both keyboard shortcuts and raw form submission", async () => {
  await history([run({ state: "RUNNING", answer: null })]);

  await change(questionInput(), "合成等待前轮问题");
  assert.equal(sendButton().disabled, true);
  await click(sendButton());
  await submitWithoutButton();
  assert.equal(sentThreads().length, 0);
  assert.equal(sentRuns().length, 0);
  assert.match(document.querySelector('[role="alert"]').textContent, /请等待当前咨询完成/);
});

test("whitespace-only questions cannot create threads even through raw form events", async () => {
  await mount();
  await change(questionInput(), "   \n ");
  assert.equal(sendButton().disabled, true);
  await submitWithoutButton();
  assert.equal(sentThreads().length, 0);
  assert.equal(sentRuns().length, 0);
  assert.match(document.querySelector('[role="alert"]').textContent, /请输入具体问题/);
});

test("a question without a selected or configured model cannot create an extractive-only run", async () => {
  systemStatus.model = { provider: "evidence", configured: true };
  await mount();
  await change(questionInput(), "合成需模型分析的问题");
  assert.equal(sendButton().disabled, true);
  await submitWithoutButton();
  assert.equal(sentThreads().length, 0);
  assert.equal(sentRuns().length, 0);
  assert.match(document.querySelector('[role="alert"]').textContent, /选择可用模型/);
});

test("model-first progress labels source-free analysis separately from local retrieval", async () => {
  await history([run({ state: "RUNNING", answer: null, phase: "PLANNING_QUESTION", model_snapshot: {
    model_invoked: true, planning_model_invoked: true, answer_model_invoked: false, model_request_count: 1,
  } })]);
  assert.match(messages()[0].querySelector('.pending-answer').textContent, /模型分析问题/);
  assert.doesNotMatch(messages()[0].querySelector('.pending-answer').textContent, /检索本地资料|排队/);
  assert.match(messages()[0].querySelector('.consultation-execution-details').textContent, /模型请求：1 次/);
  assert.equal(sentRuns().length, 0);
});

test("a complete model response is distinct from answer admission, with real budgets and usage", async () => {
  await history([run({ state: "FAILED", answer: null, phase: "FAILED", model_snapshot: {
    model_invoked: true, validation_status: "failed", model_request_count: 2,
    last_request: { phase: "synthesis", state: "received", attempt: 1, duration_ms: 39507,
      response_chars: 4500, finish_reason: "stop", limits: {
        total_seconds: 180, connect_seconds: 10, read_idle_seconds: 180, max_output_tokens: 8192,
      }, usage: { completion_tokens: 4055 } },
  }, failure_diagnostic: {code: "EXECUTION_OR_APPROVAL_UNVERIFIED", message: "合成待核验断言",
    phase: "synthesis", schema_errors: [], retryable: false, next_step: "合成修正后重试"} })]);
  const details = messages()[0].querySelector('.consultation-execution-details').textContent;
  assert.match(details, /模型已完整返回/);
  assert.match(details, /39.5 秒/);
  assert.match(details, /180 秒 · 8,192 输出 tokens/);
  assert.match(details, /4,055 tokens/);
  assert.match(details, /不是仍在思考/);
  assert.match(messages()[0].querySelector('.answer-failure').textContent, /已收到模型输出/);
  assert.equal(messages()[0].querySelector('.pending-answer'), null);
  assert.equal(sentRuns().length, 0);
});

test("missing historical receipt never invents elapsed time or returned output", async () => {
  await history([run({state: "FAILED", answer: null, error_code: "PROVIDER_TIMEOUT",
    model_snapshot: {model_invoked: true}})]);
  const text = messages()[0].querySelector('.consultation-execution-details').textContent;
  assert.doesNotMatch(text, /模型已完整返回|请求耗时|输出 tokens|返回：/);
});

test("preliminary analysis stays visibly unverified and disappears when a run is invalidated", async () => {
  const question_analysis = { source: "model_prior_knowledge_unverified", local_sources_loaded: 0, plan: {
    interpretation: "合成问题理解", initial_assessment: "合成待查证方向", search_queries: ["合成检索词"],
    focus_terms: ["合成术语"], decision_points: ["合成判断条件"], missing_facts: [],
  } };
  await history([run({ question_analysis }), run({ id: "invalid-plan", invalidated: true, question_analysis })]);
  const plan = messages()[0].querySelector('.answer-initial-analysis');
  assert.match(plan.textContent, /尚未核对本地依据|本阶段未读取本地知识/);
  assert.match(plan.textContent, /不是正式业务结论/);
  assert.match(plan.textContent, /合成检索词/);
  assert.equal(messages()[1].querySelector('.answer-initial-analysis'), null);
});

test("reused public plan is explicit and never represented as a cached final answer", async () => {
  const question_analysis = {source: "model_prior_knowledge_unverified", local_sources_loaded: 0,
    plan: {interpretation: "合成问题", initial_assessment: "合成计划", search_queries: ["输入核对"], focus_terms: []}};
  const model_snapshot = {planning_cache: {hit: true, saved_model_requests: 1,
    fresh_source_checks: true, final_answer_reused: false}};
  await history([run({question_analysis, model_snapshot}),
    run({id: "cold-plan", question_analysis, model_snapshot: {planning_cache: {hit: false}}}),
    run({id: "invalid-plan", invalidated: true, question_analysis, model_snapshot})]);
  const notice = messages()[0].querySelector('[data-planning-cache="reused"]');
  assert.match(notice.textContent, /省去一次重复规划/);
  assert.match(notice.textContent, /当前来源重新核验/);
  assert.match(notice.textContent, /未复用历史答案/);
  assert.equal(messages()[1].querySelector('[data-planning-cache="reused"]'), null);
  assert.equal(messages()[2].querySelector('[data-planning-cache="reused"]'), null);
});

test("final public analysis and treatment branches retain navigable exact source references", async () => {
  await history([run({ answer: answer({ analysis: {
    interpretation: "合成最终理解", checks: [{ title: "合成核对条件", reason: "合成来源支持说明", evidence_ids: ["E1"] }],
    branches: [{ condition: "合成条件成立", action: "合成处理方向", evidence_ids: ["E1"] }],
  } }) })]);
  const panel = messages()[0].querySelector('.answer-analysis');
  assert.match(panel.textContent, /合成核对条件|合成处理方向/);
  const refs = panel.querySelectorAll('button');
  assert.equal(refs.length, 2);
  await click(refs[1]);
  assert.equal(openedVersions[0][0], fixtureCitation.version_id);
});

test("a rejected synthesis shows field diagnostics without automatic retry", async () => {
  await history([run({ state: "FAILED", answer: null, failure_diagnostic: {
    code: "ANSWER_CONTENT_SCHEMA_INVALID", phase: "synthesis", message: "模型返回内容不符合输出结构。",
    schema_errors: [{ path: "$.claims[0].evidence_ids", rule: "minItems" }], retryable: false,
    next_step: "修正原因后再重试；不会自动重复调用模型。",
  } })]);
  const panel = messages()[0].querySelector('.answer-failure');
  assert.match(panel.textContent, /\$\.claims\[0\]\.evidence_ids/);
  assert.match(panel.textContent, /不会自动重复调用模型/);
  assert.equal(sentRuns().length, 0);
  assert.equal(calls.filter(c=>c.method === "POST").length, 0);
});

test("scope origin is server-recorded and missing/unknown history never implies an explicit user choice", async () => {
  const origins = ["explicit", "inherited", "legacy_default", "historical_unknown", undefined, "future_origin", "__proto__"];
  await history(origins.map((origin, index) => run({ id: `origin-${index}`, answer_scope_origin: origin })));
  const labels = () => messages().map((m) => m.querySelector(".consultation-run-scope-origin").textContent);
  const expected = [
    "范围来源：请求显式指定", "范围来源：继承父轮", "范围来源：旧接口默认",
    ...origins.slice(3).map(() => "范围来源：历史未记录，无法确认当时的请求选择"),
  ];
  assert.deepEqual(labels(), expected);

  assert.deepEqual(labels(), expected);
  assert.equal(sentRuns().length, 0);
});

for (const scope of ["formal", "reference"]) {
  test(`${scope} zero-evidence diagnostics label current ACL aggregates without changing historical scope or answers`, async () => {
    await history([zeroEvidenceRun({ answer_scope: scope, evidence_diagnostic: diagnostic({ scope }) })]);
    const message = messages()[0];
    const panel = message.querySelector('[aria-label="零证据诊断"]');
    assert.ok(panel);
    for (const text of ["未进入模型生成", "统计口径：当前访问权限（current_access）", "2026-09-08T06:30:00Z", "不代表该轮历史时点", "不会自动重跑历史问题", "资格排除原因不等于“资料不可信”", "核对资料适用范围"])
      assert.ok(panel.textContent.includes(text), text);
    assert.deepEqual([...panel.querySelectorAll("dl > div")].map((item) => [item.querySelector("dt").textContent, item.querySelector("dd").textContent]), [
      ["当前可读资料", "7"], ["当前符合范围资格", "2"], ["当前排除资料", "5"],
    ]);
    assert.deepEqual([...panel.querySelectorAll("li")].map((li) => li.textContent), ["效力状态尚未确定：3", "未找到匹配的合格证据"]);
    assert.match(message.querySelector(".consultation-execution-details").textContent, /依据数量：0/);
    assert.match(message.querySelector(".consultation-invocation-state").textContent, /^未调用模型$/);
    assert.match(message.querySelector(".answer-body").textContent, /合成回答摘要/);
    assert.ok(byText("补充事实", message));
    const before = panel.textContent;

    assert.equal(panel.textContent, before);
    assert.equal(message.querySelector(".consultation-run-scope").textContent, scope === "formal" ? "正式业务答疑" : "资料辅助答疑");
    await click(byText("补充事实", message));
    await send("合成诊断后补充");
    assert.equal(sentRuns()[0].body.answer_scope, scope);
    assert.equal(sentRuns()[0].body.parent_run_id, "run-fixture");
  });
}

test("diagnostic message, note, reason, timestamp and next step render as plain text with no injected DOM", async () => {
  const attack = '<img src="https://forbidden.test/x" onerror="window.injected=true"><script>window.injected=true</script>';
  const link = '[合成链接](javascript:alert(1))';
  await history([zeroEvidenceRun({ evidence_diagnostic: diagnostic({
    message: `合成说明 ${attack}`, note: `合成注释 ${attack}`, next_step: `合成下一步 ${link}`,
    observed_at: `合成时间 ${attack}`, reasons: [{ code: attack, label: `合成原因 ${attack}` }],
  }) })]);
  const panel = messages()[0].querySelector(".consultation-evidence-diagnostic");
  assert.ok(panel.textContent.includes(`合成说明 ${attack}`));
  assert.ok(panel.textContent.includes(`合成注释 ${attack}`));
  assert.ok(panel.textContent.includes(`合成原因 ${attack}`));
  assert.ok(panel.textContent.includes(`合成时间 ${attack}`));
  assert.ok(panel.textContent.includes(link));
  assert.equal(panel.querySelector("img, script, iframe, a, [onerror]"), null);
  assert.equal(window.injected, undefined);
});

test("missing diagnostic or unknown basis/scope does not invent current counts or exclusion reasons", async () => {
  await history([
    zeroEvidenceRun({ id: "missing", evidence_diagnostic: undefined }),
    zeroEvidenceRun({ id: "null", evidence_diagnostic: null }),
    zeroEvidenceRun({ id: "unknown-basis", evidence_diagnostic: diagnostic({ basis: "historical" }) }),
    zeroEvidenceRun({ id: "unknown-scope", evidence_diagnostic: diagnostic({ scope: "future_scope" }) }),
    zeroEvidenceRun({ id: "mismatched-scope", evidence_diagnostic: diagnostic({ scope: "reference" }) }),
  ]);
  for (const message of messages()) {
    const panel = message.querySelector(".consultation-evidence-diagnostic");
    assert.ok(panel);
    assert.match(panel.textContent, /未提供可用的证据诊断，无法确定具体原因或当前资料数量/);
    assert.equal(panel.querySelector("dl, ul, time"), null);
    assert.doesNotMatch(panel.textContent, /效力状态尚未确定|没有可读资料|当前可读资料.*0/);
    assert.match(message.querySelector(".answer-body").textContent, /合成回答摘要/);
  }
});

test("zero, missing and malformed diagnostic counts remain distinct and never derive missing totals", async () => {
  const cases = [
    [0, "0"], [undefined, "未提供"], [null, "未提供"], ["7", "未提供"],
    [-1, "未提供"], [0.5, "未提供"], [Number.MAX_SAFE_INTEGER + 1, "未提供"],
  ];
  await history(cases.map(([count], index) => zeroEvidenceRun({
    id: `count-${index}`, evidence_diagnostic: diagnostic({
      visible_resource_count: count, eligible_resource_count: undefined, excluded_resource_count: undefined,
      reasons: [{ code: "CONTEXT_REQUIRED", label: "需要适用背景", count }],
    }),
  })));
  for (const [index, [count, expected]] of cases.entries()) {
    const panel = messages()[index].querySelector(".consultation-evidence-diagnostic");
    assert.deepEqual([...panel.querySelectorAll("dd")].map((dd) => dd.textContent), [expected, "未提供", "未提供"]);
    assert.equal(panel.querySelector("li").textContent, count === 0 ? "需要适用背景：0" : "需要适用背景");
  }
});

test("diagnostics cannot override invalidation, status, invocation telemetry or nonempty evidence", async () => {
  const excluded = [
    { invalidated: true }, { state: "RUNNING" }, { state: "QUEUED" },
    { state: "FAILED", answer: null, error_code: "SYNTHETIC_DIAGNOSTIC_FAILURE" },
    { state: "CANCELLED", answer: null }, { answer: answer({ status: "ANSWERED" }) },
    { answer: answer({ status: "NEEDS_CLARIFICATION" }) },
    { model_snapshot: { model_invoked: true, evidence_count: 0 } },
    { model_snapshot: { model_invoked: null, evidence_count: 0 } },
    { model_snapshot: { evidence_count: 0 } }, { model_snapshot: null },
    { model_snapshot: { model_invoked: "false", evidence_count: 0 } },
    { model_snapshot: { model_invoked: false, evidence_count: 1 } },
    { answer: answer({ status: "INSUFFICIENT_EVIDENCE" }) },
    { model_snapshot: { model_invoked: false }, evidence_diagnostic: undefined },
  ];
  await history(excluded.map((override, index) => zeroEvidenceRun({ id: `gate-${index}`, ...override })));
  for (const message of messages()) {
    assert.equal(message.querySelector(".consultation-evidence-diagnostic"), null);
    assert.doesNotMatch(message.textContent, /效力状态尚未确定/);
  }
  assert.match(messages()[0].textContent, /来源已发生变化/);
  assert.match(messages()[0].querySelector(".answer-body").textContent, /合成回答摘要/);
  assert.match(messages()[3].textContent, /未能完成本次咨询：SYNTHETIC_DIAGNOSTIC_FAILURE/);
  assert.ok(byText("重试本次任务", messages()[3]));
  assert.match(messages()[7].querySelector(".consultation-invocation-state").textContent, /模型已调用/);
  assert.match(messages()[9].querySelector(".consultation-invocation-state").textContent, /历史调用状态未记录/);
});

test("a supplied current diagnostic does not backfill missing historical scope origin or evidence count", async () => {
  await history([zeroEvidenceRun({
    answer_scope: undefined, answer_scope_origin: undefined,
    model_snapshot: { model_invoked: false },
  })]);
  assert.ok(messages()[0].querySelector(".consultation-evidence-diagnostic"));
  assert.match(messages()[0].querySelector(".consultation-run-scope").textContent, /历史答疑范围未记录/);
  assert.match(messages()[0].querySelector(".consultation-run-scope-origin").textContent, /历史未记录/);
  assert.match(messages()[0].querySelector(".consultation-execution-details").textContent, /依据数量未记录/);

  assert.equal(sentRuns().length, 0);
});

test("fresh GET diagnostics replace prior ACL counts, disappear while refreshing, and are not retained when omitted or invalidated", async () => {
  await history([zeroEvidenceRun()]);
  assert.match(messages()[0].querySelector(".consultation-diagnostic-counts").textContent, /当前可读资料7/);
  let resolveHistory;
  intercept = ({ method, path }) => method === "GET" && path === `/threads/${fixtureThread.id}`
    ? new Promise((resolve) => { resolveHistory = resolve; }) : undefined;
  await refreshApp(1);
  assert.equal(messages()[0].querySelector(".consultation-evidence-diagnostic"), null, "old ACL counts are hidden during refresh");
  const updated = zeroEvidenceRun({ evidence_diagnostic: diagnostic({
    visible_resource_count: 1, eligible_resource_count: 1, excluded_resource_count: 0,
    reasons: [], observed_at: "2026-09-08T07:00:00Z",
  }) });
  await act(async () => {
    storedRuns = [updated];
    resolveHistory(json({ items: storedRuns, next_cursor: null }));
  });
  let panel = messages()[0].querySelector(".consultation-evidence-diagnostic");
  assert.deepEqual([...panel.querySelectorAll("dd")].map((dd) => dd.textContent), ["1", "1", "0"]);
  assert.match(panel.textContent, /2026-09-08T07:00:00Z/);
  assert.doesNotMatch(panel.textContent, /效力状态尚未确定/);
  intercept = undefined;
  storedRuns = [zeroEvidenceRun({ evidence_diagnostic: undefined })];
  await refreshApp(2);
  panel = messages()[0].querySelector(".consultation-evidence-diagnostic");
  assert.match(panel.textContent, /未提供可用的证据诊断/);
  assert.equal(panel.querySelector("dl, ul, time"), null);
  intercept = ({ method, path }) => method === "GET" && path === `/threads/${fixtureThread.id}`
    ? json({ code: "SYNTHETIC_ACCESS_FAILURE", message: "合成访问权限读取失败" }, 403) : undefined;
  await refreshApp(3);
  assert.equal(messages()[0].querySelector(".consultation-evidence-diagnostic"), null);
  assert.match(document.body.textContent, /合成访问权限读取失败/);
  intercept = undefined;
  storedRuns = [zeroEvidenceRun({ invalidated: true, answer: null })];
  await refreshApp(4);
  assert.equal(messages()[0].querySelector(".consultation-evidence-diagnostic"), null);
  assert.match(messages()[0].textContent, /来源已发生变化/);
  assert.match(messages()[0].textContent, /当前无法读取答案/);
  assert.equal(sentRuns().length, 0);
});

function previewRun(overrides = {}) {
  return run({ state: "RUNNING", attempt: 1, answer: null, answer_scope: "reference",
    phase: "GENERATING", model_snapshot: { model_invoked: true,
      last_request: { phase: "synthesis", state: "waiting", attempt: 1 },
      public_preview: { state: "pending", notice: "生成中，尚未完成核验", run_id: "run-fixture",
        attempt: 1, revision: 1, source_check: "current_access", final_validation: "pending",
        text: "## 分段处理说明\n\n**核对输入**后继续处理。[E1]\n\n|阶段|事项|\n|---|---|\n|第一步|核对|\n\n[临时链接](https://example.invalid)\n\n" } },
    ...overrides });
}

test("public paragraph preview renders Markdown without claiming completion or enabling links", async () => {
  await history([previewRun()]);
  const panel = messages()[0].querySelector('[aria-label="暂存正文预览"]');
  assert.ok(panel);
  assert.match(panel.textContent, /生成中，尚未完成核验/);
  assert.equal(panel.querySelector("h2")?.textContent, "分段处理说明");
  assert.equal(panel.querySelector("strong")?.textContent, "核对输入");
  assert.ok(panel.querySelector("table"));
  assert.equal(panel.querySelector("a, img, button, script"), null);
  assert.match(panel.textContent, /E1/);
  assert.doesNotMatch(messages()[0].querySelector(".response-heading").textContent, /处理完成/);
  assert.equal(sentRuns().length, 0);
});

for (const state of ["FAILED", "CANCELLED", "COMPLETED"]) {
  test(`terminal ${state} must never display a stale public preview`, async () => {
    await history([previewRun({ state, ...(state === "COMPLETED" ? { answer: answer() } : {}) })]);
    assert.equal(document.querySelector(".answer-preview"), null);
  });
}

test("preview identity, attempt, phase and source invalidation remain hard display gates", async () => {
  const current = previewRun();
  current.model_snapshot.public_preview.attempt = 2;
  await history([current]);
  assert.equal(document.querySelector(".answer-preview"), null);
  storedRuns = [previewRun({ invalidated: true })];
  await refreshApp(1);
  assert.equal(document.querySelector(".answer-preview"), null);
  const wrongPhase = previewRun();
  wrongPhase.model_snapshot.last_request.phase = "planning";
  storedRuns = [wrongPhase];
  await refreshApp(2);
  assert.equal(document.querySelector(".answer-preview"), null);
});

test("failed current read removes preview instead of retaining cached pending prose", async () => {
  await history([previewRun()]);
  assert.ok(document.querySelector(".answer-preview"));
  intercept = ({ method, path }) => method === "GET" && path === `/threads/${fixtureThread.id}`
    ? json({ code: "SYNTHETIC_ACCESS_FAILURE", message: "合成访问权限读取失败" }, 403) : undefined;
  await refreshApp(1);
  assert.equal(document.querySelector(".answer-preview"), null);
  assert.equal(sentRuns().length, 0);
});

test("performance: typing does not re-render twenty unchanged historical answers", async () => {
  await history(Array.from({ length: 20 }, (_, index) => run({ id: `history-${index}`,
    answer: answer({ format: "wiki_markdown", narrative_markdown: `# 合成完整回答 ${index}\n\n${"段落与引用 [E1]。\n\n".repeat(40)}末尾完整内容` }) })));
  const before = { ...globalThis.__consultationRenderCounts };
  for (let i = 1; i <= 12; i++) await change(questionInput(), "合成问题输入回归测试样本".slice(0, i));
  const delta = Object.fromEntries(Object.entries(globalThis.__consultationRenderCounts).map(([key, count]) => [key, count - (before[key] ?? 0)]));
  performanceMeasurements.push({ id: "typing-history-renders", historical_answers: 20, input_events: 12, render_calls: delta });
  assert.equal(messages().length, 20);
  assert.ok(messages().every(row => row.textContent.includes("末尾完整内容")));
  if (!measureBaseline) assert.equal(delta.AnswerView, 0);
});

test("performance: a fast completed run is published without waiting for another run GET", async () => {
  let resolveSlow;
  const slow = run({ id: "slow-run", state: "RUNNING", answer: null });
  const fast = run({ id: "fast-run", state: "RUNNING", answer: null });
  intercept = ({ path, method }) => {
    if (method === "GET" && path === "/runs/slow-run") return new Promise(resolve => { resolveSlow = resolve; });
    if (method === "GET" && path === "/runs/fast-run") return json({ ...fast, state: "COMPLETED", answer: answer({ summary: "先到达的完整答复" }) });
  };
  await history([slow, fast]);
  const shownBeforeSlow = document.body.textContent.includes("先到达的完整答复");
  performanceMeasurements.push({ id: "independent-run-publication", fast_answer_visible_while_other_get_pending: shownBeforeSlow });
  if (!measureBaseline) assert.equal(shownBeforeSlow, true);
  await act(async () => resolveSlow(json({ ...slow, state: "CANCELLED" })));
});

test("performance: unchanged polling snapshots do not re-render historical answer bodies", async t => {
  t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
  const pending = previewRun();
  await history([...Array.from({ length: 20 }, (_, i) => run({ id: `completed-${i}` })), pending]);
  const before = { ...globalThis.__consultationRenderCounts };
  const readsBefore = calls.filter(call => call.path.startsWith("/runs/")).length;
  for (let i = 0; i < 4; i++) await act(async () => t.mock.timers.tick(1600));
  const delta = Object.fromEntries(Object.entries(globalThis.__consultationRenderCounts).map(([key, count]) => [key, count - (before[key] ?? 0)]));
  performanceMeasurements.push({ id: "unchanged-poll-renders", ticks: 4,
    run_reads: calls.filter(call => call.path.startsWith("/runs/")).length - readsBefore, render_calls: delta });
  if (!measureBaseline) assert.equal(delta.AnswerView, 0);
});

test("performance: initial document inventory stays lazy until the attachment chooser is opened", async () => {
  await mount();
  const initialReads = calls.filter(call => call.path === "/resources").length;
  performanceMeasurements.push({ id: "lazy-document-inventory", document_gets_before_attachments_open: initialReads });
  if (!measureBaseline) assert.equal(initialReads, 0);
  await click(document.querySelector('[aria-label="添加资料附件"]'));
  assert.equal(calls.filter(call => call.path === "/resources").length, 1);
});

function progressFrame(value, overrides = {}) {
  const { query_path, hybrid_retrieval, ...snapshot } = value.model_snapshot ?? {};
  return { progress_version: 1, run_id: value.id, thread_id: value.thread_id, job_id: value.job_id,
    state: value.state, phase: value.phase ?? "GENERATING", attempt: value.attempt ?? 1,
    invalidated: !!value.invalidated, source_check: "current_access", model_snapshot: snapshot,
    error_code: value.error_code, ...overrides };
}
const sse = (value, eventId = "synthetic-event") => `retry: 1500\nid: ${eventId}\nevent: stage\ndata: ${JSON.stringify(value)}\n\n`;
const sseResponse = value => new Response(sse(value), { headers: { "Content-Type": "text/event-stream" } });
const runReads = () => calls.filter(call => call.method === "GET" && /^\/runs\/[^/]+$/.test(call.path));
const progressReads = () => calls.filter(call => call.method === "GET" && call.path.endsWith("/events"));
const advancePoll = t => act(async () => t.mock.timers.tick(1600));

test("performance: progress polling avoids repeated full payloads and loads all final Markdown/citations once", async t => {
  t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
  const initial = previewRun();
  initial.model_snapshot.query_path = { deferred_ids: Array.from({ length: 3000 }, (_, i) => `synthetic-deferred-${i}-${"x".repeat(32)}`) };
  let current = initial, bytes = 0;
  intercept = ({ path, method }) => {
    if (method !== "GET") return;
    if (path === `/runs/${initial.id}/events`) {
      const text = sse(progressFrame(current)); bytes += Buffer.byteLength(text);
      return new Response(text, { headers: { "Content-Type": "text/event-stream" } });
    }
    if (path === `/runs/${initial.id}`) { bytes += Buffer.byteLength(JSON.stringify(current)); return json(current); }
  };
  await history([initial]);
  for (let i = 0; i < 3; i++) await advancePoll(t);
  current = { ...initial, state: "COMPLETED", answer: answer({ format: "wiki_markdown",
    narrative_markdown: `# 完整终态\n\n|字段|值|\n|---|---|\n|合成|正文|\n\n${"保留全部段落 [E1]。\n\n".repeat(50)}末尾完整引用 [E1]` }) };
  await advancePoll(t);
  assert.match(document.querySelector('[aria-label="完整综合答复"]').textContent, /末尾完整引用/);
  assert.ok(document.querySelector('[aria-label="完整综合答复"] table'));
  await click(document.querySelector('[aria-label="完整综合答复"] [data-evidence-id="E1"]'));
  assert.deepEqual(openedVersions.at(-1), [fixtureCitation.version_id, fixtureCitation.block_id]);
  const beforeIdle = calls.length;
  await act(async () => t.mock.timers.tick(16000));
  assert.equal(calls.length, beforeIdle);
  performanceMeasurements.push({ id: "progress-transfer", polls: 5, synthetic_deferred_ids: 3000,
    full_run_gets: runReads().length, progress_gets: progressReads().length, polling_response_bytes: bytes,
    final_markdown_complete: true, final_citation_navigation: true });
  if (!measureBaseline) { assert.equal(runReads().length, 1); assert.equal(progressReads().length, 5); }
});

test("progress v1 updates same-stage public text and removes omitted or invalidated preview", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  let current = progressFrame(previewRun());
  intercept = ({ path }) => path.endsWith("/events") ? sseResponse(current) : undefined;
  await history([previewRun()]);
  current = { ...current, model_snapshot: { ...current.model_snapshot, public_preview: {
    ...current.model_snapshot.public_preview, revision: 2, text: "## 新公开段落\n\n完整第二版 [E1]" } } };
  await advancePoll(t);
  assert.match(document.querySelector(".answer-preview").textContent, /完整第二版/);
  assert.equal(document.querySelector(".answer-preview a, .answer-preview button"), null);
  current = { ...current, model_snapshot: { ...current.model_snapshot, public_preview: undefined } };
  await advancePoll(t); assert.equal(document.querySelector(".answer-preview"), null);
  current = { ...progressFrame(previewRun()), invalidated: true };
  await advancePoll(t); assert.equal(document.querySelector(".answer-preview"), null);
  assert.match(messages()[0].textContent, /来源已发生变化/);
  assert.match(messages()[0].textContent, /模型已调用/);
  assert.equal(runReads().length, 0);
});

for (const state of ["COMPLETED", "FAILED", "CANCELLED"]) test(`progress ${state} reads the full result once and stops all timers`, async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const next = run({ state, answer: state === "COMPLETED" ? answer() : null });
  intercept = ({ path }) => path.endsWith("/events") ? sseResponse(progressFrame(next))
    : path === `/runs/${next.id}` ? json(next) : undefined;
  await history([previewRun()]);
  assert.equal(progressReads().length, 1); assert.equal(runReads().length, 1);
  assert.equal(document.querySelector(".answer-preview"), null);
  await act(async () => t.mock.timers.tick(16000));
  assert.equal(progressReads().length, 1); assert.equal(runReads().length, 1);
});

for (const code of [401, 403]) test(`progress ${code} removes public text, pauses reads and never falls back to full GET`, async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  intercept = ({ path }) => path.endsWith("/events") ? json({ message: "当前访问被拒绝" }, code) : undefined;
  await history([previewRun()]);
  assert.equal(document.querySelector(".answer-preview"), null);
  assert.equal(runReads().length, 0);
  await act(async () => t.mock.timers.tick(16000));
  assert.equal(progressReads().length, 1);
});

for (const code of [404, 501]) test(`legacy progress ${code} switches to full GET once without retrying the absent endpoint`, async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  intercept = ({ path }) => path.endsWith("/events") ? json({}, code) : undefined;
  await history([previewRun()]); await advancePoll(t);
  assert.equal(progressReads().length, 1); assert.equal(runReads().length, 2);
  assert.ok(document.querySelector(".answer-preview"));
});

for (const [label, overrides] of [
  ["wrong run", { run_id: "another-run" }], ["wrong thread", { thread_id: "another-thread" }],
  ["wrong job", { job_id: "another-job" }], ["missing authority", { source_check: undefined }],
  ["unknown version", { progress_version: 2 }],
]) test(`invalid progress ${label} is rejected without fetching another endpoint`, async () => {
  intercept = ({ path }) => path.endsWith("/events") ? sseResponse(progressFrame(previewRun(), overrides)) : undefined;
  await history([previewRun()]);
  assert.equal(document.querySelector(".answer-preview"), null);
  assert.equal(runReads().length, 0);
  assert.ok(document.querySelector('[role="alert"]'));
});

test("cancel aborts an in-flight progress read and ignores its later RUNNING response", async () => {
  let resolveProgress;
  const pending = previewRun();
  intercept = ({ path, method }) => {
    if (path.endsWith("/events")) return new Promise(resolve => { resolveProgress = resolve; });
    if (method === "POST" && path.endsWith("/cancel")) return json({});
    if (path === `/runs/${pending.id}`) return json({ ...pending, state: "CANCELLED" });
  };
  await history([pending]);
  const signal = progressReads()[0].signal;
  await click(byText("取消", messages()[0]));
  assert.equal(signal.aborted, true); assert.equal(runReads().length, 1);
  await act(async () => resolveProgress(sseResponse(progressFrame(pending))));
  assert.equal(document.querySelector(".answer-preview"), null);
  assert.equal(document.querySelector(".pending-answer"), null);
  assert.match(messages()[0].textContent, /本次咨询已取消/);
});

test("a terminal event withdraws preview while a single full GET is pending; full-read failure requires manual retry", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  let resolveFull;
  const pending = previewRun();
  intercept = ({ path }) => path.endsWith("/events") ? sseResponse(progressFrame(pending, { state: "COMPLETED" }))
    : path === `/runs/${pending.id}` ? new Promise(resolve => { resolveFull = resolve; }) : undefined;
  await history([pending]);
  assert.equal(document.querySelector(".answer-preview"), null);
  assert.match(document.querySelector(".pending-answer").textContent, /正在读取完整结果/);
  assert.equal(byText("取消", messages()[0]), undefined);
  for (let i = 0; i < 3; i++) await advancePoll(t);
  assert.equal(runReads().length, 1);
  await act(async () => resolveFull(json({ message: "完整答复读取失败" }, 503)));
  await advancePoll(t);
  assert.equal(runReads().length, 1); assert.equal(progressReads().length, 1);
  assert.match(document.body.textContent, /完整答复读取失败/);
  assert.match(messages()[0].textContent, /服务端处理已结束/);
  intercept = undefined;
  storedRuns = [run()];
  await click(byText("重试"));
  assert.match(messages()[0].textContent, /合成回答摘要/);
  assert.doesNotMatch(document.body.textContent, /完整答复读取失败/);
  assert.equal(sentRuns().length, 0);
});

test("a slow progress request never overlaps its next scheduled poll", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  let resolveProgress;
  intercept = ({ path }) => path.endsWith("/events") ? new Promise(resolve => { resolveProgress = resolve; }) : undefined;
  await history([previewRun()]);
  await act(async () => t.mock.timers.tick(1900));
  assert.equal(progressReads().length, 1);
  await act(async () => resolveProgress(sseResponse(progressFrame(previewRun()))));
  await act(async () => t.mock.timers.tick(0));
  assert.equal(progressReads().length, 2, "resume immediately after a slow read, without an extra polling interval");
});

test("thread switching aborts progress and a late old preview cannot appear in the new conversation", async () => {
  let resolveProgress;
  intercept = ({ path }) => path.endsWith("/events") ? new Promise(resolve => { resolveProgress = resolve; }) : undefined;
  await history([previewRun()]); const signal = progressReads()[0].signal;
  await click(byText("新建咨询"));
  assert.equal(signal.aborted, true);
  await act(async () => resolveProgress(sseResponse(progressFrame(previewRun()))));
  assert.equal(messages().length, 0);
  assert.equal(document.querySelector(".answer-preview"), null);
});

test("progress phase follows actual planning, retrieval and synthesis instead of a fixed retrieval spinner", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  let phase = "PLANNING_QUESTION";
  intercept = ({ path }) => path.endsWith("/events") ? sseResponse(progressFrame(previewRun(), { phase: null, stage: phase })) : undefined;
  await history([run({ state: "RUNNING", answer: null })]);
  assert.match(document.querySelector(".pending-answer").textContent, /模型分析问题/);
  phase = "HYBRID_RETRIEVAL"; await advancePoll(t);
  assert.match(document.querySelector(".pending-answer").textContent, /关键词与语义融合检索/);
  phase = "GENERATING"; await advancePoll(t);
  assert.match(document.querySelector(".pending-answer").textContent, /模型综合解答/);
  assert.equal(runReads().length, 0);
});
