// No browser, model API or backend service is launched. SVG DOM and force-model
// checks are interaction/contract evidence, not browser frame-rate or visual QA.
import test, { after, afterEach } from "node:test";
import assert from "node:assert/strict";
import Module, { createRequire } from "node:module";
import { resolve } from "node:path";
import { build } from "esbuild";
const require = createRequire(import.meta.url);
const { JSDOM } = require("jsdom");
const React = require("react");
const { act } = React;
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',
  { url: "http://localhost/#/knowledge", pretendToBeVisual: true });
const { window } = dom;
for (const key of ["window", "document", "HTMLElement", "HTMLDialogElement", "Element", "Node", "MutationObserver", "Event", "MouseEvent", "FormData"])
  globalThis[key] = key === "window" ? window : window[key];
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
const computed = window.getComputedStyle.bind(window);
globalThis.getComputedStyle = window.getComputedStyle = (element) => new Proxy(computed(element), {
  get(target, key) {
    const value = target[key];
    if (["translate", "scale", "rotate"].includes(key) && value === "") return "none";
    return typeof value === "function" ? value.bind(target) : value;
  },
});
const media = new Map();
window.matchMedia = (query) => {
  if (!media.has(query)) {
    const entry = new window.EventTarget();
    Object.assign(entry, { media: query, matches: query.includes("reduce") && !query.includes("no-preference"),
      addListener(listener) { this.addEventListener("change", listener); },
      removeListener(listener) { this.removeEventListener("change", listener); } });
    media.set(query, entry);
  }
  return media.get(query);
};
window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
window.HTMLDialogElement.prototype.close = function () { this.open = false; };
const observers = new Set();
globalThis.ResizeObserver = class {
  constructor(callback) { this.callback = callback; observers.add(this); }
  observe() {}
  disconnect() { observers.delete(this); }
};
const { createRoot } = require("react-dom/client");
const gsap = require("gsap").gsap;
const { ScrollTrigger } = require("gsap/dist/ScrollTrigger");
let requests = [];
let root;
const failures = [];
window.addEventListener("error", (event) => failures.push(event.error));
let respond = () => { throw new Error("Unexpected test HTTP request"); };
const originalFetch = globalThis.fetch;
globalThis.fetch = async (input, options = {}) => {
  const url = new URL(String(input), "http://localhost");
  const record = { path: url.pathname.replace(/^\/api\/v1/, ""), params: url.searchParams,
    method: options.method || "GET", body: options.body ? JSON.parse(options.body) : undefined, signal: options.signal,
    headers: new Headers(options.headers) };
  requests.push(record);
  const value = await respond(record);
  return new Response(JSON.stringify(value.body ?? value), { status: value.status ?? 200,
    headers: { "Content-Type": "application/json", ...(value.headers || {}) } });
};

const result = await build({
  stdin: { contents: `export * from './src/knowledgeTypes'; export * from './src/KnowledgeWorkspace';
    export * from './src/WikiMaintenanceDialog';
    export * from './src/WikiBuildCoverage';
    export * from './src/KnowledgeGraph'; export * from './src/wikiSessionCache'; export { AppContext } from './src/ui'; export {richHtml} from './src/richText';`,
    resolveDir: process.cwd(), loader: "tsx" },
  bundle: true, write: false, format: "cjs", platform: "node",
  external: ["react", "react/*", "react-dom", "react-dom/*", "gsap", "@gsap/react", "gsap/ScrollTrigger"],
  loader: { ".css": "empty" }, define: { "import.meta.env": "{}" },
});
const filename = resolve("src/__knowledge_test_bundle.cjs");
const compiled = new Module(filename);
compiled.filename = filename;
compiled.paths = Module._nodeModulePaths(resolve("src"));
const runtimeRequire = compiled.require.bind(compiled);
compiled.require = id => id === "gsap" ? gsap : id === "gsap/ScrollTrigger" ? { ScrollTrigger } : runtimeRequire(id);
compiled._compile(result.outputFiles[0].text, filename);
const wiki = compiled.exports;

const ID = "00000000-0000-4000-8000-000000000001";
const SOURCE = "00000000-0000-4000-8000-000000000002";
const VERSION = "00000000-0000-4000-8000-000000000003";
const source = { id: SOURCE, space_id: "space", name: "合成来源文件", kind: "document", active_version_id: VERSION,
  suspended: false, deleted_at: null, category: "法规", tags: [], revision: 1 };
const resource = { ...source, id: ID, name: "合成知识页", kind: "knowledge" };
const page = { id: ID, name: resource.name, kind: "knowledge", knowledge_type: "term", category: "运营/估值", tags: ["估值"],
  version_id: VERSION, version_no: 1, state: "PUBLISHED", excerpt: "合成测试内容", updated_at: null, link_count: 1, backlink_count: 0 };
const workspace = { pages: [page], categories: [{ path: "运营/估值", name: "估值", count: 1 }], tags: [{ name: "估值", count: 1 }],
  stats: {}, mode: "wiki", truncated: false };
const taxonomy = { space_id: "space", revision: 7, categories: workspace.categories, truncated: false };
const model = { connection_id: "connection", model_id: "model", configured: true, allow_document_transfer: true,
  connection_name: "合成连接", model_name: "合成测试模型", brand: "custom", kind: "local", protocol: "ollama" };
const node = (id, overrides = {}) => ({ id, label: `节点 ${id}`, kind: "knowledge", knowledge_type: "rule", category: "运营", state: "PUBLISHED", version_id: VERSION, ...overrides });
const edge = (id, from, to) => ({ id, source: from, target: to, type: "WIKI_LINK", origin: "wikilink", state: "ACTIVE" });
const opened = [];
const app = { space: { id: "space", name: "合成空间", roles: ["admin", "editor"] }, refresh: 0, me: { id: "user", spaces: [] },
  bump() {}, notify() {}, navigate(value) { opened.push(value); }, openResource(...args) { opened.push(args); },
  openVersion(...args) { opened.push(args); }, ask() {} };

