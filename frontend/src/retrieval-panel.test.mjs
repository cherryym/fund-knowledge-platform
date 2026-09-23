import assert from "node:assert/strict";
import { after, afterEach, beforeEach, test } from "node:test";
import { createRequire, Module } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { readFile } from "node:fs/promises";
import { build } from "esbuild";
import { qwen, bge, profiles, selection, frozenSelection, retrievalKey } from "./retrievalProfiles.test-fixtures.mjs";

const require = createRequire(import.meta.url);
const sourceDir = dirname(fileURLToPath(import.meta.url));
const { JSDOM } = require("jsdom");
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: "http://retrieval.test/", pretendToBeVisual: true,
});
for (const key of ["window", "document", "HTMLElement", "Element", "Node", "Event", "MouseEvent", "HTMLInputElement"])
  globalThis[key] = key === "window" ? dom.window : dom.window[key];
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
globalThis.getComputedStyle = window.getComputedStyle.bind(window);
window.matchMedia = media => ({ media, matches: media.includes("reduce"),
  addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });

let calls = [], unexpected = [], routes = new Map();
// Installed before importing the panel, retained for the entire process. No native fetch fallback.
const interceptedFetch = async (url, init = {}) => {
  const request = { url: String(url), ...init, method: init.method ?? "GET" };
  calls.push(request);
  const key = `${request.method} ${request.url}`;
  const allowed = /^GET \/api\/v1\/retrieval\/profiles\?space_id=[^&]+$/.test(key)
    || /^GET \/api\/v1\/retrieval\/model-runtime\?space_id=[^&]+(?:&profile_id=[^&]+)?$/.test(key)
    || /^POST \/api\/v1\/retrieval\/model-warmup$/.test(key)
    || /^GET \/api\/v1\/retrieval\/status\?space_id=[^&]+(?:&profile_id=[^&]+)?$/.test(key)
    || /^POST \/api\/v1\/retrieval\/(search|index-jobs)$/.test(key)
    || /^GET \/api\/v1\/(jobs|resources)\/[^/?]+$/.test(key)
    || /^POST \/api\/v1\/jobs\/[^/?]+\/(cancel|retry)$/.test(key);
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
    contents: 'export { RetrievalPanel } from "./RetrievalPanel"; export { AppContext } from "./appContext"; export { setSession, clearSession } from "./api";',
    resolveDir: sourceDir, loader: "tsx",
  },
  bundle: true, platform: "node", format: "cjs", write: false, logLevel: "silent",
  external: ["react", "react-dom", "react-dom/*", "react/jsx-runtime", "gsap", "gsap/ScrollTrigger", "@gsap/react"], loader: { ".css": "empty" },
});
const runtime = new Module(resolve(sourceDir, "retrieval-test-runtime.cjs"));
runtime.filename = resolve(sourceDir, "retrieval-test-runtime.cjs");
runtime.paths = Module._nodeModulePaths(sourceDir);
const runtimeRequire = runtime.require.bind(runtime);
runtime.require = id => id === "gsap" ? gsap : id === "gsap/ScrollTrigger" ? { ScrollTrigger } : runtimeRequire(id);
runtime._compile(compiled.outputFiles[0].text, runtime.filename);
const { RetrievalPanel, AppContext, setSession, clearSession } = runtime.exports;
// ui.tsx registers existing motion plugins; this panel itself has no animated lifecycle.
ScrollTrigger.disable();
gsap.ticker.sleep();

const json = (value, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { "Content-Type": "application/json" },
});
const stub = (method, path, response) => routes.set(`${method} /api/v1${path}`,
  typeof response === "function" ? response : () => json(response));
