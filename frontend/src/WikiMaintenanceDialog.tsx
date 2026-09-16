import { useEffect, useState } from "react";
import { ArrowClockwise, ArrowSquareOut, Check, Link, MagnifyingGlass } from "@phosphor-icons/react";
import { api, get, query } from "./api";
import { BlockView } from "./BlockView";
import type { Block } from "./types";
import { Empty, ErrorBox, Field, Loading, Modal, Notice, useApp, useLoad, useTask } from "./ui";
import "./wiki-maintenance.css";

type EntryOption = {id: string; name: string; version_id?: string | null};
type Entry = { resource_id: string; title: string; aliases: string[]; canonical_key: string | null;
  canonical_resource_id: string | null; canonical_available: boolean; is_canonical: boolean; can_edit: boolean };
type Kind = "REVISION" | "CONSOLIDATION" | "CONFLICT";
type Proposal = { id: string; kind: Kind; status: "PROPOSED" | "ACCEPTED" | "REJECTED"; reason: string;
  entries: {resource_id: string; title: string; version_id: string}[]; target_resource_id: string | null;
  origin: string; snapshot_current: boolean; can_review: boolean; conflict_state?: string;
  source_changes: {old_version_id: string; new_version_id: string}[];
  has_compiled_candidate?: boolean; application_blockers?: string[];
  detection?: {methods: string[]; title_similarity: number | null; title_similarity_threshold: number};
  compiled_revision?: {candidate: {title: string; blocks: Block[]}} | null;
  review?: {comment: string}; resolution?: {comment: string};
  result?: {new_version_id?: string; requires_edit?: boolean; action?: string} };
const kindLabels = {REVISION: "修订", CONSOLIDATION: "导航归并", CONFLICT: "冲突"};
const statusLabels = {PROPOSED: "待审阅", ACCEPTED: "已接受", REJECTED: "已拒绝"};
const base = "/wiki/maintenance";

