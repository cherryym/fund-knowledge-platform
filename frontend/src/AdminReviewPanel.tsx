import { useEffect, useRef, useState } from "react";
import { ApiError, get, post } from "./api";
import type { Version } from "./types";
import { ErrorBox, Field, Loading, Notice, useApp, useLoad, useTask } from "./ui";

type AdminConfirmation = {
  mode: "ADMIN_CONFIRMED";
  actor_id: string;
  confirmed_at: string;
  reason: string;
  independent_review: false;
  version_id?: string;
  content_sha256?: string;
};
type AdminReviewStatus = {
  version_id: string;
  can_confirm: boolean;
  current_sha256: string;
  confirmation: AdminConfirmation | null;
};
type AdminReviewResult = {
  version_id: string;
  state: "APPROVED";
  confirmation: AdminConfirmation;
};
type Props = { version: Version; refresh: () => void; readOnly?: boolean };

function validConfirmation(record: AdminConfirmation, versionId: string, hash: string) {
  return record?.mode === "ADMIN_CONFIRMED" && record.independent_review === false
    && typeof record.actor_id === "string" && Boolean(record.actor_id.trim())
    && typeof record.reason === "string" && Boolean(record.reason.trim())
    && typeof record.confirmed_at === "string" && Number.isFinite(Date.parse(record.confirmed_at))
    && (record.version_id === undefined || record.version_id === versionId)
    && (record.content_sha256 === undefined || record.content_sha256 === hash);
}

function ConfirmationRecord({ record, compact }: { record: AdminConfirmation; compact?: boolean }) {
  const app = useApp();
  const actorLabel = app.me.id === record.actor_id ? (app.me.display_name || record.actor_id) : record.actor_id;
  const title = "最新管理员确认（非独立复核）";
  const content = <>
    <p>确认人 {actorLabel} · <time dateTime={record.confirmed_at}>{new Date(record.confirmed_at).toLocaleString("zh-CN")}</time></p>
    <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>确认原因：{record.reason}</p>
    <small>ADMIN_CONFIRMED · 非独立法规审核；法规效力、适用日期及生成时的未核验来源警告保持原记录。
      资料辅助答疑仍按现有权限提供，正式效力须按原独立流程核验。</small>
  </>;
  return compact ? <details className="review-record" aria-label={title} style={{ flexShrink: 0 }}>
    <summary>{title} · {actorLabel}</summary>{content}
  </details> : <article className="review-record" aria-label={title}>
    <strong>{title}</strong>{content}
  </article>;
}

// Remount consent and in-flight UI state whenever the reviewed snapshot or user changes.
export function AdminReviewPanel(props: Props) {
  const app = useApp();
  return <AdminReviewContent key={`${app.me.id}:${app.space.id}:${app.refresh}:${props.version.id}:${props.version.revision}:${props.version.state}`} {...props} />;
}

