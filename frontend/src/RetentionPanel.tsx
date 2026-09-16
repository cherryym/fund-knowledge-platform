import { useEffect, useId, useRef, useState } from "react";
import { api } from "./api";
import { useApp } from "./appContext";
import "./retention.css";

export type RetentionPolicy = {
  space_id: string; revision: number; status: "UNCONFIGURED" | "UNAPPROVED" | "APPROVED" | "EXPIRED" | "INVALID";
  configured: boolean; approved: boolean; retention_days: number; suggested_retention_days: number;
  purge_allowed_roles: string[]; approved_by: string | null; approved_at: string | null;
  approval_expires_at: string | null; automatic_purge: false; backfilled_count?: number;
  permissions: { can_configure: boolean; can_request_purge: boolean; can_manage_preservation: boolean };
};
export type PurgeEligibility = {
  resource_id: string; space_id: string; revision: number; policy_revision: number;
  policy_status: RetentionPolicy["status"]; eligible: boolean; can_request_purge: boolean;
  deleted_at: string | null; retain_until: string | null; expiry: string | null;
  legal_hold: boolean; checked_at: string; automatic_purge: false;
  reasons: { code: string; message: string }[];
};

const states: Record<RetentionPolicy["status"], string> = {
  UNCONFIGURED: "尚未配置 · 清除已禁用", UNAPPROVED: "尚未批准 · 清除已禁用",
  APPROVED: "策略已批准", EXPIRED: "审批已失效 · 清除已禁用", INVALID: "策略无效 · 清除已禁用",
};
const roleLabels: Record<string, string> = { admin: "空间管理员", owner: "个人库所有者", editor: "编辑者", reviewer: "审核者", publisher: "发布者" };
const dateLabel = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "尚未确定";
const localTimestamp = (value: string | null) => {
  if (!value) return "";
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};

/** Mount directly in Settings. Server capabilities, not client role names, authorize controls. */
export function RetentionPanel({ readOnly = false }: { readOnly?: boolean } = {}) {
  const app = useApp();
  const spaceId = app.space.id;
  const [policy, setPolicy] = useState<RetentionPolicy | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [reload, setReload] = useState(0);
  const [approved, setApproved] = useState(false);
  const [purgeRoles, setPurgeRoles] = useState<string[]>(["admin", "owner"]);
  const [days, setDays] = useState(2);
  const [reason, setReason] = useState("");
  const [backfill, setBackfill] = useState(true);
  const [approvalExpiry, setApprovalExpiry] = useState("");
  const [conflict, setConflict] = useState(false);
  const formId = useId();
  const generation = useRef(0);
  useEffect(() => {
    const current = ++generation.current;
    const abort = new AbortController();
    setLoading(true); setPolicy(null); setError(""); setNotice(""); setConflict(false); setReason("");
    api<RetentionPolicy>(`/spaces/${spaceId}/retention-policy`, { signal: abort.signal })
      .then(value => {
        if (current !== generation.current) return;
        setPolicy(value); setDays(value.retention_days); setApproved(value.approved);
        setApprovalExpiry(localTimestamp(value.approval_expires_at));
        setPurgeRoles(value.purge_allowed_roles); setBackfill(true);
      }).catch(error => {
        if (!abort.signal.aborted && current === generation.current) setError(error.message || "读取保留策略失败");
      }).finally(() => { if (current === generation.current) setLoading(false); });
    return () => { generation.current++; abort.abort(); };
  }, [spaceId, app.refresh, reload]);
  const currentPolicy = policy?.space_id === spaceId ? policy : null;
  const canEdit = !!currentPolicy?.permissions.can_configure && !readOnly && !loading && !saving && !conflict;
  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canEdit || !currentPolicy || !reason.trim()) return;
    const current = generation.current;
    setSaving(true); setError(""); setNotice("");
    try {
      const value = await api<RetentionPolicy>(`/spaces/${spaceId}/retention-policy`, {
        method: "PUT", revision: currentPolicy.revision,
        body: { retention_days: days, approved, purge_allowed_roles: purgeRoles, reason: reason.trim(),
          backfill_trash: backfill, approval_expires_at: approvalExpiry ? new Date(approvalExpiry).toISOString() : null },
      });
      if (current !== generation.current) return;
      setPolicy(value); setReason("");
      setNotice(value.approved
        ? `保存成功：已批准删除后保留 ${value.retention_days} 天，已补齐或延长 ${value.backfilled_count ?? 0} 条回收记录。未清除文件。`
        : "保存成功：策略尚未批准，永久清除已禁用。未清除文件。");
    } catch (error) {
      if (current !== generation.current) return;
      setError(error instanceof Error ? error.message : "保存失败");
      if (typeof error === "object" && error && "status" in error && [409, 412].includes(Number(error.status))) setConflict(true);
    } finally { if (current === generation.current) setSaving(false); }
  }
  // A completed request for the previous space must never lock the next space's form.
  useEffect(() => { setSaving(false); }, [spaceId, app.refresh, reload]);
  return <section className="retention-panel" aria-labelledby={`${formId}-title`} aria-busy={loading || saving}>
    <div className="retention-heading"><div><h2 id={`${formId}-title`}>回收保留与清除权限</h2>
      <p>个人库与团队库均从删除时间起保留 2 天，期间可以恢复。</p></div>
      <button type="button" disabled={saving} onClick={() => setReload(value => value + 1)}>重新读取</button></div>
    <p className="retention-note">2 天是产品回收保留设置；既有法定要求、法律保全和更长保留期限优先。保留期届满后，仍需有权限的用户申请清除，并在执行时再次检查。</p>
    {loading && <p role="status">正在读取保留策略…</p>}
    {error && <p role="alert" className="retention-error">{error}</p>}
    {notice && <p role="status" className="retention-success">{notice}</p>}
    {currentPolicy && <>
      <div className={`retention-status ${currentPolicy.approved ? "is-approved" : ""}`}>
        <strong>{states[currentPolicy.status]}</strong><span>当前设置：{currentPolicy.retention_days} 天</span>
      </div>
      <p className="retention-permissions">{currentPolicy.permissions.can_configure ? "你可以配置策略和清除角色。" : "你只有查看权限，配置需由空间管理员或个人库所有者完成。"}
        {readOnly && " 当前面板以只读方式打开。"}普通编辑者不能修改策略或给自己授权。</p>
      <form onSubmit={save}>
        <fieldset disabled={!canEdit}>
          <legend>保留策略</legend>
          <label className="retention-field" htmlFor={`${formId}-days`}>删除后保留天数
            <input id={`${formId}-days`} type="number" min={2} max={36500} step={1} value={days} required onChange={event => setDays(Number(event.target.value))} />
            <span>用户已确定的默认值为 2 天；可以延长，不能少于 2 天。</span>
          </label>
          <label className="retention-choice"><input type="checkbox" checked={approved} onChange={event => setApproved(event.target.checked)} />批准此空间的保留与清除权限策略</label>
          <label className="retention-field" htmlFor={`${formId}-approval-expiry`}>审批有效期（可选，本地时间）
            <input id={`${formId}-approval-expiry`} type="datetime-local" value={approvalExpiry} onChange={event => setApprovalExpiry(event.target.value)} />
            <span>留空表示审批不设到期日。已失效的审批需重新设置此项并批准；资源保留期限仍独立生效。</span>
          </label>
          <label className="retention-choice"><input type="checkbox" checked={backfill} onChange={event => setBackfill(event.target.checked)} />补齐旧回收记录的保留期限（只补齐或延长）</label>
        </fieldset>
        <fieldset disabled={!canEdit}>
          <legend>可以申请永久清除的角色</legend>
          <p>还须具备该资源的管理权限；受限资源继续遵守额外授权。</p>
          <div className="retention-roles">{Object.entries(roleLabels).map(([role, label]) =>
            <label className="retention-choice" key={role}><input type="checkbox" checked={purgeRoles.includes(role)}
              onChange={event => setPurgeRoles(previous => event.target.checked ? [...previous, role] : previous.filter(item => item !== role))} />{label}</label>)}</div>
          <label className="retention-field" htmlFor={`${formId}-reason`}>配置／审批原因
            <textarea id={`${formId}-reason`} rows={2} maxLength={500} required value={reason} onChange={event => setReason(event.target.value)} placeholder="记录本次配置的依据，写入审计记录" />
          </label>
        </fieldset>
        <div className="retention-actions"><button type="submit" className="primary" disabled={!canEdit || !reason.trim()}>{saving ? "正在保存…" : "保存策略"}</button>
          <span>本操作不清除文件，也不自动发起清除任务。</span></div>
      </form>
      {currentPolicy.approved_at && <p className="retention-meta">审批时间：{dateLabel(currentPolicy.approved_at)}{currentPolicy.approval_expires_at && ` · 审批到期：${dateLabel(currentPolicy.approval_expires_at)}`}</p>}
    </>}
  </section>;
}