async function settle() { await act(async () => { await new Promise((done) => setTimeout(done, 25)); }); }
async function mount(component, props = {}) {
  root = createRoot(document.getElementById("root"));
  await act(async () => root.render(React.createElement(wiki.AppContext.Provider, { value: app }, React.createElement(component, props))));
  await settle();
}
function button(label) { return [...document.querySelectorAll("button")].find((item) => item.textContent.includes(label)); }
async function click(element) { assert.ok(element); await act(async () => element.click()); await settle(); }
async function fill(selector, value) {
  const element = document.querySelector(selector);
  assert.ok(element);
  await act(async () => {
    Object.getOwnPropertyDescriptor(element.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype, "value").set.call(element, value);
    element.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  await settle();
}
async function selectValue(selector, value) {
  const element = document.querySelector(selector);
  assert.ok(element);
  await act(async () => { element.value = value; element.dispatchEvent(new window.Event("change", { bubbles: true })); });
  await settle();
}
async function debounceSearch() { await act(async () => { await new Promise((done) => setTimeout(done, 280)); }); await settle(); }
function defaultResponse(request) {
  if (request.path === "/wiki/workspace") return workspace;
  if (request.path === "/wiki/taxonomy") return taxonomy;
  if (request.path === "/wiki/compilation-specs") return {spec_version:"wiki-compilation/1.0", types:
    ["topic", "atomic_rule", "scenario", "sop"].map(compilation_type => ({compilation_type, purpose:"保留完整结构", sections:[]}))};
  if (request.path === `/wiki/entries/${ID}/maintenance`) return {headers:{ETag:'"wme-0-synthetic"'}, body:{
    resource_id:ID,title:"合成知识页",aliases:["知识简称"],canonical_key:null,canonical_resource_id:ID,
    canonical_available:true,is_canonical:true,can_edit:true}};
  if (request.path === "/wiki/maintenance/proposals") return {items:[],total:0};
  if (request.path === "/wiki/graph") return { nodes: [node(SOURCE, { kind: "document" }), node(ID)], edges: [edge("link", ID, SOURCE)], truncated: false, total_visible_nodes: 2 };
  if (request.path === `/resources/${SOURCE}`) return source;
  if (request.path === `/resources/${ID}`) return resource;
  if (request.path === "/resources") return { items: request.params.get("kind") === "template" ? [] : [source], next_cursor: null };
  if (request.path === "/model-options") return { items: [model], default: null };
  if (request.path === `/versions/${VERSION}`) return { id: VERSION, resource_id: ID, title: resource.name, version_no: 1,
    state: "APPROVED", revision: 1, author_id: "user", knowledge_type: "term", blocks: [], applicability: {}, required_facts: [] };
  if (request.path === `/wiki/pages/${ID}/links`) return { outgoing: [], incoming: [], sources: [], unresolved: [{ title: "尚未建立" }] };
  throw new Error(`Unexpected test request: ${request.path}`);
}
respond = defaultResponse;
afterEach(async () => {
  if (root) { await act(async () => root.unmount()); root = undefined; }
  wiki.clearWikiSessionCache();
  assert.equal(observers.size, 0, "ResizeObserver leaked after graph unmount");
  assert.equal(document.querySelectorAll('.star-surface').length, 0, "SVG graph remained after unmount");
  assert.deepEqual(failures.splice(0), []);
  requests = [];
  opened.length = 0;
  respond = defaultResponse;
  window.history.replaceState(null, "", "#/knowledge");
});
after(() => {
  globalThis.fetch = originalFetch;
  try { assert.equal(ScrollTrigger.getAll().length, 0); }
  finally { ScrollTrigger.disable(); gsap.ticker.sleep(); dom.window.close(); }
});

test("classification tree keeps authoritative counts and adds only navigation ancestors", () => {
  const tree = wiki.categoryTree([{ path: "运营/估值/债券", name: "债券", count: 2 }, { path: "运营/估值", name: "估值", count: 3 }]);
  assert.equal(tree[0].count, null);
  assert.equal(tree[0].children[0].count, 3, "parent count must not double-count descendants");
  assert.equal(tree[0].children[0].children[0].count, 2);
  assert.deepEqual(wiki.categoryTree([]), []);
});

test("four compilation types are explicit and changing type never calls a model", async () => {
  await mount(wiki.WikiBuildDialog, {close(){}});
  const control = document.querySelector('[aria-label="知识编译类型"]');
  assert.deepEqual([...control.options].map(o=>o.value), ["topic","atomic_rule","scenario","sop"]);
  await selectValue('[aria-label="知识编译类型"]', "sop");
  assert.match(document.body.textContent, /完整结构要求/);
  assert.equal(requests.filter(r=>r.method!=="GET").length, 0);
  assert.equal(wiki.buildRequest("space", [SOURCE], model, 1, true, "published", "topic", "sop").compilation_type, "sop");
});

test("partial build exposes every coverage unit without automatically starting another model job", async () => {
  let continued = 0;
  const units = Array.from({length:1007}, (_,index) => ({unit_id:`unit-${index}`,status:index<32?"INCORPORATED":"NOT_SENT",
    source_blocks:[{version_id:VERSION,block_id:`source-block-${index}`,char_start:0,char_end:1200}]}));
  await mount(wiki.WikiBuildCoverage, {job:{state:"SUCCEEDED",result:{revision_proposal_ids:["revision"],batch:{
    scope_status:"PARTIAL",total_units:1007,outside_scope_blocks:0,counts:{INCORPORATED:32,NOT_SENT:975},units}}},
    continueBatch(){continued++;}});
  assert.match(document.body.textContent, /部分编译/);
  assert.match(document.body.textContent, /source-block-1006/);
  assert.match(document.body.textContent, /完整修订候选/);
  assert.equal(continued,0);
  assert.equal(requests.length,0);
  await click(button("继续整理剩余来源"));
  assert.equal(continued,1);
  assert.equal(requests.length,0);
});

test("maintenance metadata saves to server with snapshot ETag and does not alter manuscript", async () => {
  respond = r => r.method === "PUT" ? defaultResponse({...r,method:"GET"}) : defaultResponse(r);
  await mount(wiki.WikiMaintenanceDialog, {close(){}, pages:[page], initialId:ID});
  assert.equal(requests.filter(r=>r.method!=="GET").length, 0);
  await fill('[aria-label="主条目标识"]', "稳定主条目");
  await fill('[aria-label="条目别名"]', "别名甲\n别名乙");
  await fill('[aria-label="元数据维护原因"]', "统一业务名称");
  await click(button("保存标识与别名"));
  const write = requests.find(r=>r.method === "PUT");
  assert.equal(write.path, `/wiki/entries/${ID}/maintenance`);
  assert.equal(write.headers.get("If-Match"), '"wme-0-synthetic"');
  assert.deepEqual(write.body, {canonical_key:"稳定主条目", aliases:["别名甲","别名乙"], reason:"统一业务名称"});
  assert.equal(requests.filter(r=>r.path.startsWith("/versions/") && r.method!=="GET").length, 0);
});

test("maintenance suggestion is separate from accept and stale input cannot be accepted", async () => {
  const proposal = {id:"synthetic-proposal", kind:"REVISION", status:"PROPOSED", reason:"需核对新来源",
    entries:[{resource_id:ID,title:page.name,version_id:VERSION}], target_resource_id:null, origin:"HUMAN",
    snapshot_current:false,can_review:true,source_changes:[]};
  respond = r => r.path === "/wiki/maintenance/proposals" ? (r.method === "POST"
    ? {status:201,body:proposal} : {items:[proposal],total:1})
    : r.path === "/wiki/maintenance/proposals/synthetic-proposal" ? {headers:{ETag:'"wmp-1-synthetic"'},body:proposal}
    : r.path.endsWith("/review") ? {...proposal,status:"REJECTED"} : defaultResponse(r);
  await mount(wiki.WikiMaintenanceDialog, {close(){}, pages:[page], initialId:ID});
  await fill('[aria-label="维护建议原因"]', "需核对新来源");
  await click(button("登记建议，待审阅"));
  await settle();
  assert.equal(requests.filter(r=>r.path.endsWith("/review")).length, 0);
  await fill('[aria-label="维护审阅说明"]', "输入已变，请重新提出");
  assert.equal(button("接受并创建修订草稿").disabled, true);
  await click(button("拒绝建议"));
  const write = requests.find(r=>r.path.endsWith("/review"));
  assert.equal(write.headers.get("If-Match"), '"wmp-1-synthetic"');
  assert.deepEqual(write.body, {decision:"REJECT", comment:"输入已变，请重新提出"});
});
test("node categories distinguish original sources, wiki pages, terms and templates", () => {
  assert.equal(wiki.graphGroup(node("a", { kind: "document", knowledge_type: "source" })), "source");
  assert.equal(wiki.graphGroup(node("b", { knowledge_type: "term" })), "term");
  assert.equal(wiki.graphGroup(node("c", { kind: "template" })), "template");
  assert.equal(wiki.graphGroup(node("d")), "wiki");
});
test("graph keeps all returned nodes and only flags invalid dangling edges", () => {
  const data = { nodes: Array.from({ length: 230 }, (_, i) => node(String(i))),
    edges: [edge("a", "0", "1"), edge("b", "0", "229"), edge("c", "0", "hidden")], truncated: false, total_visible_nodes: 230 };
  const original = structuredClone(data);
  const graph = wiki.boundedGraph(data, 10000);
  assert.equal(graph.nodes.length, 230);
  assert.deepEqual(graph.edges.map((item) => item.id), ["a", "b"]);
  assert.equal(graph.truncated, true);
  assert.deepEqual(data, original);
  assert.ok(!graph.nodes.some((item) => item.id === "hidden"));
});
test("graph deduplicates IDs without an edge ceiling and namespaces nodes separately from edges", () => {
  const graph = wiki.boundedGraph({ nodes: [node("same"), node("same"), node("target")],
    edges: Array.from({ length: 810 }, (_, i) => edge(i === 0 ? "same" : String(i), "same", "target")), truncated: false, total_visible_nodes: 2 });
  assert.equal(graph.nodes.length, 2);
  assert.equal(graph.edges.length, 810);
  const elements = wiki.graphElements(graph);
  assert.equal(new Set(elements.map((item) => item.data.id)).size, elements.length);
});
test("empty graph is actually empty; no decorative placeholders become knowledge nodes", () => {
  const graph = wiki.boundedGraph({ nodes: [], edges: [], truncated: false, total_visible_nodes: 0 });
  assert.deepEqual(wiki.graphElements(graph), []);
  assert.equal(graph.truncated, false);
});
test("build request freezes explicit sources/model and enforces transfer consent and page budget", () => {
  assert.deepEqual(wiki.buildRequest("space", [SOURCE, SOURCE], model, 4, true), { space_id: "space", source_resource_ids: [SOURCE],
    model_selection: { connection_id: "connection", model_id: "model" }, max_pages: 4, consent: true });
  for (const value of [0, 13, 1.5, Number.NaN]) assert.throws(() => wiki.buildRequest("space", [SOURCE], model, value, true));
  assert.throws(() => wiki.buildRequest("space", [], model, 4, true));
  assert.throws(() => wiki.buildRequest("space", Array.from({ length: 9 }, (_, i) => String(i)), model, 4, true));
  assert.throws(() => wiki.buildRequest("space", [SOURCE], undefined, 4, true));
  assert.throws(() => wiki.buildRequest("space", [SOURCE], { ...model, configured: false }, 4, true));
  assert.throws(() => wiki.buildRequest("space", [SOURCE], { ...model, allow_document_transfer: false }, 4, true));
  assert.throws(() => wiki.buildRequest("space", [SOURCE], model, 4, false));
  assert.equal(wiki.buildRequest("space", [SOURCE], model, 3, true, "unverified_draft").source_mode, "unverified_draft");
});
test("knowledge point requests require draft mode and preserve source, model, consent and page limits", () => {
  const topic = wiki.buildRequest("space", [SOURCE], model, 4, true);
  assert.deepEqual(wiki.buildRequest("space", [SOURCE], model, 4, true, "published", "topic"), topic);
  const draft = wiki.buildRequest("space", [SOURCE], model, 4, true, "unverified_draft", "topic");
  assert.equal(Object.hasOwn(draft, "granularity"), false);
  assert.deepEqual(wiki.buildRequest("space", [SOURCE, SOURCE], model, 4, true, "unverified_draft", "knowledge_points"),
    { ...draft, granularity: "knowledge_points" });
  assert.throws(() => wiki.buildRequest("space", [SOURCE], model, 4, true, "published", "knowledge_points"), /仅支持待核验/);
  for (const granularity of ["relations", "unknown", "", null])
    assert.throws(() => wiki.buildRequest("space", [SOURCE], model, 4, true, "unverified_draft", granularity), /生成粒度无效/);
  assert.throws(() => wiki.buildRequest("space", [SOURCE], model, 4, false, "unverified_draft", "knowledge_points"), /明确确认/);
  assert.throws(() => wiki.buildRequest("space", [], model, 4, true, "unverified_draft", "knowledge_points"), /请选择/);
  for (const count of [0, 13, 1.5, NaN])
    assert.throws(() => wiki.buildRequest("space", [SOURCE], model, count, true, "unverified_draft", "knowledge_points"), /1–12/);
  for (const option of [undefined, { ...model, configured: false }, { ...model, allow_document_transfer: false }])
    assert.throws(() => wiki.buildRequest("space", [SOURCE], option, 4, true, "unverified_draft", "knowledge_points"));
});

test("draft build is explicitly selected and still requires model and consent", async () => {
  const draft = {...source,active_version_id:null,latest_state:"DRAFT"};
  respond = request => request.path === "/resources" ? {items:[draft],next_cursor:null}
    : request.path === `/resources/${SOURCE}` ? draft
    : request.path === "/wiki/builds" ? {status:202,body:{id:"job",kind:"COMPILE",state:"QUEUED",stage:"",attempts:0,error_code:null,result:null}}
    : defaultResponse(request);
  await mount(wiki.WikiBuildDialog,{close(){}});
  assert.equal(document.querySelector('.wiki-source-options input').disabled,true);
  const select = [...document.querySelectorAll('select')].find(el=>[...el.options].some(o=>o.value==='unverified_draft'));
  await act(async()=>{select.value='unverified_draft';select.dispatchEvent(new window.Event('change',{bubbles:true}));});
  await settle();
  assert.equal(document.querySelector('.wiki-source-options input').disabled,false);
  await click(document.querySelector('.wiki-source-options input'));
  await chooseBuildModelAndConsent();
  await click(button('提交 Wiki 构建'));
  assert.equal(requests.find(r=>r.path==='/wiki/builds').body.source_mode,'unverified_draft');
  assert.equal(Object.hasOwn(requests.find(r=>r.path==='/wiki/builds').body, 'granularity'), false);
});
test("draft-only granularity selector sends knowledge_points with max_pages and no reference IDs", async () => {
  const draft = { ...source, active_version_id: null, latest_state: "DRAFT" };
  respond = (r) => r.path === "/resources" ? { items: [draft], next_cursor: null }
    : r.path === "/wiki/builds" ? { status: 202, body: { id: "job", kind: "COMPILE", state: "QUEUED", stage: "", attempts: 0, error_code: null, result: null } }
    : defaultResponse(r);
  await mount(wiki.WikiBuildDialog, { close() {} });
  assert.equal(document.querySelector('[aria-label="生成粒度"]'), null);
  await selectValue('[aria-label="构建模式"]', "unverified_draft");
  const granularity = document.querySelector('[aria-label="生成粒度"]');
  assert.equal(granularity.value, "topic");
  assert.deepEqual([...granularity.options].map((option) => option.value), ["topic", "knowledge_points"]);
  await selectValue('[aria-label="生成粒度"]', "knowledge_points");
  await click(document.querySelector('.wiki-source-options input'));
  assert.equal(button("提交 Wiki 构建").disabled, true);
  await chooseBuildModelAndConsent();
  await fill('.wiki-build-form input[type="number"]', "3");
  await click(button("提交 Wiki 构建"));
  const post = requests.find((r) => r.path === "/wiki/builds");
  assert.deepEqual(post.body, { space_id: "space", source_resource_ids: [SOURCE], model_selection: { connection_id: "connection", model_id: "model" },
    max_pages: 3, consent: true, source_mode: "unverified_draft", granularity: "knowledge_points", compilation_type: "atomic_rule" });
  assert.match(document.body.textContent, /构建任务已提交/);
});
test("mode switches reset granularity and consent, clear sources and cannot carry draft choices into published builds", async () => {
  const draft = { ...source, active_version_id: null, latest_state: "DRAFT" };
  const published = { ...source, id: ID, name: "已发布来源", latest_state: "PUBLISHED" };
  respond = (r) => r.path === "/resources" ? { items: [draft, published], next_cursor: null }
    : r.path === "/wiki/builds" ? { status: 202, body: { id: "job", kind: "COMPILE", state: "QUEUED", stage: "", attempts: 0, error_code: null, result: null } }
    : defaultResponse(r);
  await mount(wiki.WikiBuildDialog, { close() {} });
  await selectValue('[aria-label="构建模式"]', "unverified_draft");
  await selectValue('[aria-label="生成粒度"]', "knowledge_points");
  await click(document.querySelector('.wiki-source-options input'));
  await chooseBuildModelAndConsent();
  await selectValue('[aria-label="生成粒度"]', "topic");
  assert.equal(document.querySelector('.wiki-build-consent input').checked, false);
  assert.equal(button("提交 Wiki 构建").disabled, true);
  await selectValue('[aria-label="生成粒度"]', "knowledge_points");
  await click(document.querySelector('.wiki-build-consent input'));
  await selectValue('[aria-label="构建模式"]', "published");
  assert.equal(document.querySelector('[aria-label="生成粒度"]'), null);
  assert.equal(document.querySelector('.wiki-build-consent input').checked, false);
  assert.equal(document.querySelectorAll('.wiki-source-options input:checked').length, 0);
  assert.equal(document.querySelector('.wiki-source-options input').disabled, true, "draft-only source must remain unavailable in formal mode");
  assert.equal(button("提交 Wiki 构建").disabled, true);
  // Even a synthetic form submit cannot bypass the request builder's checks.
  await act(async () => document.querySelector('.wiki-build-form').dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })));
  await settle();
  assert.ok(!requests.some((r) => r.path === "/wiki/builds"));
  await selectValue('[aria-label="构建模式"]', "unverified_draft");
  assert.equal(document.querySelector('[aria-label="生成粒度"]').value, "topic", "re-entering draft mode must not restore knowledge_points");
  await selectValue('[aria-label="构建模式"]', "published");
  await click(document.querySelectorAll('.wiki-source-options input')[1]);
  assert.equal(button("提交 Wiki 构建").disabled, true);
  await click(document.querySelector('.wiki-build-consent input'));
  await click(button("提交 Wiki 构建"));
  const post = requests.find((r) => r.path === "/wiki/builds");
  assert.deepEqual(post.body, { space_id: "space", source_resource_ids: [ID], model_selection: { connection_id: "connection", model_id: "model" }, max_pages: 6, consent: true, compilation_type: "topic" });
});
test("knowledge point build reports server source-edit permission rejection without a successful task", async () => {
  respond = (r) => r.path === "/resources" ? { items: [{ ...source, active_version_id: null, latest_state: "DRAFT" }], next_cursor: null }
    : r.path === "/wiki/builds" ? { status: 403, body: { code: "FORBIDDEN", message: "当前用户不可编辑该来源草稿" } }
    : defaultResponse(r);
  await mount(wiki.WikiBuildDialog, { close() {} });
  await selectValue('[aria-label="构建模式"]', "unverified_draft");
  await selectValue('[aria-label="生成粒度"]', "knowledge_points");
  await click(document.querySelector('.wiki-source-options input'));
  await chooseBuildModelAndConsent();
  await click(button("提交 Wiki 构建"));
  assert.equal(requests.find((r) => r.path === "/wiki/builds").body.granularity, "knowledge_points");
  assert.match(document.body.textContent, /当前用户不可编辑该来源草稿/);
  assert.equal(document.querySelector('.wiki-build-result'), null);
  assert.ok(!document.body.textContent.includes("构建任务已提交"));
});

