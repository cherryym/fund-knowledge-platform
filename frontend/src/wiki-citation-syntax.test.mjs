// Pure Markdown renderer regression: synthetic citations, in-memory compilation.
// No app mount, API request, generation, source document, or on-disk build.
import test, { after } from "node:test";
import assert from "node:assert/strict";
import Module from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { build, stop } from "esbuild";
import { JSDOM } from "jsdom";

const srcDir = dirname(fileURLToPath(import.meta.url));
const compiled = await build({
  entryPoints: [join(srcDir, "WikiAnswerMarkdown.tsx")],
  bundle: true, platform: "node", format: "cjs", write: false,
  external: ["react", "react/jsx-runtime", "react-dom", "markdown-it"],
  loader: { ".css": "empty" },
  plugins: [{ name: "no-app-runtime", setup(builder) {
    builder.onResolve({ filter: /^\.\/ui$/ }, () => ({ path: "ui", namespace: "synthetic" }));
    builder.onLoad({ filter: /.*/, namespace: "synthetic" }, () => ({
      contents: 'export function useApp() { throw new Error("pure renderer test must not mount the app"); }',
      loader: "js",
    }));
  } }],
});
const runtime = new Module(join(srcDir, "wiki-citation-syntax-memory.cjs"));
runtime.filename = join(srcDir, "wiki-citation-syntax-memory.cjs");
runtime.paths = Module._nodeModulePaths(srcDir);
runtime._compile(compiled.outputFiles[0].text, runtime.filename);
const { wikiAnswerHtml, wikiPreviewHtml } = runtime.exports;
const dom = new JSDOM("<!doctype html><body></body>");
after(() => { dom.window.close(); stop(); });

const citations = [41, 42, 43].map(i => ({
  id: `E${i}`, resource_id: "synthetic-resource", version_id: "synthetic-version",
  block_id: `synthetic-block-${i}`, content_sha256: "a".repeat(64), source_title: "合成来源",
  excerpt: `合成摘录 ${i}`, locator: { label: "合成位置" },
}));
function render(text, registered = citations) {
  const host = dom.window.document.createElement("div");
  host.innerHTML = wikiAnswerHtml(text, registered);
  return host;
}
function chips(host) {
  return [...host.querySelectorAll(".citation-chip")];
}
function originalText(host) {
  const copy=host.cloneNode(true);
  for (const label of copy.querySelectorAll('[data-citation-label]')) label.replaceWith(dom.window.document.createTextNode(label.dataset.citationLabel));
  return copy.textContent;
}

for (const marker of ["[E41]", "【E41】", "［E41］", "〔E41〕", "[ E41 ]"]) {
  test(`closed citation ${marker} keeps explanatory dash outside its source button`, () => {
    const text = `${marker}——即合成解释。`;
    const host = render(text);
    assert.equal(originalText(host).trim(), text);
    assert.deepEqual(chips(host).map(button => button.dataset.evidenceIds), ["E41"]);
    assert.equal(chips(host)[0].textContent, "合成来源");
    assert.ok(chips(host)[0].querySelector('svg[aria-hidden="true"]'));
  });
}

test("English explanations and later independent references do not form a range", () => {
  const text = "[E41] — Explanation follows. See [E43].";
  const host = render(text);
  assert.equal(originalText(host).trim(), text);
  assert.deepEqual(chips(host).map(button => button.dataset.evidenceIds), ["E41", "E43"]);
});

test("HTML entity punctuation has the same single-reference rendering", () => {
  const host = render("&#12304;E41&#12305;&mdash;&mdash;合成解释。");
  assert.equal(originalText(host).trim(), "【E41】——合成解释。");
  assert.deepEqual(chips(host).map(button => button.dataset.evidenceIds), ["E41"]);
});

test("a genuine supported bare range still links its registered interior", () => {
  const host = render("E41—E43");
  assert.deepEqual(chips(host).map(button => button.dataset.evidenceIds), ["E41,E42,E43"]);
  assert.equal(originalText(host).trim(), "E41—E43");
});

test("unknown IDs before prose do not become clickable", () => {
  const host = render("【E99】——合成解释。");
  assert.equal(chips(host).length, 0);
  assert.equal(host.textContent.trim(), "【E99】——合成解释。");
});

test("inline and fenced code never add citation buttons beside normal prose", () => {
  const host = render("【E41】——合成解释。\n\n`E42—E43`\n\n```text\n【E42】——【E43】\n```");
  assert.deepEqual(chips(host).map(button => button.dataset.evidenceIds), ["E41"]);
  assert.equal(host.querySelector("code button"), null);
  assert.match(host.textContent, /【E42】——【E43】/);
});