function AdminReviewContent({ version, refresh, readOnly }: Props) {
  const app = useApp();
  const task = useTask();
  const [validSources, setValidSources] = useState(false);
  const [notIndependent, setNotIndependent] = useState(false);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState<AdminConfirmation>();
  const [needsReload, setNeedsReload] = useState(false);
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);
  const path = `/versions/${encodeURIComponent(version.id)}/admin-review`;
  const status = useLoad(async (signal) => {
    const result = await get<AdminReviewStatus>(path, signal);
    if (result?.version_id !== version.id || typeof result.can_confirm !== "boolean"
      || !/^[0-9a-f]{64}$/.test(result.current_sha256)
      || (result.confirmation !== null && !validConfirmation(result.confirmation, version.id, result.current_sha256)))
      throw new Error("管理员确认信息不完整或与当前版本不一致，请重新读取。");
    return result;
  }, [version.id, version.revision]);
  const record = confirmed ?? status.data?.confirmation;
  if (readOnly) {
    if (record) return <ConfirmationRecord record={record} compact />;
    return status.error ? <Notice>管理员确认记录暂不可用，当前无法核对最新确认。</Notice> : null;
  }
  const unavailable = status.error instanceof ApiError && [404, 405, 501].includes(status.error.status);
  const eligibleState = version.state === "DRAFT" || version.state === "IN_REVIEW";
  const reasonLength = Array.from(reason.trim()).length;
  const canSubmit = status.data?.can_confirm === true && eligibleState && !record && !needsReload
    && !status.loading && !status.error && !task.busy && validSources && notIndependent
    && reasonLength >= 8 && reasonLength <= 1000;

  return <section className="form-stack" aria-label="管理员确认发布">
    <h3>管理员确认发布</h3>
    <Notice>由管理员明确确认本版本及来源有效，单独记录为管理员确认，不构成独立人员复核或独立法规审核。
      原有未核验来源警告保留。确认通过后可使用“发布此版本”，发布结果以后台任务为准。</Notice>
    {status.loading && <Loading label="正在读取管理员确认权限与记录…" />}
    {unavailable ? <Notice>当前后端暂不支持管理员确认发布，阅读、编辑和普通复核仍可使用。</Notice>
      : <ErrorBox error={status.error} retry={status.reload} />}
    {record && <ConfirmationRecord record={record} />}
    {status.data && !record && !status.data.can_confirm && <p className="muted">服务端未授予当前用户此版本的管理员确认权限。</p>}
    {status.data && !record && !eligibleState && <p className="muted">{version.state === "APPROVED"
      ? "此版本已通过，暂无管理员确认记录；可使用已有发布入口。"
      : "此版本状态不支持管理员确认，请先创建修订。"}</p>}
    {status.data && !record && eligibleState && status.data.can_confirm && <form className="form-stack" onSubmit={(event) => {
      event.preventDefault();
      if (!canSubmit) return;
      void task.run(async () => {
        try {
          const result = await post<AdminReviewResult>(path, {
            reviewed_sha256: status.data!.current_sha256,
            reason: reason.trim(),
            confirm_valid_sources: true,
            acknowledge_not_independent: true,
          }, version.revision);
          if (!active.current) return;
          if (result?.version_id !== version.id || result.state !== "APPROVED"
            || !validConfirmation(result.confirmation, version.id, status.data!.current_sha256))
            throw new Error("服务未返回有效管理员确认记录，操作结果尚未确认，请重新读取版本。");
          setConfirmed(result.confirmation);
          setValidSources(false);
          setNotIndependent(false);
          app.notify("管理员确认已记录（非独立复核），可继续发布此版本。");
          refresh();
        } catch (error) {
          if (!active.current) return;
          // Re-read after any rejection or ambiguous response; never replay a confirmation automatically.
          setNeedsReload(true);
          setValidSources(false);
          setNotIndependent(false);
          throw error;
        }
      });
    }}>
      <dl><dt>本次确认的版本指纹（SHA-256）</dt><dd className="hash-text">{status.data.current_sha256}</dd></dl>
      <Field label="管理员确认原因" hint="至少 8 个字符，最多 1000 个字符；记录核对依据与确认范围。">
        <textarea required minLength={8} maxLength={1000} rows={3} value={reason} disabled={task.busy || needsReload}
          onChange={(event) => setReason(event.target.value)} />
      </Field>
      <label className="checkbox-label"><input type="checkbox" required checked={validSources} disabled={task.busy || needsReload}
        onChange={(event) => setValidSources(event.target.checked)} />我确认本版本文档内容及所依赖来源有效，同意将本版本审核通过并用于发布</label>
      <label className="checkbox-label"><input type="checkbox" required checked={notIndependent} disabled={task.busy || needsReload}
        onChange={(event) => setNotIndependent(event.target.checked)} />我知晓这是管理员确认，不是独立人员复核或独立法规审核，原有历史警告继续保留</label>
      <div className="inline-actions"><button type="submit" className="primary" disabled={!canSubmit}>
        {task.busy ? "正在记录管理员确认…" : "确认有效并通过"}
      </button></div>
    </form>}
    <ErrorBox error={task.error} />
    {needsReload && <Notice>确认结果或版本状态需要重新核对。请保留所填原因，重新读取版本后再确认。
      <button type="button" onClick={refresh}>重新读取版本</button>
    </Notice>}
  </section>;
}