for (const state of ["DRAFT", "IN_REVIEW"]) test(`immutable source mode distinguishes current ${state} without editable tags`, async () => {
  respond=request=>request.path==='/wiki/workspace' ? {...workspace,pages:[{...page,state,tags:[],source_mode:'unverified_draft'}]}
    : defaultResponse(request);
  await mount(wiki.KnowledgeWorkspace);
  const banner = document.querySelector('.wiki-source-mode-banner').textContent;
  assert.match(banner, /当前列表仍包含草稿\/待复核 Wiki/);
  assert.match(banner, /保留生成时来源提示，发布不代表独立专业复核；当前状态见条目\/管理员确认记录/);
  assert.ok(!banner.includes("已确认发布"));
});
for (const state of ["APPROVED", "ACTIVE", "PUBLISHED"]) test(`historical source mode in ${state} is not a current publication ban and adds no per-page requests`, async () => {
  const raw = { block_id: "original-warning", ordinal: 0, block_type: "paragraph",
    data: { text: "生成时来源提示：不能直接发布。", text_format: "plain" }, locator: {}, citations: [] };
  const original = structuredClone(raw);
  const returned = { ...workspace, pages: Array.from({ length: 8 }, (_, i) => ({ ...page, id: i ? `historical-${i}` : ID,
    state, tags: [], source_mode: "unverified_draft" })) };
  const frozen = structuredClone(returned);
  respond = (r) => r.path === "/wiki/workspace" ? returned
    : r.path === `/versions/${VERSION}` ? { ...defaultResponse(r), blocks: [raw] } : defaultResponse(r);
  await mount(wiki.KnowledgeWorkspace);
  const banner = document.querySelector('.wiki-source-mode-banner').textContent;
  assert.match(banner, /当前列表包含已确认发布、但保留待核验来源生成历史的 Wiki/);
  assert.match(banner, /发布不代表独立专业复核；当前状态见条目\/管理员确认记录/);
  assert.ok(!banner.includes("不能直接发布"));
  assert.ok(!banner.includes("仍包含草稿"));
  assert.match(document.querySelector('.wiki-reading-body').textContent, /生成时来源提示：不能直接发布。/);
  assert.deepEqual(raw, original);
  assert.deepEqual(returned, frozen);
  assert.deepEqual(requests.map((r) => r.path).sort(), ["/wiki/workspace", `/resources/${ID}`, `/versions/${VERSION}`, `/wiki/pages/${ID}/links`].sort(),
    "banner must reuse workspace metadata without fetching admin records or every page");
  assert.ok(requests.every((r) => r.method === "GET"));
});
test("source-mode banner distinguishes mixed states using the currently returned pages only", async () => {
  const published = { ...page, state: "APPROVED", tags: [], source_mode: "unverified_draft" };
  const draft = { ...page, id: "current-draft", state: "DRAFT", tags: [], source_mode: "unverified_draft" };
  const reviewing = { ...page, id: "current-review", state: "IN_REVIEW", tags: [], source_mode: "unverified_draft" };
  respond = (r) => r.path === "/wiki/workspace" ? { ...workspace,
    pages: r.params.get("status") ? [published] : [published, draft, reviewing], stats: { total_pages: 546 }, truncated: true,
  } : defaultResponse(r);
  await mount(wiki.KnowledgeWorkspace);
  const banner = () => document.querySelector('.wiki-source-mode-banner').textContent;
  assert.match(banner(), /当前列表包含仍处于草稿\/待复核的 Wiki，也包含已确认发布的 Wiki/);
  assert.ok(!banner().includes("不能直接发布"));
  await selectValue('[aria-label="筛选知识状态"]', "PUBLISHED");
  assert.match(banner(), /当前列表包含已确认发布、但保留待核验来源生成历史的 Wiki/);
  assert.ok(!banner().includes("草稿/待复核"), "do not infer pending pages outside the returned list");
  assert.ok(!requests.some((r) => /admin|publication/.test(r.path)));
});
test("legacy source tags keep a neutral history banner when current state is unknown", async () => {
  respond = (r) => r.path === "/wiki/workspace" ? { ...workspace, pages: [{ ...page, state: "UNKNOWN", tags: ["unverified-sources"] }] } : defaultResponse(r);
  await mount(wiki.KnowledgeWorkspace);
  const banner = document.querySelector('.wiki-source-mode-banner').textContent;
  assert.match(banner, /当前列表包含保留待核验来源生成历史的 Wiki/);
  assert.ok(!banner.includes("已确认发布"));
  assert.ok(!banner.includes("草稿/待复核"));
  assert.match(banner, /发布不代表独立专业复核/);
});
test("hash launches only supported local routes and UUID sources, never external URL content", () => {
  assert.deepEqual(wiki.wikiLaunch("#/knowledge"), { build: false });
  assert.deepEqual(wiki.wikiLaunch("#/knowledge?build=1"), { build: true });
  assert.deepEqual(wiki.wikiLaunch(`#/knowledge?build_source=${SOURCE}`), { build: true, sourceId: SOURCE });
  assert.deepEqual(wiki.wikiLaunch(`#/knowledge?focus=${SOURCE}&graph=1`), { build: false, graph: true, focusId: SOURCE });
  for (const hash of ["#/knowledge?build_source=https://evil.invalid", "#/knowledge?build_source=../secret", `#/knowledge?build_source=${SOURCE}&build_source=${ID}`])
    assert.ok(wiki.wikiLaunch(hash).error);
  assert.deepEqual(wiki.wikiLaunch(`https://evil.invalid/#/knowledge?build_source=${SOURCE}`), { build: false });
  assert.ok(wiki.wikiLaunch("#/knowledge?focus=https://evil.invalid&graph=1").error);
});
test("shared renderer keeps plain source text literal and renders wiki aliases as non-navigating spans", () => {
  const plain = "**字面量** [[估值规则|说明]] <script>";
  const html = wiki.richHtml(plain, "plain");
  assert.ok(html.includes("说明"));
  assert.ok(!html.includes("<strong>字面量</strong>"));
  assert.ok(!html.includes("<script>"));
  const rendered = wiki.richHtml("**强调** [[估值规则|说明]]", "markdown");
  assert.ok(rendered.includes("<strong>强调</strong>"));
  assert.match(rendered, /data-wiki-title="估值规则"/);
  assert.match(rendered, /role="link"/);
  assert.ok(!rendered.includes("#wiki/"));
});
test("code spans stay literal and malicious link titles cannot become executable hrefs", () => {
  const code = "`[[literal]]`\n\n```text\n[[also literal]]\n```";
  assert.ok(!wiki.richHtml(code, "markdown").includes("data-wiki-title"));
  assert.ok(!wiki.richHtml("\\[[escaped]]", "markdown").includes("data-wiki-title"));
  const html = wiki.richHtml('[[javascript:alert(1)|<img src=x>]]', "markdown");
  assert.ok(!/href=["']javascript:/i.test(html));
  assert.ok(!html.includes("<img"));
});
test("graph renders only real SVG nodes and accessible open buttons drill through", async () => {
  const activated = [];
  await mount(wiki.KnowledgeGraph, { data: { nodes: [node(ID), node(SOURCE, { kind: "document" })],
    edges: [edge("a", ID, SOURCE)], truncated: false, total_visible_nodes: 2 }, onOpenNode: (value) => activated.push(value.id) });
  assert.equal(document.querySelectorAll('svg.star-surface').length, 1);
  assert.equal(document.querySelectorAll('[data-star-node]').length, 2);
  assert.equal(document.querySelectorAll('[data-star-edge]').length, 1);
  assert.ok([...document.querySelectorAll('[data-star-node]')].every(item =>
    item.getAttribute('transform')?.includes('translate') && !item.getAttribute('transform').includes('NaN')));
  assert.ok([...document.querySelectorAll('.star-dot')].every(item => item.getAttribute('fill')?.startsWith('#')));
  const items = document.querySelectorAll('ul[aria-label="图谱节点"] .star-list-open');
  assert.equal(items.length, 2);
  await click(items[0]);
  assert.deepEqual(activated, [ID]);
  await click(items[1]);
  assert.deepEqual(activated, [ID, SOURCE]);
});
test("empty graph shows honest empty state and allocates no SVG renderer", async () => {
  await mount(wiki.KnowledgeGraph, { data: { nodes: [], edges: [], truncated: false, total_visible_nodes: 0 }, onOpenNode() {} });
  assert.match(document.body.textContent, /还没有可见节点/);
  assert.equal(document.querySelectorAll('svg.star-surface').length, 0);
});
test("graph truncation is visible rather than silently presenting a complete network", async () => {
  await mount(wiki.KnowledgeGraph, { data: { nodes: [node(ID)], edges: [], truncated: true, total_visible_nodes: 999 }, onOpenNode() {} });
  assert.match(document.body.textContent, /图谱数据未完整返回/);
  assert.match(document.body.textContent, /999/);
});
test("workspace uses wiki pages, source links and existing continuous reader; document rows are excluded", async () => {
  respond = (request) => request.path === "/wiki/workspace" ? { ...workspace, pages: [page, { ...page, id: SOURCE, kind: "document", name: "不应成为知识页的原件" }] } : defaultResponse(request);
  await mount(wiki.KnowledgeWorkspace);
  assert.ok(document.querySelector('.wiki-reading-body .document-workbench'));
  assert.match(document.querySelector('.wiki-page-index').textContent, /合成知识页/);
  assert.ok(!document.querySelector('.wiki-page-index').textContent.includes("不应成为知识页的原件"));
  assert.match(document.body.textContent, /未解析链接/);
  assert.ok(requests.some((request) => request.path === `/wiki/pages/${ID}/links`));
  assert.equal(requests.filter((request) => request.path === "/wiki/workspace").length, 1,
    "taxonomy must reuse the workspace response instead of loading the full wiki twice");
  assert.equal(document.querySelectorAll('svg.star-surface').length, 0, "graph must stay lazy on the reading view");
  assert.equal(document.querySelector('.wiki-source-mode-banner'), null, "ordinary pages must not acquire invented source history");
});
test("wiki list header distinguishes 200 loaded pages from the server total without loading the entire collection", async () => {
  respond = (r) => r.path === "/wiki/workspace" ? { ...workspace,
    pages: Array.from({ length: 200 }, (_, i) => ({ ...page, id: i ? `page-${i}` : ID })),
    stats: { total_pages: 274, matched_pages: 274 }, truncated: true,
  } : defaultResponse(r);
  await mount(wiki.KnowledgeWorkspace);
  const heading = document.querySelector('.wiki-index-heading');
  assert.equal(heading.querySelector('strong').textContent, "知识页");
  assert.equal(heading.querySelector('span').textContent, "当前 200 / 全库 274 篇 · 部分结果");
  assert.equal(document.querySelectorAll('.wiki-page-index > ul > li').length, 200);
  assert.equal(document.querySelector('.wiki-page-index > .wiki-truncated').textContent, "结果已截断，请细化分类、标签或搜索。");
  assert.equal(requests.filter((r) => r.path === "/wiki/workspace").length, 1);
  assert.ok(!requests.some((r) => r.path === "/wiki/graph" || r.params.has("cursor")));
});
test("wiki list filters show matched_pages from stats while titles and total_pages stay distinct", async () => {
  respond = (r) => r.path === "/wiki/workspace" ? { ...workspace, stats: { total_pages: 999, matched_pages: 123 } } : defaultResponse(r);
  await mount(wiki.KnowledgeWorkspace);
  const header = () => document.querySelector('.wiki-index-heading span').textContent;
  assert.equal(header(), "当前 1 / 全库 999 篇");
  const filters = [
    () => click(document.querySelector('.wiki-category-row button[title="运营/估值"]')),
    () => click(button("#估值")),
    () => selectValue('[aria-label="筛选知识资源类型"]', "knowledge"),
    () => selectValue('[aria-label="筛选知识状态"]', "IN_REVIEW"),
    async () => { await fill('[aria-label="搜索知识空间"]', "估值"); await debounceSearch(); },
  ];
  for (const [index, filter] of filters.entries()) {
    await filter();
    assert.equal(header(), "当前 1 / 全库 999 篇 · 筛选匹配 123 篇");
    assert.equal(document.querySelector('.wiki-index-heading strong').textContent, index === 4 ? "搜索结果" : "知识页");
    await click(button("清除筛选"));
    if (index === 4) await debounceSearch();
    assert.equal(header(), "当前 1 / 全库 999 篇");
  }
});
test("wiki list counts stay unknown when stats are absent or invalid and preserve explicit zero totals", async () => {
  let stats;
  respond = (r) => r.path === "/wiki/workspace" ? { ...workspace, pages: stats?.total_pages === 0 ? [] : [page], stats } : defaultResponse(r);
  await mount(wiki.KnowledgeWorkspace);
  const header = () => document.querySelector('.wiki-index-heading span').textContent;
  assert.equal(header(), "当前 1 / 全库 未知 篇");
  await selectValue('[aria-label="筛选知识状态"]', "DRAFT");
  assert.equal(header(), "当前 1 / 全库 未知 篇 · 筛选匹配 未知 篇");
  stats = { total_pages: 0, matched_pages: 0 };
  await selectValue('[aria-label="筛选知识状态"]', "IN_REVIEW");
  assert.equal(header(), "当前 0 / 全库 0 篇 · 筛选匹配 0 篇");
  stats = { total_pages: -1, matched_pages: 1.5 };
  await selectValue('[aria-label="筛选知识状态"]', "PUBLISHED");
  assert.equal(header(), "当前 1 / 全库 未知 篇 · 筛选匹配 未知 篇");
});
test("workspace service errors are visible and never converted to sample knowledge pages", async () => {
  respond = () => ({ status: 503, body: { code: "WIKI_UNAVAILABLE", message: "知识服务尚不可用" } });
  await mount(wiki.KnowledgeWorkspace);
  assert.match(document.body.textContent, /知识服务尚不可用/);
  assert.equal(document.querySelectorAll('.wiki-page-index li').length, 0);
});
test("source graph hash opens focused graph through the real graph endpoint shape", async () => {
  window.history.replaceState(null, "", `#/knowledge?focus=${SOURCE}&graph=1`);
  await mount(wiki.KnowledgeWorkspace);
  assert.ok(requests.some((request) => request.path === "/wiki/graph" && request.params.get("focus_id") === SOURCE
    && !request.params.has("limit")));
  assert.match(document.body.textContent, /局部关系图谱/);
  await click(button("返回全局图谱"));
  assert.ok(requests.some((request) => request.path === "/wiki/graph" && !request.params.has("focus_id")));
});
test("graph queries validate role, depth and text without requesting a quantity limit", () => {
  const text = "  债券 & q=新词/参数 + #条件?  ";
  const params = wiki.wikiGraphParameters({ spaceId: "space", search: text, role: "condition", depth: 2, focusId: ID });
  assert.equal(params.q, text.trim());
  assert.equal(params.node_role, "condition");
  assert.equal(Object.hasOwn(params, "limit"), false);
  assert.equal(params.focus_id, ID);
  assert.equal(wiki.wikiGraphParameters({ spaceId: "space" }).node_role, undefined);
  for (const role of ["all", "constructor", "__proto__", "RULE", "rule&limit=9999"]) {
    assert.throws(() => wiki.wikiGraphParameters({ spaceId: "space", role }), /节点角色无效/);
  }
  for (const depth of [-1, 4, 1.5, NaN]) assert.throws(() => wiki.wikiGraphParameters({ spaceId: "space", depth }));
  for (const search of ["词".repeat(301), "估\u0000值", "估\n值"]) assert.throws(() => wiki.wikiSearchQuery(search));
  assert.equal(wiki.wikiSearchQuery("词".repeat(300)).length, 300);
});
test("all eight graph roles issue server queries; default/reset omit role and local depth is preserved", async () => {
  const search = " 债券 & node_role=bad + #? ";
  await mount(wiki.GraphQuery, { focusId: ID, category: "运营/估值", search, onOpenNode() {} });
  const latest = () => requests.filter((r) => r.path === "/wiki/graph").at(-1);
  assert.equal(latest().params.has("node_role"), false);
  assert.equal(latest().params.get("q"), search.trim());
  assert.equal(latest().params.getAll("q").length, 1);
  await selectValue('[aria-label="局部图谱展开深度"]', "2");
  for (const role of Object.keys(wiki.wikiNodeRoleLabels)) {
    await selectValue('[aria-label="筛选图谱节点角色"]', role);
    assert.equal(latest().params.get("node_role"), role);
    assert.equal(latest().params.get("focus_id"), ID);
    assert.equal(latest().params.get("depth"), "2");
    assert.equal(latest().params.get("category"), "运营/估值");
    assert.equal(latest().params.has("limit"), false);
  }
  await selectValue('[aria-label="局部图谱展开深度"]', "3");
  await selectValue('[aria-label="筛选图谱节点角色"]', "");
  assert.equal(latest().params.get("depth"), "3");
  assert.equal(latest().params.has("node_role"), false);
});
test("invalid graph search is visible and sends no malformed request", async () => {
  await mount(wiki.GraphQuery, { search: "x".repeat(301), onOpenNode() {} });
  assert.match(document.body.textContent, /搜索词最多 300 字/);
  assert.equal(requests.length, 0);
  assert.equal(document.querySelector('.wiki-graph-coverage'), null);
});
test("graph coverage distinguishes drawn, authorized total and matches, including a truncated response", async () => {
  respond = (r) => r.path === "/wiki/graph" ? {
    nodes: Array.from({ length: 205 }, (_, i) => node(String(i))), edges: [],
    truncated: false, total_visible_nodes: 1234, matched_visible_nodes: 250,
  } : defaultResponse(r);
  await mount(wiki.GraphQuery, { onOpenNode() {} });
  assert.match(document.querySelector('.wiki-graph-coverage').textContent, /当前绘制 205\/1234 节点、0 条线/);
  assert.match(document.querySelector('.wiki-graph-scope').textContent, /筛选后 250 节点/);
  assert.match(document.querySelector('.wiki-graph-query > .wiki-truncated').textContent, /未显示不代表不存在/);
  assert.equal(document.querySelectorAll('[data-star-node]').length, 205);
  assert.match(document.body.textContent, /模型提出的语义关系待核验/);
  assert.equal(document.querySelectorAll('.wiki-graph-relation-help dt').length, 5);
});
test("a complete 591-node graph retains every node and all 1182 edges with no truncation warning", async () => {
  const nodes = Array.from({ length: 591 }, (_, i) => node(`complete-${i}`));
  const edges = Array.from({ length: 1182 }, (_, i) => edge(`complete-edge-${i}`, nodes[i % 591].id, nodes[(i % 591 + (i < 591 ? 1 : 17)) % 591].id));
  respond = () => ({ nodes, edges, truncated: false, total_visible_nodes: 591, matched_visible_nodes: 591,
    total_visible_edges: 1182, matched_visible_edges: 1182, semantic_relation_count: 0 });
  await mount(wiki.GraphQuery, { onOpenNode() {} });
  assert.equal(requests[0].params.has('limit'), false);
  assert.equal(document.querySelectorAll('[data-star-node]').length, 591);
  assert.equal(document.querySelectorAll('[data-star-edge]').length, 1182);
  assert.equal(document.querySelector('.wiki-graph-query > .wiki-truncated'), null);
  assert.match(document.querySelector('.wiki-graph-coverage').textContent, /当前绘制 591\/591 节点、1182 条线/);
  assert.ok(document.querySelector('[data-star-node="complete-590"]'));
  assert.doesNotMatch(document.body.textContent, /最多.*200|800 条关系/);
});

test("missing match count is unknown and a zero-match graph remains explicitly zero", async () => {
  let matched;
  respond = () => ({ nodes: [], edges: [], truncated: false, total_visible_nodes: 42, matched_visible_nodes: matched });
  await mount(wiki.GraphQuery, { onOpenNode() {} });
  assert.match(document.querySelector('.wiki-graph-coverage').textContent, /当前绘制 0\/42 节点、0 条线/);
  assert.match(document.querySelector('.wiki-graph-scope').textContent, /筛选后 未知 节点/);
  matched = 0;
  await selectValue('[aria-label="筛选图谱节点角色"]', "exception");
  assert.match(document.querySelector('.wiki-graph-coverage').textContent, /筛选后 0/);
  assert.equal(document.querySelectorAll('[data-star-node]').length, 0);
});
test("match totals expose omitted nodes even if the backend misses its truncation flag", async () => {
  respond = () => ({ nodes: [node(ID)], edges: [], truncated: false, total_visible_nodes: 999, matched_visible_nodes: 50 });
  await mount(wiki.GraphQuery, { onOpenNode() {} });
  assert.match(document.querySelector('.wiki-graph-query > .wiki-truncated').textContent, /图谱数据尚未完整/);
  assert.match(document.querySelector('.wiki-graph-coverage').textContent, /当前绘制 1\/999 节点、0 条线/);
  assert.match(document.querySelector('.wiki-graph-scope').textContent, /筛选后 50 节点/);
});
test("graph totals retain all returned edges and expose genuinely incomplete server data", async () => {
  const data = { nodes: [node(ID), node(SOURCE)], edges: Array.from({ length: 810 }, (_, i) => edge(`line-${i}`, ID, SOURCE)),
    truncated: false, total_visible_nodes: 999, matched_visible_nodes: 2, total_visible_edges: 9000,
    matched_visible_edges: 4000, semantic_relation_count: 2400, node_role_counts: { method: 400, topic_or_source: 599 } };
  const original = structuredClone(data);
  respond = () => data;
  await mount(wiki.GraphQuery, { onOpenNode() {} });
  assert.match(document.querySelector('.wiki-graph-coverage').textContent,
    /当前绘制 2\/999 节点、810 条线；全空间 9000 条关系、2400 条语义草稿/);
  assert.match(document.querySelector('.wiki-graph-scope').textContent, /筛选后 2 节点、4000 条关系/);
  assert.match(document.querySelector('.wiki-graph-query > .wiki-truncated').textContent, /不再设固定显示上限/);
  assert.equal(wiki.boundedGraph(data).edges.length, 810);
  assert.deepEqual(data, original, "bounded rendering must not change the authoritative counters");
  assert.equal(requests[0].params.has("limit"), false);
});
test("filtered local graphs keep global relation totals separate from matched and rendered counts", async () => {
  respond = (r) => ({ nodes: [node(ID, { state: "DRAFT" }), node(SOURCE, { state: "IN_REVIEW" })],
    edges: [{ ...edge("document-link", ID, SOURCE), type: "CITES", origin: "citation", verification_status: "PROPOSED", citation_precision: "DOCUMENT" }],
    truncated: false, total_visible_nodes: 443, matched_visible_nodes: 2, total_visible_edges: 1200, semantic_relation_count: 700,
    matched_visible_edges: r.params.get("node_role") ? 1 : 30 });
  await mount(wiki.GraphQuery, { focusId: ID, onOpenNode() {} });
  const text = () => document.querySelector('.wiki-graph-coverage').textContent;
  assert.match(text(), /当前绘制 2\/443 节点、1 条线；全空间 1200 条关系、700 条语义草稿/);
  assert.match(document.querySelector('.wiki-graph-scope').textContent, /筛选后 2 节点、30 条关系/);
  assert.ok(document.querySelector('.wiki-graph-query > .wiki-truncated'), "omitted edges must be apparent even without a backend flag");
  await selectValue('[aria-label="筛选图谱节点角色"]', "method");
  assert.match(text(), /全空间 1200 条关系、700 条语义草稿/);
  assert.match(document.querySelector('.wiki-graph-scope').textContent, /筛选后 2 节点、1 条关系/);
  assert.equal(document.querySelector('.wiki-graph-query > .wiki-truncated'), null);
  assert.equal(requests.at(-1).params.get("focus_id"), ID);
  assert.equal(requests.at(-1).params.get("node_role"), "method");
  assert.equal(requests.at(-1).params.has("limit"), false);
});
test("missing or invalid relationship totals stay unknown and explicit zeros remain zero", async () => {
  let value;
  respond = () => ({ nodes: [], edges: [], truncated: false, total_visible_nodes: 0, matched_visible_nodes: 0,
    total_visible_edges: value, matched_visible_edges: value, semantic_relation_count: value });
  await mount(wiki.GraphQuery, { onOpenNode() {} });
  assert.match(document.querySelector('.wiki-graph-coverage').textContent, /全空间 未知 条关系、未知 条语义草稿/);
  for (const [index, invalid] of [null, -1, 1.5, "8"].entries()) {
    value = invalid;
    await selectValue('[aria-label="筛选图谱节点角色"]', index % 2 ? "method" : "rule");
    assert.match(document.querySelector('.wiki-graph-coverage').textContent, /全空间 未知 条关系、未知 条语义草稿/);
    assert.match(document.querySelector('.wiki-graph-scope').textContent, /筛选后 0 节点、未知 条关系/);
  }
  value = 0;
  await selectValue('[aria-label="筛选图谱节点角色"]', "");
  assert.match(document.querySelector('.wiki-graph-coverage').textContent, /当前绘制 0\/0 节点、0 条线；全空间 0 条关系、0 条语义草稿/);
  assert.match(document.querySelector('.wiki-graph-scope').textContent, /筛选后 0 节点、0 条关系/);
});
test("locator searches the complete authorized workspace and opens a node absent from the global graph", async () => {
  const omitted = "00000000-0000-4000-8000-000000000999";
  respond = (r) => r.path === "/wiki/workspace" && !r.params.has("hydrate")
    ? { ...workspace, pages: [{ ...page, id: omitted, name: "截断以外的估值参数" }], next_cursor: "more", truncated: true }
    : r.path === "/wiki/graph" ? { nodes: [node(r.params.get("focus_id") || ID)], edges: [], truncated: !r.params.has("focus_id"),
      total_visible_nodes: 900, matched_visible_nodes: r.params.has("focus_id") ? 1 : 900 } : defaultResponse(r);
  await mount(wiki.KnowledgeWorkspace);
  await click(button("全局图谱"));
  assert.equal(requests.filter((r) => r.path === "/wiki/workspace").length, 1, "mount must not download another workspace for the locator");
  await click(document.querySelector('.wiki-category-row button[title="运营/估值"]'));
  await selectValue('[aria-label="筛选图谱节点角色"]', "parameter");
  await fill('[aria-label="搜索知识空间"]', "旧搜索");
  await debounceSearch();
  await fill('[aria-label="定位知识节点"]', " 估值 & 条件+规则 ");
  await click(button("查找节点"));
  const searches = requests.filter((r) => r.path === "/wiki/workspace" && !r.params.has("hydrate"));
  assert.equal(searches.length, 1);
  assert.equal(searches[0].params.get("q"), "估值 & 条件+规则");
  assert.equal(searches[0].params.get("category"), "运营/估值");
  for (const key of ["node_role", "tag", "status", "kind", "cursor"]) assert.equal(searches[0].params.has(key), false);
  assert.equal(searches[0].params.has("limit"), false);
  assert.match(document.querySelector('.wiki-graph-locator-results').textContent, /结果尚未完整/);
  assert.ok(!document.querySelector(`[data-star-node="${omitted}"]`));
  await click(document.querySelector('[aria-label="查看「截断以外的估值参数」的局部图谱"]'));
  const focused = requests.filter((r) => r.path === "/wiki/graph").at(-1);
  assert.equal(focused.params.get("focus_id"), omitted);
  assert.equal(focused.params.has("limit"), false);
  for (const key of ["q", "category", "node_role"]) assert.equal(focused.params.has(key), false);
  assert.ok(!requests.some((r) => r.path === `/resources/${omitted}`), "locating should open a graph, not fetch a body");
  await selectValue('[aria-label="局部图谱展开深度"]', "2");
  assert.equal(requests.filter((r) => r.path === "/wiki/graph").at(-1).params.get("depth"), "2");
  await click(button("返回全局图谱"));
  assert.equal(requests.filter((r) => r.path === "/wiki/graph").at(-1).params.has("focus_id"), false);
  assert.ok(requests.every((r) => r.method === "GET"));
});
test("locator renders every returned match without a 20-item cap", async () => {
  respond = (r) => r.path === "/wiki/workspace" ? { ...workspace,
    pages: Array.from({ length: 25 }, (_, i) => ({ ...page, id: `item-${i}`, name: `可见知识 ${i}` })), next_cursor: "page-2" } : defaultResponse(r);
  await mount(wiki.GraphQuery, { onOpenNode() {}, onFocusChange() {} });
  assert.equal(button("查找节点").disabled, true);
  await fill('[aria-label="定位知识节点"]', "可见知识");
  await click(button("查找节点"));
  assert.equal(document.querySelectorAll('.wiki-graph-locator-results li').length, 25);
  assert.equal(requests.filter((r) => r.path === "/wiki/workspace").length, 1);
  await fill('[aria-label="定位知识节点"]', "");
  assert.equal(document.querySelector('.wiki-graph-locator-results'), null);
});
test("editing locator input aborts pending results and suppresses late content", async () => {
  let resolveOld;
  respond = (r) => r.path === "/wiki/workspace" ? new Promise((done) => { resolveOld = done; }) : defaultResponse(r);
  await mount(wiki.GraphQuery, { onOpenNode() {}, onFocusChange() {} });
  await fill('[aria-label="定位知识节点"]', "旧词");
  await click(button("查找节点"));
  const old = requests.find((r) => r.path === "/wiki/workspace");
  await fill('[aria-label="定位知识节点"]', "新词");
  assert.equal(old.signal.aborted, true);
  await act(async () => resolveOld({ ...workspace, pages: [{ ...page, name: "过期定位内容" }] }));
  await settle();
  assert.equal(document.querySelector('.wiki-graph-locator-results'), null);
  assert.ok(!document.body.textContent.includes("过期定位内容"));
});
test("locator permission failure shows an error and no results", async () => {
  respond = (r) => r.path === "/wiki/workspace" ? { status: 403, body: { code: "FORBIDDEN", message: "当前无访问权限" } } : defaultResponse(r);
  await mount(wiki.GraphQuery, { onOpenNode() {}, onFocusChange() {} });
  await fill('[aria-label="定位知识节点"]', "受限知识");
  await click(button("查找节点"));
  assert.match(document.querySelector('.wiki-graph-locator-results').textContent, /当前无访问权限/);
  assert.equal(document.querySelectorAll('.wiki-graph-locator-results li').length, 0);
});
test("existing node entry drills into a local graph without changing renderer open behavior", async () => {
  const focused = [], openedNodes = [];
  await mount(wiki.GraphQuery, { onFocusChange: (id) => focused.push(id), onOpenNode: (n) => openedNodes.push(n.id) });
  await selectValue('[aria-label="选择已绘制节点进入局部图"]', SOURCE);
  await click(button("查看节点局部图"));
  assert.deepEqual(focused, [SOURCE]);
  assert.deepEqual(openedNodes, []);
  await click(document.querySelector('.star-list-open'));
  assert.equal(openedNodes.length, 1);
});
test("list tag, kind and status filters survive graph role filtering and do not constrain the locator", async () => {
  await mount(wiki.KnowledgeWorkspace);
  await click(button("#估值"));
  await selectValue('[aria-label="筛选知识资源类型"]', "knowledge");
  await selectValue('[aria-label="筛选知识状态"]', "DRAFT");
  await click(button("全局图谱"));
  await selectValue('[aria-label="筛选图谱节点角色"]', "rule");
  const graph = requests.filter((r) => r.path === "/wiki/graph").at(-1);
  for (const key of ["tag", "kind", "status"]) assert.equal(graph.params.has(key), false);
  await fill('[aria-label="定位知识节点"]', "规则");
  await click(button("查找节点"));
  const locator = requests.filter((r) => r.path === "/wiki/workspace").at(-1);
  for (const key of ["tag", "kind", "status", "node_role"]) assert.equal(locator.params.has(key), false);
  await click(document.querySelector('[aria-label="知识空间视图"] button'));
  assert.equal(document.querySelector('[aria-label="筛选知识状态"]').value, "DRAFT");
  assert.equal(document.querySelector('[aria-label="筛选知识资源类型"]').value, "knowledge");
  assert.equal(button("#估值").getAttribute("aria-pressed"), "true");
  const list = requests.filter((r) => r.path === "/wiki/workspace" && r.params.get("hydrate") === "true").at(-1);
  assert.equal(list.params.get("tag"), "估值");
  assert.equal(list.params.get("status"), "DRAFT");
  assert.equal(list.params.get("kind"), "knowledge");
});
test("semantic relation metadata remains proposed even when linked content is published", async () => {
  const link = { id: SOURCE, name: "有关联的来源", kind: "document", version_id: VERSION, relation_type: "EXPLAINS",
    status: "PUBLISHED", verification_status: "PROPOSED", evidence_count: 2 };
  respond = (r) => r.path === `/wiki/pages/${ID}/links` ? {
    outgoing: [link], incoming: [{ ...link, evidence_count: 0 }], sources: [link],
    unresolved: [{ title: "待解析", verification_status: "PROPOSED", evidence_count: 1 }],
  } : defaultResponse(r);
  await mount(wiki.KnowledgeWorkspace);
  assert.match(document.querySelector('.wiki-connections').textContent, /解释 · 待核验 · 关联内容：已发布 · 原文定位 2 处/);
  assert.match(document.querySelector('.wiki-connections').textContent, /原文定位 0 处/);
  assert.match(document.querySelector('.wiki-unresolved-link').textContent, /重新解析 · 待核验.*原文定位 1 处/);
  assert.equal(wiki.wikiRelationTitle("EXPLAINS"), "解释", "legacy relation data does not acquire an invented verification status");
  for (const count of [-1, NaN, 1.5, undefined]) assert.equal(wiki.wikiEvidenceText(count), "");
  const data = { nodes: [node(ID, { node_role: "parameter" }), node(SOURCE)], edges: [{ ...edge("semantic", ID, SOURCE),
    type: "REQUIRES", origin: "semantic", state: "DRAFT", verification_status: "PROPOSED", evidence_count: 2 }],
    truncated: false, total_visible_nodes: 2 };
  assert.deepEqual(wiki.boundedGraph(data), data, "renderer bounds preserve semantic metadata without promoting it");
});
test("document precision marks bibliography as unverified and cannot claim original-text anchors", () => {
  for (const verification of [undefined, "PROPOSED", "VERIFIED"])
    assert.equal(wiki.wikiRelationTitle("CITES", verification, "DOCUMENT"), "书目关联 · 待核验（未逐段定位）");
  for (const count of [undefined, 0, 1, 999]) assert.equal(wiki.wikiEvidenceText(count, "DOCUMENT"), "");
  assert.equal(wiki.wikiRelationTitle("CITES"), "引用");
  assert.equal(wiki.wikiRelationTitle("CITES", "PROPOSED", null), "引用 · 待核验");
  assert.equal(wiki.wikiEvidenceText(2), "原文定位 2 处");
});
test("source and link entries distinguish document bibliography from anchored citations without changing navigation", async () => {
  const bibliography = { id: SOURCE, name: "本地笔记所列书目", kind: "document", version_id: VERSION, relation_type: "CITES",
    status: "PUBLISHED", citation_precision: "DOCUMENT", verification_status: "PROPOSED", evidence_count: 999,
    explanation: "保留原笔记列出的来源，尚未核对具体段落。" };
  const linked = { sources: [bibliography, { ...bibliography, name: "已有定位的来源", citation_precision: null,
    verification_status: undefined, evidence_count: 2, explanation: null }], outgoing: [bibliography],
    incoming: [{ ...bibliography, verification_status: undefined }], unresolved: [] };
  const original = structuredClone(linked);
  respond = (r) => r.path === `/wiki/pages/${ID}/links` ? linked : defaultResponse(r);
  await mount(wiki.KnowledgeWorkspace);
  const connections = document.querySelector('.wiki-connections');
  const entries = [...connections.querySelectorAll('li button')];
  assert.equal(entries.length, 4);
  for (const row of [entries[0], entries[2], entries[3]]) {
    assert.equal(row.querySelector('.wiki-proposed-badge').textContent, "书目关联 · 待核验（未逐段定位）");
    assert.ok(!row.textContent.includes("原文定位"));
    assert.ok(!row.textContent.includes("引用"));
    assert.match(row.textContent, /关联内容：已发布/);
    assert.equal(row.querySelector('.wiki-relation-explanation').textContent, `关系解释：${bibliography.explanation}`);
  }
  assert.match(entries[1].textContent, /引用 · 关联内容：已发布 · 原文定位 2 处/);
  assert.equal(entries[1].querySelector('.wiki-proposed-badge'), null);
  assert.match(connections.querySelector('.wiki-relation-draft-note').textContent, /书目关联未逐段定位，不构成已定位的事实引用/);
  await click(entries[0]);
  assert.deepEqual(opened, [[VERSION]]);
  assert.deepEqual(linked, original);
  assert.ok(requests.every((r) => r.method === "GET"));
});
test("graph bibliography retains citation origin and displays document precision without inventing semantic or anchor evidence", async () => {
  const data = { nodes: [node(ID, { label: "原笔记" }), node(SOURCE, { label: "关联书目", kind: "document" })],
    edges: [
      { ...edge("bibliography", ID, SOURCE), type: "CITES", origin: "citation", state: "DRAFT", verification_status: "PROPOSED",
        citation_precision: "DOCUMENT", evidence_count: 123, explanation: "原笔记仅记载了书目名称。" },
      { ...edge("bibliography-missing-status", ID, SOURCE), type: "CITES", origin: "citation", state: "DRAFT", citation_precision: "DOCUMENT" },
      { ...edge("anchored", ID, SOURCE), type: "CITES", origin: "citation", state: "ACTIVE", evidence_count: 1 },
    ], truncated: false, total_visible_nodes: 2, matched_visible_nodes: 2 };
  const original = structuredClone(data);
  respond = () => data;
  await mount(wiki.GraphQuery, { onOpenNode() {} });
  const details = document.querySelector('.wiki-semantic-relations');
  assert.match(details.querySelector('summary').textContent, /关系草稿（含书目关联）/);
  assert.ok(!details.textContent.includes("模型提出"));
  assert.match(details.textContent, /不构成已定位的事实引用/);
  const rows = [...details.querySelectorAll('li')];
  assert.equal(rows.length, 2);
  for (const row of rows) {
    assert.equal(row.querySelector('.wiki-proposed-badge').textContent, "书目关联 · 待核验（未逐段定位）");
    assert.ok(!row.textContent.includes("原文定位"));
  }
  assert.equal(rows[0].querySelector('strong').textContent, "原笔记 → 关联书目");
  assert.match(rows[0].querySelector('.wiki-relation-explanation').textContent, /原笔记仅记载了书目名称/);
  assert.deepEqual(wiki.boundedGraph(data), original);
  assert.deepEqual(data, original);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].params.has("limit"), false);
});
test("graph semantic entries display proposed badges and optional plain-text explanations, including nullable roles", async () => {
  const data = { nodes: [node(ID, { label: "估值方法", node_role: null }), node(SOURCE, { label: "价格参数", node_role: "parameter" })],
    edges: [
      { ...edge("semantic-a", ID, SOURCE), type: "DEPENDS_ON", origin: "semantic", state: "DRAFT", verification_status: "PROPOSED",
        evidence_count: 2, explanation: "  <b>方法</b>需要该价格参数。  " },
      { ...edge("semantic-b", SOURCE, ID), type: "EXPLAINS", origin: "semantic", state: "DRAFT", evidence_count: 0, explanation: null },
      edge("legacy", ID, SOURCE),
    ], truncated: false, total_visible_nodes: 2, matched_visible_nodes: 2 };
  const original = structuredClone(data);
  respond = () => data;
  await mount(wiki.GraphQuery, { onOpenNode() {} });
  const details = document.querySelector('.wiki-semantic-relations');
  assert.ok(details);
  assert.equal(details.open, false, "explanation list should not expand the workbench by default");
  assert.match(details.textContent, /不作为正式依赖或发布关系/);
  const rows = [...details.querySelectorAll('li')];
  assert.equal(rows.length, 2, "ordinary links remain outside the semantic draft list");
  assert.equal(rows[0].querySelector('strong').textContent, "估值方法 → 价格参数");
  assert.equal(rows[0].querySelector('.wiki-proposed-badge').textContent, "依赖 · 待核验");
  assert.equal(rows[0].querySelector('.wiki-relation-explanation').textContent, "关系解释：<b>方法</b>需要该价格参数。");
  assert.equal(rows[0].querySelector('b'), null, "explanations must not be rendered as HTML");
  assert.equal(rows[1].querySelector('.wiki-proposed-badge').textContent, "解释 · 待核验", "semantic origin alone still means draft projection");
  assert.match(rows[1].textContent, /原文定位 0 处/);
  assert.equal(rows[1].querySelector('.wiki-relation-explanation'), null);
  assert.deepEqual(data, original);
  assert.ok(requests.every((r) => r.method === "GET"));
});
test("all semantic explanations are present without pagination or extra requests", async () => {
  respond = () => ({ nodes: [node(ID), node(SOURCE)], edges: Array.from({ length: 25 }, (_, i) => ({
    ...edge(`semantic-${i}`, ID, SOURCE), origin: "semantic", state: "DRAFT", verification_status: "PROPOSED", explanation: `关系解释 ${i}`,
  })), truncated: false, total_visible_nodes: 2 });
  await mount(wiki.GraphQuery, { onOpenNode() {} });
  const list = document.querySelector('[aria-label="待核验语义关系条目"]');
  assert.equal(list.children.length, 25);
  assert.match(list.textContent, /关系解释 24/);
  assert.equal(button("下一组关系"), undefined);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].params.has("limit"), false);
});