test("existing hyperlinks never receive a nested source button", () => {
  const host = render("[E41](https://example.invalid)——合成解释。");
  assert.equal(host.querySelector("a button"), null);
  assert.equal(chips(host).length, 0);
});

test("preview rendering has no source buttons or active hyperlinks", () => {
  const host = dom.window.document.createElement("div");
  host.innerHTML = wikiPreviewHtml("【E41】——合成解释。[普通链接](https://example.invalid)");
  assert.equal(host.querySelector("button, a"), null);
  assert.match(host.textContent, /【E41】——合成解释/);
});

test("the source is recognizable before clicking and its full title/locator stay available",()=>{
  const title='关于发布《合成来源指引》的通知—附件：附件：合成来源指引.docx';
  const host=render('说明【E41】', [{...citations[0],source_title:title,locator:{label:'第12页 · 第三条'}}]);
  assert.equal(chips(host)[0].textContent,'合成来源指引');
  assert.match(chips(host)[0].title,/第12页 · 第三条/);
  assert.ok(chips(host)[0].title.includes(title));
  assert.ok(chips(host)[0].getAttribute('aria-label').includes(title));
});

test("a multi-document range has a separate named button for each exact version",()=>{
  const host=render('【E41-E43】', [citations[0], {...citations[1],version_id:'other-version',source_title:'另一份来源'},citations[2]]);
  assert.deepEqual(chips(host).map(b=>b.textContent),['合成来源','另一份来源']);
  assert.deepEqual(chips(host).map(b=>b.dataset.evidenceIds),['E41,E43','E42']);
  assert.equal(originalText(host).trim(),'【E41-E43】');
});

test("joint notices use the named attachment without guessing another document",()=>{
  const title='关于发布《合成甲》《合成乙》的通知—附件：附件1：《合成甲（2025年修订）》.pdf';
  assert.equal(chips(render('E41',[{...citations[0],source_title:title}]))[0].textContent,'合成甲（2025年修订）');
  const joint='关于发布《合成甲》《合成乙》的通知';
  assert.equal(chips(render('E41',[{...citations[0],source_title:joint}]))[0].textContent,joint);
});

test("source titles and locator labels are inert even when they contain markup",()=>{
  const host=render('E41',[{...citations[0],source_title:'来源"<img src=x onerror="bad()">',locator:{label:'<script>bad()</script>'}}]);
  assert.equal(host.querySelector('img,script,[onerror]'),null);
  assert.ok(chips(host)[0].title.includes('<script>bad()</script>'));
});

test("conflicting identities for one evidence number are never labelled with a guessed source",()=>{
  const host=render('【E41】',[citations[0],{...citations[0],version_id:'different-version',source_title:'错误来源'}]);
  assert.equal(chips(host).length,0);
  assert.equal(host.textContent.trim(),'【E41】');
});

test("one bracketed group from the same document is one named chip with all references preserved",()=>{
  for (const text of ['【E43、E41-E42】','[E43, E41-E42]','［E43；E41-E42］','〔E43，E41-E42〕']) {
    const host=render(text);
    assert.equal(chips(host).length,1);
    assert.equal(chips(host)[0].dataset.evidenceIds,'E41,E42,E43');
    assert.equal(originalText(host).trim(),text);
  }
});

test("citation grouping never swallows ordinary explanatory words or unrelated punctuation",()=>{
  const text='【参见E41，说明仍要保留】；E42，另一项说明E43。';
  const host=render(text);
  assert.equal(originalText(host).trim(),text);
  assert.equal(chips(host).length,3);
});

test("a partially registered group retains its missing marker without assigning it a document",()=>{
  const host=render('【E41、E99】');
  assert.equal(chips(host).length,1);
  assert.equal(chips(host)[0].dataset.evidenceIds,'E41');
  assert.match(host.querySelector('.citation-unresolved').textContent,/E99/);
  assert.equal(originalText(host).trim(),'【E41、E99】');
});

test("a hole in a range is exposed and invalid numeric endpoints never alias registered IDs",()=>{
  const host=render('E41-E43',[citations[0],citations[2]]);
  assert.equal(chips(host)[0].dataset.evidenceIds,'E41,E43');
  assert.match(host.querySelector('.citation-unresolved').textContent,/未完整关联/);
  assert.equal(chips(render('E041-E043')).length,0);
});