const deferred = () => {
  let resolvePromise;
  const promise = new Promise(resolve => { resolvePromise = resolve; });
  // Intentionally ignores abort: the component must also ignore a late response.
  return { promise, resolve: resolvePromise };
};
const status = (overrides = {}) => ({
  space_id: "space-a", mode: "hybrid", enabled: true,
  vector: { backend: "qdrant", mode: "local", available: true, status: "ready", collection: "synthetic-collection", embedding_mode: "fastembed" },
  embedding: { mode: "fastembed", model: "synthetic-chinese-embedding", dimensions: 512, development_only: false },
  coverage: { catalog_pages: 19, indexed_pages: 13, dirty_pages: 6, indexed_blocks: 41, indexed_chunks: 67 },
  permissions: { can_index: true }, active_job: null, notes: ["合成状态说明"], ...overrides,
});
const job = (overrides = {}) => ({
  id: "index-job", kind: "COMPILE", state: "RUNNING", stage: "VECTOR_INDEX", attempts: 1, error_code: null,
  result: { phase: "embedding", total_versions: 7, completed_versions: 3, indexed_versions: 2,
    skipped_versions: 1, failed_versions: 0, indexed_blocks: 11, indexed_chunks: 23, current_version: "synthetic-version-3" },
  ...overrides,
});
const hit = (overrides = {}) => ({
  page_id: "page-a", resource_id: "resource-a", version_id: "version-a", title: "合成检索候选甲", kind: "knowledge",
  score: 0.0325, channels: ["vector", "lexical"], vector_rank: 2, lexical_rank: 1,
  matched_block_ids: ["block-a", "block-b"], ...overrides,
});
const result = (overrides = {}) => ({
  query: "合成检索词", scope: "reference", mode: "hybrid", hits: [hit()], catalog_pages: 19,
  indexed_catalog_pages: 13, total_candidates: 16, returned: 1, warnings: ["合成覆盖警示"], timing_ms: 18.75,
  evidence_preview: false, ...overrides,
});
const resource = (overrides = {}) => ({ id: "resource-a", space_id: "space-a", kind: "knowledge", name: "合成检索候选甲", ...overrides });
let root, currentApp, opened = [], confirmations = [], bumps = 0;
const app = (overrides = {}) => ({
  me: { id: "synthetic-user", csrf_token: "synthetic-csrf", is_admin: false },
  space: { id: "space-a", name: "合成空间甲", roles: ["editor"] }, refresh: 0,
  openResource: (...args) => opened.push(args), notify: () => {}, bump: () => { bumps++; }, ...overrides,
});
const button = name => {
  const element = [...document.querySelectorAll("button")].find(item => item.textContent.trim() === name);
  assert.ok(element, `Missing button: ${name}`);
  return element;
};
const click = element => act(async () => element.click());
const submit = () => act(async () => document.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
const body = () => document.body.textContent;
const metric = name => document.querySelector(`[data-metric="${name}"] dd`)?.textContent;
const writes = () => calls.filter(call => call.method !== "GET");
const jobReads = () => calls.filter(call => call.method === "GET" && call.url.includes("/jobs/"));
async function type(value = "合成检索词") {
  const input = document.querySelector('input[type="search"]');
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  await act(async () => { setter.call(input, value); input.dispatchEvent(new Event("input", { bubbles: true })); });
}
async function mount(value = app(), strict = false) {
  currentApp = value;
  setSession(value.me);
  root = createRoot(document.getElementById("root"));
  await act(async () => root.render(h(AppContext.Provider, { value }, strict ? h(React.StrictMode, null, h(RetrievalPanel)) : h(RetrievalPanel))));
}
async function switchApp(value) {
  currentApp = value;
  setSession(value.me);
  await act(async () => root.render(h(AppContext.Provider, { value }, h(RetrievalPanel))));
}
async function unmount() {
  if (root) await act(async () => root.unmount());
  root = null;
}
const tick = (t, ms = 2000) => act(async () => t.mock.timers.tick(ms));

beforeEach(() => {
  window.localStorage.clear();
  calls = []; unexpected = []; routes = new Map(); opened = []; confirmations = []; bumps = 0;
  clearSession();
  for (const space of ["space-a", "space-b"])
    stub("GET", `/retrieval/profiles?space_id=${space}`, { default_profile_id: null, items: [], enabled: false });
  stub("GET", "/retrieval/status?space_id=space-a", status());
  window.confirm = text => { confirmations.push(text); return false; };
});
afterEach(async () => {
  await unmount();
  clearSession();
  assert.deepEqual(unexpected, [], "Every request must have an explicit mock; no other endpoints are permitted");
  assert.ok(calls.every(call => !/\/(threads|runs|models|connections)(?:\/|\?|$)/.test(call.url)), "Retrieval must never invoke a generation flow");
  assert.equal(globalThis.fetch, interceptedFetch);
  assert.equal(window.fetch, interceptedFetch);
  assert.equal(bumps, 0, "Panel actions must not invalidate the full knowledge-space cache");
});
after(() => dom.window.close());

test("opening and typing only read status; no automatic index, search, model or idle polling", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  await mount(app(), true);
  assert.equal(document.querySelector("input").value, "");
  assert.equal(button("检索").disabled, true);
  assert.equal(document.querySelectorAll("input").length, 1);
  assert.equal(document.querySelectorAll('input[type="password"], select').length, 0);
  assert.match(body(), /synthetic-chinese-embedding/);
  assert.match(body(), /只读/);
  assert.match(body(), /只检索，不调用生成模型/);
  assert.equal(metric("catalog_pages"), "19");
  assert.equal(metric("indexed_pages"), "13");
  assert.equal(metric("dirty_pages"), "6");
  await submit();
  await type();
  await tick(t, 20000);
  assert.equal(writes().length, 0);
  assert.ok(calls.every(call => /^\/api\/v1\/retrieval\/(status|profiles)\?space_id=space-a$/.test(call.url)));
});

test("HTTP embedding and development-only configuration are disclosed without credential requests", async () => {
  stub("GET", "/retrieval/status?space_id=space-a", status({
    embedding: { mode: "http", model: "synthetic-http-embedding", dimensions: 384, development_only: true },
  }));
  await mount();
  assert.match(body(), /会使用已配置的嵌入服务/);
  assert.match(body(), /仅供开发验证/);
  assert.match(body(), /384/);
  assert.equal(writes().length, 0);
});

for (const loaded of [false, true]) test(`Qwen reranker identity and real load state are visible: ${loaded}`, async () => {
  const current = status();
  current.vector.reranking = { mode: "local", model: "Qwen/Qwen3-Reranker-4B", loaded };
  stub("GET", "/retrieval/status?space_id=space-a", current);
  await mount();
  assert.match(body(), /Qwen\/Qwen3-Reranker-4B/);
  assert.ok(body().includes(loaded ? "本地模型已加载" : "已配置 · 当前进程尚未加载"));
  assert.equal(writes().length, 0);
});

for (const loaded of [undefined, null, "true", 1]) test(`missing/invalid load state stays unknown: ${loaded}`, async () => {
  const current = status();
  current.vector.reranking = { mode: "local", model: "Qwen/Qwen3-Reranker-4B", loaded };
  stub("GET", "/retrieval/status?space_id=space-a", current);
  await mount();
  assert.match(body(), /已配置 · 加载状态未知/);
  assert.doesNotMatch(body(), /本地模型已加载|当前进程尚未加载/);
  assert.equal(writes().length, 0);
});

if (process.env.RERANK_STATUS_CONTRACT_DIR) {
  for (const [state, expected] of [["configured", "已配置 · 当前进程尚未加载"], ["loaded", "本地模型已加载"], ["disabled", "未启用"]]) {
    test(`actual Python HTTP response renders in React: ${state}`, async () => {
      const payload = JSON.parse(await readFile(resolve(process.env.RERANK_STATUS_CONTRACT_DIR, `${state}.json`), "utf8"));
      assert.equal(payload.vector.reranking.model, "Qwen/Qwen3-Reranker-4B");
      stub("GET", `/retrieval/profiles?space_id=${payload.space_id}`, { default_profile_id: null, items: [], enabled: false });
      stub("GET", `/retrieval/status?space_id=${payload.space_id}`, payload);
      await mount(app({ space: { id: payload.space_id, name: "实际合成HTTP空间", roles: ["editor"] } }));
      assert.match(body(), /Qwen\/Qwen3-Reranker-4B/);
      assert.ok(body().includes(expected));
      assert.doesNotMatch(body(), /接口未返回模型信息|接口未返回明确状态/);
      assert.match(body(), /不代表某次查询已经执行重排/);
      assert.equal(writes().length, 0);
    });
  }
}

for (const [mode, expected] of [["local_cross_encoder", "本次检索已执行语义重排"], ["unavailable", "本次重排不可用"],
  ["disabled", "本次检索未执行重排"], [undefined, "本次检索未返回重排执行记录"]]) {
  test(`per-query receipt does not infer execution from model load state: ${mode}`, async () => {
    const current = status();
    current.vector.reranking = { mode: "local", model: "Qwen/Qwen3-Reranker-4B", loaded: true };
    stub("GET", "/retrieval/status?space_id=space-a", current);
    stub("POST", "/retrieval/search", result({ reranking: mode ? { mode, model: "Qwen/Qwen3-Reranker-4B", input_units: 8 } : undefined }));
    await mount(); await type(); await submit();
    const receipt = document.querySelector('[data-query-reranking]');
    assert.ok(receipt.textContent.includes(expected));
    if (mode !== "local_cross_encoder") assert.doesNotMatch(receipt.textContent, /已执行语义重排/);
    assert.equal(writes().length, 1);
  });
}

