import assert from "node:assert/strict";
import { after, afterEach, beforeEach, test } from "node:test";
import { createRequire, Module } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const require = createRequire(import.meta.url), sourceDir = dirname(fileURLToPath(import.meta.url));
const { JSDOM } = require("jsdom");
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: "http://login.test/", pretendToBeVisual: true });
for (const key of ["window", "document", "HTMLElement", "Element", "Node", "Event", "MouseEvent"])
  globalThis[key] = key === "window" ? dom.window : dom.window[key];
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
globalThis.getComputedStyle = window.getComputedStyle.bind(window);
window.matchMedia = media => ({ media, matches: media.includes("reduce"), addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
const React = require("react"), { act } = React, h = React.createElement, { createRoot } = require("react-dom/client");
const gsap = require("gsap").gsap, { ScrollTrigger } = require("gsap/dist/ScrollTrigger");
const compiled = await build({
  stdin: { contents: 'export { Login } from "./Application"; export { ApiError, clearSession } from "./api";', resolveDir: sourceDir, loader: "tsx" },
  bundle: true, platform: "node", format: "cjs", write: false, logLevel: "silent", loader: { ".css": "empty", ".svg": "text" },
  define: { "import.meta.env.DEV": "false" },
  external: ["react", "react-dom", "react-dom/*", "react/jsx-runtime", "gsap", "gsap/ScrollTrigger", "@gsap/react"],
});
const runtime = new Module(resolve(sourceDir, "login-test-runtime.cjs"));
runtime.filename = resolve(sourceDir, "login-test-runtime.cjs");runtime.paths = Module._nodeModulePaths(sourceDir);
const originalRequire = runtime.require.bind(runtime);
runtime.require = name => name === "gsap" ? gsap : name === "gsap/ScrollTrigger" ? { ScrollTrigger } : originalRequire(name);
runtime._compile(compiled.outputFiles[0].text, runtime.filename);
const { Login, ApiError, clearSession } = runtime.exports;
const response = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
let root, calls, ready, reloads, routes;
const user = { id: "synthetic-admin", display_name: "合成管理员", roles: ["admin"] };
const me = { id: user.id, display_name: user.display_name, csrf_token: "synthetic-test-only", spaces: [] };
beforeEach(() => {
  calls = []; ready = []; reloads = 0; routes = new Map(); clearSession();
  document.body.innerHTML = '<div id="root"></div>';
  root = createRoot(document.getElementById("root"));
  globalThis.fetch = window.fetch = async (url, options) => {
    const key = `${options.method ?? "GET"} ${url}`;calls.push(key);
    if (!routes.has(key)) throw new Error("Unexpected request blocked: " + key);
    return routes.get(key)(options);
  };
});
afterEach(async () => { await act(async () => root.unmount()); ScrollTrigger.getAll().forEach(x => x.kill()); gsap.ticker.sleep(); });
after(() => { ScrollTrigger.disable(); gsap.ticker.sleep(); dom.window.close(); });
const mount = () => act(async () => root.render(h(Login, { ready: x => ready.push(x), error: new ApiError(401, "AUTH_REQUIRED", "请先登录"), reload: () => reloads++ })));
const button = text => [...document.querySelectorAll("button")].find(x => x.textContent.includes(text));

test("production-built frontend uses enabled local login and confirms the session", async () => {
  routes.set("GET /api/v1/auth/demo", () => response([user]));
  routes.set("POST /api/v1/auth/demo", options => { assert.deepEqual(JSON.parse(options.body), { user_id: user.id }); return response({ id: user.id }); });
  routes.set("GET /api/v1/me", () => response(me));
  await mount();assert.ok(button(user.display_name));assert.equal(document.querySelector('a[href="/api/v1/auth/login"]'), null);
  await act(async () => button(user.display_name).click());
  assert.deepEqual(ready, [me]);assert.deepEqual(calls, ["GET /api/v1/auth/demo", "POST /api/v1/auth/demo", "GET /api/v1/me"]);
});
test("server-disabled local login keeps the institutional login route", async () => {
  routes.set("GET /api/v1/auth/demo", () => response({ code: "NOT_FOUND", message: "接口不可用" }, 404));
  await mount();assert.ok(document.querySelector('a[href="/api/v1/auth/login"]'));assert.equal(button(user.display_name), undefined);
  assert.ok(!calls.some(x => x.startsWith("POST")));
});
test("an unavailable auth endpoint shows an error and can be retried without selecting OIDC", async () => {
  routes.set("GET /api/v1/auth/demo", () => response({ code: "BACKEND_UNAVAILABLE", message: "合成连接失败" }, 503));
  await mount();assert.ok(document.querySelector('[role="alert"]'));assert.equal(document.querySelector('a[href="/api/v1/auth/login"]'), null);
  routes.set("GET /api/v1/auth/demo", () => response([user]));
  await act(async () => button("重试").click());
  assert.ok(button(user.display_name));assert.equal(reloads, 1);
});
test("pending discovery does not offer the wrong login branch", async () => {
  let finish;routes.set("GET /api/v1/auth/demo", () => new Promise(resolve => { finish = resolve; }));
  await mount();assert.equal(document.querySelector('a[href="/api/v1/auth/login"]'), null);assert.match(document.body.textContent, /正在读取登录方式/);
  await act(async () => finish(response([user])));assert.ok(button(user.display_name));
});
test("an empty local directory stays local and explains the missing identities", async () => {
  routes.set("GET /api/v1/auth/demo", () => response([]));
  await mount();assert.match(document.body.textContent, /暂无可用身份/);assert.equal(document.querySelector('a[href="/api/v1/auth/login"]'), null);
});