test("hydrated workspace displays every page, manuscript and source links with one request", async () => {
  const fullPages = Array.from({length: 240}, (_, i) => ({...page, id: i === 0 ? ID : `full-${i}`, name: `完整知识 ${i}`}));
  const version = defaultResponse({path: `/versions/${VERSION}`});
  const links = {...defaultResponse({path: `/wiki/pages/${ID}/links`}), truncated: false};
  const data = {...workspace, pages: fullPages, stats: {total_pages: 240, matched_pages: 240},
    reader: {resource, version, links}, graph: defaultResponse({path: '/wiki/graph'})};
  respond = (request) => {
    assert.equal(request.path, '/wiki/workspace');
    assert.equal(request.params.get('hydrate'), 'true');
    return data;
  };
  await mount(wiki.KnowledgeWorkspace);
  assert.equal(document.querySelectorAll('.wiki-page-index li').length, 240);
  assert.match(document.querySelector('.wiki-reader').textContent, /链接与依据/);
  assert.equal(requests.length, 1);
  await click(button('全局图谱'));
  assert.equal(document.querySelectorAll('[data-star-node]').length, 2);
  assert.equal(requests.length, 1, 'initial graph must reuse the same authorized snapshot');
});

test("StrictMode hydration never falls back to duplicate manuscript and graph reads", async () => {
  const data = {...workspace, reader: {resource, version: defaultResponse({path: `/versions/${VERSION}`}),
    links: defaultResponse({path: `/wiki/pages/${ID}/links`})}, graph: defaultResponse({path: '/wiki/graph'})};
  respond = (request) => {
    assert.equal(request.path, '/wiki/workspace');
    return data;
  };
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(React.StrictMode, null,
    React.createElement(wiki.AppContext.Provider, {value: app}, React.createElement(wiki.KnowledgeWorkspace)))));
  await settle();
  assert.match(document.querySelector('.wiki-reader').textContent, /链接与依据/);
  assert.ok(requests.every(r => r.path === '/wiki/workspace'));
  assert.equal(requests.length, 1, 'the StrictMode abandoned effect must not send a full-library request');
});