test("server permissions disable indexing even for an admin-shaped context; search stays readable", async () => {
  stub("GET", "/retrieval/status?space_id=space-a", status({ permissions: { can_index: false }, active_job: job({ state: "FAILED" }) }));
  stub("POST", "/retrieval/search", result());
  await mount(app({ me: { id: "synthetic-user", csrf_token: "synthetic-csrf", is_admin: true } }));
  for (const name of ["增量同步", "完整重建", "重试作业"]) {
    assert.equal(button(name).disabled, true);
    await click(button(name));
  }
  assert.match(body(), /只有检索查看权限/);
  assert.equal(confirmations.length, 0);
  assert.equal(writes().length, 0);
  await type();
  await submit();
  assert.equal(writes().length, 1);
  assert.equal(writes()[0].url, "/api/v1/retrieval/search");
});

test("successful search refreshes load metadata once without another model/index request", async () => {
  let reads = 0;
  stub("GET", "/retrieval/status?space_id=space-a", () => {
    const current = status();
    current.vector.reranking = { mode: "local", model: "Qwen/Qwen3-Reranker-4B", loaded: ++reads > 1 };
    return json(current);
  });
  stub("POST", "/retrieval/search", result({ reranking: { mode: "local_cross_encoder", model: "Qwen/Qwen3-Reranker-4B", input_units: 8 } }));
  await mount();
  assert.match(body(), /当前进程尚未加载/);
  await type(); await submit();
  assert.match(body(), /本地模型已加载/);
  assert.match(body(), /本次检索已执行语义重排/);
  assert.equal(reads, 2);
  assert.equal(writes().length, 1);
});

const warmState = (state, extra={}) => ({runtime_id:"synthetic-runtime",process_id:123,scope:"current_process",policy:"auto",
  supported:true,state,phase:state === "READY" ? "ready" : state === "FAILED" ? "failed" : "loading_embedding",
  attempts:1,self_tested:state === "READY",...extra});
const warmResponse = (runtime,can_prepare=true) => ({space_id:"space-a",model_runtime:runtime,can_prepare});
function warmStatus(runtime) {
  const current=status();
  current.vector.model_runtime=runtime;
  current.vector.reranking={mode:"local",model:"Qwen/Qwen3-Reranker-4B",loaded:runtime.state === "READY"};
  return current;
}

test("startup readiness polling stays read-only and stops after real READY", async t => {
  t.mock.timers.enable({apis:["setTimeout"]});
  let runtime=warmState("LOADING");
  stub("GET","/retrieval/status?space_id=space-a",()=>json(warmStatus(runtime)));
  stub("GET","/retrieval/model-runtime?space_id=space-a",()=>json(warmResponse(runtime)));
  await mount();
  assert.match(body(),/加载与自检中/);
  assert.match(body(),/查询会等待同一个准备任务/);
  await tick(t,1000);
  runtime=warmState("READY");
  await tick(t,1000);
  assert.match(body(),/推理就绪/);
  assert.match(body(),/本地模型已加载/);
  const count=calls.length;
  await tick(t,5000);
  assert.equal(calls.length,count);
  assert.equal(writes().length,0);
});

test("failed preparation is visible and only an explicit retry starts local work", async t => {
  t.mock.timers.enable({apis:["setTimeout"]});
  let runtime=warmState("FAILED",{error_code:"LOCAL_MODEL_HASH_MISMATCH"});
  stub("GET","/retrieval/status?space_id=space-a",()=>json(warmStatus(runtime)));
  stub("GET","/retrieval/model-runtime?space_id=space-a",()=>json(warmResponse(runtime)));
  stub("POST","/retrieval/model-warmup",request=>{
    assert.equal(JSON.parse(request.body).retry,true);
    runtime=warmState("LOADING"); return json(warmResponse(runtime),202);
  });
  await mount();
  assert.match(body(),/模型文件校验未通过/);
  const before=calls.length;
  await tick(t,5000); assert.equal(calls.length,before);
  await click(button("重试预热"));
  assert.equal(writes().length,1);
  runtime=warmState("READY"); await tick(t,1000);
  assert.match(body(),/推理就绪/);
  assert.equal(writes().length,1);
});

test("read-only member can see readiness but cannot force preparation", async () => {
  const runtime=warmState("NOT_LOADED",{phase:"idle",attempts:0});
  stub("GET","/retrieval/status?space_id=space-a",warmStatus(runtime));
  stub("GET","/retrieval/model-runtime?space_id=space-a",warmResponse(runtime,false));
  await mount();
  assert.match(body(),/待准备/);
  assert.equal([...document.querySelectorAll("button")].some(b=>b.textContent.includes("预热本地模型")),false);
  assert.equal(writes().length,0);
});

test("preparation metadata error is not presented as ready and does not start a retry loop", async t => {
  t.mock.timers.enable({apis:["setTimeout"]});
  stub("GET","/retrieval/status?space_id=space-a",warmStatus(warmState("LOADING")));
  stub("GET","/retrieval/model-runtime?space_id=space-a",()=>json({code:"SYNTHETIC",message:"合成读取失败"},503));
  await mount();
  assert.match(body(),/本地推理：状态读取失败/);
  const before=calls.length; await tick(t,5000);
  assert.equal(calls.length,before); assert.equal(writes().length,0);
});

test("disabled indexing explains deployment gating and still displays a Wiki search fallback", async () => {
  stub("GET", "/retrieval/status?space_id=space-a", status({ enabled: false, mode: "wiki",
    vector: { backend: "qdrant", mode: "local", available: false, status: "disabled" } }));
  stub("POST", "/retrieval/search", result({ mode: "wiki", hits: [], returned: 0, total_candidates: 0 }));
  await mount();
  assert.equal(button("增量同步").disabled, true);
  assert.equal(button("完整重建").disabled, true);
  assert.match(body(), /当前部署未启用 RAG 索引/);
  assert.match(body(), /向量通道当前不可用/);
  await type();
  await submit();
  assert.match(document.querySelector(".retrieval-results").textContent, /Wiki 检索/);
  assert.match(body(), /未找到匹配候选/);
});

