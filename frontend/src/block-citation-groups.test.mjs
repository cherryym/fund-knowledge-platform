// BlockView citation chips: one chip per cited source document, titled where the reader supplies source names.
// Synthetic blocks only; no app mount, API request, generation or source document.
import test, { after } from "node:test";
import assert from "node:assert/strict";
import Module from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { build, stop } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const srcDir = dirname(fileURLToPath(import.meta.url));
const compiled = await build({
  entryPoints: [join(srcDir, "BlockView.tsx")],
  bundle: true, platform: "node", format: "cjs", write: false,
  external: ["react", "react/jsx-runtime", "react-dom", "markdown-it", "dompurify"],
  loader: { ".css": "empty" },
  plugins: [{ name: "no-app-runtime", setup(builder) {
    builder.onResolve({ filter: /^\.\/(ui|Pickers)$/ }, (args) => ({ path: args.path.slice(2), namespace: "synthetic" }));
    builder.onResolve({ filter: /^@phosphor-icons\/react$/ }, () => ({ path: "icons", namespace: "synthetic" }));
    builder.onLoad({ filter: /^icons$/, namespace: "synthetic" }, () => ({ loader: "js",
      contents: "export function Link() { return null; }" }));
    builder.onLoad({ filter: /^ui$/, namespace: "synthetic" }, () => ({ loader: "js", contents: `
      export function useApp() { return { openVersion() {} }; }
      export function useTask() { return { busy: false, error: undefined, run: async () => {} }; }
      export function ErrorBox() { return null; }
      export function readable(value) { return value == null ? "" : String(value); }` }));
    builder.onLoad({ filter: /^Pickers$/, namespace: "synthetic" }, () => ({ loader: "js",
      contents: 'export const purposeLabels = { FACT: "事实来源", RULE: "规则依据" };' }));
  } }],
});
const runtime = new Module(join(srcDir, "block-citation-groups-memory.cjs"));
runtime.filename = join(srcDir, "block-citation-groups-memory.cjs");
runtime.paths = Module._nodeModulePaths(srcDir);
runtime._compile(compiled.outputFiles[0].text, runtime.filename);
const { BlockView, citationGroups } = runtime.exports;
after(() => stop());

const block = { block_id: "page-block", ordinal: 0, block_type: "paragraph", data: { text: "合成正文" }, locator: {},
  citations: [
    { version_id: "v-rules", block_id: "r1", purpose: "FACT" }, { version_id: "v-rules", block_id: "r2", purpose: "FACT" },
    { version_id: "v-notice", block_id: "n9", purpose: "RULE" }, { version_id: "v-rules", block_id: "r3", purpose: "RULE" },
  ] };

function chips(sourceNames) {
  const html = renderToStaticMarkup(React.createElement(BlockView, { block, sourceNames }));
  return [...html.matchAll(/class="citation-chip"[^>]*>(.*?)<\/button>/g)].map((m) => m[1].replace(/<[^>]+>/g, ""));
}

test("citations group by source document in first-cited order and open its first cited block", () => {
  assert.deepEqual(citationGroups(block.citations), [
    { version_id: "v-rules", block_id: "r1", purposes: ["FACT", "RULE"], count: 3 },
    { version_id: "v-notice", block_id: "n9", purposes: ["RULE"], count: 1 },
  ]);
});

test("a titled chip names the document and counts its cited paragraphs instead of one chip per paragraph", () => {
  assert.deepEqual(chips({ "v-rules": "合成交易规则" }), ["《合成交易规则》 · 事实来源、规则依据 · 3段", "来源 2 · 规则依据"]);
  assert.deepEqual(chips(undefined), ["来源 1 · 事实来源、规则依据 · 3段", "来源 2 · 规则依据"]);
});
