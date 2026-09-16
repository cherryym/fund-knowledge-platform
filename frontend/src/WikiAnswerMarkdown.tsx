import { useMemo } from "react";
import MarkdownIt from "markdown-it";
import { safeLink, escapeText } from "./richText";
import type { Evidence } from "./types";
import { useApp } from "./ui";

const md = new MarkdownIt({ html: false, breaks: true, linkify: false, typographer: false }).disable("image");
md.validateLink = safeLink;
function references(label: string, citations: Set<string>) {
  const ids = new Set<string>();
  const ranges = [...label.matchAll(/E(\d+)\s*[-‐‑–—－~～至]\s*E?(\d+)/g)];
  for (const range of ranges) {
    const first = Number(range[1]), last = Number(range[2]);
    if (!Number.isSafeInteger(first) || !Number.isSafeInteger(last) || first > last) continue;
    for (const id of citations) {
      const value = Number(id.slice(1));
      if (value >= first && value <= last) ids.add(id);
    }
  }
  const singles = label.replace(/E\d+\s*[-‐‑–—－~～至]\s*E?\d+/g, " ");
  for (const id of singles.match(/E\d+/g) ?? []) if (citations.has(id)) ids.add(id);
  return [...ids].sort((a,b)=>Number(a.slice(1))-Number(b.slice(1)));
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
  for (const match of text.matchAll(/\bE\d+(?:\s*[-‐‑–—－~～至]\s*E?\d+)?\b/g)) {
    html += escapeText(text.slice(cursor, match.index));
    const ids = references(match[0], env.citations);
    const label = escapeText(match[0]);
    html += ids.length ? `<button type="button" class="citation-chip" data-evidence-id="${ids[0]}" data-evidence-ids="${ids.join(',')}" aria-label="查看来源 ${label}" title="已关联 ${ids.length} 个原文段落">${label}</button>` : label;
    cursor = match.index! + match[0].length;
  }
  return html + escapeText(text.slice(cursor));
};
md.renderer.rules.link_open = (tokens, index, options, _env, renderer) => {
  tokens[index].attrSet("target", "_blank");
  tokens[index].attrSet("rel", "noopener noreferrer");
  return renderer.renderToken(tokens, index, options);
};

export function wikiAnswerHtml(text: string, citations: Evidence[]) {
  return md.render(text, { citations: new Set(citations.map(c => c.id)) });
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
  const html = useMemo(() => wikiAnswerHtml(text, citations), [text, citations]);
  return <section className="rendered-rich-text wiki-answer-markdown" aria-label="完整综合答复"
    onClick={event => {
      const target = (event.target as Element).closest<HTMLElement>("[data-evidence-id]");
      if (!target) return;
      const citation = citations.find(c => c.id === target.dataset.evidenceId);
      if (citation) { event.preventDefault(); app.openVersion(citation.version_id, citation.block_id); }
    }} dangerouslySetInnerHTML={{ __html: html }} />;
}