test("status failure and a mismatched space fail closed until an explicit status retry", async () => {
  stub("GET", "/retrieval/status?space_id=space-a", () => json({ code: "STATUS_UNAVAILABLE", message: "合成状态读取失败" }, 503));
  await mount();
  assert.match(body(), /合成状态读取失败/);
  assert.equal(button("增量同步").disabled, true);
  await type();
  await submit();
  assert.equal(writes().length, 0);
  stub("GET", "/retrieval/status?space_id=space-a", status({ space_id: "wrong-space" }));
  await click(button("重试"));
  assert.match(body(), /检索状态与当前空间不一致/);
  assert.equal(button("完整重建").disabled, true);
  stub("GET", "/retrieval/status?space_id=space-a", status());
  await click(button("重试"));
  assert.equal(button("增量同步").disabled, false);
});

test("incremental sync sends force=false once, reports real counts and stops polling on failure", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const pending = deferred();
  stub("POST", "/retrieval/index-jobs", () => pending.promise);
  stub("GET", "/jobs/index-job", job());
  await mount();
  await click(button("增量同步"));
  await click(button("增量同步"));
  assert.equal(writes().length, 1);
  const write = writes()[0];
  assert.deepEqual(JSON.parse(write.body), { space_id: "space-a", force: false });
  assert.equal(write.headers.get("X-CSRF-Token"), "synthetic-csrf");
  assert.ok(write.headers.get("Idempotency-Key"));
  assert.equal(write.credentials, "include");
  assert.ok(write.signal instanceof AbortSignal);
  await act(async () => pending.resolve(json(job({ state: "QUEUED", result: null }))));
  assert.equal(metric("total_versions"), "7");
  assert.equal(metric("completed_versions"), "3");
  assert.equal(metric("indexed_versions"), "2");
  assert.equal(metric("skipped_versions"), "1");
  assert.equal(metric("failed_versions"), "0");
  assert.equal(metric("pending_versions"), "4");
  assert.equal(metric("job_chunks"), "23");
  assert.match(body(), /向量索引/);
  assert.match(body(), /以下数量只统计当前可读目录版本/);
  assert.match(body(), /作业可能包含可读历史版本，版本总数不能等同于目录页数/);
  assert.match(body(), /阶段：计算嵌入/);
  assert.match(body(), /synthetic-version-3/);
  assert.equal(button("完整重建").disabled, true);
  stub("GET", "/jobs/index-job", job({ state: "FAILED", error_code: "SYNTHETIC_INDEX_FAILURE",
    result: { phase: "failed", total_versions: 7, completed_versions: 4, indexed_versions: 2, skipped_versions: 1, failed_versions: 1 } }));
  await tick(t);
  assert.equal(metric("failed_versions"), "1");
  assert.equal(metric("pending_versions"), "3");
  assert.equal(metric("job_chunks"), "未提供");
  assert.match(body(), /索引作业失败，尚未全部成功/);
  assert.match(body(), /SYNTHETIC_INDEX_FAILURE/);
  assert.doesNotMatch(body(), /本次作业已完成|100%/);
  const readCount = jobReads().length;
  await tick(t, 10000);
  assert.equal(jobReads().length, readCount);
  assert.equal(writes().length, 1);
});

test("rebuild requires the source-preserving confirmation and sends force=true only after acceptance", async () => {
  stub("POST", "/retrieval/index-jobs", job({ state: "QUEUED" }));
  stub("GET", "/jobs/index-job", job());
  await mount();
  await click(button("完整重建"));
  assert.equal(writes().length, 0);
  assert.match(confirmations[0], /只重建派生索引，不修改或删除原始文档与 Wiki 正文/);
  window.confirm = text => { confirmations.push(text); return true; };
  await click(button("完整重建"));
  assert.deepEqual(JSON.parse(writes()[0].body), { space_id: "space-a", force: true });
  assert.equal(writes().length, 1);
});

test("failed counts cannot become complete even when the Job reports SUCCEEDED", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  stub("GET", "/retrieval/status?space_id=space-a", status({ active_job: job({ state: "SUCCEEDED",
    result: { total_versions: 4, completed_versions: 4, indexed_versions: 2, skipped_versions: 1, failed_versions: 1 } }) }));
  await mount();
  assert.match(body(), /仍有失败版本，不能视为全部索引完成/);
  assert.equal(document.querySelector(".retrieval-job .badge").textContent, "失败");
  assert.doesNotMatch(body(), /本次作业已完成|100%/);
  await tick(t, 6000);
  assert.equal(jobReads().length, 0);
});

test("missing Job counters stay unknown and never imply zero or full success", async () => {
  stub("GET", "/retrieval/status?space_id=space-a", status({ active_job: job({ state: "SUCCEEDED", result: null }) }));
  await mount();
  assert.equal(metric("failed_versions"), "未提供");
  assert.equal(metric("completed_versions"), "未提供");
  assert.match(body(), /完整成功计数尚未确认/);
  assert.equal(document.querySelector(".retrieval-job .badge").textContent, "计数待核对");
});

test("fully reconciled success refreshes coverage exactly once and ends polling", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  stub("GET", "/retrieval/status?space_id=space-a", status({ active_job: job() }));
  stub("GET", "/jobs/index-job", job());
  await mount();
  stub("GET", "/retrieval/status?space_id=space-a", status({ coverage: { catalog_pages: 19, indexed_pages: 19, dirty_pages: 0, indexed_blocks: 60, indexed_chunks: 80 } }));
  stub("GET", "/jobs/index-job", job({ state: "SUCCEEDED", result: {
    phase: "completed", total_versions: 7, completed_versions: 7, indexed_versions: 6, skipped_versions: 1, failed_versions: 0,
  } }));
  await tick(t);
  assert.match(body(), /本次作业已完成，成功索引 6 个版本，跳过 1 个版本，失败 0 个/);
  assert.equal(metric("indexed_pages"), "19");
  assert.equal(metric("dirty_pages"), "0");
  const reads = calls.length;
  await tick(t, 10000);
  assert.equal(calls.length, reads);
});

const completedIndex = (id = "new-index-job") => job({ id, state: "SUCCEEDED", stage: "COMPLETED", force: true,
  created_at: "2026-09-23T08:40:00Z", completed_at: "2026-09-23T09:00:00Z", counts_verified: true,
  application_state: "CURRENT_GENERATION_VERIFIED", matched_current_versions: 19, current_catalog_versions: 19,
  result: {phase: "COMPLETED", total_versions: 20, completed_versions: 20, indexed_versions: 19, skipped_versions: 1,
    failed_versions: 0, indexed_blocks: 41, indexed_chunks: 67} });
