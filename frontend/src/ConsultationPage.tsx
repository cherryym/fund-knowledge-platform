import { memo, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  ArrowClockwise,
  ArrowUp,
  ChatCircleDots,
  CheckCircle,
  ClockCounterClockwise,
  Copy,
  FileText,
  Flag,
  Link,
  Paperclip,
  Plus,
  Stop,
  ThumbsUp,
  Trash,
  X,
} from "@phosphor-icons/react";
import { allPages, api, del, get, post, query } from "./api";
import { ModelPicker } from "./ModelPicker";
import { useConsultationModelPreference } from "./consultationModelPreference";
import { ConsultationHistoryResizeHandle } from "./ConsultationHistoryResizeHandle";
import { RetrievalProfilePicker, useRetrievalProfile } from "./retrievalProfiles";
import { BrandIcon } from "./BrandIcon";
import { WikiAnswerMarkdown, wikiPreviewHtml } from "./WikiAnswerMarkdown";
import { AnswerTimingDetails, RetrievalDiagnostics } from "./RetrievalDiagnostics";
import { EvidenceReviewNotice, type EvidenceReviewReport } from "./EvidenceReview";
import { citationDescription, sourceLabel } from "./citationPresentation";
import type {
  Answer,
  Evidence,
  Job,
  Resource,
  Run,
  Thread,
  Version,
  Page,
} from "./types";
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  FormModal,
  Loading,
  Motion,
  Notice,
  readable,
  textValue,
  useApp,
  useLoad,
  useTask,
} from "./ui";
import { useModelState } from "./Pickers";
import { replaceRun, useConsultationRunPolling } from "./useConsultationRunPolling";
import "./consultation-scope.css";
import "./consultation-layout.css";
import "./answer-preview.css";

export const AnswerPreview = memo(function AnswerPreview({ run, readable = true }: { run: Run; readable?: boolean }) {
  const preview = run.model_snapshot?.public_preview;
  const html = useMemo(() => wikiPreviewHtml(preview?.text ?? ""), [preview?.text]);
  if (!readable || run.state !== "RUNNING" || run.invalidated || run.answer || !preview?.text ||
      preview.run_id !== run.id || preview.attempt !== run.attempt || preview.state !== "pending" ||
      preview.source_check !== "current_access" || preview.final_validation !== "pending" ||
      run.model_snapshot?.last_request?.phase !== "synthesis") return null;
  return <section className="answer-preview" aria-label="暂存正文预览" aria-busy="true">
    <p className="answer-preview-notice" role="status">生成中，尚未完成核验</p>
    <div className="answer-preview-text rendered-rich-text" dangerouslySetInnerHTML={{ __html: html }} />
    <small>已检查当前来源访问权限；完整事件、全文引用及业务结论仍待核验。失败或取消时撤去预览。</small>
  </section>;
});

function withoutPreview(run: Run): Run {
  return { ...run, model_snapshot: { ...run.model_snapshot, public_preview: undefined } };
}