test("all cached manuscripts switch locally and route return restores selection without requests", async () => {
  const secondId='00000000-0000-4000-8000-000000000088', secondVersion='00000000-0000-4000-8000-000000000089';
  const firstReader={resource,version:defaultResponse({path:`/versions/${VERSION}`}),links:defaultResponse({path:`/wiki/pages/${ID}/links`})};
  const secondReader={resource:{...resource,id:secondId,name:'第二个完整知识点'},
    version:{...firstReader.version,id:secondVersion,resource_id:secondId,title:'第二个完整知识点'},links:firstReader.links};
  const data={...workspace,pages:[page,{...page,id:secondId,name:'第二个完整知识点',version_id:secondVersion}],
    readers:{[ID]:firstReader,[secondId]:secondReader},reader:firstReader,graph:defaultResponse({path:'/wiki/graph'})};
  respond=r=>{assert.equal(r.path,'/wiki/workspace');return data;};
  await mount(wiki.KnowledgeWorkspace);
  await click(document.querySelectorAll('.wiki-page-index li > button')[1]);
  assert.match(document.querySelector('.wiki-reader-path').textContent,/第二个完整知识点/);
  assert.equal(document.querySelector('.wiki-reader .loading'),null);
  assert.equal(requests.length,1);
  await act(async()=>root.unmount());root=undefined;
  await mount(wiki.KnowledgeWorkspace);
  assert.match(document.querySelector('.wiki-reader-path').textContent,/第二个完整知识点/);
  assert.equal(requests.length,1,'route remount must reuse the complete authorized snapshot');
});
test("outgoing and incoming entries show proposed explanations without empty placeholders or HTML execution", async () => {
  const link = { id: SOURCE, name: "价格规则来源", kind: "document", version_id: VERSION, relation_type: "REQUIRES",
    status: "PUBLISHED", verification_status: "PROPOSED", evidence_count: 3 };
  respond = (r) => r.path === "/wiki/workspace" ? { ...workspace, pages: [{ ...page, node_role: null }] }
    : r.path === `/wiki/pages/${ID}/links` ? {
      outgoing: [{ ...link, explanation: '  <img src=x onerror="alert(1)">参数决定计算结果  ' }, { ...link, explanation: "  \n  " }],
      incoming: [{ ...link, explanation: "仅在条件满足时适用。" }, { ...link, explanation: null }],
      sources: [{ ...link, verification_status: undefined }], unresolved: [],
    } : defaultResponse(r);
  await mount(wiki.KnowledgeWorkspace);
  const connections = document.querySelector('.wiki-connections');
  const explanations = [...connections.querySelectorAll('.wiki-relation-explanation')];
  assert.equal(explanations.length, 2, "missing, null and blank explanations have no placeholder");
  assert.equal(explanations[0].textContent, '关系解释：<img src=x onerror="alert(1)">参数决定计算结果');
  assert.equal(explanations[1].textContent, "关系解释：仅在条件满足时适用。");
  assert.equal(connections.querySelectorAll('img, script').length, 0);
  assert.equal(connections.querySelectorAll('.wiki-proposed-badge').length, 4);
  assert.match(connections.textContent, /草稿投影，不作为正式依赖或发布关系/);
  assert.match(connections.textContent, /要求 · 待核验 · 关联内容：已发布/);
  await click(explanations[0].closest('button'));
  assert.deepEqual(opened, [[VERSION]], "source navigation stays on the established authorized version action");
  assert.ok(requests.every((r) => r.method === "GET"));
});
test("source build hash opens dialog, preselects and reauthorizes a source without submitting a model job", async () => {
  window.history.replaceState(null, "", `#/knowledge?build_source=${SOURCE}`);
  await mount(wiki.KnowledgeWorkspace);
  assert.ok(document.querySelector('dialog[open]'));
  assert.ok(document.querySelector('.wiki-source-options input[type="checkbox"]').checked);
  assert.ok(requests.some((request) => request.path === `/resources/${SOURCE}`));
  assert.ok(!requests.some((request) => request.method !== "GET"));
  assert.equal(button("提交 Wiki 构建").disabled, true, "selection still needs a model and explicit transfer consent");
});

