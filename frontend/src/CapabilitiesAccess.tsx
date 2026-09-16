import { useEffect, useRef, useState } from "react";
import { apiUrl, ApiError, get, query } from "./api";
import { ErrorBox, Field, Loading, Notice, useApp, useLoad } from "./ui";
import { isAccessError, pathId, revision, stringList, useCapabilitiesTask, validDate } from "./CapabilitiesShared";

const scopes = { "capabilities:read": "读取能力定义", "runs:write": "领取运行并回传步骤", "sources:read": "读取任务绑定来源" };
type AccessScope = keyof typeof scopes;
type AccessMetadata = { id: string; name: string; space_id: string; scopes: AccessScope[]; expires_at: string; revoked_at: string | null; created_at: string; revision: number };
type AccessList = { items: AccessMetadata[]; can_create: boolean; base_url: string; notes: string[] };
const textLength = (value: string) => [...value].length;
const zonedTimestamp = (value: unknown): value is string => typeof value === "string" && validDate(value.slice(0, 10))
  && /T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(value) && Number.isFinite(Date.parse(value));

function metadata(value: AccessMetadata, spaceId: string): AccessMetadata {
  if (!value || typeof value.id !== "string" || !value.id || value.space_id !== spaceId || typeof value.name !== "string" || !value.name.trim() || textLength(value.name) > 200
      || !Array.isArray(value.scopes) || !value.scopes.length || value.scopes.some(scope => !Object.hasOwn(scopes, scope)) || new Set(value.scopes).size !== value.scopes.length
      || !revision(value.revision) || !zonedTimestamp(value.created_at) || !zonedTimestamp(value.expires_at)
      || Date.parse(value.expires_at) <= Date.parse(value.created_at)
      || (value.revoked_at !== null && (!zonedTimestamp(value.revoked_at) || Date.parse(value.revoked_at) < Date.parse(value.created_at))))
    throw new Error("接入凭据元数据与当前知识库不一致。");
  // Keep only the contract's non-secret metadata, including on list responses.
  return { id: value.id, name: value.name, space_id: value.space_id, scopes: [...value.scopes], expires_at: value.expires_at,
    revoked_at: value.revoked_at, created_at: value.created_at, revision: value.revision };
}