export function WikiMaintenanceDialog({close, pages, initialId}: {
  close: () => void; pages: EntryOption[]; initialId?: string;
}) {
  const app = useApp();
  const [tab, setTab] = useState<"entries" | "proposals">("entries");
  const [entryId, setEntryId] = useState(initialId || pages[0]?.id || "");
  const [entrySearch, setEntrySearch] = useState("");
  const [aliases, setAliases] = useState("");
  const [canonicalKey, setCanonicalKey] = useState("");
  const [reason, setReason] = useState("");
  const [kind, setKind] = useState<Kind>("REVISION");
  const [otherId, setOtherId] = useState("");
  const [targetId, setTargetId] = useState(entryId);
  const [proposalId, setProposalId] = useState("");
  const [comment, setComment] = useState("");
  const [filter, setFilter] = useState("");
  const [epoch, setEpoch] = useState(0);
  const [scanSummary, setScanSummary] = useState("");
  const task = useTask();
  const canEdit = app.space.roles?.includes("editor");
  const entryPath = `/wiki/entries/${encodeURIComponent(entryId)}/maintenance`;
  const entry = useLoad(signal => entryId ? get<Entry>(entryPath, signal) : Promise.resolve(null), [entryId, epoch]);
  const proposals = useLoad(signal => get<{items: Proposal[]; total: number}>(`${base}/proposals?${query({
    space_id: app.space.id, status: filter})}`, signal), [app.space.id, filter, epoch]);
  const detailPath = `${base}/proposals/${encodeURIComponent(proposalId)}`;
  const detailReadPath = `${detailPath}?include_candidate=true`;
  const detail = useLoad(signal => proposalId ? get<Proposal>(detailReadPath, signal) : Promise.resolve(null), [proposalId, epoch]);
  useEffect(() => {
    if (!entry.data) return;
    setAliases(entry.data.aliases.join("\n")); setCanonicalKey(entry.data.canonical_key || ""); setReason("");
  }, [entry.data]);
  useEffect(() => {setOtherId(""); setTargetId(entryId); setComment("");}, [entryId]);
  useEffect(() => {setComment("");}, [proposalId]);
  const choices = pages.filter(p => !entrySearch || p.name.toLocaleLowerCase().includes(entrySearch.toLocaleLowerCase()) || p.id === entryId);
  function changed(message: string) {
    setEpoch(value => value + 1); app.bump(); app.notify(message);
  }
  function review(decision: "ACCEPT" | "REJECT") {
    task.run(async () => {
      if (!detail.data || detail.data.id !== proposalId || detail.loading || !comment.trim()) throw new Error("请先读取当前建议并填写审阅说明。");
      await api<Proposal>(`${detailPath}/review`, {method: "POST", etagPath: detailReadPath, body: {decision, comment}});
      changed(decision === "ACCEPT" ? "维护建议已接受；没有自动发布或删除原条目。" : "维护建议已拒绝，原条目不变。");
    });
  }
  return <Modal title="知识维护" close={close} wide busy={task.busy}>
    <div className="wiki-maintenance modal-body">
      <nav className="wiki-maint-tabs" aria-label="知识维护视图">
        <button type="button" aria-pressed={tab === "entries"} onClick={() => setTab("entries")}>主条目与别名</button>
        <button type="button" aria-pressed={tab === "proposals"} onClick={() => setTab("proposals")}>维护建议 {proposals.data?.total ?? ""}</button>
      </nav>
      <Notice>归并只调整主条目导航，不删除正文。修订建立独立草稿；冲突需登记处理说明。所有原文、旧版本和审核状态均保留。</Notice>
      {tab === "entries" ? <div className="wiki-maint-grid">
        <section className="form-stack">
          <Field label="查找知识条目"><input value={entrySearch} placeholder="按标题查找" onChange={e => setEntrySearch(e.target.value)} /></Field>
          <Field label="当前知识条目"><select aria-label="当前知识条目" value={entryId} onChange={e => setEntryId(e.target.value)}>
            {!pages.length && <option value="">暂无可维护知识页</option>}
            {choices.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select></Field>
          {entry.loading && <Loading label="读取条目最新状态…" />}<ErrorBox error={entry.error} retry={entry.reload} />
          {entry.data?.resource_id === entryId && !entry.loading && <form className="form-stack" onSubmit={e => {
            e.preventDefault(); task.run(async () => {
              await api(entryPath, {method: "PUT", etagPath: entryPath, body: {
                canonical_key: canonicalKey.trim() || null,
                aliases: aliases.split("\n").map(value => value.trim()).filter(Boolean), reason,
              }});
              changed("主条目标识与别名已保存，正文未改动。");
            });
          }}>
            <p className="wiki-maint-state">{entry.data.is_canonical ? "独立主条目" : entry.data.canonical_available
              ? `导航主条目：${pages.find(p => p.id === entry.data!.canonical_resource_id)?.name || "当前可访问的主条目"}`
              : "原导航主条目当前不可访问；本条目仍保留。"}</p>
            <Field label="主条目标识" hint="建议使用稳定的业务名称，供别名解析与去重核对。"><input aria-label="主条目标识"
              maxLength={300} value={canonicalKey} onChange={e => setCanonicalKey(e.target.value)} disabled={!entry.data.can_edit || task.busy} /></Field>
            <Field label="别名" hint="每行一个名称。重名会提示先审阅归并建议，不静默绑定其他知识。"><textarea aria-label="条目别名"
              value={aliases} onChange={e => setAliases(e.target.value)} rows={5} disabled={!entry.data.can_edit || task.busy} /></Field>
            <Field label="维护原因"><textarea aria-label="元数据维护原因" required maxLength={2000} rows={2}
              value={reason} onChange={e => setReason(e.target.value)} disabled={!entry.data.can_edit || task.busy} /></Field>
            <button className="primary" type="submit" disabled={task.busy || !entry.data.can_edit || !reason.trim()}><Check />保存标识与别名</button>
          </form>}
        </section>
        <section className="form-stack wiki-maint-propose"><h3>提出维护建议</h3>
          <p>选择对象与原因，再单独审阅。本次提交不会直接改动知识正文。</p>
          <ProposalForm entryId={entryId} pages={pages} canEdit={Boolean(entry.data?.resource_id === entryId && !entry.loading && entry.data?.can_edit)} busy={task.busy}
            kind={kind} setKind={setKind} otherId={otherId} setOtherId={setOtherId} targetId={targetId} setTargetId={setTargetId}
            submit={(draftReason, draftTitle) => task.run(async () => {
              const result = await api<Proposal>(`${base}/proposals`, {method: "POST", body: {
                space_id: app.space.id, kind, resource_ids: kind === "REVISION" ? [entryId] : [entryId, otherId],
                reason: draftReason, ...(kind === "CONSOLIDATION" ? {target_resource_id: targetId} : {}),
                ...(kind === "REVISION" && draftTitle.trim() ? {draft_title: draftTitle.trim()} : {}),
              }});
              setProposalId(result.id); setTab("proposals"); changed("建议已登记，等待审阅。");
            })} />
        </section>
      </div> : <div className="wiki-maint-proposals">
        <div className="wiki-maint-toolbar">
          <select aria-label="建议状态" value={filter} onChange={e => {setFilter(e.target.value); setProposalId("");}}>
            <option value="">全部状态</option>{Object.entries(statusLabels).map(([key, value]) => <option key={key} value={key}>{value}</option>)}
          </select>
          <button type="button" disabled={task.busy || !canEdit} onClick={() => task.run(async () => {
            const result = await api<{resource_count: number; created_count: number; existing_count: number}>(`${base}/scans`, {
              method: "POST", body: {space_id: app.space.id, kinds: ["duplicates", "source_changes"]}});
            setScanSummary(`已检查 ${result.resource_count} 个有编辑权限的条目；新增 ${result.created_count} 条建议，已有 ${result.existing_count} 条。`);
            setEpoch(value => value + 1);
          })}><MagnifyingGlass />{task.busy ? "正在处理…" : "扫描重复与来源更新"}</button>
          <button type="button" disabled={task.busy} onClick={() => setEpoch(value => value + 1)}><ArrowClockwise />刷新</button>
        </div>
        {scanSummary && <p role="status">{scanSummary}扫描候选未经模型或专业核验，没有自动改写。</p>}
        <div className="wiki-maint-grid">
          <section>{proposals.loading && <Loading label="读取维护建议…" />}<ErrorBox error={proposals.error} retry={proposals.reload} />
            {proposals.data?.items.length === 0 && <Empty title="暂无维护建议" detail="可以扫描当前知识库，或为选中条目提出修订、归并和冲突建议。" />}
            <ul className="wiki-maint-list">{proposals.data?.items.map(p => <li key={p.id}>
              <button type="button" className={p.id === proposalId ? "selected" : ""} onClick={() => setProposalId(p.id)}>
                <span>{kindLabels[p.kind]} · {statusLabels[p.status]}</span><strong>{p.entries.map(e => e.title).join(" / ")}</strong>
                <small>{p.reason}</small>{!p.snapshot_current && <small>输入已变化，接受前需重新提出建议</small>}
              </button>
            </li>)}</ul>
          </section>
          <section className="form-stack wiki-maint-propose">
            {detail.loading && <Loading label="读取建议快照…" />}<ErrorBox error={detail.error} retry={detail.reload} />
            {!proposalId && <Empty title="选择一条建议" detail="查看来源差异、受影响条目及审阅操作。" />}
            {detail.data?.id === proposalId && !detail.loading && <><h3>{kindLabels[detail.data.kind]} · {statusLabels[detail.data.status]}</h3>
              <p>{detail.data.reason}</p><small>{detail.data.origin === "SCAN" ? "自动扫描候选，尚未核验" : "已登记建议，尚不代表专业确认"}</small>
              {detail.data.detection && <p>候选信号：{detail.data.detection.methods.map(method => ({
                NAME_OVERLAP:"名称或别名重合", TITLE_SIMILARITY_SAME_CATEGORY_TYPE:"同分类、同类型标题相似",
                EXACT_BODY:"正文相同", EXACT_BODY_SAME_CATEGORY_TYPE:"同分类、同类型正文相同",
              }[method] || method)).join("、")}。{detail.data.detection.title_similarity !== null &&
                `标题相似度 ${(detail.data.detection.title_similarity * 100).toFixed(1)}%，候选阈值 ${(detail.data.detection.title_similarity_threshold * 100).toFixed(0)}%。`}
                这些信号不等于语义完全相同。</p>}
              {detail.data.target_resource_id && <p>导航主条目：{detail.data.entries.find(e => e.resource_id === detail.data!.target_resource_id)?.title}</p>}
              <ul>{detail.data.entries.map(e => <li key={e.resource_id}><button type="button" onClick={() => app.openVersion(e.version_id)}>
                <Link />{e.title}</button></li>)}</ul>
              {detail.data.source_changes.map((s, i) => <div className="wiki-maint-source-change" key={`${s.old_version_id}:${s.new_version_id}`}>
                <span>来源更新 {i + 1}</span><button type="button" onClick={() => app.openVersion(s.old_version_id)}>查看旧来源</button>
                <button type="button" onClick={() => app.openVersion(s.new_version_id)}>查看新来源</button></div>)}
              {!detail.data.snapshot_current && <Notice>条目、来源或权限已变化。不会把过期建议直接应用到新内容；请重新扫描或提出建议。</Notice>}
              {Boolean(detail.data.application_blockers?.length) && <Notice>候选来源尚未满足当前知识条目的引用条件。完整候选稿仍可查看；需要先处理来源审核或重新按已授权来源构建，不能直接应用。</Notice>}
              {detail.data.compiled_revision && <details className="wiki-maint-candidate"><summary>预览完整修订候选稿 · {detail.data.compiled_revision.candidate.blocks.length} 段</summary>
                <h3>{detail.data.compiled_revision.candidate.title}</h3>
                {detail.data.compiled_revision.candidate.blocks.map(block => <BlockView key={block.block_id} block={block} />)}
              </details>}
              {detail.data.review && <p>审阅说明：{detail.data.review.comment}</p>}
              {detail.data.resolution && <p>冲突处理说明：{detail.data.resolution.comment}</p>}
              {detail.data.result?.new_version_id && <><Notice>已创建修订工作稿，需编辑核对后再走审核流程；旧版保留。</Notice>
                <button type="button" onClick={() => app.openVersion(detail.data!.result!.new_version_id!)}><ArrowSquareOut />打开修订草稿</button></>}
              {detail.data.can_review && (detail.data.status === "PROPOSED" || detail.data.conflict_state === "OPEN") && <>
                <Field label="审阅或处理说明"><textarea aria-label="维护审阅说明" rows={3} required maxLength={2000}
                  value={comment} onChange={e => setComment(e.target.value)} disabled={task.busy} /></Field>
                {detail.data.status === "PROPOSED" ? <div className="wiki-maint-toolbar">
                  <button type="button" disabled={task.busy || !comment.trim()} onClick={() => review("REJECT")}>拒绝建议</button>
                  <button type="button" className="primary" disabled={task.busy || !comment.trim() || !detail.data.snapshot_current || Boolean(detail.data.application_blockers?.length)} onClick={() => review("ACCEPT")}>
                    {detail.data.kind === "REVISION" ? "接受并创建修订草稿" : detail.data.kind === "CONSOLIDATION" ? "接受导航归并（保留原文）" : "接受并登记冲突"}</button>
                </div> : <button type="button" className="primary" disabled={task.busy || !comment.trim()} onClick={() => task.run(async () => {
                  await api(`${detailPath}/resolve`, {method: "POST", etagPath: detailReadPath, body: {comment}});
                  changed("冲突处理说明已记录；知识效力和发布状态未变。");
                })}>登记处理说明并关闭冲突</button>}
              </>}
            </>}
          </section>
        </div>
      </div>}
      <ErrorBox error={task.error} />
    </div>
    <footer className="modal-foot"><button type="button" onClick={close} disabled={task.busy}>返回知识空间</button></footer>
  </Modal>;
}

function ProposalForm({entryId, pages, canEdit, busy, kind, setKind, otherId, setOtherId, targetId, setTargetId, submit}: {
  entryId: string; pages: EntryOption[]; canEdit: boolean; busy: boolean; kind: Kind; setKind: (k: Kind) => void;
  otherId: string; setOtherId: (id: string) => void; targetId: string; setTargetId: (id: string) => void;
  submit: (reason: string, title: string) => void;
}) {
  const [reason, setReason] = useState(""); const [title, setTitle] = useState("");
  return <form className="form-stack" onSubmit={e => {e.preventDefault(); submit(reason, title);}}>
    <Field label="建议类型"><select aria-label="维护建议类型" value={kind} onChange={e => setKind(e.target.value as Kind)}>
      {Object.entries(kindLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
    </select></Field>
    {kind !== "REVISION" && <Field label="关联知识条目"><select aria-label="关联知识条目" required value={otherId} onChange={e => {
      setOtherId(e.target.value); setTargetId(entryId);
    }}><option value="">请选择另一个条目</option>{pages.filter(p => p.id !== entryId).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>}
    {kind === "CONSOLIDATION" && <Field label="导航主条目"><select aria-label="归并导航主条目" value={targetId} onChange={e => setTargetId(e.target.value)}>
      {pages.filter(p => [entryId, otherId].includes(p.id)).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>}
    {kind === "REVISION" && <Field label="修订草稿标题（选填）"><input value={title} maxLength={300} onChange={e => setTitle(e.target.value)} /></Field>}
    <Field label="建议原因与待处理事项"><textarea aria-label="维护建议原因" required maxLength={2000} rows={5}
      value={reason} onChange={e => setReason(e.target.value)} /></Field>
    <button type="submit" disabled={busy || !canEdit || !reason.trim() || !entryId || (kind !== "REVISION" && !otherId)}>登记建议，待审阅</button>
  </form>;
}