const indexSnapshot = (generation = "a".repeat(64), op = completedIndex()) => ({version: 1,
  checked_at: "2026-09-23T09:00:02Z", state: "CURRENT_CATALOG_INDEXED", generation,
  last_indexed_at: "2026-09-23T08:59:59Z", latest_job: op, last_rebuild: op});

test("same counts after rebuild still update generation and clear old search results", async t => {
  t.mock.timers.enable({apis: ["setTimeout"]});
  const original = status({active_job: job(), index_snapshot: indexSnapshot("a".repeat(64), completedIndex("old-job"))});
  stub("GET", "/retrieval/status?space_id=space-a", original);
  stub("GET", "/jobs/index-job", job());
  stub("POST", "/retrieval/search", result());
  await mount(); await type(); await submit();
  assert.ok(document.querySelector(".retrieval-results"));
  const done = completedIndex("index-job");
  stub("GET", "/jobs/index-job", done);
  stub("GET", "/retrieval/status?space_id=space-a", status({index_snapshot: indexSnapshot("b".repeat(64), done)}));
  await tick(t);
  assert.equal(document.querySelector('[data-index-generation]').getAttribute('data-index-generation'), "b".repeat(64));
  assert.equal(metric("indexed_chunks"), "67", "same cardinality is not fake incremented");
  assert.match(body(), /重建已生效/);
  assert.match(body(), /已自动重新核验索引状态/);
  assert.match(body(), /2026\/9\/23 17:00:00/);
  assert.equal(document.querySelector(".retrieval-results"), null);
  assert.equal(calls.filter(c => c.method === "POST").length, 1, "no automatic new search or rebuild");
  const reads = calls.length;
  await tick(t, 10000);
  assert.equal(calls.length, reads);
});

test("completed rebuild survives page remount with distinct current and job count scopes", async () => {
  stub("GET", "/retrieval/status?space_id=space-a", status({index_snapshot: indexSnapshot()}));
  await mount();
  assert.match(body(), /最近作业结果 · 全部处理版本（含历史版本）/);
  assert.equal(metric("catalog_pages"), "19");
  assert.equal(metric("total_versions"), "20");
  assert.match(body(), /当前索引代次/);
  await unmount(); await mount();
  assert.match(body(), /new-index-job/);
  assert.match(body(), /重建已生效/);
  assert.equal(writes().length, 0);
});

test("post-completion status error never confirms a new generation", async t => {
  t.mock.timers.enable({apis: ["setTimeout"]});
  stub("GET", "/retrieval/status?space_id=space-a", status({active_job: job()}));
  stub("GET", "/jobs/index-job", job());
  await mount();
  stub("GET", "/jobs/index-job", completedIndex("index-job"));
  stub("GET", "/retrieval/status?space_id=space-a", () => json({code: "SYNTHETIC", message: "合成读取失败"}, 503));
  await tick(t);
  assert.match(body(), /最新索引状态读取失败/);
  assert.doesNotMatch(body(), /重建已生效/);
  assert.equal(document.querySelector('[data-index-generation]'), null);
});

test("failed or unreconciled historical operation cannot be labelled applied", async () => {
  const failed = completedIndex();
  failed.counts_verified = false;
  failed.result.failed_versions = 1;
  stub("GET", "/retrieval/status?space_id=space-a", status({index_snapshot: indexSnapshot("c".repeat(64), failed)}));
  await mount();
  assert.match(body(), /最近作业未全部成功/);
  assert.doesNotMatch(body(), /重建已生效/);
});

if (process.env.INDEX_REFRESH_CONTRACT_DIR) test("actual rebuild HTTP snapshots update generation in React with unchanged counts", async () => {
  const read = async name => JSON.parse(await readFile(resolve(process.env.INDEX_REFRESH_CONTRACT_DIR, `${name}.json`), "utf8"));
  const before = await read("before"), after = await read("after");
  assert.deepEqual(before.coverage, after.coverage);
  assert.notEqual(before.index_snapshot.generation, after.index_snapshot.generation);
  stub("GET", `/retrieval/profiles?space_id=${before.space_id}`, {default_profile_id: null, items: [], enabled: false});
  stub("GET", `/retrieval/status?space_id=${before.space_id}`, before);
  await mount(app({space: {id: before.space_id, name: "合成重建空间", roles: ["editor"]}}));
  assert.equal(document.querySelector('[data-index-generation]').dataset.indexGeneration, before.index_snapshot.generation);
  stub("GET", `/retrieval/status?space_id=${before.space_id}`, after);
  await click(button("刷新状态"));
  assert.equal(document.querySelector('[data-index-generation]').dataset.indexGeneration, after.index_snapshot.generation);
  assert.match(body(), /重建已生效/);
  assert.ok(body().includes(after.index_snapshot.last_rebuild.id));
  assert.equal(writes().length, 0);
});

test("active job reads pause on error, preserve counts, and resume only on explicit retry", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  stub("GET", "/retrieval/status?space_id=space-a", status({ active_job: job() }));
  stub("GET", "/jobs/index-job", () => json({ code: "POLL_FAILURE", message: "合成进度读取失败" }, 503));
  await mount();
  assert.match(body(), /进度读取已暂停/);
  assert.equal(metric("completed_versions"), "3");
  await tick(t, 10000);
  assert.equal(jobReads().length, 1);
  stub("GET", "/jobs/index-job", job({ state: "CANCELLED" }));
  await click(button("重试"));
  assert.equal(jobReads().length, 2);
  assert.match(body(), /作业已取消/);
  assert.equal(writes().length, 0);
});

test("cancel uses the existing endpoint, aborts stale polling, and waits for terminal confirmation", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const oldPoll = deferred();
  const terminalPoll = deferred();
  let readNo = 0;
  stub("GET", "/retrieval/status?space_id=space-a", status({ active_job: job() }));
  stub("GET", "/jobs/index-job", () => ++readNo === 1 ? oldPoll.promise : terminalPoll.promise);
  stub("POST", "/jobs/index-job/cancel", job({ result: null }));
  await mount();
  const oldSignal = jobReads()[0].signal;
  await click(button("取消作业"));
  assert.equal(oldSignal.aborted, true);
  assert.match(body(), /已请求取消，等待后台确认/);
  assert.equal(button("取消作业").disabled, true);
  assert.equal(writes()[0].url, "/api/v1/jobs/index-job/cancel");
  assert.equal(writes()[0].body, undefined);
  await act(async () => terminalPoll.resolve(json(job({ state: "CANCELLED", result: null }))));
  await act(async () => oldPoll.resolve(json(job())));
  assert.match(body(), /作业已取消/);
  assert.doesNotMatch(body(), /等待后台确认/);
  const reads = jobReads().length;
  await tick(t, 8000);
  assert.equal(jobReads().length, reads);
});

