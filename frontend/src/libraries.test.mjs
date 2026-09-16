// Real React rendering with synthetic HTTP responses. No business data or model calls.
import test, { afterEach, after } from "node:test";
import assert from "node:assert/strict";
import Module, { createRequire } from "node:module";
import { resolve, dirname } from "node:path";
import { build } from "esbuild";
const require = createRequire(import.meta.url);
const { JSDOM } = require("jsdom");
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: "http://localhost", pretendToBeVisual: true });
for (const key of ["window", "document", "HTMLElement", "Element", "Node", "MutationObserver", "Event", "MouseEvent", "FormData"]) globalThis[key] = key === "window" ? dom.window : dom.window[key];
globalThis.getComputedStyle = window.getComputedStyle.bind(window);
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
window.matchMedia = query => ({ media: query, matches: query.includes("reduce"), addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
window.HTMLDialogElement.prototype.close = function () { this.open = false; };
const React = require("react");
const { act } = React;
const { createRoot } = require("react-dom/client");
const gsap = require("gsap").gsap;
const { ScrollTrigger } = require("gsap/dist/ScrollTrigger");
const compiled = await build({ stdin: { contents: 'export * from "./LibraryManager"; export {AppContext} from "./ui"; export {setSession,clearSession} from "./api";', resolveDir: resolve("src"), loader: "tsx" }, bundle: true, platform: "node", format: "cjs", external: ["react", "react-dom", "react-dom/*", "react/jsx-runtime", "gsap", "gsap/ScrollTrigger", "@gsap/react"], loader: { ".css": "empty" }, write: false, logLevel: "silent" });
const runtime = new Module(resolve("src/library-test-runtime.cjs"));
runtime.filename = resolve("src/library-test-runtime.cjs"); runtime.paths = Module._nodeModulePaths(dirname(runtime.filename));
const baseRequire = runtime.require.bind(runtime);
runtime.require = id => id === "gsap" ? gsap : id === "gsap/ScrollTrigger" ? { ScrollTrigger } : baseRequire(id);
runtime._compile(compiled.outputFiles[0].text, runtime.filename);
const { LibraryManager, LibraryMembers, LibraryCreate, AppContext, setSession, clearSession } = runtime.exports;
const originalFetch = globalThis.fetch;
let root; let calls = [];
const personal = { id: "personal", name: "我的运营手册", kind: "personal", owner_id: "u1", governed: true, revision: 1, roles: ["reader", "editor", "admin"] };
const team = { ...personal, id: "team", name: "团队运营手册", kind: "team" };
const context = space => ({ me: { id: "u1", display_name: "测试用户", spaces: [space], csrf_token: "synthetic-csrf" }, space, refresh: 0, bump() {}, notify() {}, navigate() {}, selectSpace() {}, openResource() {}, openVersion() {}, ask() {} });
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
function http(handler) { globalThis.fetch = async (url, options = {}) => { calls.push([url, options]); return handler(url, options); }; }
async function mount(component, ctx = context(personal)) { root = createRoot(document.getElementById("root")); setSession(ctx.me); await act(async () => root.render(React.createElement(AppContext.Provider, { value: ctx }, component))); }
async function click(text) { const button = [...document.querySelectorAll("button")].find(node => node.textContent === text || node.textContent.trim() === text); assert.ok(button, `button ${text}`); await act(async () => button.click()); }
afterEach(async () => { if (root) await act(async () => root.unmount()); root = undefined; calls = []; globalThis.fetch = originalFetch; clearSession(); });
after(() => { ScrollTrigger.killAll(); ScrollTrigger.disable(); gsap.globalTimeline.clear(); gsap.ticker.sleep(); dom.window.close(); });

test("library manager labels privacy and team sharing using server projections", async () => {
  http(() => json({ items: [personal, team] }));
  await mount(React.createElement(LibraryManager));
  assert.match(document.body.textContent, /个人知识库/); assert.match(document.body.textContent, /团队知识库/);
  assert.equal(document.querySelectorAll(".library-card").length, 2);
  assert.match(document.body.textContent, /仅本人可见与编辑/);
  assert.equal(calls.length, 1); assert.equal(calls[0][0], "/api/v1/libraries");
});
test("personal member page never fetches or offers an external member directory", async () => {
  http(() => { throw new Error("unexpected request"); });
  await mount(React.createElement(LibraryMembers));
  assert.match(document.body.textContent, /不能添加其他成员/); assert.equal(calls.length, 0);
});
test("team reader cannot manage membership from the UI", async () => {
  http(() => { throw new Error("unexpected request"); });
  await mount(React.createElement(LibraryMembers), context({ ...team, roles: ["reader"] }));
  assert.match(document.body.textContent, /知识库管理员维护/); assert.equal(calls.length, 0);
});
test("new library defaults private and never auto-submits sharing", async () => {
  http(() => { throw new Error("unexpected request"); });
  await mount(React.createElement(LibraryCreate, { close() {}, created() {} }));
  const pressed = document.querySelector('[aria-pressed="true"]');
  assert.match(pressed.textContent, /个人知识库/); assert.equal(calls.length, 0);
  await act(async () => document.querySelectorAll(".library-type-choice button")[1].click());
  assert.match(document.querySelector('[aria-pressed="true"]').textContent, /团队知识库/); assert.equal(calls.length, 0);
});
test("first library creation sends CSRF, idempotency and explicit chosen kind", async () => {
  let created;
  http((url, options) => { assert.equal(url, "/api/v1/libraries"); assert.equal(options.method, "POST"); return json(personal, 201); });
  await mount(React.createElement(LibraryCreate, { close() {}, created(value) { created = value; } }));
  document.querySelector('input[name="name"]').value = " 我的运营手册 ";
  await act(async () => document.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  assert.deepEqual(JSON.parse(calls[0][1].body), { name: personal.name, kind: "personal" });
  assert.equal(calls[0][1].headers.get("X-CSRF-Token"), "synthetic-csrf");
  assert.ok(calls[0][1].headers.get("Idempotency-Key")); assert.equal(created.id, personal.id);
});
test("renaming uses library revision and failed conflicts keep the dialog draft", async () => {
  http((url, options) => options.method === "PATCH" ? json({ code: "PRECONDITION_FAILED", message: "conflict" }, 412) : json({ items: [personal] }));
  await mount(React.createElement(LibraryManager)); await click("重命名");
  document.querySelector('input[name="name"]').value = "待保存名称";
  await act(async () => document.querySelector("dialog form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  const request = calls.find(([, options]) => options.method === "PATCH");
  assert.equal(request[1].headers.get("If-Match"), '"1"');
  assert.equal(document.querySelector('input[name="name"]').value, "待保存名称"); assert.match(document.body.textContent, /内容已被其他人更新/);
});
