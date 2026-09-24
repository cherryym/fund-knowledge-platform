import type { Evidence } from "./types";

/** Presentation only: no source lookup, guesses, new references or version changes. */
export function citationLookup(citations: Evidence[]): Map<string, Evidence> {
  const result = new Map<string, Evidence>(), ambiguous = new Set<string>();
  for (const cite of citations) {
    if (!/^E[1-9]\d*$/.test(cite.id) || ambiguous.has(cite.id)) continue;
    const old = result.get(cite.id);
    if (old && (old.resource_id !== cite.resource_id || old.version_id !== cite.version_id ||
      old.block_id !== cite.block_id || old.content_sha256 !== cite.content_sha256 || old.source_title !== cite.source_title)) {
      result.delete(cite.id); ambiguous.add(cite.id);
    } else if (!old) result.set(cite.id, cite);
  }
  return result;
}

export function sourceLabel(title: string): string {
  let label = typeof title === "string" ? title.trim().replace(/\s+/g, " ") : "";
  if (!label) return "来源文献";
  // Prefer the actual attachment name, not another document in a joint notice.
  const attachment = /[—–-]\s*附件\s*[：:]/.exec(label);
  if (attachment) label = label.slice(attachment.index + attachment[0].length).trim();
  label = label.replace(/^(?:附件\s*[\d一二三四五六七八九十]*\s*[：:]\s*)+/, "");
  const names = [...label.matchAll(/《([^《》]+)》/g)];
  if (names.length === 1) label = names[0][1];
  return label.replace(/\.(?:docx?|pdf|xlsx?|pptx?|md|txt|html?)$/i, "").trim() || "来源文献";
}

export function citationLocator(cite: Evidence): string {
  if (typeof cite.locator?.label === "string" && cite.locator.label.trim()) return cite.locator.label.trim();
  const page = cite.locator?.source_page;
  return typeof page === "number" && Number.isSafeInteger(page) && page > 0 ? `第${page}页` : "位置未提供";
}

export function citationGroups(ids: string[], lookup: Map<string, Evidence>): Evidence[][] {
  const groups = new Map<string, Evidence[]>();
  for (const id of ids) {
    const cite = lookup.get(id);
    if (!cite) continue;
    const key = JSON.stringify([cite.resource_id, cite.version_id]);
    const group = groups.get(key);
    if (group) group.push(cite); else groups.set(key, [cite]);
  }
  return [...groups.values()];
}

export function citationDescription(group: Evidence[]): string {
  const locations = [...new Set(group.map(citationLocator))];
  return `${group[0].source_title || "来源文献（标题未提供）"}\n${locations.join(" · ")}\n关联 ${group.length} 处原文`;
}
