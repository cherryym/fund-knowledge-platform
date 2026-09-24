import { useEffect, useId, useRef, useState } from "react";
import { api, get, query as queryString } from "./api";
import type { Job, Resource } from "./types";
import { Badge, ErrorBox, Notice, useApp, useLoad, useTask } from "./ui";
import { RetrievalProfilePicker, useRetrievalProfile, type FrozenRetrievalSelection } from "./retrievalProfiles";
import "./retrieval-panel.css";
import { LocalModelReadiness, type ModelRuntimeState } from "./LocalModelReadiness";
import { RetrievalDiagnostics, type CandidateTrace } from "./RetrievalDiagnostics";

type IndexOperation = Job & {force: boolean; counts_verified: boolean; created_at?: string; completed_at?: string;
  application_state?: string; matched_current_versions?: number; current_catalog_versions?: number};
type IndexSnapshot = {version: number; checked_at: string; state: string; generation: string | null;
  last_indexed_at: string | null; latest_job: IndexOperation | null; last_rebuild: IndexOperation | null};

export type RetrievalStatus = {
  retrieval_selection?: FrozenRetrievalSelection;
  space_id: string;
  mode: "wiki" | "hybrid";
  enabled: boolean;
  vector: {
    backend: string;
    mode: string;
    available: boolean;
    status: string;
    collection?: string;
    embedding_mode?: string;
    reranking?: { mode: string; model?: string; revision?: string; loaded?: boolean };
    model_runtime?: ModelRuntimeState;
  };
  embedding: { mode: string; model: string; dimensions: number; development_only: boolean };
  coverage: {
    catalog_pages: number;
    indexed_pages: number;
    dirty_pages: number;
    indexed_blocks: number;
    indexed_chunks: number;
  };
  permissions: { can_index: boolean };
  active_job: Job | null;
  index_snapshot?: IndexSnapshot;
  notes: string[];
};

type RetrievalHit = {
  page_id: string;
  resource_id: string;
  version_id: string;
  title: string;
  kind: string;
  score: number;
  channels: string[];
  vector_rank?: number;
  lexical_rank?: number;
  matched_block_ids?: string[];
};

export type RetrievalSearchResult = {
  retrieval_selection?: FrozenRetrievalSelection;
  query: string;
  scope: "reference";
  mode: "wiki" | "wiki_fallback" | "hybrid" | "hybrid_unit_rerank" | "universal_unit_retrieval";
  hits: RetrievalHit[];
  catalog_pages: number;
  indexed_catalog_pages: number;
  total_candidates: number;
  returned: number;
  warnings: string[];
  timing_ms: number;
  evidence_preview: false;
  reranking?: {mode: string; model?: string | null; input_units?: number; elapsed_ms?: number};
  retrieval_trace?: CandidateTrace;
};

const active = (job: Job | null) => !!job && ["QUEUED", "RUNNING"].includes(job.state);
const count = (value: unknown) =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
const countText = (value: unknown) => count(value)?.toLocaleString("zh-CN") ?? "未提供";
const timeText = (value: unknown) => {
  if (typeof value !== "string" || !value || !Number.isFinite(Date.parse(value))) return "未提供";
  return new Date(value).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false});
};
const rerankerStateText = (value: RetrievalStatus["vector"]["reranking"]) => {
  if (value?.mode === "disabled") return "未启用";
  if (value?.mode !== "local") return "接口未返回明确状态";
  if (!value.model) return "模型配置待核对";
  if (value.loaded === true) return "本地模型已加载";
  if (value.loaded === false) return "已配置 · 当前进程尚未加载";
  return "已配置 · 加载状态未知";
};
const modeText = (mode: string) => ["hybrid", "hybrid_unit_rerank", "universal_unit_retrieval"].includes(mode) ? "Wiki + RAG 混合检索"
  : mode === "wiki_fallback" ? "Wiki 检索（向量未就绪或已降级）" : "Wiki 检索";
const vectorModeText = (mode: string) => (({ remote: "独立 Qdrant 服务", local: "内嵌 Qdrant（本地）",
  disabled: "未启用" } as Record<string, string>)[mode] ?? mode) || "未提供";