async function chooseBuildModelAndConsent() {
  await click(document.querySelector('.model-picker-trigger'));
  await click(document.querySelector('[role="option"]:not(:disabled)'));
  await click(document.querySelector('.wiki-build-consent input'));
  assert.equal(button("提交 Wiki 构建").disabled, false);
}
test("building submits the exact model/source/consent tuple and reports queued rather than model success", async () => {
  respond = (request) => request.path === "/wiki/builds" ? { status: 202, body: { id: "job", kind: "COMPILE", state: "QUEUED",
    stage: "", attempts: 0, error_code: null, result: null } } : defaultResponse(request);
  await mount(wiki.WikiBuildDialog, { close() {}, initialSourceId: SOURCE });
  await chooseBuildModelAndConsent();
  await click(button("提交 Wiki 构建"));
  const post = requests.find((request) => request.path === "/wiki/builds");
  assert.equal(post.method, "POST");
  assert.deepEqual(post.body, { space_id: "space", source_resource_ids: [SOURCE], model_selection: { connection_id: "connection", model_id: "model" }, max_pages: 6, consent: true, compilation_type: "topic" });
  assert.match(document.body.textContent, /构建任务已提交/);
  assert.ok(!document.body.textContent.includes("Wiki 草稿构建完成"));
});
test("missing model/build service returns an error and cannot display successful construction", async () => {
  respond = (request) => request.path === "/wiki/builds" ? { status: 503, body: { code: "WIKI_PROVIDER_UNAVAILABLE", message: "Wiki模型配置或构建服务尚不可用" } } : defaultResponse(request);
  await mount(wiki.WikiBuildDialog, { close() {}, initialSourceId: SOURCE });
  await chooseBuildModelAndConsent();
  await click(button("提交 Wiki 构建"));
  assert.match(document.body.textContent, /构建服务尚不可用/);
  assert.equal(document.querySelector('.wiki-build-result'), null);
});
test("source hash reauthorization failure prevents submission but allows explicit source removal", async () => {
  respond = (request) => request.path === `/resources/${SOURCE}` ? { status: 404, body: { code: "NOT_FOUND", message: "对象不存在或不可访问" } } : defaultResponse(request);
  await mount(wiki.WikiBuildDialog, { close() {}, initialSourceId: SOURCE });
  await click(document.querySelector('.model-picker-trigger'));
  await click(document.querySelector('[role="option"]:not(:disabled)'));
  await click(document.querySelector('.wiki-build-consent input'));
  assert.equal(button("提交 Wiki 构建").disabled, true);
  assert.match(document.body.textContent, /对象不存在或不可访问/);
  assert.ok(!requests.some((request) => request.method === "POST"));
  await click(document.querySelector('.wiki-selected-sources button'));
  assert.equal(document.querySelector('.wiki-selected-sources'), null);
  assert.ok(!document.body.textContent.includes("对象不存在或不可访问"));
});