const answerScopes = {
  reference: {
    label: "资料辅助答疑",
    note: "可使用待复核资料，结果仅供参考、不代表现行制度。",
  },
  formal: {
    label: "正式业务答疑",
    note: "仅使用已核验并发布的知识及其来源作为业务依据；证据不足时需补充资料。",
  },
};
const referenceBlockedNote = "资料辅助答疑尚未就绪，暂不能提交；不会自动改用正式业务答疑。";
const referenceFollowupBlockedNote = "本轮为资料辅助答疑，服务就绪前无法补充提交，不能降级为正式业务答疑。";
function runScopeLabel(run: Run) {
  return run.answer_scope === "reference" || run.answer_scope === "formal"
    ? answerScopes[run.answer_scope].label
    : "历史答疑范围未记录";
}
function runScopeOriginLabel(run: Run) {
  switch (run.answer_scope_origin) {
    case "explicit": return "范围来源：请求显式指定";
    case "inherited": return "范围来源：继承父轮";
    case "legacy_default": return "范围来源：旧接口默认";
    default: return "范围来源：历史未记录，无法确认当时的请求选择";
  }
}
function isRecordedCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
const phaseLabels: Record<string,string> = {
  queued:"等待执行", QUEUED:"等待执行", CLAIMED:"准备处理", STARTED:"准备处理",
  PLANNING_QUESTION:"模型分析问题", RETRIEVING:"检索本地资料", SELECTING_SOURCES:"选择相关条款",
  LOADING_WIKI_CATALOG:"载入完整知识目录", READING_WIKI_INDEX:"模型查阅知识索引", READING_WIKI_PAGES:"模型阅读已选资料",
  HYBRID_RETRIEVAL:"关键词与语义融合检索",
  WARMING_RETRIEVAL_MODELS:"等待本地嵌入与重排模型准备",
  PREPARING_ANSWER:"核对证据", GENERATING:"模型综合解答", VALIDATING_ANSWER:"检查答案与引用", CLARIFYING:"核对缺失信息",
  COMPLETED:"处理完成", FAILED:"处理未完成", CANCELLED:"已取消",
};
function phaseLabel(run: Run) { return phaseLabels[run.phase ?? ""] ?? (run.state === "QUEUED" ? "等待执行" : "正在处理"); }
const RunStatus = memo(function RunStatus({ run, finalRead, readFailed }: { run: Run; finalRead?: string; readFailed?: boolean }) {
  const snapshot = run.model_snapshot;
  const request = snapshot?.last_request;
  const retrievalQueries = snapshot?.hybrid_retrieval?.queries;
  const lastRetrieval = retrievalQueries?.at(-1);
  const progress = snapshot?.reading_progress;
  const contextCompletion = run.invalidated ? undefined : snapshot?.context_completion;
  const dependencyLabels: Record<string, string> = {resolved: "已精确定位", pending: "待补读",
    broader_context: "已补读所在整条，具体款待核对", ambiguous: "存在多个可能位置", unresolved: "未定位",
    external: "外部引用待核对", unavailable: "当前不可读取", locator_required: "缺少章节定位",
    not_in_authorized_catalog: "当前授权目录未找到", stale_receipt: "目标未读或回执版本已变化，须重新查证"};
  const batchesKnown = !!progress && isRecordedCount(progress.total_batches) && progress.total_batches! > 0
    && isRecordedCount(progress.completed_batches) && progress.completed_batches! <= progress.total_batches!;
  const readingStages: Record<string, string> = {loading_sections: "正在定位完整知识页与原文小节",
    completing_dependencies: "正在补查原文引用与缺失依赖", reranking_context: "正在对完整证据组进行语义重排",
    reading_sections: "正在阅读相关完整内容", synthesis: "正在综合生成答复", consolidating: "正在汇总已读材料",
    batch_completed: "已完成当前批次，继续核对与综合"};
  // A selected model or an execution mode is not proof of an actual invocation.
  const invoked = snapshot?.model_invoked;
  const invocationLabel =
    invoked === true
      ? "模型已调用"
      : invoked === false
        ? "未调用模型"
        : ["QUEUED","RUNNING"].includes(run.state) ? "等待模型处理" : "历史调用状态未记录";
  const executionLabels: Record<string, string> = {
    reference_grounded: "资料辅助解答",
    http_grounded: "证据约束解答",
    extractive: "证据摘录",
    deterministic_clarification: "规则澄清",
    planning_only: "仅完成问题研判，尚未生成证据解答",
    generation_rejected: "生成结果未通过检查",
    wiki_reading: "LLM Wiki 阅读与综合",
  };
  const executionMode = snapshot?.execution_mode;
  const evidenceCount = snapshot?.evidence_count;
  const recordedEvidenceCount = isRecordedCount(evidenceCount);
  return (
    <div className="consultation-run-status">
      <div className="response-heading">
        <ChatCircleDots size={24} />
        <strong>基金知识助手</strong>
        <span className="badge consultation-invocation-state">
          {invocationLabel}
        </span>
        {snapshot?.model_id && (
          <span className="answer-model-source">
            <BrandIcon brand={snapshot.brand ?? ""} size={18} decorative />
            {invoked === true ? "调用模型：" : "所选模型："}
            {snapshot.model_id}
          </span>
        )}
        {finalRead ? <span className="badge">{readFailed ? "完整结果读取未完成" : "正在读取完整结果"}</span> : run.state === "COMPLETED" ? (
          <span className="badge">处理完成</span>
        ) : ["QUEUED","RUNNING"].includes(run.state) ? (
          <span className="badge">{phaseLabel(run)}</span>
        ) : (
          <Badge value={run.state} />
        )}
      </div>
      {progress && ["QUEUED", "RUNNING"].includes(run.state) && <div className="consultation-reading-progress" role="status" aria-live="polite">
        <div><strong>{readingStages[progress.stage] ?? "正在处理已选择的资料"}</strong>
          {batchesKnown && <span>已完成 {progress.completed_batches} / {progress.total_batches} 批
            {isRecordedCount(progress.current_batch) && progress.current_batch! > progress.completed_batches! ? ` · 当前第 ${progress.current_batch} 批` : ""}</span>}
        </div>
        {batchesKnown && <progress aria-label="本轮资料阅读批次" value={progress.completed_batches} max={progress.total_batches} />}
        {isRecordedCount(snapshot?.wiki_reading?.source_sections) && <small>原文按完整小节核对：{snapshot!.wiki_reading!.source_sections} 个
          {snapshot?.wiki_reading?.scoped_source_pages ? " · 未展开的其他章节不代表已读" : ""}</small>}
      </div>}
      <details className="consultation-run-details" data-run-execution="technical">
        <summary>执行记录</summary>
        <p className="retrieval-scheme-used">{snapshot?.retrieval_selection
          ? <>本次检索：{snapshot.retrieval_selection.model} · {snapshot.retrieval_selection.dimensions.toLocaleString()} 维 · BM25 混合召回</>
          : "本轮检索方案未记录"}</p>
        <div className="consultation-execution-details">
          {snapshot?.retrieval_selection && <span>检索方案：{snapshot.retrieval_selection.profile_id} · 指纹：{snapshot.retrieval_selection.fingerprint}</span>}
          {run.phase && <span>当前阶段：{phaseLabel(run)}</span>}
          {isRecordedCount(snapshot?.model_request_count) && <span>模型请求：{snapshot!.model_request_count} 次</span>}
          {request && <span>最近请求：{{planning:"问题研判",wiki_index:"查阅索引",wiki_notes:"阅读与整理",synthesis:"综合回答"}[request.phase]} · {
            request.state === "received" ? (request.finish_reason === "stop" ? "模型已完整返回" : "模型返回未完整结束") :
            request.state === "failed" ? "请求未完成" : "等待模型响应"}</span>}
          {request && isRecordedCount(request.duration_ms) && <span>请求耗时：{(request.duration_ms / 1000).toFixed(1)} 秒</span>}
          {request && isRecordedCount(request.response_chars) && <span>模型返回：{request.response_chars} 字符（含结构字段）</span>}
          {request?.limits && <span>{request.limits.total_seconds === null ? "无固定思考时限，可随时取消" : `本阶段上限：${request.limits.total_seconds} 秒`} · {request.limits.max_output_tokens.toLocaleString()} 输出 tokens</span>}
          {request?.limits && snapshot?.protocol !== "codex_app_server" && <span>连接上限：{request.limits.connect_seconds} 秒 · {request.limits.read_idle_seconds === null ? "等待模型期间无固定读取时限" : `无数据等待上限：${request.limits.read_idle_seconds} 秒`}</span>}
          {snapshot?.wiki_reading && <span>可读目录：{snapshot.wiki_reading.catalog_pages} 页 · {snapshot.wiki_reading.full_text_loaded ? "已加载全文" : "已按相关章节读取"}：{snapshot.wiki_reading.loaded_pages} 页 / {snapshot.wiki_reading.loaded_blocks} 段</span>}
          {snapshot?.wiki_reading?.scoped_source_pages ? <span>其中 {snapshot.wiki_reading.scoped_source_pages} 份原文为完整小节范围，不是整本已读；全文仍可查看。</span> : null}
          {contextCompletion && <span>关联补全：{contextCompletion.structural_groups_added} 组结构上下文 · 显式引用 {contextCompletion.resolved_count} / {contextCompletion.reference_count} 已定位 · 剩余 {contextCompletion.gap_count} 处缺口</span>}
          {!run.invalidated && snapshot?.retrieval_runtime && <span>执行进程的本地模型准备：{
            snapshot.retrieval_runtime.state === "READY" && snapshot.retrieval_runtime.self_tested ? "自检已通过"
              : snapshot.retrieval_runtime.state === "FAILED" ? "准备失败" : "等待准备完成"} · 进程 {snapshot.retrieval_runtime.process_id}
            {snapshot.retrieval_runtime.error_code ? ` · ${snapshot.retrieval_runtime.error_code}` : ""}</span>}
          {isRecordedCount(contextCompletion?.direction_count) && <span>查证方向：{contextCompletion.direction_source_read_count ?? 0} / {contextCompletion.direction_count} 已关联原文阅读 · {contextCompletion.direction_gap_count ?? 0} 项待补查（不是业务准确率）</span>}
          {contextCompletion?.additional_searches ? <span>针对未定位引用已补查 {contextCompletion.additional_searches} 次；补查结果仍须核对原文。</span> : null}
          {contextCompletion?.group_rerank && <span>证据组重排：{contextCompletion.group_rerank.model ?? "未配置"} · {
            {scored: "已评分，保留全部依赖", not_needed: "仅一组，无须重排", not_enabled: "未启用，保留原序",
              unavailable_preserved_order: "重排不可用，保留原序与全部证据"}[contextCompletion.group_rerank.status] ?? "状态待核对"}</span>}
          {lastRetrieval && <span>知识检索：Wiki + RAG · {retrievalQueries!.length} 次检索 · 最近返回 {lastRetrieval.returned} 个候选页（候选不等于依据）</span>}
          {lastRetrieval && <span>最近检索：{["hybrid", "hybrid_unit_rerank", "universal_unit_retrieval"].includes(lastRetrieval.mode)
            ? "关键词与语义融合" : ["wiki", "wiki_fallback"].includes(lastRetrieval.mode) ? "Wiki 目录回退" : "检索方式未确认"} · 当前已索引 {lastRetrieval.indexed_catalog_pages} / {lastRetrieval.catalog_pages} 页</span>}
          {lastRetrieval?.warnings?.length ? <span>检索提示：{lastRetrieval.warnings.join("、")}；仍可查阅完整授权目录。</span> : null}
          {request?.usage && isRecordedCount(request.usage.completion_tokens) && <span>实际输出用量：{request.usage.completion_tokens.toLocaleString()} tokens{
            isRecordedCount(request.usage.reasoning_tokens) ? `（其中思考 ${request.usage.reasoning_tokens.toLocaleString()}）` : ""}</span>}
          {snapshot?.validation_status === "failed" && request?.state === "received" && <span>模型已返回；答案未通过检查，不是仍在思考。</span>}
          <span className="consultation-run-scope">{runScopeLabel(run)}</span>
          <span className="consultation-run-scope-origin">{runScopeOriginLabel(run)}</span>
          <span>
            {executionMode
              ? `执行方式：${executionLabels[executionMode] ?? `未知方式（${executionMode}）`}`
              : "执行方式未记录"}
          </span>
          <span>
            {recordedEvidenceCount
              ? `依据数量：${evidenceCount}`
              : "依据数量未记录"}
          </span>
        </div>
        {(run.answer_scope === "reference" || run.answer_scope === "formal") && (
          <p className="consultation-scope-note" data-scope={run.answer_scope}>
            {answerScopes[run.answer_scope].note}
          </p>
        )}
      {!run.invalidated && snapshot?.pipeline_timing && <AnswerTimingDetails timing={snapshot.pipeline_timing} />}
      {!run.invalidated && lastRetrieval?.retrieval_observations?.map((trace, index) =>
        <RetrievalDiagnostics key={`${trace.query_sha256 ?? "query"}:${index}`} trace={trace} />)}
      {contextCompletion?.reading_coverage && <details className="consultation-run-details" data-reading-coverage="ledger">
        <summary>查证方向与原文阅读 · {contextCompletion.reading_coverage.gap_count ? "仍有阅读缺口" : "各方向已关联原文"}</summary>
        <p className="muted">记录计划查证方向是否读到了原文，不代表该段支持结论、规则适用或全部业务事项已解决。仅命中候选或 Wiki 不算原文已读。</p>
        <ul>{contextCompletion.reading_coverage.directions.map(direction => <li key={direction.id}>
          <strong>{direction.query}</strong> · {{SOURCE_READ: "已读对应原文，待核对支持性", WIKI_ONLY: "仅已读 Wiki，缺原文依据",
            UNREAD: "有候选，尚未读到对应原文", NO_CANDIDATE: "尚无可用候选，请补查"}[direction.status] ?? "阅读状态待核对"}
          {direction.evidence_ids.length > 0 && <span> · 来源定位：{direction.evidence_ids.join("、")}</span>}
        </li>)}</ul>
      </details>}
      {contextCompletion?.references?.length ? <details className="consultation-run-details">
        <summary>关联补全详情 · {contextCompletion.gap_count ? "仍有待核对项" : "已检查当前显式引用"}</summary>
        <p className="muted">仅检查已读资料中的显式关系；不代表全库召回完整、业务正确或已通过专家复核。重排不会删除已选依赖。</p>
        <ul>{contextCompletion.references.map((ref, i) => <li key={i}>
          {ref.source_page_id} · {ref.text}：{dependencyLabels[ref.status] ?? "待核对"}{ref.target_page_id ? ` → ${ref.target_page_id}` : ""}
        </li>)}</ul>
      </details> : null}
      {snapshot?.wiki_reading?.page_titles?.length && !run.invalidated ? <details className="consultation-run-details">
        <summary>本轮查阅的知识与来源（{snapshot.wiki_reading.loaded_pages}）</summary>
        <ul>{snapshot.wiki_reading.page_titles.map((title,i)=><li key={i}>{title}</li>)}</ul>
      </details> : null}
      </details>
      {run.question_analysis?.source === "model_prior_knowledge_unverified" && !run.invalidated && (
        <details className="answer-initial-analysis" open={run.state === "RUNNING"}>
          <summary>模型的初步问题研判 · 尚未核对本地依据</summary>
          {snapshot?.planning_cache?.hit === true && <p className="muted" data-planning-cache="reused">
            已复用同一问题、相同模型与条件下的查证计划，省去一次重复规划。当前来源重新核验，最终答案仍由本轮模型生成；未复用历史答案。
          </p>}
          <p>{run.question_analysis.plan.interpretation}</p>
          <p>{run.question_analysis.plan.initial_assessment}</p>
          <p className="muted">本阶段未读取本地知识；以下仅是待验证思路，不是正式业务结论。</p>
          <ul>{run.question_analysis.plan.decision_points?.map((item,i)=><li key={i}>{item}</li>)}</ul>
          <div className="analysis-searches">计划查证：{run.question_analysis.plan.search_queries.join("；")}</div>
        </details>
      )}
    </div>
  );
});