const stageLabels: Record<string, string> = {
  queued: "等待处理", catalog: "读取目录", indexing: "建立索引", embedding: "计算嵌入",
  staging: "写入待激活索引", activating: "激活索引", completed: "处理结束",
  succeeded: "处理结束", failed: "处理失败", cancelled: "已取消", retry_wait: "等待后台重试",
};
const stageText = (value: unknown) => typeof value === "string" && value
  ? stageLabels[value.toLowerCase()] ?? value : "未提供";
const nonRetryable = new Set(["FILE_REJECTED", "UNSUPPORTED_FORMAT", "PASSWORD_REQUIRED", "LEGAL_HOLD"]);

function Metric({ label, value, name }: { label: string; value: unknown; name: string }) {
  return <div data-metric={name}><dt>{label}</dt><dd>{countText(value)}</dd></div>;
}

function IndexUpdate({snapshot}: {snapshot: IndexSnapshot}) {
  const rebuild = snapshot.last_rebuild;
  const verification = rebuild?.application_state;
  const verified = snapshot.state === "CURRENT_CATALOG_INDEXED" && verification === "CURRENT_GENERATION_VERIFIED"
    && rebuild?.counts_verified === true && rebuild.state === "SUCCEEDED" && !!snapshot.generation;
  const latestNeedsReview = snapshot.latest_job && snapshot.latest_job.counts_verified !== true;
  return <div className="retrieval-update" data-index-generation={snapshot.generation ?? "unknown"}>
    <p className="retrieval-update-title" role="status">{latestNeedsReview ? "最近作业未全部成功，当前索引状态请结合下方回执核对"
      : verified ? "重建已生效 · 当前目录已核验使用该次新索引"
      : snapshot.state === "CURRENT_CATALOG_INDEXED" ? "当前目录索引已核验"
      : snapshot.state === "SYNC_REQUIRED" ? "当前目录仍有待同步内容"
      : snapshot.state === "EMPTY_CATALOG" ? "当前目录没有可检索内容" : "当前索引状态尚不可用"}</p>
    <dl className="retrieval-config">
      <div><dt>当前索引代次</dt><dd><code title={snapshot.generation ?? undefined}>{snapshot.generation?.slice(0, 16) ?? "未核验"}</code></dd></div>
      <div><dt>最近索引写入（北京时间）</dt><dd>{timeText(snapshot.last_indexed_at)}</dd></div>
      <div><dt>最近完整重建结束（北京时间）</dt><dd>{timeText(rebuild?.completed_at)}</dd></div>
      <div><dt>状态核验时间（北京时间）</dt><dd>{timeText(snapshot.checked_at)}</dd></div>
    </dl>
    {rebuild && <p className="retrieval-meta">最近重建：<code>{rebuild.id}</code> · {
      ({CURRENT_GENERATION_VERIFIED: "当前目录的新代次已核验", OPERATION_NOT_FULLY_SUCCESSFUL: "任务未全部成功，请核对下方结果",
        CURRENT_STATE_UNVERIFIED: "当前代次尚未核验通过", NO_CURRENT_CONTENT: "当前目录无可索引内容",
        CURRENT_GENERATION_DIFFERS: "当前代次与该次重建不同，可能已有后续更新"} as Record<string, string>)[verification ?? ""] ?? "状态待核对"}</p>}
    <p className="retrieval-meta">索引代次来自当前可读版本的实际索引回执；同样的资料重新构建，数量可以不变，代次与写入时间会更新。最近作业仅显示当前账号、空间及所选方案的记录。</p>
  </div>;
}