export function CapabilitiesAccess({ deny }: { deny: () => void }) {
  const app = useApp();
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<AccessMetadata>();
  const [reason, setReason] = useState("");
  const [issued, setIssued] = useState<AccessMetadata>();
  const token = useRef("");
  const [revealed, setRevealed] = useState(false);
  const [message, setMessage] = useState("");
  const clearToken = () => { token.current = ""; setIssued(undefined); setRevealed(false); };
  const clearAndDeny = () => { clearToken(); deny(); };
  useEffect(() => () => { token.current = ""; }, []);
  const task = useCapabilitiesTask(clearAndDeny);
  const loaded = useLoad(async signal => {
    try {
      const result = await get<AccessList>(`/agent-access?${query({ space_id: app.space.id })}`, signal);
      if (!result || !Array.isArray(result.items) || typeof result.can_create !== "boolean" || typeof result.base_url !== "string" || !stringList(result.notes))
        throw new Error("接入列表不完整。");
      return { items: result.items.map(item => metadata(item, app.space.id)), can_create: result.can_create, base_url: result.base_url, notes: result.notes };
    } catch (error) {
      if (!signal.aborted && isAccessError(error)) clearAndDeny();
      throw new Error("无法确认当前接入凭据列表，请重新读取。不会自动创建或重试发放凭据。");
    }
  }, [app.space.id, app.me.id]);
  async function revoke() {
    if (!revoking || !reason.trim() || textLength(reason.trim()) > 2000 || task.busy) return;
    const current = revoking;
    await task.run(async (signal, write) => {
      try {
        const result = metadata(await write<AccessMetadata>(`/agent-access/${pathId(current.id)}/revoke`, { reason: reason.trim() }, current.revision), app.space.id);
        signal.throwIfAborted();
        if (result.id !== current.id || !result.revoked_at || result.revision <= current.revision) throw new Error("未确认撤销回执。");
        if (issued?.id === current.id) clearToken();
        setRevoking(undefined); setReason(""); loaded.reload(); setMessage("凭据已撤销；不会删除能力定义或运行记录。");
      } catch (error) {
        if (!signal.aborted && isAccessError(error)) clearAndDeny();
        if (!signal.aborted && error instanceof ApiError && [409, 412].includes(error.status)) setRevoking(undefined);
        throw new Error(error instanceof ApiError && [409, 412].includes(error.status)
          ? "凭据状态已变化，请重新读取列表后再决定是否撤销。"
          : "撤销结果未确认，请重新读取列表核对。不会回显服务端敏感错误内容。");
      }
    });
  }
  return <section className="capabilities-surface" aria-label="Agent接入管理">
    <div className="capabilities-row"><h2>Agent 接入</h2><button disabled={creating || task.busy} onClick={() => { clearToken(); setRevoking(undefined); loaded.reload(); }}>刷新接入列表</button></div>
    <Notice>接入用于外部 Agent 通过 HTTP／MCP 领取步骤、读取绑定来源和回传产物。凭据不能编辑能力、审核发布或代替人工核对；有凭据也不代表 Agent 已连接或任务已执行。</Notice>
    <ErrorBox error={loaded.error} retry={loaded.reload} /><ErrorBox error={task.error} />
    {message && <p role="status">{message}</p>}{loaded.loading && <Loading label="正在读取个人接入元数据…" />}
    {loaded.data && <>
      <p>当前知识库：<strong>{app.space.name}</strong></p>
      <p>API 地址：<code>{loaded.data.base_url}</code></p>
      <p className="capabilities-muted">在你的 Agent 中配置 FKB_AGENT_API_URL 和 FKB_AGENT_TOKEN；本页不会修改任何 Agent、模型或宿主配置。</p>
      <button className="primary" disabled={!loaded.data.can_create || creating || !!revoking || task.busy || !!issued}
        onClick={() => { clearToken(); setMessage(""); setCreating(true); }}>创建 Agent 接入凭据</button>
      {!loaded.data.can_create && <p>当前无创建权限。</p>}
      {!loaded.data.items.length && <p className="capabilities-empty">未配置接入凭据 · 尚未连接 Agent。</p>}
      {creating && loaded.data.can_create && <CredentialCreation deny={clearAndDeny} close={() => setCreating(false)}
        inspect={() => { setCreating(false); loaded.reload(); setMessage("请核对列表。若凭据已创建但未收到明文，先撤销该凭据，再明确创建新的凭据。"); }}
        created={(access, plaintext) => { token.current = plaintext; setIssued(access); setCreating(false); setRevealed(false); loaded.reload(); }} />}
      <div className="capabilities-access-list">{loaded.data.items.map(item => <article key={item.id} className="capabilities-card">
        <h3>{item.name}</h3><p>{item.revoked_at ? "已撤销" : Date.parse(item.expires_at) <= Date.now() ? "已过期" : "凭据有效期内 · 连接状态未验证"}</p>
        <p>{item.scopes.map(scope => scopes[scope]).join("、")}</p><p>有效期至：{item.expires_at}</p><small>创建时间：{item.created_at}</small>
        <button disabled={!!item.revoked_at || creating || !!revoking || task.busy} onClick={() => { setRevoking(item); setReason(""); }}>撤销接入凭据</button>
      </article>)}</div>
      {revoking && <form className="capabilities-inset" aria-label="撤销接入凭据" onSubmit={event => { event.preventDefault(); void revoke(); }}>
        <h3>撤销：{revoking.name}</h3><p>撤销后该凭据无法再访问本知识库，保留非秘密元数据和历史运行。</p>
        <Field label="凭据撤销原因" hint="最多 2000 个字符"><textarea aria-label="凭据撤销原因" value={reason} disabled={task.busy} onChange={event => setReason(event.target.value)} /></Field>
        {textLength(reason.trim()) > 2000 && <p role="alert">凭据撤销原因不能超过 2000 个字符。</p>}
        <button type="submit" disabled={!reason.trim() || textLength(reason.trim()) > 2000 || task.busy}>确认撤销凭据</button><button type="button" disabled={task.busy} onClick={() => setRevoking(undefined)}>取消撤销凭据</button>
      </form>}
      {loaded.data.notes.map((note, index) => <p className="capabilities-muted" key={index}>{note}</p>)}
    </>}
    {issued && token.current && <section className="capabilities-secret" aria-label="一次性接入凭据">
      <h3>一次性明文已返回</h3><p>仅当前页面可见，切换页面或知识库后清除。请自行安全保存；不会写入浏览器存储、日志或导出文件。</p>
      <Field label="一次性明文"><input aria-label="一次性明文" readOnly type={revealed ? "text" : "password"} value={token.current} autoComplete="off" spellCheck={false} /></Field>
      <div className="capabilities-row"><button onClick={() => setRevealed(value => !value)}>{revealed ? "隐藏明文" : "显示明文"}</button>
        <button onClick={async () => { try { await navigator.clipboard.writeText(token.current); setMessage("已复制，请自行安全保存。"); } catch { setMessage("复制未完成，请手动选择当前明文保存。"); } }}>复制一次性凭据</button>
        <button onClick={clearToken}>已保存，清除本页明文</button></div>
    </section>}
  </section>;
}

