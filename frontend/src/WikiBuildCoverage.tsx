import { Notice } from "./ui";
import type { Job } from "./types";

type Batch = {scope_status: string; total_units: number; outside_scope_blocks: number;
  counts: Record<string, number>; units: {unit_id: string; status: string; source_blocks: {
    resource_id: string; version_id: string; block_id: string; char_start: number; char_end: number}[]}[]};
const labels: Record<string, string> = {INCORPORATED:"已纳入草稿", NOT_SENT:"尚未送入", SENT_NOT_INCORPORATED:"已送入但未纳入", REVIEW_REQUIRED:"仍需核验"};

export function WikiBuildCoverage({job, continueBatch}: {job: Job; continueBatch: () => void}) {
  const batch = job.result?.batch as Batch | undefined;
  if (!batch || !batch.counts || !Array.isArray(batch.units)) return null;
  const pending = (batch.counts.NOT_SENT || 0) + (batch.counts.SENT_NOT_INCORPORATED || 0);
  const revisions = Array.isArray(job.result?.revision_proposal_ids) ? job.result.revision_proposal_ids.length : 0;
  return <div className="wiki-build-coverage">
    <Notice>{batch.scope_status === "COMPLETE" ? "本次指定范围的输入已纳入草稿。" : "本次仍是部分编译，不能视为整篇或全库完成。"}
      输入覆盖不等于业务知识穷尽或专业核验；生成页保持待复核。</Notice>
    <p>范围内共 {batch.total_units} 个完整语义单元；{Object.entries(labels).map(([key,label]) => `${label} ${batch.counts[key] || 0}`).join(" · ")}。</p>
    {revisions > 0 && <Notice>{revisions} 个同名知识已有完整修订候选，未覆盖旧版。请在“知识维护 → 维护建议”预览并审阅。</Notice>}
    {batch.outside_scope_blocks > 0 && <p>另有 {batch.outside_scope_blocks} 个原文段落不在本次显式选区内。</p>}
    <details><summary>查看全量覆盖清单与原文定位</summary>
      <ol>{batch.units.map(unit => <li key={unit.unit_id}>
        <strong>{labels[unit.status] || unit.status}</strong>
        <ul>{unit.source_blocks.map(block => <li key={`${block.version_id}:${block.block_id}:${block.char_start}`}>
          版本 {block.version_id} · 原文段 {block.block_id} · 字符 {block.char_start}–{block.char_end}
        </li>)}</ul>
      </li>)}</ol>
    </details>
    {job.state === "SUCCEEDED" && pending > 0 && <button type="button" onClick={continueBatch}>继续整理剩余来源（重新确认后提交）</button>}
  </div>;
}