function JobProgress({ job }: { job: Job }) {
  const result = job.result;
  const total = count(result?.total_versions);
  const completed = count(result?.completed_versions);
  const indexed = count(result?.indexed_versions);
  const skipped = count(result?.skipped_versions);
  const failed = count(result?.failed_versions);
  const ended = ["SUCCEEDED", "COMPLETED"].includes(job.state);
  const complete = ended && failed === 0 && total !== undefined && completed === total
    && indexed !== undefined && skipped !== undefined && indexed + skipped === total;
  const badge = ended && !complete ? (failed ? "FAILED" : "计数待核对") : job.state;
  return <div className="retrieval-job" aria-label="索引作业进度" aria-live="polite">
    <div className="retrieval-row">
      <Badge value={badge} />
      <span>阶段：{stageText(result?.phase ?? job.stage)}</span>
      {typeof result?.phase === "string" && result.phase && result.phase !== job.stage && <span>作业阶段：{stageText(job.stage)}</span>}
      <span>执行次数：{countText(job.attempts)}</span>
    </div>
    <dl className="retrieval-metrics retrieval-job-metrics">
      <Metric name="total_versions" label="版本总数" value={total} />
      <Metric name="completed_versions" label="已处理版本" value={completed} />
      <Metric name="indexed_versions" label="成功索引" value={indexed} />
      <Metric name="skipped_versions" label="跳过版本" value={skipped} />
      <Metric name="failed_versions" label="失败版本" value={failed} />
      <Metric name="pending_versions" label="待处理版本" value={total !== undefined && completed !== undefined && completed <= total ? total - completed : undefined} />
      <Metric name="job_blocks" label="已索引原文块" value={result?.indexed_blocks} />
      <Metric name="job_chunks" label="已索引子块" value={result?.indexed_chunks} />
    </dl>
    {job.state === "FAILED" && <p className="retrieval-failure" role="alert">索引作业失败，尚未全部成功。请处理错误后手动重试。</p>}
    {ended && !complete && <p className="retrieval-failure" role="alert">{failed
      ? "作业已结束，仍有失败版本，不能视为全部索引完成。"
      : "作业已结束，完整成功计数尚未确认，请核对作业计数。"}</p>}
    {complete && <p>本次作业已完成，成功索引 {countText(indexed)} 个版本，跳过 {countText(skipped)} 个版本，失败 0 个。</p>}
    {job.state === "CANCELLED" && <p>作业已取消；已处理数量以以上回执为准。</p>}
    {job.error_code && <p className="retrieval-failure">错误代码：{job.error_code}</p>}
    <p className="retrieval-meta">作业：<code>{job.id}</code>
      {typeof result?.current_version === "string" && result.current_version && <> · 当前版本：<code>{result.current_version}</code></>}
    </p>
    <p className="retrieval-meta">已处理包含成功、跳过和失败；缺失计数保留为“未提供”。作业可能包含可读历史版本，版本总数不能等同于目录页数。</p>
  </div>;
}

/** No required props. The parent supplies the existing AppContext. */
export function RetrievalPanel() {
  const app = useApp();
  // Remount before painting another identity's results, including permission revisions.
  const identity = JSON.stringify([app.me.id, app.space.id, app.space.roles, app.me.is_admin, app.refresh]);
  return <RetrievalScope key={identity} />;
}

function RetrievalScope() {
  const profiles = useRetrievalProfile();
  const [draft, setDraft] = useState("");
  const [submitted, setSubmitted] = useState("");
  const profileKey = JSON.stringify([profiles.selectedId, profiles.selection?.fingerprint,
    profiles.loading, profiles.indexBlockedReason]);
  return <RetrievalWorkspace key={profileKey} profiles={profiles} initialQuery={draft} autoQuery={submitted}
    onQueryChange={setDraft} onQuerySubmitted={setSubmitted} />;
}