function RunEvidenceDiagnostic({ run }: { run: Run }) {
  // A diagnostic never establishes invocation state or overrides invalidation.
  if (
    run.invalidated || run.state !== "COMPLETED" ||
    run.answer?.status !== "INSUFFICIENT_EVIDENCE" ||
    run.model_snapshot?.model_invoked !== false ||
    run.answer.citations.length > 0
  ) return null;
  const supplied = run.evidence_diagnostic;
  const diagnostic = supplied?.basis === "current_access" &&
    (supplied.scope === "formal" || supplied.scope === "reference") &&
    (!run.answer_scope || supplied.scope === run.answer_scope) &&
    Array.isArray(supplied.reasons) ? supplied : undefined;
  const evidenceCount = run.model_snapshot.evidence_count;
  // The server only supplies diagnostics for an empty original snapshot. Do not
  // invent missing historical telemetry, or display contradictory counts.
  if (evidenceCount != null && evidenceCount !== 0) return null;
  if (!diagnostic && evidenceCount !== 0) return null;
  return (
    <section className="consultation-evidence-diagnostic" aria-label="零证据诊断">
      <h3>未进入模型生成</h3>
      {diagnostic ? (
        <>
          <p>{diagnostic.message}</p>
          <p className="consultation-diagnostic-basis">
            统计口径：当前访问权限（current_access） · {answerScopes[diagnostic.scope].label}
            <br />
            统计时间：<time dateTime={diagnostic.observed_at}>{diagnostic.observed_at}</time>
          </p>
          <dl className="consultation-diagnostic-counts">
            {([
              ["当前可读资料", diagnostic.visible_resource_count],
              ["当前符合范围资格", diagnostic.eligible_resource_count],
              ["当前排除资料", diagnostic.excluded_resource_count],
            ] as const).map(([label, count]) => (
              <div key={label}><dt>{label}</dt><dd>{isRecordedCount(count) ? count : "未提供"}</dd></div>
            ))}
          </dl>
          {diagnostic.reasons.length > 0 && (
            <ul className="consultation-diagnostic-reasons">
              {diagnostic.reasons.map((reason, index) => (
                <li key={index}>
                  {reason.label}{isRecordedCount(reason.count) ? `：${reason.count}` : ""}
                </li>
              ))}
            </ul>
          )}
          <p>{diagnostic.note}</p>
          <p>这些统计与原因依据当前权限重新计算，不代表该轮历史时点；不会自动重跑历史问题。资格排除原因不等于“资料不可信”。</p>
          <p><strong>下一步：</strong>{diagnostic.next_step}</p>
        </>
      ) : (
        <p>未提供可用的证据诊断，无法确定具体原因或当前资料数量；不会自动重跑历史问题。</p>
      )}
    </section>
  );
}

