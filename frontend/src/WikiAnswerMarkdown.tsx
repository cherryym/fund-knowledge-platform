import { useEffect, useId, useLayoutEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import MarkdownIt from "markdown-it";
import { safeLink, escapeText } from "./richText";
import type { Evidence } from "./types";
import { useApp } from "./ui";
import { citationDescription, citationGroups, citationLocator, citationLookup, sourceLabel } from "./citationPresentation";
import "./citation-preview.css";

// Small code-native document glyph: no remote images, icon fetch or per-citation React root.
const sourceIcon = '<svg class="citation-doc-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true" focusable="false"><path d="M11.5 2.5H5a1 1 0 0 0-1 1v13a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1v-9z"/><path d="M11.5 2.5v5H16M7 11h6M7 14h4"/></svg>';
function previewPosition(anchor: HTMLElement) {
  const box=anchor.getBoundingClientRect(), width=Math.min(360,Math.max(120,window.innerWidth-24));
  const above=window.innerHeight-box.bottom<220 && box.top>220;
  return {width,above,left:Math.max(12,Math.min(box.left,window.innerWidth-width-12)),top:above?box.top-8:box.bottom+8};
}

const md = new MarkdownIt({ html: false, breaks: true, linkify: false, typographer: false }).disable("image");
md.validateLink = safeLink;
const citationAtom = String.raw`E\d+(?:\s*[-‐‑–—－~～至]\s*E?\d+)?`;
const citationPattern = new RegExp([
  String.raw`\[\s*${citationAtom}(?:\s*[,，、;；]\s*${citationAtom})+\s*\]`,
  ...[["【", "】"], ["［", "］"], ["〔", "〕"]].map(([open, close]) =>
    `${open}\\s*${citationAtom}(?:\\s*[,，、;；]\\s*${citationAtom})+\\s*${close}`),
  String.raw`\b${citationAtom}\b`,
].join("|"), "g");
function references(label: string, citations: Set<string>) {
  const ids = new Set<string>();
  const ranges = [...label.matchAll(/E(\d+)\s*[-‐‑–—－~～至]\s*E?(\d+)/g)];
  for (const range of ranges) {
    const first = Number(range[1]), last = Number(range[2]);
    if (!Number.isSafeInteger(first) || !Number.isSafeInteger(last) || first < 1 || first > last ||
      String(first) !== range[1] || String(last) !== range[2]) continue;
    for (const id of citations) {
      const value = Number(id.slice(1));
      if (value >= first && value <= last) ids.add(id);
    }
  }
  const singles = label.replace(/E\d+\s*[-‐‑–—－~～至]\s*E?\d+/g, " ");
  for (const id of singles.match(/E\d+/g) ?? []) if (citations.has(id)) ids.add(id);
  return [...ids].sort((a,b)=>Number(a.slice(1))-Number(b.slice(1)));
}
function unresolvedReferences(label: string, citations: Set<string>) {
  const unresolved: string[] = [];
  for (const atom of label.matchAll(new RegExp(String.raw`\b${citationAtom}\b`, "g"))) {
    const range = /^E(\d+)\s*[-‐‑–—－~～至]\s*E?(\d+)$/.exec(atom[0]);
    if (range) {
      const first=Number(range[1]), last=Number(range[2]);
      if (!Number.isSafeInteger(first) || !Number.isSafeInteger(last) || first<1 || first>last ||
        String(first)!==range[1] || String(last)!==range[2] || references(atom[0],citations).length!==last-first+1) unresolved.push(atom[0]);
    } else if (!citations.has(atom[0])) unresolved.push(atom[0]);
  }
  return unresolved;
}
// Text tokens cover both bracketed prose and bare table citations. Code tokens
// bypass this renderer, and existing hyperlinks must never nest new buttons.
md.renderer.rules.text = (tokens, index, _options, env) => {
  const text = tokens[index].content;
  let links = 0;
  for (const token of tokens.slice(0, index)) {
    if (token.type === "link_open") links++;
    if (token.type === "link_close") links--;
  }
  if (links) return escapeText(text);
  let html = "", cursor = 0;
  for (const match of text.matchAll(citationPattern)) {
    html += escapeText(text.slice(cursor, match.index));
    const ids = references(match[0], env.citations);
    const label = escapeText(match[0]);
    const groups = citationGroups(ids, env.lookup ?? new Map());
    if (groups.length) {
      const buttons = groups.map(group => {
        const name = escapeText(sourceLabel(group[0].source_title));
        const description = escapeText(citationDescription(group));
        const accessibility = escapeText(`查看来源：${group[0].source_title || "来源文献"} · ${citationLocator(group[0])} · ${match[0]}`);
        const tip = env.tooltipId ? `aria-describedby="${escapeText(env.tooltipId)}"` : `title="${description}"`;
        return `<button type="button" class="citation-chip citation-doc-chip" data-evidence-id="${group[0].id}" data-evidence-ids="${group.map(c => c.id).join(',')}" data-evidence-label="${label}" aria-label="${accessibility}" ${tip}>${sourceIcon}<span class="citation-doc-name">${name}</span></button>`;
      });
      const framed = /^[\[【［〔]/.test(match[0]);
      const missing=unresolvedReferences(match[0],env.citations);
      const notice=missing.length ? ` <span class="citation-unresolved" title="未完整关联到本轮可用来源">${escapeText(missing.join("、"))}（未完整关联）</span>` : "";
      html += `<span class="citation-doc-group" data-citation-label="${label}">${framed ? escapeText(match[0][0]) : ""}${buttons.join(" ")}${notice}${framed ? escapeText(match[0].at(-1)!) : ""}</span>`;
    } else html += label;
    cursor = match.index! + match[0].length;
  }
  return html + escapeText(text.slice(cursor));
};
md.renderer.rules.link_open = (tokens, index, options, _env, renderer) => {
  tokens[index].attrSet("target", "_blank");
  tokens[index].attrSet("rel", "noopener noreferrer");
  return renderer.renderToken(tokens, index, options);
};

export function wikiAnswerHtml(text: string, citations: Evidence[], tooltipId?: string) {
  const lookup = citationLookup(citations);
  return md.render(text, { citations: new Set(lookup.keys()), lookup, tooltipId });
}

export function wikiPreviewHtml(text: string) {
  const env = { citations: new Set<string>() };
  const tokens = md.parse(text, env);
  const removeLinks = (items: typeof tokens) => {
    for (const token of items) {
      // Pending paragraphs have no verified citation targets or external actions.
      // Keep link labels readable while disabling links until final validation.
      if (token.type === "link_open" || token.type === "link_close") {
        token.type = "text";
        token.content = "";
        token.tag = "";
        token.nesting = 0;
      }
      if (token.children) removeLinks(token.children);
    }
  };
  removeLinks(tokens);
  return md.renderer.render(tokens, md.options, env);
}

export function WikiAnswerMarkdown({ text, citations }: { text: string; citations: Evidence[] }) {
  const app = useApp();
  const tooltipId = useId();
  const lookup = useMemo(() => citationLookup(citations), [citations]);
  const html = useMemo(() => wikiAnswerHtml(text, citations, tooltipId), [text, citations, tooltipId]);
  // Keep the rendered answer DOM (and keyboard focus) stable while only the
  // source preview changes. Replacing innerHTML on hover would detach buttons.
  const markup = useMemo(() => ({__html: html}), [html]);
  const [preview, setPreview] = useState<{anchor: HTMLElement; ids: string[]; left: number; top: number; width: number; above: boolean} | null>(null);
  useEffect(() => setPreview(null), [html]);
  useEffect(() => {
    if (!preview) return;
    const anchor=preview.anchor;
    const update = () => {
      const box=anchor.getBoundingClientRect();
      if (!anchor.isConnected || document.activeElement!==anchor || box.bottom<=0 || box.top>=window.innerHeight) {
        setPreview(null); return;
      }
      const position=previewPosition(anchor);
      setPreview(old=>old?.anchor===anchor ? {...old,...position} : old);
    };
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => { window.removeEventListener("scroll", update, true); window.removeEventListener("resize", update); };
  }, [preview?.anchor]);
  useLayoutEffect(() => {
    if (!preview) return;
    const position=previewPosition(preview.anchor);
    setPreview(old=>old?.anchor===preview.anchor ? {...old,...position} : old);
  }, [preview?.anchor]);
  const showPreview = (target: HTMLElement) => {
    if (preview?.anchor === target) return;
    const ids = (target.dataset.evidenceIds ?? "").split(",").filter(id => lookup.has(id));
    if (!ids.length) return;
    setPreview({anchor: target, ids, ...previewPosition(target)});
  };
  const group = preview?.ids.map(id => lookup.get(id)).filter((cite): cite is Evidence => !!cite) ?? [];
  const locations = [...new Set(group.map(citationLocator))];
  return <><section className="rendered-rich-text wiki-answer-markdown" aria-label="完整综合答复"
    onPointerOver={event => {
      if (event.pointerType === "touch") return;
      const target = (event.target as Element).closest<HTMLElement>("[data-evidence-id]");
      if (target && event.currentTarget.contains(target)) showPreview(target);
    }}
    onPointerOut={event => {
      const target = (event.target as Element).closest<HTMLElement>("[data-evidence-id]");
      const related = event.relatedTarget instanceof Node ? event.relatedTarget : null;
      if (target && !target.contains(related) && document.activeElement !== target) setPreview(null);
    }}
    onFocusCapture={event => {
      const target = (event.target as Element).closest<HTMLElement>("[data-evidence-id]");
      if (target) showPreview(target);
    }}
    onBlurCapture={() => setPreview(null)}
    onKeyDown={event => { if (event.key === "Escape") setPreview(null); }}
    onClick={event => {
      const target = (event.target as Element).closest<HTMLElement>("[data-evidence-id]");
      if (!target || !event.currentTarget.contains(target)) return;
      const citation = lookup.get(target.dataset.evidenceId ?? "");
      if (citation) { event.preventDefault(); setPreview(null); app.openVersion(citation.version_id, citation.block_id); }
    }} dangerouslySetInnerHTML={markup} />
    {typeof document !== "undefined" && createPortal(<div id={tooltipId} role="tooltip" className="fundkb-citation-preview"
      hidden={!preview || !group.length} style={preview ? {left: preview.left, top: preview.top, width: preview.width,
        transform: preview.above ? "translateY(-100%)" : undefined} : undefined}>
      {group.length > 0 && <><small>文献来源</small><strong>{group[0].source_title || "来源文献（标题未提供）"}</strong>
        <p>{locations.slice(0, 3).join(" · ")}{locations.length > 3 ? ` · 等${locations.length}个位置` : ""}</p>
        <span>关联 {group.length} 处原文 · 点击打开准确出处</span></>}
    </div>, document.body)}</>;
}
