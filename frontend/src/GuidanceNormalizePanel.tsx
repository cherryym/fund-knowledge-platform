import { useEffect, useMemo, useRef, useState } from "react";
import { get, post } from "./api";
import { ModelPicker, type ModelSelection } from "./ModelPicker";
import type { Job, Resource, Version } from "./types";
import { Badge, ErrorBox, Field, Notice, useApp, useTask } from "./ui";
import { guidanceBlocks, guidanceText, isGuidanceCategory, type GuidanceApplied, type GuidanceNormalization } from "./documents.types";
import { richHtml } from "./richText";
import "./document-management.css";

export type GuidanceNormalizePanelProps = {resource: Resource; version: Version;
  onApplied?: (result: GuidanceApplied) => void; dirtyChange?: (dirty: boolean) => void};

export function GuidanceNormalizePanel({resource, version, onApplied, dirtyChange}: GuidanceNormalizePanelProps) {
  const app = useApp(), task = useTask();
  const [model, setModel] = useState<ModelSelection | null>(null);
  const [consent, setConsent] = useState(false);
  const [record, setRecord] = useState<GuidanceNormalization>();
  const [jobId, setJobId] = useState("");
  const [title, setTitle] = useState("");
  const [draftText, setDraftText] = useState("");
  const [editing, setEditing] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [reviewNote, setReviewNote] = useState("");
  const [pollError, setPollError] = useState<Error>();
  const [retry, setRetry] = useState(0);
  const hydrated = useRef("");
  const current = useRef(`${resource.id}:${version.id}`);
  current.current = `${resource.id}:${version.id}`;
  const storageKey = `fkb:guidance:${app.me.id}:${resource.id}:${version.id}`;
  const eligible = resource.kind === "document" && isGuidanceCategory(resource.category);
  useEffect(() => {
    setRecord(undefined); setModel(null); setConsent(false); setTitle(""); setDraftText(""); setEditing(false); setReviewed(false);
    setReviewNote(""); setPollError(undefined); hydrated.current = "";
    try { setJobId(sessionStorage.getItem(storageKey) ?? ""); } catch { setJobId(""); }
  }, [storageKey]);
  useEffect(() => {
    if (!jobId) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const value = await get<GuidanceNormalization>(`/document-normalizations/${jobId}`, abort.signal);
        if (abort.signal.aborted) return;
        setRecord(value); setPollError(undefined);
        if (value.suggestion && hydrated.current !== jobId) {
          hydrated.current = jobId; setTitle(value.suggestion.title);
          setDraftText(guidanceText(value.suggestion.blocks));
        }
        if (!value.suggestion && ["QUEUED", "RUNNING"].includes(value.job.state)) timer = setTimeout(() => void poll(), 1800);
      } catch (error) {
        if (!abort.signal.aborted) setPollError(error as Error);
      }
    };
    void poll();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [jobId, retry]);
  const remember = (id: string) => {
    setJobId(id);
    try { if (id) sessionStorage.setItem(storageKey, id); else sessionStorage.removeItem(storageKey); } catch { /* Only the task ID is cached. */ }
  };
  const changed = () => { setReviewed(false); dirtyChange?.(true); };
  const parsed = useMemo(() => {
    try { return {blocks: guidanceBlocks(draftText, record?.suggestion?.evidence ?? []), error: undefined}; }
    catch (error) { return {blocks: [], error: error as Error}; }
  }, [draftText, record?.suggestion]);
  const preview = parsed.blocks.map(block => `${block.markdown}\n\n*来源：${block.evidence_ids.join("、")}*`).join("\n\n");
  if (!eligible) return <Notice>内部指引规范化：请先将文档归入「内部指引」或其子分类，再从来源版本发起。</Notice>;
  const generating = jobId && (!record || ["QUEUED", "RUNNING"].includes(record.job.state)) && !record?.suggestion;
  return <section className="guidance-normalize-panel" aria-label="内部指引AI规范化">
    <div><h3>内部指引 AI 规范化</h3><p>基于当前原件版本生成建议，预览编辑后采纳为关联知识草稿。</p></div>
    <Notice>原文与上传原件保留。AI 只提供待复核表述，不确认法规依据或业务有效性；采纳后仍需独立审核发布。</Notice>
    {!jobId && <div className="form-stack">
      <ModelPicker value={model} onChange={setModel} spaceId={resource.space_id} required requireTransfer disabled={task.busy}/>
      <label className="checkbox-label"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)}/>
        我同意将本版本正文发送到所选模型，生成待审核建议</label>
      <button type="button" className="primary" disabled={task.busy || !model || !consent || !version.blocks.length} onClick={() => void task.run(async () => {
        const identity = current.current;
        const job = await post<Job>(`/documents/${resource.id}/normalizations`, {
          source_version_id: version.id, model_selection: model, consent: true,
        }, version.revision);
        if (identity !== current.current) return;
        remember(job.id); app.notify("已创建规范化任务，等待模型建议");
      })}>生成规范化建议</button>
      {!version.blocks.length && <Notice>本版本尚无可用解析内容。</Notice>}
    </div>}
    {record && <p role="status">规范化任务 <Badge value={record.job.state}/> · {record.job.stage}
      {record.job.error_code && <span className="text-red"> · {record.job.error_code}</span>}</p>}
    {generating && <p aria-live="polite">正在等待规范化结果…</p>}
    <ErrorBox error={pollError} retry={() => setRetry(value => value + 1)}/>
    {record?.suggestion && !pollError && <div className="form-stack">
      <details><summary>原文、引用和版本指纹</summary>
        <p>来源版本：{record.suggestion.source_snapshot.version_id}</p>
        <p className="guidance-hash">原件 SHA-256：{record.suggestion.source_snapshot.blob_sha256}</p>
        <p className="guidance-hash">版本 SHA-256：{record.suggestion.source_snapshot.content_sha256}</p>
        <p>生成模型：{record.suggestion.model.model_id} · 连接版本 {record.suggestion.model.revision}</p>
        {!record.suggestion.source_snapshot.source_verified && <Notice>本来源尚未核验，不构成已生效规则。</Notice>}
        {record.suggestion.evidence.map(evidence => <article className="guidance-source" key={evidence.id}>
          <strong>{evidence.id}</strong><pre>{evidence.text}</pre>
          <button type="button" onClick={() => app.openVersion(evidence.version_id, evidence.block_id)}>定位原文</button>
        </article>)}
      </details>
      <details><summary>模型原始建议（保留记录）</summary>
        <h4>{record.suggestion.title}</h4><div className="guidance-continuous-preview" dangerouslySetInnerHTML={{__html: richHtml(record.suggestion.blocks.map(block => block.markdown).join("\n\n"), "markdown")}}/>
      </details>
      {record.suggestion.gaps.length > 0 && <Notice>待核对事项：{record.suggestion.gaps.join("；")}</Notice>}
      <fieldset disabled={task.busy || Boolean(record.applied)} className="guidance-review-fields form-stack">
        <Field label="采纳稿标题"><input maxLength={300} value={title} onChange={e => {setTitle(e.target.value); changed();}}/></Field>
        <div className="inline-actions"><strong>采纳稿预览</strong><button type="button" aria-expanded={editing} onClick={() => setEditing(value => !value)}>{editing ? "收起全文编辑" : "编辑全文"}</button></div>
        {editing && <Field label="全文编辑（Markdown）"><textarea className="guidance-full-editor" rows={16} maxLength={40000}
          value={draftText} onChange={e => {setDraftText(e.target.value); changed();}}/>
          <small>每个引用段末尾保留独立一行的〔来源：S1〕，多来源用逗号分隔；可调整结构和措辞。</small>
        </Field>}
        {parsed.error ? <ErrorBox error={parsed.error}/> : <article className="guidance-continuous-preview" aria-label="规范化采纳稿预览"
          dangerouslySetInnerHTML={{__html: richHtml(preview, "markdown")}}/>}
        <Field label="审阅记录"><textarea value={reviewNote} maxLength={2000} placeholder="记录表述调整、已核对内容和仍待复核事项"
          onChange={e => {setReviewNote(e.target.value); changed();}}/></Field>
        <label className="checkbox-label"><input type="checkbox" checked={reviewed} onChange={e => setReviewed(e.target.checked)}/>
          我已预览原文和采纳稿，并核对引用，提交为待独立复核草稿</label>
        <button type="button" className="primary" disabled={!reviewed || !title.trim() || !reviewNote.trim() || Boolean(parsed.error)}
          onClick={() => void task.run(async () => {
            const result = await post<GuidanceApplied>(`/document-normalizations/${jobId}/apply`, {
              reviewed: true, title, blocks: parsed.blocks, review_note: reviewNote,
            }, record.revision);
            setRecord({...record, applied: result}); dirtyChange?.(false); app.bump();
            app.notify("已新建关联草稿，原件保留，待独立复核"); onApplied?.(result);
          })}>采纳为关联草稿</button>
      </fieldset>
    </div>}
    {record?.applied && !pollError && <Notice>已采纳为独立草稿。<button type="button" onClick={() => app.openVersion(record.applied!.version_id)}>打开关联草稿</button></Notice>}
    {jobId && ((!generating && !record?.suggestion) || pollError) && <button type="button" onClick={() => {
      remember(""); setRecord(undefined); setPollError(undefined); setConsent(false); setReviewed(false); hydrated.current = "";
    }}>重新选择模型与来源生成</button>}
    <ErrorBox error={task.error}/>
  </section>;
}
