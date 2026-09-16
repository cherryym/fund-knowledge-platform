import test from "node:test";
import assert from "node:assert/strict";
import { build } from "esbuild";
import { MarkdownManager } from "@tiptap/markdown";
import StarterKit from "@tiptap/starter-kit";

async function moduleFrom(path) {
  const result = await build({
    entryPoints: [path],
    bundle: true,
    write: false,
    format: "esm",
    platform: "node",
  });
  return import(
    "data:text/javascript;base64," +
      Buffer.from(result.outputFiles[0].text).toString("base64")
  );
}
const rich = await moduleFrom("src/richText.ts");
const ops = await moduleFrom("src/blockOperations.ts");
const {WikiLinkMark} = await moduleFrom("src/WikiLinkMark.ts");
test("Wiki links render safely while code and escaped literals remain literal",()=>{
  const output=rich.richHtml("[[费用核对|核对说明]] `[[代码内链接]]` \\[[普通括号]]", "markdown");
  assert.match(output,/data-wiki-title="费用核对"/);
  assert.match(output,/核对说明/);
  assert.ok(!output.includes('data-wiki-title="代码内链接"'));
  assert.ok(!output.includes('data-wiki-title="普通括号"'));
  assert.ok(!rich.richHtml('[[来源|<img src=x onerror=bad>]]','markdown').includes('<img'));
});
test("Wiki links keep double-bracket syntax through actual editor serialization",()=>{
  const manager=new MarkdownManager({extensions:[StarterKit,WikiLinkMark]});
  const output=manager.serialize({type:'doc',content:[{type:'paragraph',content:[{type:'text',text:'核对说明',marks:[{type:'wikiLink',attrs:{target:'费用核对'}}]}]}]});
  assert.equal(output,'[[费用核对|核对说明]]');
  assert.match(rich.richHtml(output,'markdown'),/data-wiki-title="费用核对"/);
});
test("old source text stays literal, including asterisks and angle brackets", () => {
  const text = "费率 A*B*，字面量 **非加粗** <b>原文</b>";
  const html = rich.richHtml(text);
  assert.ok(html.includes("**非加粗**"));
  assert.ok(html.includes("&lt;b&gt;原文&lt;/b&gt;"));
  assert.ok(!html.includes("<strong>"));
});
test("explicit Markdown renders real emphasis and multiline paragraphs", () => {
  const html = rich.richHtml(
    "**关键核对项**，*待复核*，~~旧说明~~，`参数`\n\n第二段。",
    "markdown",
  );
  assert.match(html, /<strong>关键核对项<\/strong>/);
  assert.match(html, /<em>待复核<\/em>/);
  assert.match(html, /<s>旧说明<\/s>/);
  assert.equal((html.match(/<p>/g) || []).length, 2);
});
test("Markdown cannot execute HTML or load remote images or unsafe links", () => {
  const html = rich.richHtml(
    "<img src=x onerror=alert(1)> ![remote](https://invalid.example/image) [x](javascript:alert(1))",
    "markdown",
  );
  assert.ok(!html.includes("<img"));
  assert.ok(!/href=["']javascript:/i.test(html));
  assert.ok(html.includes("&lt;img"));
  for (const url of [
    "javascript:alert(1)",
    "data:text/html,x",
    "https://a.test/\nsecret",
    "file:///tmp/a",
  ])
    assert.equal(rich.safeLink(url), false);
});
test("safe links isolate the new tab and heading markup stays inline", () => {
  const html = rich.richHtml(
    "[原文](https://example.org/source)",
    "markdown",
    true,
  );
  assert.ok(html.includes('rel="noopener noreferrer"'));
  assert.ok(!html.includes("<p>"));
});
test("actual Tiptap Markdown serializer retains bold and italic Chinese content", () => {
  const manager = new MarkdownManager({ extensions: [StarterKit] });
  const value = manager.serialize({
    type: "doc",
    content: [
      {
        type: "paragraph",
        content: [
          { type: "text", text: "基金运营", marks: [{ type: "bold" }] },
          { type: "text", text: "需复核", marks: [{ type: "italic" }] },
        ],
      },
    ],
  });
  assert.ok(value.includes("**基金运营**"));
  assert.match(rich.richHtml(value, "markdown"), /<em>需复核<\/em>/);
});
test("100 paragraphs keep stable ids and citations through movement", () => {
  const blocks = Array.from({ length: 100 }, (_, i) => ({
    ...ops.createBlock("paragraph"),
    ordinal: i,
    data: { text: `第${i + 1}段` },
    citations: [
      { version_id: "source", block_id: `source-${i}`, purpose: "FACT" },
    ],
  }));
  const original = structuredClone(blocks);
  const moved = ops.reorderBlock(blocks, blocks[80].block_id, -1);
  assert.equal(moved.length, 100);
  assert.equal(moved[79].block_id, blocks[80].block_id);
  assert.deepEqual(moved[79].citations, blocks[80].citations);
  assert.deepEqual(blocks, original);
  assert.equal(new Set(moved.map((b) => b.block_id)).size, 100);
});
test("insert adds exactly one block without discarding the remaining 100", () => {
  const blocks = Array.from({ length: 100 }, () =>
    ops.createBlock("paragraph"),
  );
  const next = ops.insertBlock(blocks, "table", blocks[50].block_id);
  assert.equal(next.blocks.length, 101);
  assert.equal(next.blocks[51].block_id, next.id);
  assert.equal(next.blocks[51].block_type, "table");
  assert.equal(next.blocks[52], blocks[51]);
});
