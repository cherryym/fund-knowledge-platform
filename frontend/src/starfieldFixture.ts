import type { WikiGraphData } from "./knowledgeTypes";

export const STAR_FIXTURE_COUNTS = [7, 50, 200, 300, 591, 1000] as const;
export function fixtureCount(value: unknown): number {
  const count = Number(value);
  return STAR_FIXTURE_COUNTS.some(option => option === count) ? count : 300;
}

/** Deterministic, non-business graph. This module never performs IO or calls a model. */
export function createStarfieldFixture(value: unknown = 300): WikiGraphData {
  const count = fixtureCount(value);
  return {
    nodes: Array.from({ length: count }, (_, i) => ({ id: `perf-${i}`, label: `合成知识 ${String(i + 1).padStart(3, "0")}`,
      kind: "knowledge", knowledge_type: i % 4 === 0 ? "term" : "faq", category: "性能验收合成资料", state: "DRAFT", version_id: null })),
    edges: Array.from({ length: count * 2 }, (_, i) => ({ id: `edge-${i}`, source: `perf-${i % count}`,
      target: `perf-${(i % count + (i < count ? 1 : 17)) % count}`, type: "WIKI_LINK", origin: "wikilink", state: "ACTIVE" })),
    truncated: false, total_visible_nodes: count, matched_visible_nodes: count,
  };
}