test("retry is manual, uses the existing endpoint, and displays backend rejection", async () => {
  stub("GET", "/retrieval/status?space_id=space-a", status({ active_job: job({ state: "FAILED", error_code: "SYNTHETIC_FAILURE" }) }));
  stub("POST", "/jobs/index-job/retry", () => json({ code: "RETRY_BLOCKED", message: "合成后端拒绝重试" }, 409));
  await mount();
  assert.equal(writes().length, 0);
  await click(button("重试作业"));
  assert.match(body(), /合成后端拒绝重试/);
  stub("POST", "/jobs/index-job/retry", job({ state: "QUEUED", error_code: null, result: null }));
  stub("GET", "/jobs/index-job", job());
  await click(button("重试作业"));
  assert.ok(writes().every(call => call.url === "/api/v1/jobs/index-job/retry"));
  assert.equal(writes().length, 2);
  assert.equal(jobReads().length, 1);
  assert.doesNotMatch(body(), /合成后端拒绝重试/);
});

test("search is explicit and reference-only, renders candidates and opens the exact source version", async () => {
  stub("POST", "/retrieval/search", result());
  stub("GET", "/resources/resource-a", resource());
  await mount();
  await type("  合成检索词  ");
  assert.equal(writes().length, 0);
  await submit();
  assert.deepEqual(JSON.parse(writes()[0].body), { space_id: "space-a", query: "合成检索词", scope: "reference", limit: 12 });
  const results = document.querySelector(".retrieval-results").textContent;
  assert.match(results, /合成检索候选甲/);
  assert.match(results, /vector/);
  assert.match(results, /lexical/);
  assert.match(results, /向量排名：2/);
  assert.match(results, /关键词排名：1/);
  assert.match(results, /合成覆盖警示/);
  assert.match(results, /18.75 ms/);
  assert.match(results, /共 16 个候选/);
  assert.equal(opened.length, 0);
  assert.equal(calls.some(call => call.url.includes("/resources/")), false);
  await click(button("打开来源"));
  assert.deepEqual(opened, [[resource(), "content", "version-a", "block-a"]]);
});

test("empty and failed searches clear previous candidates and never trigger generation", async () => {
  stub("POST", "/retrieval/search", result());
  await mount();
  await type();
  await submit();
  stub("POST", "/retrieval/search", () => json({ code: "SEARCH_FAILURE", message: "合成检索失败" }, 503));
  await submit();
  assert.match(body(), /合成检索失败/);
  assert.doesNotMatch(body(), /合成检索候选甲/);
  stub("POST", "/retrieval/search", result({ hits: [], returned: 0, total_candidates: 0 }));
  await submit();
  assert.match(body(), /未找到匹配候选/);
  assert.doesNotMatch(body(), /合成检索失败/);
  assert.ok(writes().every(call => call.url === "/api/v1/retrieval/search"));
});

test("source errors and cross-space responses never open a resource", async () => {
  stub("POST", "/retrieval/search", result());
  stub("GET", "/resources/resource-a", () => json({ code: "NOT_FOUND", message: "合成来源不可访问" }, 404));
  await mount();
  await type();
  await submit();
  await click(button("打开来源"));
  assert.match(body(), /合成来源不可访问/);
  assert.equal(opened.length, 0);
  stub("GET", "/resources/resource-a", resource({ space_id: "space-b" }));
  await click(button("打开来源"));
  assert.match(body(), /来源响应与当前空间或候选不一致/);
  assert.equal(opened.length, 0);
});

test("switching spaces removes visible results and ignores a pending old search", async () => {
  const pendingSearch = deferred();
  stub("POST", "/retrieval/search", result());
  stub("GET", "/retrieval/status?space_id=space-b", status({ space_id: "space-b", permissions: { can_index: false } }));
  await mount();
  await type();
  await submit();
  assert.match(body(), /合成检索候选甲/);
  await switchApp(app({ space: { id: "space-b", name: "合成空间乙", roles: ["reader"] } }));
  assert.doesNotMatch(body(), /合成检索候选甲/);
  assert.equal(document.querySelector("input").value, "");
  await switchApp(app());
  stub("POST", "/retrieval/search", () => pendingSearch.promise);
  await type();
  await submit();
  const signal = writes().at(-1).signal;
  await switchApp(app({ space: { id: "space-b", name: "合成空间乙", roles: ["reader"] } }));
  assert.equal(signal.aborted, true);
  await act(async () => pendingSearch.resolve(json(result())));
  assert.doesNotMatch(body(), /合成检索候选甲/);
  assert.equal(button("增量同步").disabled, true);
  assert.equal(document.querySelector("input").value, "");
});

test("a late old status is aborted on space change and cannot overwrite new permissions", async () => {
  const oldStatus = deferred();
  stub("GET", "/retrieval/status?space_id=space-a", () => oldStatus.promise);
  stub("GET", "/retrieval/status?space_id=space-b", status({ space_id: "space-b", permissions: { can_index: false } }));
  await mount();
  const signal = calls.find(call => call.url.includes("/retrieval/status?")).signal;
  await switchApp(app({ space: { id: "space-b", name: "合成空间乙", roles: ["reader"] } }));
  assert.equal(signal.aborted, true);
  await act(async () => oldStatus.resolve(json(status())));
  assert.equal(button("增量同步").disabled, true);
  assert.match(body(), /只有检索查看权限/);
  assert.equal(writes().length, 0);
});

test("pending index creation is aborted on space change and cannot start polling the old job", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const pendingIndex = deferred();
  stub("POST", "/retrieval/index-jobs", () => pendingIndex.promise);
  stub("GET", "/retrieval/status?space_id=space-b", status({ space_id: "space-b" }));
  await mount();
  await click(button("增量同步"));
  const signal = writes()[0].signal;
  await switchApp(app({ space: { id: "space-b", name: "合成空间乙", roles: ["editor"] } }));
  assert.equal(signal.aborted, true);
  await act(async () => pendingIndex.resolve(json(job())));
  await tick(t, 10000);
  assert.equal(jobReads().length, 0);
  assert.doesNotMatch(body(), /synthetic-version-3/);
  assert.equal(button("增量同步").disabled, false);
});

