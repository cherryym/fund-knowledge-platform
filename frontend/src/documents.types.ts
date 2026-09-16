import type { Job } from "./types";

export type DocumentCategory = { path: string; name: string; parent_path: string | null;
  count: number; direct_count: number; protected: boolean };
export type DocumentTaxonomy = { space_id: string; revision: number; total_visible: number;
  can_manage: boolean; categories: DocumentCategory[] };
export type GuidanceBlock = { markdown: string; evidence_ids: string[] };
export type GuidanceEvidence = { id: string; version_id: string; block_id: string; text: string;
  content_sha256: string; char_start: number; char_end: number; locator: Record<string, unknown> };
export type GuidanceApplied = { resource_id: string; version_id: string; state: "DRAFT"; revision: number };
export type GuidanceSuggestion = { title: string; blocks: GuidanceBlock[]; gaps: string[];
  evidence: GuidanceEvidence[]; source_snapshot: { content_sha256: string; blob_sha256: string;
    source_verified: boolean; version_id: string };
  model: { model_id: string; provider_id: string; revision: number } };
export type GuidanceNormalization = { job: Job; revision: number; suggestion: GuidanceSuggestion | null;
  applied: GuidanceApplied | null };
export type DocumentMoveResult = { items: { id: string; revision: number; category: string }[];
  taxonomy_revision: number };
export type DocumentPurgeEligibility = { resource_id: string; eligible: boolean; expiry: string | null;
  reasons: { code: string; message: string }[]; checked_at: string; policy_status: string };

export const isGuidanceCategory = (category: string) => category === "内部指引" || category.startsWith("内部指引/");

export const guidanceText = (blocks: GuidanceBlock[]) => blocks.map(block => `${block.markdown}\n\n〔来源：${block.evidence_ids.join(",")}〕`).join("\n\n");
export function guidanceBlocks(text: string, evidence: GuidanceEvidence[]): GuidanceBlock[] {
  const rows: GuidanceBlock[] = [], known = new Set(evidence.map(row => row.id));
  const pattern = /^〔来源：([^\n]+)〕[ \t]*$/gm;
  let start = 0;
  for (const match of text.matchAll(pattern)) {
    const markdown = text.slice(start, match.index).trim();
    const evidence_ids = match[1].split(/[,，]/).map(id => id.trim());
    if (!markdown || markdown.length > 6000 || new Set(evidence_ids).size !== evidence_ids.length || evidence_ids.some(id => !known.has(id)))
      throw new Error("每个引用段需有1至6000字正文，来源标记只能包含本次提供的不重复编号。");
    rows.push({markdown, evidence_ids}); start = match.index! + match[0].length;
  }
  if (!rows.length || rows.length > 32 || text.slice(start).trim()) throw new Error("每个引用段末尾请保留独立一行的〔来源：S1〕标记，最多32个引用段。");
  return rows;
}