test("category path normalization matches the backend hierarchy contract", () => {
  assert.equal(wiki.categoryPath(" 运营 / 估值 / 债券 "), "运营/估值/债券");
  assert.equal(wiki.categoryPath("ＡＢＣ/估值"), "ABC/估值");
  assert.equal(wiki.categoryPath("分".repeat(200)), "分".repeat(200));
  for (const value of ["", "a//b", "a/../b", "/a", "a/", "<a>", "a\\b", "a\u0000b", "x".repeat(201), Array(9).fill("a").join("/")])
    assert.throws(() => wiki.categoryPath(value));
});
test("category rename and delete bind directory revision and block known nonempty branches", () => {
  assert.deepEqual(wiki.categoryWrite("create", "space", "运营/新分类", "", taxonomy),
    { method: "POST", body: { space_id: "space", path: "运营/新分类" } });
  assert.deepEqual(wiki.categoryWrite("rename", "space", "运营/估值", "运营/估值核对", taxonomy),
    { method: "PATCH", body: { space_id: "space", path: "运营/估值", new_path: "运营/估值核对" }, revision: 7 });
  assert.throws(() => wiki.categoryWrite("rename", "space", "运营/估值", "运营/估值/子目录", taxonomy));
  assert.throws(() => wiki.categoryWrite("delete", "space", "运营/估值", "", taxonomy));
  const empty = { ...taxonomy, categories: [{ path: "空分类", name: "空分类", count: 0 }] };
  assert.deepEqual(wiki.categoryWrite("delete", "space", "空分类", "", empty),
    { method: "DELETE", body: { space_id: "space", path: "空分类" }, revision: 7 });
  assert.ok(wiki.categoryDeletionReason({ ...empty, categories: [...empty.categories, { path: "空分类/子类", name: "子类", count: 0 }] }, "空分类"));
});
test("category creation writes the authorized space and selected hierarchical path", async () => {
  const saved = [];
  respond = (request) => request.path === "/wiki/categories" ? { status: 201, body: { ...taxonomy, revision: 8,
    categories: [...taxonomy.categories, { path: request.body.path, name: "债券", count: 0 }] } } : defaultResponse(request);
  await mount(wiki.WikiCategoryDialog, { operation: "create", path: "运营/估值", close() {}, saved(...values) { saved.push(values); } });
  assert.equal(document.querySelector('input[aria-label="新分类路径"]').value, "运营/估值/");
  await fill('input[aria-label="新分类路径"]', "运营/估值/债券");
  await click(button("保存分类"));
  const write = requests.find((request) => request.path === "/wiki/categories");
  assert.equal(write.method, "POST");
  assert.deepEqual(write.body, { space_id: "space", path: "运营/估值/债券" });
  assert.equal(saved.length, 1);
  assert.equal(saved[0][3], "运营/估值/债券");
});
test("category rename sends If-Match and never merges silently on a conflict", async () => {
  const saved = [];
  respond = (request) => request.path === "/wiki/categories" ? { status: 409, body: { code: "CATEGORY_EXISTS", message: "目标分类已存在" } } : defaultResponse(request);
  await mount(wiki.WikiCategoryDialog, { operation: "rename", path: "运营/估值", close() {}, saved(...values) { saved.push(values); } });
  await fill('input[aria-label="重命名后的分类路径"]', "运营/估值核对");
  await click(button("保存分类"));
  const write = requests.find((request) => request.path === "/wiki/categories");
  assert.equal(write.method, "PATCH");
  assert.equal(write.headers.get("If-Match"), '"7"');
  assert.deepEqual(write.body, { space_id: "space", path: "运营/估值", new_path: "运营/估值核对" });
  assert.match(document.body.textContent, /目标分类已存在/);
  assert.equal(saved.length, 0);
});
test("known nonempty classification cannot submit DELETE", async () => {
  await mount(wiki.WikiCategoryDialog, { operation: "delete", path: "运营/估值", close() {}, saved() { assert.fail("cannot delete nonempty category"); } });
  assert.equal(button("删除空分类").disabled, true);
  assert.match(document.body.textContent, /含有知识页/);
  assert.ok(!requests.some((request) => request.method === "DELETE"));
});
test("server nonempty protection keeps an apparently empty category intact", async () => {
  const saved = [];
  respond = (request) => request.path === "/wiki/taxonomy" ? { ...taxonomy, categories: [{ path: "空分类", name: "空分类", count: 0 }] }
    : request.path === "/wiki/categories" ? { status: 409, body: { code: "CATEGORY_NOT_EMPTY", message: "分类包含子分类或保留资源，不能删除" } } : defaultResponse(request);
  await mount(wiki.WikiCategoryDialog, { operation: "delete", path: "空分类", close() {}, saved(...values) { saved.push(values); } });
  await click(document.querySelector('.wiki-category-form input[type="checkbox"]'));
  await click(button("删除空分类"));
  const write = requests.find((request) => request.method === "DELETE");
  assert.deepEqual(write.body, { space_id: "space", path: "空分类" });
  assert.equal(write.headers.get("If-Match"), '"7"');
  assert.match(document.body.textContent, /保留资源，不能删除/);
  assert.equal(saved.length, 0);
});
test("confirmed empty category deletion returns the authoritative updated directory", async () => {
  const saved = [];
  respond = (request) => request.path === "/wiki/taxonomy" ? { ...taxonomy, categories: [{ path: "空分类", name: "空分类", count: 0 }] }
    : request.path === "/wiki/categories" ? { ...taxonomy, revision: 8, categories: [] } : defaultResponse(request);
  await mount(wiki.WikiCategoryDialog, { operation: "delete", path: "空分类", close() {}, saved(...values) { saved.push(values); } });
  await click(document.querySelector('.wiki-category-form input[type="checkbox"]'));
  await click(button("删除空分类"));
  assert.equal(saved.length, 1);
  assert.equal(saved[0][1], "delete");
  assert.deepEqual(saved[0][0].categories, []);
});
test("stale category revision requires refresh and a second explicit confirmation", async () => {
  let reads = 0;
  const saved = [];
  respond = (request) => {
    if (request.path === "/wiki/taxonomy") return { ...taxonomy, revision: ++reads === 1 ? 7 : 8 };
    if (request.path === "/wiki/categories") return request.headers.get("If-Match") === '"7"'
      ? { status: 412, body: { code: "REVISION_CONFLICT", message: "分类目录已变化" } } : { ...taxonomy, revision: 9 };
    return defaultResponse(request);
  };
  await mount(wiki.WikiCategoryDialog, { operation: "rename", path: "运营/估值", close() {}, saved(...values) { saved.push(values); } });
  await fill('input[aria-label="重命名后的分类路径"]', "运营/估值核对");
  await click(button("保存分类"));
  assert.equal(saved.length, 0);
  await click(button("读取最新目录后再确认"));
  assert.equal(requests.filter((request) => request.method === "PATCH").length, 1, "refresh must not silently retry a write");
  assert.equal(document.querySelector('input[aria-label="重命名后的分类路径"]').value, "运营/估值核对");
  await click(button("保存分类"));
  assert.equal(requests.filter((request) => request.method === "PATCH")[1].headers.get("If-Match"), '"8"');
  assert.equal(saved.length, 1);
});
test("knowledge category assignment patches only metadata with the current resource revision", async () => {
  let saved = 0;
  respond = (request) => request.path === `/resources/${ID}` ? { ...resource, revision: request.method === "PATCH" ? 6 : 5,
    category: request.body?.category ?? resource.category } : defaultResponse(request);
  await mount(wiki.WikiAssignCategoryDialog, { resource, close() {}, saved() { saved++; } });
  await fill('input[aria-label="知识页归属分类"]', "运营/估值/债券");
  await click(button("保存归类"));
  const write = requests.find((request) => request.method === "PATCH");
  assert.equal(write.path, `/resources/${ID}`);
  assert.deepEqual(write.body, { category: "运营/估值/债券" });
  assert.equal(write.headers.get("If-Match"), '"5"');
  assert.equal(saved, 1);
});
test("category assignment starts from current server metadata but preserves edits after a 412 refresh", async () => {
  let reads = 0;
  let saved = 0;
  respond = (request) => {
    if (request.path === `/resources/${ID}` && request.method === "GET") {
      reads++;
      return { ...resource, category: reads === 1 ? "最新分类" : "另一人分类", revision: reads === 1 ? 8 : 9 };
    }
    if (request.path === `/resources/${ID}` && request.method === "PATCH") return request.headers.get("If-Match") === '"8"'
      ? { status: 412, body: { code: "REVISION_CONFLICT", message: "资源已被修改" } }
      : { ...resource, category: request.body.category, revision: 10 };
    return defaultResponse(request);
  };
  await mount(wiki.WikiAssignCategoryDialog, { resource, close() {}, saved() { saved++; } });
  assert.equal(document.querySelector('input[aria-label="知识页归属分类"]').value, "最新分类");
  await fill('input[aria-label="知识页归属分类"]', "目标分类");
  await click(button("保存归类"));
  assert.equal(saved, 0);
  await click(button("重新读取资源信息"));
  assert.equal(requests.filter((request) => request.method === "PATCH").length, 1);
  assert.equal(document.querySelector('input[aria-label="知识页归属分类"]').value, "目标分类");
  await click(button("保存归类"));
  assert.equal(requests.filter((request) => request.method === "PATCH")[1].headers.get("If-Match"), '"9"');
  assert.equal(saved, 1);
});
test("creating knowledge in a selected category reuses the original dialog with that default", async () => {
  await mount(wiki.KnowledgeWorkspace);
  await click(document.querySelector('button[title="运营/估值"]'));
  await click(button("新建知识"));
  const category = document.querySelector('dialog input[name="category"]');
  assert.ok(category);
  assert.equal(category.value, "运营/估值");
});