test("space changes and unmounts abort active polling and ignore late counts", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const pendingPoll = deferred();
  stub("GET", "/retrieval/status?space_id=space-a", status({ active_job: job() }));
  stub("GET", "/jobs/index-job", () => pendingPoll.promise);
  stub("GET", "/retrieval/status?space_id=space-b", status({ space_id: "space-b" }));
  await mount();
  const oldSignal = jobReads()[0].signal;
  await switchApp(app({ space: { id: "space-b", name: "合成空间乙", roles: ["reader"] } }));
  assert.equal(oldSignal.aborted, true);
  await act(async () => pendingPoll.resolve(json(job())));
  assert.doesNotMatch(body(), /synthetic-version-3/);
  await tick(t, 8000);
  assert.equal(jobReads().length, 1);
  stub("GET", "/jobs/index-job", job());
  await switchApp(app());
  const lastSignal = jobReads().at(-1).signal;
  await unmount();
  assert.equal(lastSignal.aborted, true);
  const reads = jobReads().length;
  await tick(t, 10000);
  assert.equal(jobReads().length, reads);
});

test("late source navigation is cancelled on unmount", async () => {
  const pendingSource = deferred();
  stub("POST", "/retrieval/search", result());
  stub("GET", "/resources/resource-a", () => pendingSource.promise);
  await mount();
  await type();
  await submit();
  await click(button("打开来源"));
  const signal = calls.at(-1).signal;
  await unmount();
  assert.equal(signal.aborted, true);
  await act(async () => pendingSource.resolve(json(resource())));
  assert.equal(opened.length, 0);
});

test("user and permission revisions isolate prior search results within the same space", async () => {
  stub("POST", "/retrieval/search", result());
  await mount();
  await type();
  await submit();
  await switchApp(app({ me: { id: "synthetic-other-user", csrf_token: "synthetic-other-csrf" } }));
  assert.doesNotMatch(body(), /合成检索候选甲/);
  assert.equal(document.querySelector("input").value, "");
  await type();
  await submit();
  stub("GET", "/retrieval/status?space_id=space-a", status({ permissions: { can_index: false } }));
  await switchApp({ ...currentApp, refresh: 1 });
  assert.doesNotMatch(body(), /合成检索候选甲/);
  assert.equal(button("增量同步").disabled, true);
});

const profilePicker = () => document.querySelector('[aria-label="选择检索方案"]');
async function chooseProfile(id) {
  await act(async () => {
    profilePicker().value = id;
    profilePicker().dispatchEvent(new window.Event("change", { bubbles: true }));
  });
}
function dualProfiles(items = [qwen, bge], defaultId = "qwen", space = "space-a") {
  stub("GET", `/retrieval/profiles?space_id=${space}`, profiles(items, defaultId));
  for (const item of items) stub("GET", `/retrieval/status?space_id=${space}&profile_id=${item.id}`,
    status({ space_id: space, retrieval_selection: frozenSelection(item) }));
}
const searchCalls = () => writes().filter(call => call.url.endsWith("/retrieval/search"));

test("server Qwen default loads its status without persisting names or fingerprints or starting work", async () => {
  dualProfiles([bge, qwen]);
  await mount();
  assert.equal(profilePicker().value, "qwen");
  assert.ok(calls.some(call => call.url.endsWith("space_id=space-a&profile_id=qwen")));
  assert.equal(window.localStorage.length, 0);
  assert.equal(writes().length, 0);
});

test("switching profiles reruns the submitted query, preserves the edited draft and ignores a late old result", async () => {
  dualProfiles();
  const old = deferred();
  stub("POST", "/retrieval/search", request => {
    const data = JSON.parse(request.body);
    return data.retrieval_selection.profile_id === "qwen" ? old.promise
      : json(result({ retrieval_selection: frozenSelection(bge), hits: [hit({ title: "BGE 当前结果" })] }));
  });
  await mount(); await type(); await submit();
  const oldSignal = searchCalls()[0].signal;
  await type("尚未提交的编辑");
  await chooseProfile("bge");
  assert.equal(oldSignal.aborted, true);
  assert.equal(document.querySelector('input[type="search"]').value, "尚未提交的编辑");
  assert.equal(searchCalls().length, 2);
  assert.deepEqual(searchCalls().map(call => JSON.parse(call.body).query), ["合成检索词", "合成检索词"]);
  assert.deepEqual(JSON.parse(searchCalls()[1].body).retrieval_selection, selection(bge));
  await act(async () => old.resolve(json(result({ retrieval_selection: frozenSelection(qwen) }))));
  assert.match(body(), /BGE 当前结果/);
  assert.doesNotMatch(body(), /合成检索候选甲/);
});

test("an A to B to A switch rejects old A responses even though the selection matches again", async () => {
  dualProfiles(); const oldA = deferred(); const oldB = deferred(); let requests = 0;
  stub("POST", "/retrieval/search", () => ++requests === 1 ? oldA.promise : requests === 2 ? oldB.promise
    : json(result({ retrieval_selection: frozenSelection(qwen), hits: [hit({ title: "新 A 结果" })] })));
  await mount(); await type(); await submit(); await chooseProfile("bge"); await chooseProfile("qwen");
  await act(async () => {
    oldA.resolve(json(result({ retrieval_selection: frozenSelection(qwen), hits: [hit({ title: "旧 A 结果" })] })));
    oldB.resolve(json(result({ retrieval_selection: frozenSelection(bge) })));
  });
  assert.equal(searchCalls().length, 3);
  assert.match(body(), /新 A 结果/); assert.doesNotMatch(body(), /旧 A 结果|合成检索候选甲/);
});