/** Read-only preflight for a single resource; never sends a purge request. */
export function PurgeEligibilityPanel({ resourceId, onEligibility }: {
  resourceId: string; onEligibility?: (value: PurgeEligibility | null) => void;
}) {
  const [result, setResult] = useState<PurgeEligibility | null>(null);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const callback = useRef(onEligibility);
  callback.current = onEligibility;
  useEffect(() => {
    const abort = new AbortController();
    let active = true;
    setResult(null); setError(""); callback.current?.(null);
    api<PurgeEligibility>(`/resources/${resourceId}/purge-eligibility`, { signal: abort.signal })
      .then(value => { if (active) { setResult(value); callback.current?.(value); } })
      .catch(error => { if (active) { setError(error.message || "无法核验清除条件"); callback.current?.(null); } });
    return () => { active = false; abort.abort(); callback.current?.(null); };
  }, [resourceId, reload]);
  const current = result?.resource_id === resourceId ? result : null;
  return <section className="retention-panel retention-preflight" aria-label="永久清除条件检查">
    <div className="retention-heading"><h3>永久清除条件检查</h3><button type="button" onClick={() => setReload(value => value + 1)}>重新检查</button></div>
    {error && <p role="alert" className="retention-error">{error}</p>}
    {!current && !error && <p role="status">正在核验权限、保留期限和引用依赖…</p>}
    {current && <><p role="status">{current.eligible ? "当前检查通过，可以继续提交申请。" : "当前不可清除。"}</p>
      <dl className="retention-dates"><div><dt>删除时间</dt><dd>{dateLabel(current.deleted_at)}</dd></div><div><dt>最早到期时间</dt><dd>{dateLabel(current.expiry)}</dd></div>
        <div><dt>法律保全</dt><dd>{current.legal_hold ? "保全中" : "未设置"}</dd></div></dl>
      {current.reasons.length > 0 && <ul>{current.reasons.map(reason => <li key={reason.code}>{reason.message}</li>)}</ul>}
      <p className="retention-meta">检查时间：{dateLabel(current.checked_at)}。执行前仍会再次核验；此预检不代表已清除。</p></>}
  </section>;
}