function CredentialCreation({ deny, close, inspect, created }: { deny: () => void; close: () => void; inspect: () => void; created: (access: AccessMetadata, token: string) => void }) {
  const app = useApp();
  const [name, setName] = useState("");
  const [selectedScopes, setSelectedScopes] = useState<AccessScope[]>(["capabilities:read"]);
  const [expires, setExpires] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<Error>();
  const attempt = useRef(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const expiresAt = expires && Number.isFinite(Date.parse(expires)) ? new Date(expires).toISOString() : "";
  const ready = !!name.trim() && textLength(name.trim()) <= 200 && selectedScopes.length > 0 && !!expiresAt && Date.parse(expiresAt) > Date.now() && confirmed && !uncertain;
  async function submit() {
    if (!ready || attempt.current) return;
    attempt.current = true; const controller = new AbortController(); request.current = controller;
    setBusy(true); setError(undefined);
    const body = { request_id: crypto.randomUUID(), name: name.trim(), space_id: app.space.id, scopes: [...selectedScopes], expires_at: expiresAt };
    try {
      // Deliberately bypass api(): credential creation uses request_id, not the generic
      // Idempotency-Key path, and no server error body is retained or rendered.
      const response = await fetch(apiUrl("/agent-access"), { method: "POST", credentials: "include", signal: controller.signal,
        headers: { Accept: "application/json", "Content-Type": "application/json", "X-CSRF-Token": app.me.csrf_token }, body: JSON.stringify(body) });
      controller.signal.throwIfAborted();
      if ([401, 403, 404].includes(response.status)) { deny(); return; }
      if (response.status !== 201) throw new Error("Creation outcome uncertain");
      const result = await response.json(); controller.signal.throwIfAborted();
      const access = metadata(result.access, app.space.id);
      if (typeof result.token !== "string" || !result.token || access.name !== body.name || access.revoked_at
          || Date.parse(access.expires_at) !== Date.parse(body.expires_at) || JSON.stringify([...access.scopes].sort()) !== JSON.stringify([...body.scopes].sort()))
        throw new Error("Credential receipt mismatch");
      created(access, result.token);
    } catch {
      if (!controller.signal.aborted) { setUncertain(true); setError(new Error("创建结果未确认。为避免重复发放，不会重试此次创建；请查询列表核对。明文不会被重放。")); }
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  const changed = () => setConfirmed(false);
  return <form className="capabilities-inset" aria-label="创建 Agent 接入凭据" onSubmit={event => { event.preventDefault(); void submit(); }}>
    <h3>确认接入范围后创建</h3><fieldset disabled={busy || uncertain}>
      <Field label="凭据名称" hint="最多 200 个字符"><input aria-label="凭据名称" value={name} onChange={event => { setName(event.target.value); changed(); }} autoComplete="off" /></Field>
      {textLength(name.trim()) > 200 && <p role="alert">凭据名称不能超过 200 个字符。</p>}
      <p>知识库：<strong>{app.space.name}</strong>，此凭据仅能用于该库。</p>
      <fieldset className="capabilities-dependencies"><legend>允许范围</legend>{Object.entries(scopes).map(([scope, label]) => <label className="capabilities-check" key={scope}>
        <input type="checkbox" aria-label={label} checked={selectedScopes.includes(scope as AccessScope)} onChange={event => {
          setSelectedScopes(previous => event.target.checked ? [...previous, scope as AccessScope] : previous.filter(value => value !== scope)); changed();
        }} />{label}</label>)}</fieldset>
      <Field label="有效期至（本地时间）"><input type="datetime-local" aria-label="凭据有效期" value={expires} onChange={event => { setExpires(event.target.value); changed(); }} /></Field>
      {expiresAt && <p>实际提交的有效期：<time>{expiresAt}</time></p>}
      <label className="capabilities-check"><input type="checkbox" aria-label="确认知识库范围和有效期" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />
        我确认所选知识库、允许范围及有效期，并了解该凭据不能代替人工审批或外部业务授权。</label>
    </fieldset><ErrorBox error={error} />
    <div className="capabilities-row"><button className="primary" type="submit" disabled={!ready || busy || attempt.current}>{busy ? "正在创建一次性凭据…" : "确认创建一次性凭据"}</button>
      {uncertain ? <button type="button" onClick={inspect}>只查询凭据列表</button> : <button type="button" disabled={busy} onClick={close}>取消创建凭据</button>}</div>
  </form>;
}