test("late status and source navigation are cancelled when the profile changes", async () => {
  dualProfiles(); const oldStatus = deferred(); const oldSource = deferred();
  stub("GET", "/retrieval/status?space_id=space-a&profile_id=qwen", () => oldStatus.promise);
  await mount(); await chooseProfile("bge");
  await act(async () => oldStatus.resolve(json(status({ retrieval_selection: frozenSelection(qwen), permissions: { can_index: false } }))));
  assert.equal(button("增量同步").disabled, false);
  stub("POST", "/retrieval/search", request => json(result({
    retrieval_selection: frozenSelection(JSON.parse(request.body).retrieval_selection.profile_id === "qwen" ? qwen : bge),
  })));
  stub("GET", "/resources/resource-a", () => oldSource.promise);
  await type(); await submit(); await click(button("打开来源"));
  const signal = calls.at(-1).signal;
  stub("GET", "/retrieval/status?space_id=space-a&profile_id=qwen", status({ retrieval_selection: frozenSelection(qwen) }));
  await chooseProfile("qwen");
  assert.equal(signal.aborted, true);
  await act(async () => oldSource.resolve(json(resource())));
  assert.equal(opened.length, 0);
});

for (const withReadiness of [true, false]) test(`unbuilt Qwen remains selectable and indexable; readiness fields ${withReadiness ? "present" : "legacy"}`, async () => {
  const unbuilt = { ...qwen, available: false, state: "NOT_READY", index_ready: false, index_state: "NOT_BUILT" };
  if (!withReadiness) for (const field of ["model_ready", "index_ready", "index_state", "can_index"]) delete unbuilt[field];
  dualProfiles([unbuilt, bge], "bge");
  stub("POST", "/retrieval/index-jobs", job({ state: "QUEUED" }));
  stub("GET", "/jobs/index-job", job());
  await mount();
  assert.equal([...profilePicker().options].find(item => item.value === "qwen").disabled, false);
  await chooseProfile("qwen"); await type(); await submit();
  assert.equal(searchCalls().length, 0);
  assert.equal(button("增量同步").disabled, false);
  assert.equal(button("完整重建").disabled, false);
  await click(button("增量同步"));
  assert.deepEqual(JSON.parse(writes()[0].body), { space_id: "space-a", force: false, retrieval_selection: selection(qwen) });
  assert.equal(button("取消作业").disabled, false);
});

test("server readiness blocks new index work but does not prevent cancelling an existing job", async () => {
  dualProfiles([{ ...qwen, available: false, model_ready: false, can_index: false }, bge]);
  stub("GET", "/retrieval/status?space_id=space-a&profile_id=qwen", status({ retrieval_selection: frozenSelection(qwen), active_job: job() }));
  stub("GET", "/jobs/index-job", job());
  stub("POST", "/jobs/index-job/cancel", job({ state: "CANCELLED" }));
  await mount();
  assert.equal(button("增量同步").disabled, true);
  assert.equal(button("取消作业").disabled, false);
  await click(button("取消作业"));
  assert.equal(writes().length, 1);
  assert.ok(writes()[0].url.endsWith("/cancel"));
});

test("a missing reranker blocks retrieval but allows embedding-only index work when can_index is true", async () => {
  dualProfiles([{ ...qwen, available: false, model_ready: false, can_index: true }, bge]);
  await mount(); await type(); await submit();
  assert.equal(searchCalls().length, 0);
  assert.equal(button("增量同步").disabled, false);
  assert.match(body(), /模型运行前提尚未就绪/);
});

test("profile metadata cannot override status permissions", async () => {
  dualProfiles();
  stub("GET", "/retrieval/status?space_id=space-a&profile_id=qwen", status({ retrieval_selection: frozenSelection(qwen), permissions: { can_index: false } }));
  await mount(); assert.equal(button("增量同步").disabled, true);
});

test("refresh reads new readiness and fingerprint and permits the same query after indexing", async () => {
  dualProfiles([{ ...qwen, available: false, index_ready: false }, bge]);
  await mount(); await type(); await submit();
  assert.equal(searchCalls().length, 0);
  const updated = { ...qwen, fingerprint: "c".repeat(64) };
  dualProfiles([updated, bge]);
  stub("POST", "/retrieval/search", result({ retrieval_selection: frozenSelection(updated) }));
  await click(button("刷新状态"));
  assert.equal(document.querySelector('input[type="search"]').value, "合成检索词");
  assert.equal(searchCalls().length, 0, "a blocked draft was never a submitted query");
  await submit();
  assert.deepEqual(JSON.parse(searchCalls()[0].body).retrieval_selection, selection(updated));
});

for (const returned of [undefined, frozenSelection(bge), { ...frozenSelection(qwen), fingerprint: "f".repeat(64) }])
  test(`search rejects missing or mismatched selection: ${returned?.profile_id ?? "missing"}/${returned?.fingerprint?.[0] ?? "none"}`, async () => {
    dualProfiles();
    stub("POST", "/retrieval/search", result({ retrieval_selection: returned }));
    await mount(); await type(); await submit();
    assert.match(body(), /返回结果与当前检索方案不一致/);
    assert.equal(document.querySelector(".retrieval-results"), null);
    assert.equal(searchCalls().length, 1);
  });

test("removed preferences never issue a server-default status, search or index request", async () => {
  dualProfiles();
  window.localStorage.setItem(retrievalKey("synthetic-user", "space-a"), "removed-profile");
  await mount(); await type(); await submit(); await click(button("增量同步"));
  assert.equal(profilePicker().value, "removed-profile");
  assert.equal(writes().length, 0);
  assert.ok(calls.every(call => call.url.includes("/retrieval/profiles?")));
  await click(button("恢复默认"));
  assert.equal(profilePicker().value, "qwen");
  assert.equal(window.localStorage.length, 0);
  assert.equal(writes().length, 0);
});

for (const mode of ["wiki", "wiki_fallback"]) test(`explicit profiles cannot silently display ${mode} as a successful semantic search`, async () => {
  dualProfiles();
  stub("POST", "/retrieval/search", result({ mode, retrieval_selection: frozenSelection(qwen) }));
  await mount(); await type(); await submit();
  assert.equal(document.querySelector(".retrieval-results"), null);
  assert.match(body(), /不会自动降级/);
  assert.equal(searchCalls().length, 1);
});

test("same-profile cross-tab changes do not duplicate a search; another profile reuses the query", async () => {
  dualProfiles();
  stub("POST", "/retrieval/search", request => json(result({
    retrieval_selection: frozenSelection(JSON.parse(request.body).retrieval_selection.profile_id === "qwen" ? qwen : bge),
  })));
  await mount(); await type(); await submit();
  const key = retrievalKey("synthetic-user", "space-a");
  for (const id of ["qwen", "bge"]) await act(async () => {
    window.localStorage.setItem(key, id);
    window.dispatchEvent(new window.StorageEvent("storage", { key, newValue: id }));
  });
  assert.equal(profilePicker().value, "bge");
  assert.equal(searchCalls().length, 2);
});
