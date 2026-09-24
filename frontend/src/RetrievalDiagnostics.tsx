export type CandidateTrace = {
  version: string; query_sha256?: string; candidate_count: number; reranked_count: number; unscored_count: number;
  candidate_policy?: string; channel_counts: Record<string, number>; phases_ms: Record<string, number>;
  timing_scope?: string; semantic_support?: string;
};
export type PipelineTiming = {
  version: string; execution_elapsed_ms: number; phases: Record<string, {elapsed_ms: number; calls: number}>;
  first_visible_answer_ms: number | null; first_visible_answer_status: string;
};

const seconds = (value: unknown) => typeof value === "number" && Number.isFinite(value) && value >= 0
  ? `${(value / 1000).toFixed(3)} 秒` : "未测量";
const count = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0
  ? value.toLocaleString("zh-CN") : "未记录";
const phaseLabels: Record<string, string> = {
  initial_authority_ms: "初始权限与索引核对", candidate_search_ms: "混合候选检索",
  candidate_verification_and_fusion_ms: "候选原文核对与融合", reranking_ms: "候选语义重排",
  final_authority_ms: "最终权限与索引复验", model_planning: "模型问题研判",
  model_synthesis: "模型综合回答", model_wiki_index: "模型查阅索引", model_wiki_notes: "模型分包阅读",
  local_model_preparation: "本地模型准备", catalog_and_policy: "目录与来源策略",
  retrieval_and_navigation: "检索与图谱导航", source_reading: "完整原文读取", context_reranking: "证据组重排",
};

export function RetrievalDiagnostics({trace}: {trace: CandidateTrace}) {
  return <details className="consultation-run-details" data-retrieval-diagnostics={trace.version}>
    <summary>检索链路诊断 · {count(trace.candidate_count)} 个片段 / {count(trace.reranked_count)} 个已重排</summary>
    <p>本次候选池（片段单元）：{count(trace.candidate_count)} · 已重排：{count(trace.reranked_count)} · 未评分：{count(trace.unscored_count)}</p>
    <p className="muted">上方返回数量按候选页计算；这里按页内片段单元计算，两种数量不要求相同。</p>
    <p>候选策略：{trace.candidate_policy === "complete_pool" ? "完整融合候选池重排"
      : trace.candidate_policy === "ranked_prefix" ? "旧策略：仅融合排名前部重排" : "未记录"}</p>
    <p>向量通道：{count(trace.channel_counts.vector)} · BM25 通道：{count(trace.channel_counts.bm25)} · 目录加权：{count(trace.channel_counts.catalog)}</p>
    {Object.keys(trace.phases_ms).length > 0 && <dl className="retrieval-config">{Object.entries(trace.phases_ms).map(([phase, ms]) =>
      <div key={phase}><dt>{phaseLabels[phase] ?? "其他阶段"}</dt><dd>{seconds(ms)}</dd></div>)}</dl>}
    {trace.timing_scope?.startsWith("shared_batch") && <p className="muted">阶段耗时为整个查询批次共享，不能逐问题相加。</p>}
    <p className="muted">不同通道可能命中同一原文，数量不能相加当作独立证据。以上只说明当前候选与计算状态，不是全库召回率或专业准确率。</p>
  </details>;
}

export function AnswerTimingDetails({timing}: {timing: PipelineTiming}) {
  return <details className="consultation-run-details" data-pipeline-timing={timing.version}>
    <summary>全链路耗时 · 执行 {seconds(timing.execution_elapsed_ms)}</summary>
    <dl className="retrieval-config">{Object.entries(timing.phases).map(([phase, row]) => <div key={phase}>
      <dt>{phaseLabels[phase] ?? "其他阶段"}</dt><dd>{seconds(row.elapsed_ms)} · {count(row.calls)} 次</dd>
    </div>)}</dl>
    <p className="muted">模型阶段包含网络、服务商排队和来源复验，不能等同于纯思考时间。阶段可能嵌套，不能直接相加；执行耗时不包含作业开始前的排队。</p>
    <p className="muted">首个可见答案：{timing.first_visible_answer_status === "MEASURED" ? seconds(timing.first_visible_answer_ms) : "未测量，不能用作完整答复耗时"}。</p>
  </details>;
}