test("workspace category CRUD refreshes the tree and selected path without deleting knowledge resources", async () => {
  let categories = [...taxonomy.categories];
  let revision = taxonomy.revision;
  respond = (request) => {
    if (request.path === "/wiki/taxonomy") return { ...taxonomy, categories, revision };
    if (request.path === "/wiki/workspace") return { ...workspace, categories,
      pages: request.params.get("category") && request.params.get("category") !== page.category ? [] : [page] };
    if (request.path === "/wiki/categories") {
      if (request.method === "POST") categories = [...categories, { path: request.body.path, name: request.body.path, count: 0 }];
      if (request.method === "PATCH") categories = categories.map((item) => item.path === request.body.path
        ? { ...item, path: request.body.new_path, name: request.body.new_path } : item);
      if (request.method === "DELETE") categories = categories.filter((item) => item.path !== request.body.path);
      revision++;
      return { status: request.method === "POST" ? 201 : 200, body: { ...taxonomy, categories, revision } };
    }
    return defaultResponse(request);
  };
  await mount(wiki.KnowledgeWorkspace);
  await click(document.querySelector('.wiki-taxonomy-actions button'));
  await fill('input[aria-label="新分类路径"]', "待整理分类");
  await click(button("保存分类"));
  assert.ok(document.querySelector('button[title="待整理分类"]'));
  assert.match(document.querySelector('.wiki-filterbar').textContent, /待整理分类/);
  await click(button("重命名"));
  await fill('input[aria-label="重命名后的分类路径"]', "整理后分类");
  await click(button("保存分类"));
  assert.equal(document.querySelector('button[title="待整理分类"]'), null);
  assert.ok(document.querySelector('button[title="整理后分类"]'));
  assert.match(document.querySelector('.wiki-filterbar').textContent, /整理后分类/);
  await click(document.querySelector('button[aria-label="删除当前空分类"]'));
  await click(document.querySelector('.wiki-category-form input[type="checkbox"]'));
  await click(button("删除空分类"));
  assert.equal(document.querySelector('button[title="整理后分类"]'), null);
  assert.match(document.querySelector('.wiki-page-index').textContent, /合成知识页/);
  assert.ok(!requests.some((request) => request.method === "DELETE" && request.path.startsWith("/resources")));
  const writes = requests.filter((request) => request.path === "/wiki/categories");
  assert.deepEqual(writes.map((request) => request.method), ["POST", "PATCH", "DELETE"]);
  assert.equal(writes[1].headers.get("If-Match"), '"8"');
  assert.equal(writes[2].headers.get("If-Match"), '"9"');
});

for (const activation of ["click", "Enter"]) test(`native Wiki span ${activation} resolves into the workspace and leaves raw blocks unchanged`, async () => {
  const targetId = "00000000-0000-4000-8000-000000000010";
  const targetVersion = "00000000-0000-4000-8000-000000000011";
  const raw = { block_id: "wiki-block", ordinal: 0, block_type: "paragraph",
    data: { text: "请参见 [[估值定义|定义说明]]。", text_format: "markdown" }, locator: {}, citations: [] };
  respond = (request) => {
    if (request.path === `/versions/${VERSION}`) return { ...defaultResponse(request), blocks: [raw] };
    if (request.path === "/wiki/resolve") return { resource_id: targetId, version_id: targetVersion, title: "估值定义" };
    if (request.path === `/resources/${targetId}`) return { ...resource, id: targetId, active_version_id: targetVersion, name: "估值定义" };
    if (request.path === `/versions/${targetVersion}`) return { ...defaultResponse({ path: `/versions/${VERSION}` }), id: targetVersion,
      resource_id: targetId, title: "估值定义", blocks: [] };
    if (request.path === `/wiki/pages/${targetId}/links`) return { outgoing: [], incoming: [], sources: [], unresolved: [], truncated: false };
    return defaultResponse(request);
  };
  await mount(wiki.KnowledgeWorkspace);
  const link = document.querySelector('.wiki-reading-body [data-wiki-title="估值定义"]');
  assert.ok(link);
  assert.equal(link.tagName, "SPAN");
  assert.equal(link.getAttribute("role"), "link");
  assert.equal(link.getAttribute("tabindex"), "0");
  assert.equal(link.textContent, "定义说明");
  if (activation === "click") await click(link);
  else {
    await act(async () => link.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
    await settle();
  }
  assert.ok(requests.some((request) => request.path === "/wiki/resolve" && request.params.get("title") === "估值定义"));
  assert.equal(document.querySelector('.wiki-reading-body h1').textContent, "估值定义");
  assert.deepEqual(opened, [], "Wiki page traversal should stay in the workspace, not open a separate resource modal");
  assert.equal(raw.data.text, "请参见 [[估值定义|定义说明]]。");
  assert.ok(!requests.some((request) => request.method !== "GET"));
});