const contextFields = [
  ["product_type", "产品类型"],
  ["fund_label", "基金标识"],
  ["share_class", "份额类别"],
  ["asset_type", "资产类型"],
  ["market", "市场"],
  ["business_event", "业务事件"],
  ["business_date", "业务日期"],
  ["business_state", "业务状态"],
  ["knowledge_cutoff", "知识截止时点"],
];
function EvidenceLinks({
  ids,
  citations,
}: {
  ids: string[];
  citations: Evidence[];
}) {
  const app = useApp();
  return (
    <span className="inline-citations">
      {ids.map((id) => {
        const source = citations.find((c) => c.id === id);
        return source ? (
          <button
            className="citation-chip citation-doc-chip"
            key={id}
            title={citationDescription([source])}
            aria-label={`查看来源：${source.source_title} · ${id}`}
            onClick={() => app.openVersion(source.version_id, source.block_id)}
          >
            <FileText size={12} aria-hidden="true" />
            <span className="citation-doc-name">{sourceLabel(source.source_title)}</span>
          </button>
        ) : (
          <span className="text-red" key={id}>
            引用未返回：{id}
          </span>
        );
      })}
    </span>
  );
}
const AnswerView = memo(function AnswerView({ answer, evidenceReview }: { answer: Answer; evidenceReview?: EvidenceReviewReport }) {
  const app = useApp();
  const sources = new Map<string, Evidence[]>();
  for (const citation of answer.citations) {
    const group = sources.get(citation.version_id);
    if (group) group.push(citation); else sources.set(citation.version_id, [citation]);
  }
  const sourceCard = (c: Evidence) => <button className="source-card" key={c.id}
    onClick={() => app.openVersion(c.version_id, c.block_id)}>
    <div><FileText size={20}/><strong>{c.source_title}</strong><span>{c.id}</span></div>
    <p>{c.excerpt}</p><small>{readable(c.locator.label)} · 已绑定原始版本</small>
  </button>;
  return (
    <div className="answer-body">
      <div className="answer-status">
        {answer.format === "wiki_markdown" ? <span className="badge">模型综合答复</span> : <Badge value={answer.status} />}
        <Badge value={answer.review_status} />
        <span>{answer.mode === "solution" ? "处理方案" : "业务答疑"}</span>
      </div>
      {answer.format === "wiki_markdown" ? <>
        <p className="muted">{answer.grounding_status === "SOURCE_LINKED" ? "引用已关联到本地原文；关联不代表结论已通过专业复核。" : "本答复未绑定可核对的本地引用，属于模型一般分析，不能当作已验证业务结论。"}</p>
        {answer.server_notice && <p className="consultation-scope-note">{answer.server_notice}</p>}
        <EvidenceReviewNotice report={evidenceReview}/>
        <WikiAnswerMarkdown text={answer.narrative_markdown ?? answer.summary} citations={answer.citations}/>
        {(answer.quality_warnings?.length ?? 0)>0 && <details className="answer-quality-warnings"><summary>核对提示（{answer.quality_warnings!.length}）</summary>
          <ul>{answer.quality_warnings!.map((warning,i)=><li key={i}>{warning.message}</li>)}</ul>
        </details>}
      </> : <p className="answer-summary">{answer.summary}</p>}
      {answer.analysis && <section className="answer-analysis" aria-label="公开分析依据与处理分支">
        <h3>分析依据与处理分支</h3>
        <p>{answer.analysis.interpretation}</p>
        {answer.analysis.checks.map((item,i)=><div className="analysis-check" key={i}>
          <h4>{item.title}</h4><p>{item.reason}<EvidenceLinks ids={item.evidence_ids} citations={answer.citations}/></p>
        </div>)}
        {answer.analysis.branches.length>0 && <ol className="analysis-branches">{answer.analysis.branches.map((item,i)=><li key={i}>
          <strong>适用前提（待核对）：{item.condition}</strong><p>{item.action}<EvidenceLinks ids={item.evidence_ids} citations={answer.citations}/></p>
        </li>)}</ol>}
      </section>}
      {Object.entries(answer.scope).length > 0 && (
        <details>
          <summary>适用范围</summary>
          <dl>
            {Object.entries(answer.scope).map(([k, v]) => (
              <div key={k}>
                <dt>{contextFields.find(([key]) => key === k)?.[1] ?? k}</dt>
                <dd>{v ?? "未提供"}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
      {answer.claims.length > 0 && (
        <section>
          <h3>依据与解答</h3>
          {answer.claims.map((claim) => (
            <p key={claim.id} className="claim">
              {claim.text}
              <EvidenceLinks
                ids={claim.evidence_ids}
                citations={answer.citations}
              />
            </p>
          ))}
        </section>
      )}
      {answer.facts.length > 0 && (
        <details>
          <summary>已知事实</summary>
          <div className="fact-list">
            {answer.facts.map((fact, i) => (
              <div key={i}>
                <strong>{readable(fact.name)}</strong>
                <span>{readable(fact.value)}</span>
                <small>
                  {readable(fact.origin)} · {readable(fact.certainty)}
                </small>
              </div>
            ))}
          </div>
        </details>
      )}
      {answer.solution && (
        <section className="solution">
          <h3>{answer.solution.goal}</h3>
          <div className="solution-preconditions">
            <TextList title="前提条件" values={answer.solution.preconditions} />
            <TextList title="所需材料" values={answer.solution.materials} />
          </div>
          {answer.solution.steps.map((step, i) => (
            <article className="solution-step" key={step.id}>
              <div className="step-number">{i + 1}</div>
              <div>
                <h4>{step.action}</h4>
                <dl>
                  <dt>责任角色</dt>
                  <dd>{step.owner_role}</dd>
                  <dt>输入</dt>
                  <dd>
                    {step.inputs.length
                      ? step.inputs.join("、")
                      : "来源未登记，需人工补充"}
                  </dd>
                  <dt>输出</dt>
                  <dd>{step.output}</dd>
                  <dt>验证要求</dt>
                  <dd>{step.verification}</dd>
                  {step.depends_on.length > 0 && (
                    <>
                      <dt>前置步骤</dt>
                      <dd>{step.depends_on.join("、")}</dd>
                    </>
                  )}
                </dl>
                <EvidenceLinks
                  ids={step.evidence_ids}
                  citations={answer.citations}
                />
              </div>
            </article>
          ))}
          {answer.solution.branches.length > 0 && (
            <section>
              <h4>条件分支</h4>
              {answer.solution.branches.map((branch, i) => (
                <p key={i}>
                  <strong>{branch.condition}</strong>：{branch.action}
                </p>
              ))}
            </section>
          )}
          <TextList
            title="完成检查"
            values={answer.solution.completion_checks}
          />
          <TextList title="升级处理" values={answer.solution.escalation} />
          <small className="muted">
            此处展示处理要求；步骤未自动执行或确认完成。
          </small>
        </section>
      )}
      {answer.citations.length > 0 && (
        <section className="answer-sources">
          <h3>
            来源引用 <span>{answer.citations.length}</span>
          </h3>
          {answer.format === "wiki_markdown" ? [...sources].map(([version, citations]) =>
            <details className="wiki-answer-source-group" key={version}>
              <summary>{citations[0].source_title} · {citations.length} 处原文定位</summary>
              {citations.map(sourceCard)}
            </details>) : answer.citations.map(sourceCard)}
        </section>
      )}
      {answer.format !== "wiki_markdown" && <TextList title="适用限制" values={answer.limitations} />}
      <TextList title="尚需补充的资料" values={answer.required_sources} />
    </div>
  );
});
function TextList({ title, values }: { title: string; values: string[] }) {
  return values.length > 0 ? (
    <section>
      <h4>{title}</h4>
      <ul>
        {values.map((v, i) => (
          <li key={i}>{v}</li>
        ))}
      </ul>
    </section>
  ) : null;
}
export function ConsultationPage({
  initialResource,
}: {
  initialResource?: Resource;
}) {
  const app = useApp();
  return <ConsultationWorkspace key={JSON.stringify([app.me.id, app.space.id])} initialResource={initialResource} />;
}

function ConsultationWorkspace({ initialResource }: { initialResource?: Resource }) {
  const model = useModelState();
  const configuredGeneration = model.data?.model?.provider === "http" && model.data.model.configured;
  const referenceReady =
    !model.loading && !model.error &&
    Array.isArray(model.data?.answer_scopes) &&
    model.data.answer_scopes.includes("reference");
  const app = useApp();
  const [threadId, setThreadId] = useState<string>();
  const [runs, setRuns] = useState<Run[]>([]);
  const previewIdentity = useRef(`${app.me.id}:${app.space.id}:${threadId ?? ""}`);
  const suppressedPreviews = useRef(new Set<string>());
  const [question, setQuestion] = useState("");
  const questionElement = useRef<HTMLTextAreaElement>(null);
  const [modelSelection, setModelSelection] = useConsultationModelPreference(app.me.id);
  const retrievalProfile = useRetrievalProfile();
  const submissions = useRef(new Set<AbortController>());
  useEffect(() => () => {
    for (const controller of submissions.current) controller.abort();
    submissions.current.clear();
  }, []);
  const composing = useRef(false);
  const compositionEndedAt = useRef(Number.NEGATIVE_INFINITY);
  const composerHintId = useId();
  const historyPanelId = useId();
  const [mode, setMode] = useState("auto");
  const [context, setContext] = useState<Record<string, string>>({});
  const [showContext, setShowContext] = useState(false);
  const [showAttachments, setShowAttachments] = useState(false);
  const [documentsRequested, setDocumentsRequested] = useState(false);
  const [attachmentIds, setAttachmentIds] = useState<string[]>(
    initialResource?.active_version_id
      ? [initialResource.active_version_id]
      : [],
  );
  const [parent, setParent] = useState<Run>();
  // Legacy runs used the API's formal default. Never inherit the current picker.
  const effectiveAnswerScope = parent
    ? (parent.answer_scope ?? "formal")
    : "reference";
  const referenceBlocked = !referenceReady && effectiveAnswerScope === "reference";
  const [facts, setFacts] = useState<Record<string, string>>({});
  const [runQuestions, setRunQuestions] = useState<Record<string, string>>({});
  const [historyFilter, setHistoryFilter] = useState("");
  const [pollError, setPollError] = useState<Error>();
  const [feedback, setFeedback] = useState<{ run: Run; kind: string }>();
  const [issueRun, setIssueRun] = useState<Run>();
  const [remove, setRemove] = useState<Thread>();
  const task = useTask();
  const action = useTask();
  const bottom = useRef<HTMLDivElement>(null);
  const identity = useRef<string | undefined>(threadId);
  identity.current = threadId;
  const history = useLoad(
    (signal) => allPages<Thread>("/threads", signal),
    [app.space.id, app.refresh],
  );
  const loaded = useLoad(
    (signal) =>
      threadId
        ? allPages<Run>(`/threads/${threadId}`, signal)
        : Promise.resolve([]),
    [threadId, app.space.id, app.me.id, app.refresh],
  );
  const documents = useLoad(
    (signal) =>
      documentsRequested ? allPages<Resource>(
        `/resources?${query({ space_id: app.space.id, kind: "document" })}`,
        signal,
      ) : Promise.resolve([]),
    [app.space.id, app.refresh, documentsRequested],
  );
  useEffect(() => {
    setRuns([]);
    previewIdentity.current = `${app.me.id}:${app.space.id}:${threadId ?? ""}`;
    suppressedPreviews.current.clear();
    setParent(undefined);
    setFacts({});
    setPollError(undefined);
  }, [threadId, app.me.id, app.space.id]);
  useEffect(() => {
    if (loaded.data)
      setRuns((current) => {
        const stored = loaded.data ?? [];
        return [
          ...stored,
          ...current.filter(
            (r) =>
              r.thread_id === threadId && !stored.some((v) => v.id === r.id),
          ),
        ].sort((a, b) =>
          (a.created_at ?? "").localeCompare(b.created_at ?? ""),
        );
      });
  }, [loaded.data]);
  useEffect(() => {
    if (initialResource && !initialResource.active_version_id) {
      const controller = new AbortController();
      get<Page<Version>>(
        `/resources/${initialResource.id}/versions?limit=1`,
        controller.signal,
      )
        .then((result) => {
          if (result.items[0]) setAttachmentIds([result.items[0].id]);
        })
        .catch((error) => {
          if (!controller.signal.aborted) setPollError(error);
        });
      return () => controller.abort();
    }
  }, [initialResource?.id]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end", behavior: "instant" });
  }, [runs.length]);
  const pending = runs.filter((r) => ["RUNNING", "QUEUED"].includes(r.state));
  function blockedReasonFor(draft: string) {
    return referenceBlocked
      ? (parent ? referenceFollowupBlockedNote : referenceBlockedNote)
      : retrievalProfile.blockedReason ? retrievalProfile.blockedReason
      : pending.length > 0
        ? "请等待当前咨询完成后再提交。"
        : !draft.trim() ? "请输入具体问题。"
        : !modelSelection && !configuredGeneration ? "请先在业务背景旁选择可用模型。" : undefined;
  }
  const sendBlockedReason = blockedReasonFor(question);
  const sendDisabled = task.busy || Boolean(sendBlockedReason);
  const polling = useConsultationRunPolling({
    scope: JSON.stringify([app.me.id, app.space.id, threadId, app.refresh]), runs,
    enabled: !loaded.loading && !loaded.error,
    onRun: next => setRuns(previous => replaceRun(previous, next)),
    withdrawPreview: id => setRuns(previous => {
      const current = previous.find(run => run.id === id);
      return current?.model_snapshot?.public_preview ? replaceRun(previous, withoutPreview(current)) : previous;
    }),
  });
  const currentPollError = pollError ?? Object.values(polling.errors)[0];
  useEffect(() => {
    if (loaded.data) { polling.clearErrors(); setPollError(undefined); }
  }, [loaded.data]);
  async function operateRun(run: Run, operation: "cancel" | "retry") {
    const controller = new AbortController();
    submissions.current.add(controller);
    polling.pause(run.id);
    if (operation === "cancel") {
      suppressedPreviews.current.add(run.id);
      setRuns(values => values.map(value => value.id === run.id ? withoutPreview(value) : value));
    }
    let readStarted = false;
    try {
      await api<Job>(`/jobs/${run.job_id}/${operation}`, { method: "POST", signal: controller.signal });
      if (controller.signal.aborted) return;
      readStarted = true;
      await polling.readNow(run);
      if (!controller.signal.aborted && operation === "cancel") app.notify("取消请求已提交");
    } catch (error) {
      if (!controller.signal.aborted) throw error;
    } finally {
      submissions.current.delete(controller);
      if (!readStarted && !controller.signal.aborted) polling.resume(run);
    }
  }
  async function send() {
    if (task.busy) return;
    // Native input/IME events can precede React's committed render. Submit the
    // visible draft, never the previous render's question after a clear/edit.
    const draft = questionElement.current?.value ?? question;
    if (draft !== question) setQuestion(draft);
    const blocked = blockedReasonFor(draft);
    if (blocked) throw new Error(blocked);
    // Freeze the complete payload before any await, including thread creation.
    const sentQuestion =
      draft.trim() +
      (Object.keys(facts).length
        ? `\n补充事实：\n${Object.entries(facts)
            .map(([key, value]) => `${key}：${value}`)
            .join("\n")}`
        : "");
    const fullContext = {
      ...context,
      ...Object.fromEntries(
        Object.entries(facts).filter(([key]) =>
          contextFields.some(([field]) => field === key),
        ),
      ),
    };
    const request = {
      question: sentQuestion,
      mode,
      answer_scope: effectiveAnswerScope,
      reasoning_strategy: "model_first",
      require_model: true,
      context: Object.fromEntries(
        Object.entries(fullContext)
          .filter(([, v]) => v.trim())
          .map(([key, value]) => [
            key,
            key === "knowledge_cutoff" ? new Date(value).toISOString() : value,
          ]),
      ),
      ...(parent ? { parent_run_id: parent.id } : {}),
      attachment_version_ids: [...attachmentIds],
      ...(modelSelection ? { model_selection: { ...modelSelection } } : {}),
      ...(retrievalProfile.selection ? { retrieval_selection: { ...retrievalProfile.selection } } : {}),
    };
    const controller = new AbortController();
    submissions.current.add(controller);
    try {
      let targetId = threadId;
      if (!targetId) {
        const thread = await api<Thread>("/threads", {
          method: "POST", signal: controller.signal,
          body: { space_id: app.space.id, title: draft.slice(0, 100) },
        });
        if (controller.signal.aborted) return;
        targetId = thread.id;
        identity.current = targetId;
        setThreadId(targetId);
      }
      const run = await api<Run>(`/threads/${targetId}/runs`, {
        method: "POST", body: request, signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setRunQuestions((previous) => ({ ...previous, [run.id]: sentQuestion }));
      if (identity.current === targetId)
        setRuns((previous) =>
          previous.some((v) => v.id === run.id) ? previous : [...previous, run],
        );
      setQuestion("");
      setParent(undefined);
      setFacts({});
      history.reload();
    } catch (error) {
      if (!controller.signal.aborted) throw error;
    } finally {
      submissions.current.delete(controller);
    }
  }
  return (
    <>
      <div className="consultation-workspace">
      <header className="page-heading consultation-heading">
        <div>
          <h1>智能答疑</h1>
          <p>从可追溯的资料出发，找到适合当前业务的答案。</p>
        </div>
      </header>
      <div className="consultation-layout">
        <aside className="conversation-history" id={historyPanelId}>
          <button
            className="new-conversation"
            disabled={task.busy}
            onClick={() => {
              setThreadId(undefined);
              setRuns([]);
              setParent(undefined);
              setQuestion("");
              setFacts({});
              setAttachmentIds([]);
            }}
          >
            <Plus />
            新建咨询
          </button>
          <label className="history-search">
            <input
              aria-label="搜索咨询历史"
              placeholder="搜索咨询历史"
              value={historyFilter}
              onChange={(e) => setHistoryFilter(e.target.value)}
            />
          </label>
          <h3>
            <ClockCounterClockwise />
            历史咨询
          </h3>
          <ErrorBox error={history.error} retry={history.reload} />
          {history.loading && <Loading />}
          {history.data
            ?.filter(
              (t) =>
                t.space_id === app.space.id && t.title.includes(historyFilter),
            )
            .map((t) => (
              <div
                className={`history-item ${threadId === t.id ? "active" : ""}`}
                key={t.id}
              >
                <button
                  disabled={task.busy}
                  title={t.title}
                  onClick={() => {
                    setThreadId(t.id);
                  }}
                >
                  <ChatCircleDots />
                  <span>
                    {t.title}
                    <small>
                      {new Date(t.created_at).toLocaleDateString("zh-CN")}
                    </small>
                  </span>
                </button>
                <button
                  className="icon-button danger"
                  aria-label={`删除咨询 ${t.title}`}
                  onClick={() => setRemove(t)}
                >
                  <Trash size={16} />
                </button>
              </div>
            ))}
          {history.data?.filter((t) => t.space_id === app.space.id).length ===
            0 && <p className="muted">你的咨询记录会保存在这里。</p>}
        </aside>
        <ConsultationHistoryResizeHandle ownerId={app.me.id} spaceId={app.space.id} panelId={historyPanelId} />
        <section className="conversation-main">
          <div className="conversation-scroll">
            <ErrorBox error={loaded.error} retry={loaded.reload} />
            {loaded.loading && threadId && <Loading />}
            {runs.length === 0 && !loaded.loading && (
              <div className="consultation-welcome">
                <ChatCircleDots size={52} weight="light" />
                <h2>让每一个解答，都有依据</h2>
                <p>
                  描述你的业务问题，或附上需要核对的资料。
                  <br />
                  证据不足时，会说明缺少的信息。
                </p>
                <div className="consultation-capabilities">
                  <span>
                    <FileText />
                    原文溯源
                  </span>
                  <span>
                    <Link />
                    精确引用
                  </span>
                  <span>
                    <CheckCircle />
                    条件核验
                  </span>
                </div>
              </div>
            )}
            {runs.map((run) => (
              <Motion
                identity={run.id}
                className="consultation-run"
                key={run.id}
              >
                <div className="user-question">
                  {run.question ?? runQuestions[run.id] ?? "历史咨询"}
                  {run.created_at && (
                    <small>
                      {new Date(run.created_at).toLocaleString("zh-CN")}
                    </small>
                  )}
                </div>
                <div className="assistant-response">
                  <RunStatus run={run} finalRead={polling.finalReads[run.id]} readFailed={!!polling.errors[run.id]} />
                  {run.invalidated && (
                    <Notice>
                      此回答引用的来源已发生变化，结果保留为历史记录，请重新核验后使用。
                    </Notice>
                  )}
                  {!loaded.loading && !loaded.error && <RunEvidenceDiagnostic run={run} />}
                  {["QUEUED", "RUNNING"].includes(run.state) && (
                    <div className="pending-answer">
                      {polling.finalReads[run.id] && polling.errors[run.id]
                        ? <p>服务端处理已结束，完整结果尚未读到；请手动重试读取。</p>
                        : <Loading label={polling.finalReads[run.id] ? "正在读取完整结果…" : `${phaseLabel(run)}…`} />}
                      {!polling.finalReads[run.id] && <button
                        disabled={action.busy}
                        onClick={() =>
                          void action.run(async () => {
                            await operateRun(run, "cancel");
                          })
                        }
                      >
                        <Stop />
                        取消
                      </button>}
                    </div>
                  )}
                  <AnswerPreview run={run} readable={!loaded.loading && !loaded.error && !pollError && !polling.errors[run.id] &&
                    !suppressedPreviews.current.has(run.id) &&
                    previewIdentity.current === `${app.me.id}:${app.space.id}:${threadId ?? ""}`} />
                  {run.state === "FAILED" && run.failure_diagnostic && !run.invalidated && <section className="answer-failure" role="alert">
                    <p>{run.failure_diagnostic.message}</p>
                    {run.model_snapshot?.last_request?.state === "received" && <p>本轮已收到模型输出，问题发生在返回后的完整性或答案检查阶段。</p>}
                    <small>{run.failure_diagnostic.code}</small>
                    {run.failure_diagnostic.schema_errors.length>0 && <ul>{run.failure_diagnostic.schema_errors.map((item,i)=><li key={i}>
                      {item.path} · {item.rule}
                    </li>)}</ul>}
                    <p>{run.failure_diagnostic.next_step}</p>
                  </section>}
                  {run.answer ? (
                    <AnswerView answer={run.answer} evidenceReview={run.invalidated ? undefined : run.model_snapshot?.evidence_review} />
                  ) : (
                    !["QUEUED", "RUNNING"].includes(run.state) && !(run.failure_diagnostic && !run.invalidated) && (
                      <p className="muted">
                        {run.state === "CANCELLED"
                          ? "本次咨询已取消。"
                          : run.error_code
                            ? `未能完成本次咨询：${run.error_code}`
                            : "当前无法读取答案，可能因来源权限或有效性变化。"}
                      </p>
                    )
                  )}
                  {run.answer && (
                    <>
                      <div className="answer-actions">
                        <button
                          onClick={() => setFeedback({ run, kind: "HELPFUL" })}
                        >
                          <ThumbsUp />
                          有帮助
                        </button>
                        <button
                          onClick={() => setFeedback({ run, kind: "WRONG" })}
                        >
                          <Flag />
                          问题反馈
                        </button>
                        <button
                          disabled={task.busy}
                          onClick={() => {
                            setParent(run);
                            setQuestion(
                              run.question ??
                                runQuestions[run.id] ??
                                "请结合补充事实继续核对上一问",
                            );
                            setFacts({});
                            setShowContext(true);
                          }}
                        >
                          <Plus />
                          补充事实
                        </button>
                        <button onClick={() => setIssueRun(run)}>
                          提交问题单
                        </button>
                        <button
                          aria-label="复制回答与引用"
                          onClick={() =>
                            void action.run(async () => {
                              await navigator.clipboard.writeText(
                                `${runScopeLabel(run)}${run.answer_scope === "reference" ? `\n${answerScopes.reference.note}` : ""}\n\n${run.answer!.narrative_markdown ?? run.answer!.summary}\n\n${run.answer!.claims.map((c) => c.text).join("\n")}\n\n${run.answer!.format === "wiki_markdown" ? [...run.answer!.limitations,...(run.answer!.quality_warnings??[]).map(w=>w.message)].join("\n") : ""}\n来源：\n${run.answer!.citations.map((c) => `${c.id} ${c.source_title} · ${readable(c.locator.label)} · ${c.version_id} / ${c.block_id}`).join("\n")}`,
                              );
                              app.notify("回答与来源已复制");
                            })
                          }
                        >
                          <Copy />
                        </button>
                      </div>
                      {run.answer.missing_facts.length > 0 && (
                        <div className="missing-facts">
                          <h4>请补充以下事实</h4>
                          {run.answer.missing_facts.map((f, i) => (
                            <div key={i}>
                              <strong>{readable(f.question)}</strong>
                              <p>{readable(f.why_needed)}</p>
                            </div>
                          ))}
                          <button
                            disabled={task.busy}
                            onClick={() => {
                              setParent(run);
                              setQuestion(
                                run.question ??
                                  runQuestions[run.id] ??
                                  "请根据补充事实继续核对",
                              );
                              setFacts(
                                Object.fromEntries(
                                  run.answer!.missing_facts.map((f) => [
                                    readable(f.field),
                                    "",
                                  ]),
                                ),
                              );
                              setShowContext(true);
                            }}
                          >
                            填写缺失事实
                          </button>
                        </div>
                      )}
                    </>
                  )}
                  {run.state === "FAILED" && (
                    <button
                      onClick={() =>
                        void action.run(async () => {
                          await operateRun(run, "retry");
                        })
                      }
                    >
                      <ArrowClockwise />
                      重试本次任务
                    </button>
                  )}
                </div>
              </Motion>
            ))}
            <div ref={bottom} />
          </div>
          <div className="composer-wrap">
            <ErrorBox
              error={task.error ?? action.error ?? currentPollError}
              retry={currentPollError ? () => loaded.reload() : undefined}
            />
            {parent && (
              <div className="followup-label">
                <Link size={16} />
                补充上一轮事实 · {parent.id.slice(0, 8)}
                <span>{runScopeLabel(parent)}</span>
                <button
                  aria-label="取消补充模式"
                  disabled={task.busy}
                  onClick={() => {
                    setParent(undefined);
                    setFacts({});
                  }}
                >
                  <X />
                </button>
              </div>
            )}
            {showContext && (
              <div className="context-panel">
                <div className="form-grid">
                  {Object.entries(facts).map(([key, value]) => (
                    <Field
                      key={key}
                      label={
                        (parent?.answer?.missing_facts.find(
                          (f) => f.field === key,
                        )?.question as string) ?? key
                      }
                    >
                      <input
                        value={value}
                        onChange={(e) =>
                          setFacts({ ...facts, [key]: e.target.value })
                        }
                      />
                    </Field>
                  ))}
                  {contextFields.map(([key, label]) => (
                    <Field key={key} label={label}>
                      <input
                        type={
                          key === "business_date"
                            ? "date"
                            : key === "knowledge_cutoff"
                              ? "datetime-local"
                              : "text"
                        }
                        value={
                          key === "knowledge_cutoff" && context[key]
                            ? context[key].slice(0, 16)
                            : (context[key] ?? "")
                        }
                        onChange={(e) =>
                          setContext({
                            ...context,
                            [key]: e.target.value,
                          })
                        }
                      />
                    </Field>
                  ))}
                </div>
              </div>
            )}
            {showAttachments && (
              <div className="attachment-panel">
                <h4>附加来源版本（作为用户事实）</h4>
                <ErrorBox error={documents.error} retry={documents.reload} />
                {documents.loading && <Loading />}
                {documents.data?.map((r) => (
                  <label className="checkbox-label" key={r.id}>
                    <input
                      type="checkbox"
                      checked={Boolean(
                        r.active_version_id &&
                        attachmentIds.includes(r.active_version_id),
                      )}
                      disabled={
                        !r.active_version_id ||
                        (!attachmentIds.includes(r.active_version_id) &&
                          attachmentIds.length >= 10)
                      }
                      onChange={(e) =>
                        setAttachmentIds((ids) =>
                          e.target.checked
                            ? [...ids, r.active_version_id!]
                            : ids.filter((id) => id !== r.active_version_id),
                        )
                      }
                    />
                    {r.name}
                    {!r.active_version_id && (
                      <small>未发布，请从文档页选择具体版本</small>
                    )}
                  </label>
                ))}
                <small>
                  最多 10 个版本；
                  {effectiveAnswerScope === "reference"
                    ? "附件作为参考材料与用户事实，不代表已核验发布。"
                    : "附件不替代已经复核发布的业务知识。"}
                </small>
              </div>
            )}
            {attachmentIds.length > 0 && (
              <div className="attachment-chips">
                {attachmentIds.map((id) => (
                  <span key={id}>
                    <Paperclip size={14} />
                    {documents.data?.find((r) => r.active_version_id === id)
                      ?.name ??
                      initialResource?.name ??
                      `版本 ${id.slice(0, 8)}`}
                    <button
                      aria-label="移除附件"
                      onClick={() =>
                        setAttachmentIds((ids) =>
                          ids.filter((value) => value !== id),
                        )
                      }
                    >
                      <X size={14} />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <form
              className="question-composer"
              onSubmit={(e) => {
                e.preventDefault();
                void task.run(send);
              }}
            >
              <textarea
                ref={questionElement}
                aria-label="咨询问题"
                aria-describedby={composerHintId}
                rows={2}
                required
                maxLength={10000}
                value={question}
                placeholder="描述你的业务问题，可补充业务日期、产品类型或具体场景…"
                onChange={(e) => setQuestion(e.target.value)}
                onCompositionStart={() => { composing.current = true; }}
                onCompositionEnd={(e) => {
                  composing.current = false;
                  compositionEndedAt.current = e.timeStamp;
                }}
                onKeyDown={(e) => {
                  if (e.key !== "Enter" || e.shiftKey || e.altKey) return;
                  // Safari may report compositionend before its confirming Enter.
                  if (composing.current || e.nativeEvent.isComposing ||
                    e.nativeEvent.keyCode === 229 || e.timeStamp - compositionEndedAt.current < 50) return;
                  e.preventDefault();
                  const draft = e.currentTarget.value;
                  if (draft !== question) setQuestion(draft);
                  if (e.repeat || task.busy || blockedReasonFor(draft)) return;
                  e.currentTarget.form?.requestSubmit();
                }}
              />
              <footer>
                <div className="inline-actions">
                  <select
                    aria-label="回答方式"
                    value={mode}
                    onChange={(e) => setMode(e.target.value)}
                  >
                    <option value="auto">自动判断</option>
                    <option value="answer">业务答疑</option>
                    <option value="solution">处理方案</option>
                  </select>
                  <button
                    type="button"
                    aria-label="添加资料附件"
                    onClick={() => { setDocumentsRequested(true); setShowAttachments((v) => !v); }}
                  >
                    <Paperclip />
                  </button>
                  <button
                    type="button"
                    aria-expanded={showContext}
                    onClick={() => setShowContext((v) => !v)}
                  >
                    业务背景
                  </button>
                  <ModelPicker
                    value={modelSelection}
                    onChange={setModelSelection}
                    requireTransfer
                    disabled={task.busy}
                    appearance="icon"
                    label="选择答疑模型"
                    emptyDescription={configuredGeneration ? "未选择个人模型，将使用系统配置的模型" : "未选择模型，请先选择可用模型；不会自动退回资料摘录"}
                  />
                  <RetrievalProfilePicker value={retrievalProfile} compact disabled={task.busy} />
                </div>
                <button
                  className="primary send-button"
                  aria-label="发送问题"
                  title={sendBlockedReason ?? "发送问题（Enter；Shift + Enter 换行）"}
                  disabled={sendDisabled}
                >
                  <ArrowUp size={21} />
                </button>
              </footer>
            </form>
            {!!retrievalProfile.blockedReason && !retrievalProfile.loading && (
              <p className="retrieval-scheme-used" role="status">{retrievalProfile.blockedReason}</p>
            )}
            {referenceBlocked && (
              <div className="composer-service-state" role="status">
                <span>{model.loading ? "正在连接答疑服务…" : model.error ? "无法读取答疑服务状态" : "答疑服务暂未就绪"}</span>
                <button type="button" onClick={model.reload} disabled={model.loading || task.busy} aria-label="刷新答疑服务状态">
                  <ArrowClockwise size={14} />刷新
                </button>
              </div>
            )}
            <p className="composer-note" id={composerHintId}>
              Enter 发送 · Shift + Enter 换行。回答仅供参考，请核对原文与适用条件。
            </p>
          </div>
        </section>
      </div>
      </div>
      {feedback && (
        <FormModal
          title="反馈本次回答"
          close={() => setFeedback(undefined)}
          label="提交反馈"
          submit={async (data) => {
            const result = await post<{ id: string; case_id: string | null }>(
              `/runs/${feedback.run.id}/feedback`,
              {
                kind: textValue(data, "kind"),
                comment: textValue(data, "comment"),
              },
            );
            app.notify(
              result.case_id
                ? `反馈已记录，并关联问题单 ${result.case_id.slice(0, 8)}`
                : "感谢反馈，已记录",
            );
          }}
        >
          <Field label="反馈类型">
            <select name="kind" defaultValue={feedback.kind}>
              <option value="HELPFUL">有帮助</option>
              <option value="WRONG">结论有误</option>
              <option value="MISSING">内容缺失</option>
              <option value="OUTDATED">资料过期</option>
              <option value="OTHER">其他问题</option>
            </select>
          </Field>
          <Field label="具体说明">
            <textarea name="comment" maxLength={5000} />
          </Field>
        </FormModal>
      )}
      {issueRun && (
        <FormModal
          title="提交内部问题单"
          close={() => setIssueRun(undefined)}
          label="创建问题单"
          submit={async (data) => {
            await post("/cases", {
              space_id: app.space.id,
              run_id: issueRun.id,
              title: textValue(data, "title"),
              description: textValue(data, "description"),
            });
            app.notify("问题单已创建，可到问题反馈页跟进");
          }}
        >
          <Field label="问题标题">
            <input required name="title" maxLength={300} />
          </Field>
          <Field label="需要核实的事项">
            <textarea required name="description" maxLength={10000} />
          </Field>
          <Notice>创建内部问题单，访问按权限控制。</Notice>
        </FormModal>
      )}
      {remove && (
        <FormModal
          title="删除咨询记录"
          close={() => setRemove(undefined)}
          label="删除记录"
          danger
          submit={async () => {
            await del(`/threads/${remove.id}`);
            if (threadId === remove.id) {
              setThreadId(undefined);
              setRuns([]);
            }
            history.reload();
          }}
        >
          <p>将移除“{remove.title}”的咨询视图，审计保留由服务端策略处理。</p>
        </FormModal>
      )}
    </>
  );
}
