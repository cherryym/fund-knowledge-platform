export type EvidenceReviewReport = {
  version: string;
  status: string;
  answer_sha256: string;
  answer_rewritten: false;
  semantic_entailment: string;
  observation_origin?: string;
  issue_count: number;
  critical_count: number;
  issues: {code: string; message: string; severity: string; evidence_ids: string[];
    answer_start?: number; answer_end?: number}[];
};

export function EvidenceReviewNotice({report}: {report?: EvidenceReviewReport}) {
  if (!report || report.version !== "evidence_review_v1" || !Array.isArray(report.issues) || !report.issues.length) return null;
  const critical = report.issues.some(issue => issue.severity === "critical");
  return <section className="evidence-review-notice" data-severity={critical ? "critical" : "warning"}
    aria-label="具体来源与业务疑点" role="status">
    <strong>{critical ? "发现具体来源冲突或分录疑点，请先核对，勿直接执行" : "来源适用边界仍需核对"}</strong>
    <ul>{report.issues.map((issue, index) => <li key={`${issue.code}-${index}`}>
      {issue.message}{issue.evidence_ids.length > 0 && <small>相关原文：{issue.evidence_ids.join("、")}</small>}
    </li>)}</ul>
    <p>这些是结构与引用对照提示，不是完整语义审查或专家结论。正文原样保留；未发现提示也不代表全部结论正确。</p>
    {report.observation_origin === "current_authorized_read_projection" && <small>按本次可访问的冻结来源核对；未重写历史答案或重新调用模型。</small>}
  </section>;
}