function RetrievalWorkspace({ profiles, initialQuery, autoQuery, onQueryChange, onQuerySubmitted }: {
  profiles: ReturnType<typeof useRetrievalProfile>; initialQuery: string; autoQuery: string;
  onQueryChange: (value: string) => void; onQuerySubmitted: (value: string) => void;
}) {
  const app = useApp();
  const spaceId = app.space.id;
  const id = useId();
  const selection = profiles.selection;
  const loaded = useLoad(async (signal) => {
    if (profiles.indexBlockedReason) return undefined;
    const value = await get<RetrievalStatus>(`/retrieval/status?${queryString({ space_id: spaceId,
      ...(selection ? { profile_id: selection.profile_id } : {}) })}`, signal);
    if (value.space_id !== spaceId) throw new Error("检索状态与当前空间不一致，请重新读取。");
    if (selection && (value.retrieval_selection?.profile_id !== selection.profile_id ||
        value.retrieval_selection?.fingerprint !== selection.fingerprint)) throw new Error("检索状态与所选方案不一致，请刷新方案。");
    return value;
  }, [spaceId, profiles.indexBlockedReason, selection?.profile_id, selection?.fingerprint]);
  const status = loaded.data;
  const reloadStatus = useRef(loaded.reload);
  reloadStatus.current = loaded.reload;
  const [trackedJob, setJob] = useState<Job | null>(null);
  const job = trackedJob ?? status?.active_job ?? status?.index_snapshot?.latest_job ?? null;
  const [refreshAfterJob, setRefreshAfterJob] = useState<string>();
  const [modelRuntime, setModelRuntime] = useState<ModelRuntimeState>();
  const [pollError, setPollError] = useState<Error>();
  const [pollRevision, setPollRevision] = useState(0);
  const [cancelRequested, setCancelRequested] = useState<string>();
  const [query, setQuery] = useState(initialQuery);
  const autoSearched = useRef(false);
  const [searchResult, setSearchResult] = useState<RetrievalSearchResult>();
  const indexTask = useTask();
  const searchTask = useTask();
  const sourceTask = useTask();
  const requests = useRef(new Set<AbortController>());
  const pollController = useRef<AbortController | null>(null);

  useEffect(() => () => {
    for (const controller of requests.current) controller.abort();
    requests.current.clear();
  }, []);

  async function scopedRequest(action: (signal: AbortSignal) => Promise<void>) {
    const controller = new AbortController();
    requests.current.add(controller);
    try {
      await action(controller.signal);
    } catch (error) {
      if (!controller.signal.aborted) throw error;
    } finally {
      requests.current.delete(controller);
    }
  }

  function refreshAfterOperation(next: Job) {
    setRefreshAfterJob(next.id);
    setSearchResult(undefined);
    // Fetch verified current receipts even when all cardinalities are unchanged.
    // Never re-use an old search result or automatically launch another search.
    reloadStatus.current();
  }

  useEffect(() => {
    if (status?.active_job) {
      const snapshot = status.active_job;
      // A status snapshot must not roll the same job back after a newer poll/action.
      setJob(current => current?.id === snapshot.id ? current : snapshot);
    }
  }, [status]);

  const activeId = active(job) ? job!.id : null;
  useEffect(() => {
    if (!activeId || indexTask.busy) return;
    const controller = new AbortController();
    pollController.current = controller;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      if (controller.signal.aborted) return;
      try {
        const next = await get<Job>(`/jobs/${encodeURIComponent(activeId!)}`, controller.signal);
        if (controller.signal.aborted) return;
        if (next.id !== activeId) throw new Error("作业响应与当前索引任务不一致。");
        setJob(next);
        setPollError(undefined);
        if (active(next)) timer = setTimeout(() => void poll(), 2000);
        else refreshAfterOperation(next);
      } catch (error) {
        if (!controller.signal.aborted) setPollError(error instanceof Error ? error : new Error(String(error)));
        // Pause after an error; only an explicit retry resumes status reads.
      }
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
      if (pollController.current === controller) pollController.current = null;
    };
  }, [activeId, indexTask.busy, pollRevision]);

  const managementReason = profiles.indexBlockedReason || (loaded.loading ? "正在核验当前空间的索引状态与权限。"
    : !status ? "尚未取得可用状态，请重新读取后再管理索引。"
    : !status.enabled ? "当前部署未启用 RAG 索引，需由部署管理员启用后才能同步或重建。"
    : !status.permissions.can_index ? "你只有检索查看权限，不能同步、重建或重试索引，请联系空间管理员。"
    : "");
  const indexReason = managementReason || (profiles.selected?.can_index === false
    ? "所选方案的索引运行前提尚未就绪，请核对模型与向量服务后刷新状态。" : "");
  const canManage = !managementReason && !indexTask.busy;
  const canCreate = canManage && !indexReason && !activeId;
  const retryable = !!job && ["FAILED", "CANCELLED"].includes(job.state);

  function createIndex(force: boolean) {
    if (!canCreate) return;
    if (force && !window.confirm("完整重建将重新计算当前空间的派生检索索引，可能需要较长时间。只重建派生索引，不修改或删除原始文档与 Wiki 正文。是否继续？")) return;
    void indexTask.run(() => scopedRequest(async signal => {
      pollController.current?.abort();
      const next = await api<Job>("/retrieval/index-jobs", {
        method: "POST", body: { space_id: spaceId, force,
          ...(selection ? { retrieval_selection: { ...selection } } : {}) }, signal,
      });
      if (signal.aborted) return;
      setJob(next);
      setPollError(undefined);
      setCancelRequested(undefined);
      setSearchResult(undefined);
      setRefreshAfterJob(undefined);
      if (!active(next)) refreshAfterOperation(next);
    }));
  }

  function jobAction(action: "cancel" | "retry") {
    if (!canManage || !job) return;
    if (action === "cancel" && (!active(job) || cancelRequested === job.id)) return;
    if (action === "retry" && (indexReason || !retryable || nonRetryable.has(job.error_code ?? ""))) return;
    const jobId = job.id;
    void indexTask.run(() => scopedRequest(async signal => {
      pollController.current?.abort();
      const next = await api<Job>(`/jobs/${encodeURIComponent(jobId)}/${action}`, { method: "POST", signal });
      if (signal.aborted) return;
      setJob(next);
      setPollError(undefined);
      setCancelRequested(action === "cancel" ? next.id : undefined);
      setSearchResult(undefined);
      if (!active(next)) refreshAfterOperation(next);
    }));
  }

  function search(value = query) {
    const text = value.trim();
    if (!text || !status || loaded.loading || searchTask.busy || profiles.blockedReason || modelRuntime?.state === "FAILED") return;
    autoSearched.current = true;
    onQuerySubmitted(text);
    void searchTask.run(() => scopedRequest(async signal => {
      setSearchResult(undefined);
      sourceTask.clearError();
      const result = await api<RetrievalSearchResult>("/retrieval/search", {
        method: "POST", body: { space_id: spaceId, query: text, scope: "reference", limit: 12,
          ...(selection ? { retrieval_selection: { ...selection } } : {}) }, signal,
      });
      if (selection && (result.retrieval_selection?.profile_id !== selection.profile_id ||
          result.retrieval_selection?.fingerprint !== selection.fingerprint)) throw new Error("返回结果与当前检索方案不一致，已停止展示。");
      if (selection && ["wiki", "wiki_fallback"].includes(result.mode))
        throw new Error("所选方案未完成语义检索，已停止展示；不会自动降级为 Wiki 检索。");
      if (result.query !== text || result.scope !== "reference") throw new Error("返回结果与本次检索请求不一致。");
      if (!signal.aborted) {
        setSearchResult(result);
        // A first local query may load the reranker. Re-read its status once,
        // without another search/index request or inferring load from success.
        reloadStatus.current();
      }
    }));
  }

  useEffect(() => {
    if (autoQuery && !autoSearched.current && status && !loaded.loading && !profiles.blockedReason) search(autoQuery);
  }, [autoQuery, status, loaded.loading, profiles.blockedReason]);

  function openSource(hit: RetrievalHit) {
    void sourceTask.run(() => scopedRequest(async signal => {
      const resource = await get<Resource>(`/resources/${encodeURIComponent(hit.resource_id)}`, signal);
      if (signal.aborted) return;
      if (resource.id !== hit.resource_id || resource.space_id !== spaceId)
        throw new Error("来源响应与当前空间或候选不一致，请重新检索。");
      app.openResource(resource, "content", hit.version_id, hit.matched_block_ids?.[0]);
    }));
  }

  return <section className="retrieval-panel" aria-labelledby={`${id}-title`}>
    <header className="retrieval-heading">
      <div><h2 id={`${id}-title`}>Wiki + RAG 检索管理</h2><p>{app.space.name} · 管理派生索引，检查当前授权资料的召回结果。</p></div>
      <button type="button" disabled={loaded.loading || indexTask.busy} onClick={() => {
        setSearchResult(undefined);
        searchTask.clearError();
        profiles.reload();
      }}>刷新状态</button>
    </header>
    <RetrievalProfilePicker value={profiles} disabled={indexTask.busy} allowUnavailable />
    {loaded.loading && <p role="status">正在读取检索状态…</p>}
    <ErrorBox error={loaded.error} retry={loaded.reload} />
    {status && <>
      <div className="retrieval-row"><Badge value={modeText(status.mode)} /><span>索引功能：{status.enabled ? "已启用" : "未启用"}</span></div>
      <dl className="retrieval-config" aria-label="只读检索配置">
        <div><dt>向量后端</dt><dd>{status.vector.backend || "未提供"}</dd></div>
        <div><dt>向量部署模式</dt><dd>{vectorModeText(status.vector.mode)}</dd></div>
        <div><dt>向量状态</dt><dd>{status.vector.available ? "可用" : "不可用"} · {status.vector.status || "未提供"}</dd></div>
        <div><dt>索引集合</dt><dd>{status.vector.collection || "未提供"}</dd></div>
        <div><dt>嵌入模式</dt><dd>{status.embedding.mode || "未提供"}</dd></div>
        <div><dt>嵌入模型</dt><dd>{status.embedding.model || "未提供"}</dd></div>
        <div><dt>向量维数</dt><dd>{countText(status.embedding.dimensions)}</dd></div>
        <div><dt>语义重排模型</dt><dd>{status.vector.reranking ? status.vector.reranking.model || "未配置" : "接口未返回模型信息"}</dd></div>
        <div><dt>重排状态</dt><dd>{rerankerStateText(status.vector.reranking)}</dd></div>
        {status.vector.embedding_mode && <div><dt>向量层嵌入模式</dt><dd>{status.vector.embedding_mode}</dd></div>}
      </dl>
      <p className="retrieval-meta">可切换已登记的检索方案；模型参数与部署配置为只读，由服务端维护。</p>
      <p className="retrieval-meta">加载状态仅代表当前 API 服务进程，不代表某次查询已经执行重排；后台任务是否重排请查看该次执行记录。刷新状态不会加载模型。</p>
      {status.vector.model_runtime?.supported && <LocalModelReadiness initial={status.vector.model_runtime}
        selection={selection} queryPending={searchTask.busy} onState={setModelRuntime}
        onReady={()=>{ if (!searchTask.busy) reloadStatus.current(); }} />}
      {status.embedding.mode === "http" && <Notice>当前嵌入模式为 HTTP：语义索引与语义检索会使用已配置的嵌入服务；检索试验不调用生成模型。</Notice>}
      {status.embedding.development_only && <Notice>当前嵌入配置仅供开发验证，不能将其结果视为生产语义检索效果。</Notice>}
      {!status.vector.available && <Notice>{selection
        ? "所选方案的向量通道当前不可用；索引就绪前不能检索，不会自动切换方案。"
        : "向量通道当前不可用。检索可能降级，实际召回模式与警示以本次结果为准。"}</Notice>}
      {status.notes.length > 0 && <Notice><ul className="retrieval-notes" aria-label="检索状态说明">{status.notes.map((note, index) => <li key={index}>{note}</li>)}</ul></Notice>}
      <h3>当前目录索引覆盖</h3>
      {status.index_snapshot && <IndexUpdate snapshot={status.index_snapshot} />}
      <p className="retrieval-meta">以下数量只统计当前可读目录版本；下方作业结果包含该次处理的历史版本，两组数字不要求相同。</p>
      <dl className="retrieval-metrics" aria-label="当前索引覆盖">
        <Metric name="catalog_pages" label="目录页数" value={status.coverage.catalog_pages} />
        <Metric name="indexed_pages" label="已索引页数" value={status.coverage.indexed_pages} />
        <Metric name="dirty_pages" label="待同步页数" value={status.coverage.dirty_pages} />
        <Metric name="indexed_blocks" label="已索引原文块" value={status.coverage.indexed_blocks} />
        <Metric name="indexed_chunks" label="已索引子块" value={status.coverage.indexed_chunks} />
      </dl>
    </>}
    {refreshAfterJob && <p className="retrieval-meta" role="status" data-index-refresh={loaded.loading ? "checking" : loaded.error ? "failed" : status?.index_snapshot ? "checked" : "unknown"}>
      {loaded.loading ? "索引作业已结束，正在核验最新代次与目录覆盖…"
        : loaded.error ? "作业已结束，但最新索引状态读取失败；请重试，暂不确认新索引已生效。"
        : status?.index_snapshot ? "已自动重新核验索引状态。旧检索结果已清除，可基于当前代次重新检索。"
        : "作业已结束，覆盖数量已刷新；服务端尚未返回索引代次，不能仅据数量确认更新。"}
    </p>}
    <section className="retrieval-section" aria-labelledby={`${id}-jobs`}>
      <h3 id={`${id}-jobs`}>向量索引</h3>
      <div className="retrieval-actions">
        <button type="button" className="primary" disabled={!canCreate} aria-describedby={`${id}-index-help`} onClick={() => createIndex(false)}>增量同步</button>
        <button type="button" disabled={!canCreate} aria-describedby={`${id}-index-help`} onClick={() => createIndex(true)}>完整重建</button>
        {activeId && <button type="button" disabled={!canManage || cancelRequested === job?.id} aria-describedby={`${id}-index-help`} onClick={() => jobAction("cancel")}>取消作业</button>}
        {retryable && <button type="button" disabled={!canManage || !!indexReason || nonRetryable.has(job!.error_code ?? "")} aria-describedby={`${id}-index-help`} onClick={() => jobAction("retry")}>重试作业</button>}
      </div>
      <p id={`${id}-index-help`} className="retrieval-meta">{indexReason || (activeId
        ? "已有活动索引作业，结束后可再次同步或重建。"
        : "增量同步处理变更内容；完整重建只重建派生索引，不修改原文。")}</p>
      {retryable && nonRetryable.has(job!.error_code ?? "") && <Notice>此错误不能直接重试，请先处理原始原因。</Notice>}
      {indexTask.busy && <p role="status">正在提交作业操作…</p>}
      <ErrorBox error={indexTask.error} />
      <ErrorBox error={pollError} retry={() => { setPollError(undefined); setPollRevision(value => value + 1); }} />
      {pollError && <p className="retrieval-meta">进度读取已暂停，以上次回执为准；点击重试恢复读取。</p>}
      {job ? <><h4>{active(job) ? "本次作业 · 全部处理版本" : "最近作业结果 · 全部处理版本（含历史版本）"}</h4><JobProgress job={job} /></>
        : status && <p className="retrieval-empty">当前没有活动索引作业。打开面板不会自动启动索引。</p>}
      {activeId && cancelRequested === job?.id && <p role="status">已请求取消，等待后台确认。</p>}
    </section>
    <section className="retrieval-section" aria-labelledby={`${id}-search`}>
      <h3 id={`${id}-search`}>检索试验</h3>
      <p id={`${id}-search-help`} className="retrieval-meta">只检索，不调用生成模型。范围为当前授权的参考资料，每次最多返回 12 个候选；候选不含证据正文预览，不代表已核验的业务结论。</p>
      <form className="retrieval-search-form" onSubmit={event => { event.preventDefault(); search(); }}>
        <label htmlFor={`${id}-query`}>检索词</label>
        <div className="retrieval-search-input">
          <input id={`${id}-query`} type="search" value={query} autoComplete="off" placeholder="输入要查找的主题或术语" aria-describedby={`${id}-search-help`}
            onChange={event => { setQuery(event.target.value); onQueryChange(event.target.value); }} />
          <button type="submit" className="primary" disabled={!status || loaded.loading || !query.trim() || searchTask.busy || !!profiles.blockedReason || modelRuntime?.state === "FAILED"}>{searchTask.busy ? "正在检索…" : "检索"}</button>
        </div>
      </form>
      <ErrorBox error={searchTask.error} />
      <ErrorBox error={sourceTask.error} />
      {searchTask.busy && <p role="status">正在读取检索候选…</p>}
      {!searchResult && !searchTask.busy && !searchTask.error && <p className="retrieval-empty">尚未检索。输入检索词并手动提交后显示候选。</p>}
      {searchResult && <div className="retrieval-results" aria-label="检索结果">
        {searchResult.retrieval_selection && <p className="retrieval-scheme-used">本次方案：{searchResult.retrieval_selection.model} · {searchResult.retrieval_selection.dimensions} 维</p>}
        <p role="status">“{searchResult.query}” · {modeText(searchResult.mode)} · 返回 {countText(searchResult.returned)} 项 / 共 {countText(searchResult.total_candidates)} 个候选 · 耗时 {Number.isFinite(searchResult.timing_ms) ? searchResult.timing_ms.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) : "未提供"} ms</p>
        <p className="retrieval-meta" data-query-reranking={searchResult.reranking?.mode ?? "unknown"}>
          {searchResult.reranking?.mode === "local_cross_encoder"
            ? `本次检索已执行语义重排 · ${searchResult.reranking.model || "模型名未返回"} · ${countText(searchResult.reranking.input_units)} 个候选单元`
            : searchResult.reranking?.mode === "unavailable" ? "本次重排不可用，已保留检索候选；不能视为重排成功。"
            : searchResult.reranking?.mode === "disabled" ? "本次检索未执行重排；请结合候选数量与模型配置核对。"
            : "本次检索未返回重排执行记录，不能仅据已配置或已加载认定执行成功。"}
        </p>
        {searchResult.retrieval_trace && <RetrievalDiagnostics trace={searchResult.retrieval_trace} />}
        <p className="retrieval-meta">本次目录 {countText(searchResult.catalog_pages)} 页，其中已索引 {countText(searchResult.indexed_catalog_pages)} 页；检索分数不代表置信度。</p>
        {searchResult.warnings.length > 0 && <Notice><ul className="retrieval-notes" aria-label="检索警示">{searchResult.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></Notice>}
        {searchResult.hits.length === 0 ? <p className="retrieval-empty">未找到匹配候选。可调整检索词，并检查覆盖统计与警示。</p> : <ol className="retrieval-hits">
          {searchResult.hits.map((hit, index) => <li key={`${hit.page_id}:${hit.version_id}:${index}`}>
            <div className="retrieval-hit-heading"><div><h4>{hit.title}</h4><span className="retrieval-meta">{({ knowledge: "知识页", document: "来源文档", template: "模板" } as Record<string, string>)[hit.kind] ?? hit.kind}</span></div>
              <button type="button" disabled={sourceTask.busy} aria-label={`打开来源：${hit.title}`} onClick={() => openSource(hit)}>打开来源</button>
            </div>
            <div className="retrieval-row">
              {hit.channels.length ? hit.channels.map((channel, i) => <Badge key={`${channel}:${i}`} value={channel} />) : <span>通道未提供</span>}
              <span>检索分数：{Number.isFinite(hit.score) ? hit.score.toLocaleString("zh-CN", { maximumFractionDigits: 6 }) : "未提供"}</span>
              {hit.vector_rank !== undefined && <span>向量排名：{countText(hit.vector_rank)}</span>}
              {hit.lexical_rank !== undefined && <span>关键词排名：{countText(hit.lexical_rank)}</span>}
            </div>
            <details className="retrieval-meta"><summary>候选版本与定位</summary><p>页面：<code>{hit.page_id}</code> · 资源：<code>{hit.resource_id}</code></p><p>版本：<code>{hit.version_id}</code></p>
              {hit.matched_block_ids && <p>命中块：{hit.matched_block_ids.length ? hit.matched_block_ids.join("、") : "未提供"}</p>}
            </details>
          </li>)}
        </ol>}
      </div>}
    </section>
  </section>;
}
