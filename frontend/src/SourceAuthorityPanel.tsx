import { useEffect, useRef, useState } from "react";
import { api, ApiError, get, query } from "./api";
import { VersionBlockPicker } from "./Pickers";
import type { Resource, Version } from "./types";
import { ErrorBox, Field, Loading, Notice, readable, useApp, useLoad } from "./ui";
import "./source-authority-panel.css";

export type SourceAuthorityRecord = {
  id: string;
  space_id: string;
  revision: number;
  state: "ACTIVE" | "REVOKED";
  predecessor_version_id: string;
  successor_version_id: string;
  evidence_version_id: string;
  predecessor_title: string;
  successor_title: string;
  evidence_title: string;
  effective_from: string;
  scope: "full" | "partial";
  reason: string;
  created_at: string;
  created_by: string;
  revoked_at?: string | null;
  revoke_reason?: string | null;
  validation_state?: "VALID" | "STALE" | "UNAVAILABLE";
  validation_note?: string;
};
type AuthorityList = { items: SourceAuthorityRecord[]; can_manage: boolean; notes: string[] };
type SourceBinding = { version_id: string; revision: number; access_epoch: number; content_sha256: string };
type SourceAuthoritySuggestion = {
  id: string; space_id: string; status: "READY" | "NEEDS_INPUT" | "CONFIRMED";
  origin: "registered_fact" | "source_text";
  existing_record_id: string | null;
  existing_validation_state: "VALID" | "STALE" | "UNAVAILABLE" | null;
  predecessor_version_id: string | null; successor_version_id: string | null; evidence_version_id: string | null;
  predecessor_title: string; successor_title: string; evidence_title: string;
  effective_from: string | null; scope: "full" | "partial" | null; reason: string;
  excerpts: { version_id: string; block_id: string; text: string; locator: unknown }[];
  missing_fields: string[]; expected_sources: SourceBinding[];
};
type SuggestionList = { items: SourceAuthoritySuggestion[]; can_manage: boolean; notes: string[] };
type Choice = { resource: Resource; version: Version };
type Endpoint = "predecessor" | "successor" | "evidence";
const endpoints: [Endpoint, string][] = [["predecessor", "旧规则文档"], ["successor", "后继规则文档"], ["evidence", "替代证据文档"]];
const endpointPath = (id: string) => `/source-authority/${encodeURIComponent(id)}`;
const permissionFailure = (error: unknown) => error instanceof ApiError && [401, 403, 404].includes(error.status);
const permissionMessage = "当前空间、记录或来源已不可读取。已清除旧结果和选择，请重新读取权限。";
const boundary = "确认仅登记来源之间的替代事实，不代表整份法规现行有效，也不等于独立专家审核。确认不修改文档内容、效力状态或历史引文。";
const validDate = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(`${value}T00:00:00Z`))
  && new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value;
const beijingToday = () => new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
}).format(new Date());

function checkedRecord(value: SourceAuthorityRecord, spaceId: string, id?: string) {
  if (!value || value.space_id !== spaceId || (id !== undefined && value.id !== id)
      || !Number.isSafeInteger(value.revision) || value.revision < 1
      || !["ACTIVE", "REVOKED"].includes(value.state) || !["full", "partial"].includes(value.scope)
      || !validDate(value.effective_from)
      || [value.id, value.predecessor_version_id, value.successor_version_id, value.evidence_version_id,
        value.predecessor_title, value.successor_title, value.evidence_title, value.created_by, value.created_at]
        .some(field => typeof field !== "string" || !field.trim()) || typeof value.reason !== "string")
    throw new Error("来源关系响应不完整或与当前空间不一致，请重新读取。");
  return value;
}

function checkedList(value: AuthorityList, spaceId: string) {
  if (!value || !Array.isArray(value.items) || typeof value.can_manage !== "boolean"
      || !Array.isArray(value.notes) || value.notes.some(note => typeof note !== "string"))
    throw new Error("来源关系与管理权限尚未确认，请重新读取。");
  value.items.forEach(item => checkedRecord(item, spaceId));
  if (new Set(value.items.map(item => item.id)).size !== value.items.length)
    throw new Error("来源关系响应包含重复记录，请重新读取。");
  return value;
}

function checkedSuggestions(value: SuggestionList, spaceId: string) {
  const text = (item: unknown): item is string => typeof item === "string" && !!item.trim();
  if (!value || !Array.isArray(value.items) || typeof value.can_manage !== "boolean"
      || !Array.isArray(value.notes) || value.notes.some(note => typeof note !== "string"))
    throw new Error("替代建议与管理权限尚未确认，请重新读取建议。");
  for (const item of value.items) {
    if (!item || !text(item.id) || item.space_id !== spaceId || !["READY", "NEEDS_INPUT", "CONFIRMED"].includes(item.status)
        || !["registered_fact", "source_text"].includes(item.origin)
        || (item.existing_record_id !== null && !text(item.existing_record_id))
        || (item.status === "CONFIRMED" && !item.existing_record_id)
        || (item.existing_validation_state !== null && !["VALID", "STALE", "UNAVAILABLE"].includes(item.existing_validation_state))
        || endpoints.some(([endpoint]) => (item[`${endpoint}_version_id`] !== null && !text(item[`${endpoint}_version_id`]))
          || typeof item[`${endpoint}_title`] !== "string")
        || (item.effective_from !== null && !validDate(item.effective_from))
        || (item.scope !== null && !["full", "partial"].includes(item.scope)) || typeof item.reason !== "string"
        || !Array.isArray(item.missing_fields) || item.missing_fields.some(field => !text(field))
        || !Array.isArray(item.excerpts) || item.excerpts.some(excerpt => !excerpt || !text(excerpt.version_id)
          || !text(excerpt.block_id) || typeof excerpt.text !== "string")
        || !Array.isArray(item.expected_sources) || item.expected_sources.some(source => !source || !text(source.version_id)
          || !Number.isSafeInteger(source.revision) || source.revision < 1 || !Number.isSafeInteger(source.access_epoch)
          || source.access_epoch < 0 || !/^[a-f\d]{64}$/i.test(source.content_sha256))
        || new Set(item.expected_sources.map(source => source.version_id)).size !== item.expected_sources.length)
      throw new Error("替代建议响应不完整或与当前空间不一致，请重新读取建议。");
    const selected = new Set(endpoints.map(([endpoint]) => item[`${endpoint}_version_id`]).filter(Boolean));
    if (item.expected_sources.some(source => !selected.has(source.version_id))
        || item.excerpts.some(excerpt => !selected.has(excerpt.version_id)))
      throw new Error("建议证据与来源版本不一致，请重新读取建议。");
  }
  if (new Set(value.items.map(item => item.id)).size !== value.items.length)
    throw new Error("替代建议包含重复标识，请重新读取建议。");
  return value;
}

function matchingRecord(records: SourceAuthorityRecord[], fields: {
  predecessor_version_id: string | null; successor_version_id: string | null; evidence_version_id: string | null;
  effective_from: string | null; scope: string | null;
}) {
  return records.find(record => record.state === "ACTIVE" && record.validation_state === "VALID"
    && endpoints.every(([endpoint]) => record[`${endpoint}_version_id`] === fields[`${endpoint}_version_id`])
    && record.effective_from === fields.effective_from && record.scope === fields.scope);
}

/** One synchronous action lock; abort and late-result guards survive a scope change. */
function useAuthorityAction(onPermissionFailure: () => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error>();
  const request = useRef<AbortController | null>(null);
  const retry = useRef<{ signature: string; key: string } | null>(null);
  useEffect(() => () => { request.current?.abort(); }, []);
  async function run(action: (signal: AbortSignal, write: <T>(path: string, body: unknown, revision?: number) => Promise<T>) => Promise<void>) {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError(undefined);
    const write = async <T,>(path: string, body: unknown, revision?: number) => {
      controller.signal.throwIfAborted();
      const signature = JSON.stringify([path, body, revision]);
      if (retry.current?.signature !== signature) retry.current = { signature, key: crypto.randomUUID() };
      try {
        const value = await api<T>(path, { method: "POST", body, revision, signal: controller.signal, key: retry.current.key });
        retry.current = null;
        return value;
      } catch (error) {
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) retry.current = null;
        throw error;
      }
    };
    try { await action(controller.signal, write); }
    catch (error) {
      if (!controller.signal.aborted) {
        if (permissionFailure(error)) onPermissionFailure();
        else setError(error instanceof Error ? error : new Error("操作未完成，请重新核对。"));
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
      if (request.current === controller) request.current = null;
      controller.abort();
    }
  }
  return { busy, error, clearError: () => setError(undefined), run,
    cancel: () => { request.current?.abort(); request.current = null; setBusy(false); setError(undefined); } };
}

async function readSource(versionId: string, spaceId: string, signal: AbortSignal): Promise<Choice> {
  const version = await get<Version>(`/versions/${encodeURIComponent(versionId)}`, signal);
  signal.throwIfAborted();
  if (version.id !== versionId || !version.resource_id) throw new Error("来源版本与选择不一致，请重新选择。");
  const resource = await get<Resource>(`/resources/${encodeURIComponent(version.resource_id)}`, signal);
  signal.throwIfAborted();
  if (resource.id !== version.resource_id || resource.space_id !== spaceId || resource.kind !== "document"
      || resource.suspended || resource.deleted_at)
    throw new Error("请选择当前空间可读、未停用的文档版本作为来源。");
  return { resource, version };
}

export function SourceAuthorityPanel() {
  const app = useApp();
  // Remount synchronously; no frame may carry another user's/space's form or result.
  const identity = JSON.stringify([app.me.id, app.space.id, app.space.revision,
    app.space.roles, app.me.is_admin, app.refresh]);
  return <SourceAuthorityScope key={identity} />;
}

function SourceAuthorityScope() {
  const app = useApp();
  type Selection = { kind: "suggestion" | "record"; id: string } | { kind: "manual" };
  const [blocked, setBlocked] = useState(false);
  const [selection, setSelection] = useState<Selection>();
  const [revoking, setRevoking] = useState<string>();
  const [notice, setNotice] = useState("");
  const [generation, setGeneration] = useState(0);
  const deny = () => { navigate.cancel(); setBlocked(true); setSelection(undefined); setRevoking(undefined); setNotice(""); };
  useEffect(() => {
    window.addEventListener("session-expired", deny);
    return () => window.removeEventListener("session-expired", deny);
  }, []);
  const loaded = useLoad(async signal => {
    try { return { generation, list: checkedList(await get<AuthorityList>(
      "/source-authority?" + query({ space_id: app.space.id }), signal), app.space.id) }; }
    catch (error) { if (!signal.aborted && permissionFailure(error)) deny(); throw error; }
  }, [app.space.id, generation]);
  const suggestions = useLoad(async signal => {
    try { return { generation, list: checkedSuggestions(await get<SuggestionList>(
      "/source-authority/suggestions?" + query({ space_id: app.space.id }), signal), app.space.id) }; }
    catch (error) { if (!signal.aborted && permissionFailure(error)) deny(); throw error; }
  }, [app.space.id, generation]);
  const navigate = useAuthorityAction(deny);
  const data = !blocked && !loaded.loading && !loaded.error && loaded.data?.generation === generation ? loaded.data.list : undefined;
  const suggested = !blocked && !suggestions.loading && !suggestions.error && suggestions.data?.generation === generation ? suggestions.data.list : undefined;
  const canManage = data?.can_manage === true && suggested?.can_manage !== false;
  const items = suggested?.items.map(item => {
    const duplicate = matchingRecord(data?.items ?? [], item);
    return duplicate ? { ...item, status: "CONFIRMED" as const, existing_record_id: duplicate.id,
      existing_validation_state: duplicate.validation_state ?? null } : item;
  }) ?? [];
  const primary = items.find(item => item.status === "READY" && !item.existing_record_id)
    ?? items.find(item => item.status === "CONFIRMED" || item.existing_record_id)
    ?? items[0];
  const selectedSuggestion = selection?.kind === "suggestion" ? items.find(item => item.id === selection.id) : undefined;
  const chooseSuggestion = (item: SourceAuthoritySuggestion) => {
    navigate.cancel(); setNotice(""); setRevoking(undefined);
    setSelection(item.existing_record_id ? { kind: "record", id: item.existing_record_id } : { kind: "suggestion", id: item.id });
  };
  const refresh = (keep?: Selection) => {
    navigate.cancel(); setBlocked(false); setSelection(keep); setRevoking(undefined); setGeneration(value => value + 1);
  };
  const complete = (message: string) => { setNotice(message); refresh(); };
  const showRecord = (id: string) => { navigate.cancel(); setSelection({ kind: "record", id }); };
  function openSource(versionId: string, blockId?: string) {
    if (!data && !suggested) return;
    void navigate.run(async signal => {
      const choice = await readSource(versionId, app.space.id, signal);
      if (!signal.aborted) app.openResource(choice.resource, blockId ? "content" : "preview", choice.version.id, blockId);
    });
  }
  const selectedId = selection?.kind === "suggestion" ? selection.id
    : selection?.kind === "record" ? items.find(item => item.existing_record_id === selection.id)?.id ?? "" : "";
  return <section className="source-authority-panel" aria-label="规则效力">
    <header className="source-authority-heading">
      <div><h2>规则效力</h2><p>{app.space.name} · 核对来源之间的替代关系</p></div>
      <button type="button" onClick={() => refresh()}>刷新关系</button>
    </header>
    <Notice>{boundary} 替代按业务日期判断；未提供业务日期时按北京时间当日。</Notice>
    {notice && <p className="source-authority-success" role="status">{notice}</p>}
    {blocked && <ErrorBox error={new Error(permissionMessage)} retry={() => refresh()} />}
    {!blocked && <><ErrorBox error={loaded.error} retry={() => refresh()} /><ErrorBox error={suggestions.error} retry={() => refresh()} /></>}
    <ErrorBox error={navigate.error} />
    {(loaded.loading || suggestions.loading) && !blocked && <Loading label="正在读取可核对建议与空间权限…" />}
    {(data || suggested) && <>
      <div className="source-authority-actions source-authority-toolbar">
        <button type="button" className="primary" disabled={!primary || !!revoking}
          onClick={() => { if (primary) chooseSuggestion(primary); }}>
          {primary?.status === "CONFIRMED" || primary?.existing_record_id ? "查看已确认信息" : "核对替代建议"}
        </button>
        <button type="button" disabled={!canManage || !!selection || !!revoking}
          onClick={() => { navigate.cancel(); setNotice(""); setSelection({ kind: "manual" }); }}>手动登记</button>
        {data && <span>当前可见 {data.items.length} 条关系</span>}
      </div>
      {!canManage && <p>你可以查看来源关系；只有空间管理员可以确认或撤销事实。</p>}
      {suggested && !items.length && <p className="source-authority-empty">暂无可预填的替代建议。需要新增关系时可手动登记，不根据上传日期猜测法规效力。</p>}
      {items.length > 0 && <div className="source-authority-suggestion-choice">
        <p>已带入可识别的文档、日期和说明，打开后核对即可；尚不能确定的要素会标为待补充。</p>
        <Field label="选择替代建议"><select aria-label="选择替代建议" value={selectedId} disabled={!!revoking}
          onChange={event => { const item = items.find(item => item.id === event.target.value); if (item) chooseSuggestion(item); }}>
          <option value="">选择要查看的建议（{items.length} 项）</option>
          {items.map(item => <option key={item.id} value={item.id}>
            {item.status === "CONFIRMED" || item.existing_record_id ? "已确认" : item.status === "READY" ? "待确认" : "待补充"}
            {" · "}{item.predecessor_title || "旧规则待确定"} → {item.successor_title || "后继规则待确定"}
          </option>)}
        </select></Field>
      </div>}
      {selection?.kind === "manual" && data && canManage && <CreateAuthorityForm spaceId={app.space.id} deny={deny}
        records={data.items} showRecord={showRecord}
        cancel={() => setSelection(undefined)} complete={() => complete("替代事实已确认。仅登记来源关系，文档效力与专家审核状态未变。正在重新读取关系。")}
        openSource={openSource} cancelOpen={navigate.cancel} />}
      {selectedSuggestion && suggested && <PrefilledAuthorityForm key={generation + ":" + selectedSuggestion.id}
        initial={selectedSuggestion} spaceId={app.space.id} canManage={canManage && suggested.can_manage} deny={deny}
        cancel={() => setSelection(undefined)} reload={() => refresh(selection)} openSource={openSource} cancelOpen={navigate.cancel}
        showRecord={showRecord} complete={() => complete("替代事实已确认。文档效力与专家审核状态未变，正在重新读取关系与建议。")} />}
      {selection?.kind === "record" && data && <SourceAuthorityRecordDetail key={generation + ":" + selection.id}
        id={selection.id} spaceId={app.space.id} deny={deny} openSource={openSource} cancel={() => setSelection(undefined)}
        suggested={items.find(item => item.existing_record_id === selection.id)} />}
      {selection?.kind === "suggestion" && suggested && !selectedSuggestion
        && <p role="status">这条建议已更新或不再可见，请选择当前可见的建议。</p>}
      {revoking && data && canManage && <RevokeAuthorityForm key={revoking} id={revoking} spaceId={app.space.id} deny={deny}
        cancel={() => setRevoking(undefined)} complete={() => complete("治理记录已停用并保留历史，文档未删除。正在重新读取关系。")} />}
      {(data?.notes.length || suggested?.notes.length) ? <ul className="source-authority-notes">
        {[...new Set([...(suggested?.notes ?? []), ...(data?.notes ?? [])])].map((note, i) => <li key={i}>{note}</li>)}
      </ul> : null}
      {data && !data.items.length && <p className="source-authority-empty">暂无当前可见的替代事实。没有已登记关系不代表法规现行有效。</p>}
      <div className="source-authority-records">{data?.items.map(record => <article key={record.id} className="source-authority-record" aria-label={record.predecessor_title + "的替代关系"}>
        <div className="source-authority-actions">
          <span className="source-authority-badge">{record.state === "REVOKED" ? "已撤销 · 记录停用" : "已确认 · 替代事实"}</span>
          <span className={"source-authority-badge " + (record.validation_state === "VALID" ? "" : "source-authority-warning")}>
            {record.validation_state === "VALID" ? "记录校验有效" : record.validation_state === "UNAVAILABLE" ? "来源不可用 · 待复核" : "记录待复核"}
          </span>
          {record.state === "ACTIVE" && record.effective_from > beijingToday() && <span className="source-authority-badge">未来日期 · 尚不适用</span>}
        </div>
        <dl className="source-authority-sources">{endpoints.map(([endpoint, label]) => <div key={endpoint}>
          <dt>{label}</dt><dd><button type="button" className="source-authority-link" disabled={navigate.busy}
            onClick={() => openSource(record[endpoint + "_version_id" as keyof SourceAuthorityRecord] as string)}>{record[endpoint + "_title" as keyof SourceAuthorityRecord]}</button>
            <small>确切版本：<code>{record[endpoint + "_version_id" as keyof SourceAuthorityRecord]}</code></small></dd>
        </div>)}</dl>
        <dl className="source-authority-meta">
          <div><dt>替代起始日期（含当日）</dt><dd>{record.effective_from}</dd></div>
          <div><dt>替代范围</dt><dd>{record.scope === "full" ? "全部替代（full）" : "部分替代（partial）"}</dd></div>
          <div><dt>确认人</dt><dd>{record.created_by === app.me.id ? app.me.display_name : record.created_by}</dd></div>
          <div><dt>确认时间</dt><dd>{record.created_at}</dd></div>
        </dl>
        {record.scope === "partial" && <p>仅按所述范围替代，不能据此整份排除旧文。</p>}
        {record.validation_state !== "VALID" && <p className="source-authority-warning">来源或授权需要重新核对，不能沿旧记录给新版本赋予效力。</p>}
        {record.validation_note && <p>{record.validation_note}</p>}
        <p className="source-authority-reason"><strong>确认原因：</strong>{record.reason}</p>
        {record.state === "REVOKED" && <p className="source-authority-reason"><strong>撤销原因：</strong>{record.revoke_reason || "未提供"} · 撤销时间：{record.revoked_at || "未提供"}</p>}
        <div className="source-authority-actions">
          <button type="button" disabled={!!revoking} onClick={() => { setNotice(""); showRecord(record.id); }}>查看已确认要素</button>
          {record.state === "ACTIVE" && <button type="button" disabled={!canManage || !!revoking || selection?.kind === "manual" || selection?.kind === "suggestion"}
            onClick={() => { navigate.cancel(); setNotice(""); setSelection(undefined); setRevoking(record.id); }}>撤销事实</button>}
        </div>
      </article>)}</div>
    </>}
  </section>;
}

function SourceAuthorityRecordDetail({ id, spaceId, deny, openSource, cancel, suggested }: {
  id: string; spaceId: string; deny: () => void; openSource: (id: string, blockId?: string) => void; cancel: () => void;
  suggested?: SourceAuthoritySuggestion;
}) {
  const loaded = useLoad(async signal => {
    try { return checkedRecord(await get<SourceAuthorityRecord>(endpointPath(id), signal), spaceId, id); }
    catch (error) { if (!signal.aborted && permissionFailure(error)) deny(); throw error; }
  }, [id, spaceId]);
  const record = loaded.data;
  const initial: SourceAuthoritySuggestion | undefined = record ? { ...record, status: "CONFIRMED", origin: "registered_fact",
    existing_record_id: record.id, existing_validation_state: record.validation_state ?? null,
    missing_fields: [], expected_sources: [], excerpts: suggested && endpoints.every(([endpoint]) =>
      suggested[`${endpoint}_version_id`] === record[`${endpoint}_version_id`]) ? suggested.excerpts : [] } : undefined;
  return <div className="source-authority-detail">
    {loaded.loading && <Loading label="正在读取已确认记录…" />}
    <ErrorBox error={loaded.error} retry={loaded.reload} />
    {initial && record && <PrefilledAuthorityForm key={record.id + ":" + record.revision} initial={initial} record={record}
      spaceId={spaceId} canManage={false} deny={deny} openSource={openSource} cancel={cancel} cancelOpen={() => {}}
      reload={loaded.reload} showRecord={() => {}} complete={() => {}} />}
  </div>;
}

function PrefilledAuthorityForm({ initial, record, spaceId, canManage, deny, complete, cancel, reload, showRecord, openSource, cancelOpen }: {
  initial: SourceAuthoritySuggestion; record?: SourceAuthorityRecord; spaceId: string; canManage: boolean;
  deny: () => void; complete: () => void; cancel: () => void; reload: () => void; showRecord: (id: string) => void;
  openSource: (id: string, blockId?: string) => void; cancelOpen: () => void;
}) {
  const app = useApp();
  const alreadyConfirmed = initial.status === "CONFIRMED" || !!initial.existing_record_id;
  const editable = canManage && !alreadyConfirmed;
  const [sources, setSources] = useState(() => Object.fromEntries(endpoints.map(([endpoint]) => [endpoint, {
    id: initial[`${endpoint}_version_id`], title: initial[`${endpoint}_title`],
  }])) as Record<Endpoint, { id: string | null; title: string }>);
  const [overrides, setOverrides] = useState<Partial<Record<Endpoint, Choice>>>({});
  const [editing, setEditing] = useState<Record<string, boolean>>({});
  const [effectiveFrom, setEffectiveFrom] = useState(initial.effective_from ?? "");
  const [scope, setScope] = useState(initial.scope ?? "");
  const [reason, setReason] = useState(initial.reason);
  const [stale, setStale] = useState(false);
  const [selectionError, setSelectionError] = useState<Error>();
  const task = useAuthorityAction(deny);
  const ids = [...new Set(endpoints.map(([endpoint]) => sources[endpoint].id).filter((id): id is string => !!id))];
  const fullBinding = ids.length === initial.expected_sources.length
    && ids.every(id => initial.expected_sources.some(source => source.version_id === id));
  const sourceChanged = endpoints.some(([endpoint]) => sources[endpoint].id !== initial[`${endpoint}_version_id`]);
  const unknownMissing = initial.missing_fields.filter(field => !["effective_from", "scope", "reason",
    ...endpoints.map(([endpoint]) => `${endpoint}_version_id`)].includes(field));
  const bindingMissing = !alreadyConfirmed && !fullBinding && !sourceChanged;
  const ready = editable && !stale && !bindingMissing && !unknownMissing.length
    && endpoints.every(([endpoint]) => !!sources[endpoint].id && !editing[endpoint]
      && (!initial.missing_fields.includes(`${endpoint}_version_id`) || !!overrides[endpoint]))
    && sources.predecessor.id !== sources.successor.id && validDate(effectiveFrom) && !!scope && !!reason.trim();
  const needsEdit = (field: string, missing: boolean) => editable && (missing || editing[field] || initial.missing_fields.includes(field));
  const excerpts = stale ? [] : initial.excerpts.filter(excerpt => ids.includes(excerpt.version_id));
  const edit = (field: string) => { cancelOpen(); task.clearError(); setEditing(previous => ({ ...previous, [field]: true })); };
  function select(endpoint: Endpoint, resource?: Resource, version?: Version) {
    cancelOpen(); setSelectionError(undefined);
    if (!version) return;
    if (!resource || resource.space_id !== spaceId || resource.kind !== "document" || resource.suspended || resource.deleted_at
        || version.resource_id !== resource.id) {
      setSelectionError(new Error("请选择当前空间可读文档的确切版本，知识页不能代替证据原件。")); return;
    }
    setSources(previous => ({ ...previous, [endpoint]: { id: version.id, title: version.title } }));
    setOverrides(previous => ({ ...previous, [endpoint]: { resource, version } }));
    setEditing(previous => ({ ...previous, [endpoint]: false, [`${endpoint}_version_id`]: false }));
  }
  async function submit() {
    if (!ready || task.busy) return;
    await task.run(async (signal, write) => {
      const fields = { space_id: spaceId, predecessor_version_id: sources.predecessor.id!, successor_version_id: sources.successor.id!,
        evidence_version_id: sources.evidence.id!, effective_from: effectiveFrom, scope, reason: reason.trim() };
      // A fresh management projection also prevents confirming a fact another user just registered.
      const current = checkedList(await get<AuthorityList>(`/source-authority?${query({ space_id: spaceId })}`, signal), spaceId);
      signal.throwIfAborted();
      if (!current.can_manage) throw new ApiError(403, "SOURCE_AUTHORITY_FORBIDDEN", permissionMessage);
      const duplicate = matchingRecord(current.items, fields);
      if (duplicate) { showRecord(duplicate.id); return; }
      try {
        const actual = await Promise.all(ids.map(id => readSource(id, spaceId, signal)));
        signal.throwIfAborted();
        for (const choice of actual) {
          const expected = initial.expected_sources.find(source => source.version_id === choice.version.id);
          // The suggestion hash is computed from actual content by the server. Do not replace it
          // with the stored Version.content_sha256 (which may be null or use a different snapshot).
          if (expected && (choice.version.revision !== expected.revision || choice.resource.access_epoch !== expected.access_epoch))
            throw new Error("建议来源版本或权限已变化，请重新读取建议。");
          for (const selected of Object.values(overrides)) if (selected?.version.id === choice.version.id
              && (selected.resource.id !== choice.resource.id || selected.resource.revision !== choice.resource.revision
                || selected.resource.access_epoch !== choice.resource.access_epoch || selected.version.revision !== choice.version.revision
                || selected.version.content_sha256 !== choice.version.content_sha256))
            throw new Error("手动选择的来源已变化，请重新读取建议并核对所选版本。");
        }
      } catch (error) { if (!signal.aborted && !permissionFailure(error)) setStale(true); throw error; }
      // A manual replacement may have no server-computed binding. In that case omit the entire
      // old set, as the contract allows; never send partial bindings or rebind old suggestions silently.
      const payload = { ...fields, ...(fullBinding ? { expected_sources: initial.expected_sources } : {}) };
      try {
        const result = await write<SourceAuthorityRecord>("/source-authority", payload);
        signal.throwIfAborted();
        try {
          checkedRecord(result, spaceId);
          if (result.state !== "ACTIVE" || Object.entries(fields).some(([key, value]) => result[key as keyof SourceAuthorityRecord] !== value))
            throw new Error("确认回执与提交内容不一致，请重新读取建议核对结果。");
        } catch (error) { setStale(true); throw error; }
        complete();
      } catch (error) {
        if (!signal.aborted && error instanceof ApiError && [409, 412].includes(error.status)) setStale(true);
        throw error;
      }
    });
  }
  return <form className="source-authority-form source-authority-prefill" aria-label="预填替代事实表单"
    onSubmit={event => { event.preventDefault(); void submit(); }}>
    <h3>{alreadyConfirmed ? "已确认的替代事实" : "核对已带入的替代要素"}</h3>
    {alreadyConfirmed ? <p className="source-authority-success" role="status">{record?.state === "REVOKED" ? "记录已撤销，保留历史供查看。" : "已确认，无需重复提交。"}</p>
      : <p>信息来自已登记来源，尚未由你确认。核对后点击“确认替代事实”才会登记，不自动认定人工已核验。</p>}
    {alreadyConfirmed && initial.existing_validation_state !== "VALID" && <p className="source-authority-warning">这条记录的来源仍需复核；查看不代表重新确认或全文现行有效。</p>}
    <fieldset disabled={task.busy || stale}>
      <div className="source-authority-sources">{endpoints.map(([endpoint, label]) => {
        const source = sources[endpoint];
        const choosing = editable && (!source.id || editing[endpoint]
          || (initial.missing_fields.includes(`${endpoint}_version_id`) && !overrides[endpoint]));
        return <section key={endpoint} aria-label={label} className="source-authority-prefill-source">
          <h4>{label}</h4><p>{source.title || "待补充"}</p>
          {source.id ? <><button type="button" className="source-authority-link" onClick={() => openSource(source.id!)}>查看{label}原文</button>
            <details><summary>版本信息</summary><code>{source.id}</code></details></> : <p className="source-authority-warning">尚未确定可读的确切版本</p>}
          {choosing ? <><VersionBlockPicker blockRequired={false} onSelect={(resource, version) => select(endpoint, resource, version)} />
            {source.id && <button type="button" onClick={() => setEditing(previous => ({ ...previous, [endpoint]: false }))}>保留原{label}</button>}</>
            : editable && <button type="button" onClick={() => edit(endpoint)}>更换{label}</button>}
        </section>;
      })}</div>
      {sources.predecessor.id && sources.predecessor.id === sources.successor.id && <p role="alert">旧规则与后继规则不能选择同一个版本。</p>}
      <div className="source-authority-meta">
        <div>{needsEdit("effective_from", !effectiveFrom) ? <Field label="替代起始日期（含当日）"><input type="date" required value={effectiveFrom}
          onChange={event => setEffectiveFrom(event.target.value)} /></Field> : <><h4>替代起始日期（含当日）</h4><p>{effectiveFrom || "待补充"}</p>
          {editable && <button type="button" onClick={() => edit("effective_from")}>修改日期</button>}</>}</div>
        <div>{needsEdit("scope", !scope) ? <Field label="替代范围"><select required value={scope} onChange={event => setScope(event.target.value)}>
          <option value="">请选择范围</option><option value="full">全部替代（full）</option><option value="partial">部分替代（partial）</option>
        </select></Field> : <><h4>替代范围</h4><p>{scope === "full" ? "全部替代（full）" : scope === "partial" ? "部分替代（partial）" : "待补充"}</p>
          {editable && <button type="button" onClick={() => edit("scope")}>修改范围</button>}</>}</div>
      </div>
      {needsEdit("reason", !reason.trim()) ? <Field label="证据说明与确认原因"><textarea required rows={3} value={reason} onChange={event => setReason(event.target.value)} /></Field>
        : <><h4>证据说明与确认原因</h4><p className="source-authority-reason">{reason || "待补充"}</p>
          {editable && <button type="button" onClick={() => edit("reason")}>修改说明</button>}</>}
    </fieldset>
    {scope === "partial" && <p>仅按所述范围替代，不能据此整份排除旧文。</p>}
    {effectiveFrom > beijingToday() && <p>未来日期 · 尚不适用；起始日期前不能作为当日替代依据。</p>}
    {sourceChanged && <p className="source-authority-warning">来源已手动调整，请同时核对日期、范围和说明；提交前会重新核对所选版本。</p>}
    {excerpts.length > 0 && <section className="source-authority-excerpts" aria-label="替代证据摘录"><h4>来源原文摘录</h4>
      {excerpts.map((excerpt, index) => <div key={excerpt.version_id + ":" + excerpt.block_id + ":" + index}>
        <blockquote>{excerpt.text}</blockquote><small>{typeof excerpt.locator === "object" && excerpt.locator !== null && "label" in excerpt.locator
          ? readable(excerpt.locator.label) : readable(excerpt.locator)}</small>
        <button type="button" disabled={task.busy} onClick={() => openSource(excerpt.version_id, excerpt.block_id)}>定位此段原文</button>
      </div>)}
    </section>}
    {record && <dl className="source-authority-meta"><div><dt>确认人</dt><dd>{record.created_by === app.me.id ? app.me.display_name : record.created_by}</dd></div><div><dt>确认时间</dt><dd>{record.created_at}</dd></div>
      <div><dt>记录修订</dt><dd>{record.revision}</dd></div><div><dt>来源校验</dt><dd>{record.validation_note || record.validation_state || "待复核"}</dd></div>
      {record.state === "REVOKED" && <><div><dt>撤销原因</dt><dd>{record.revoke_reason || "未提供"}</dd></div><div><dt>撤销时间</dt><dd>{record.revoked_at || "未提供"}</dd></div></>}
    </dl>}
    <ErrorBox error={selectionError} /><ErrorBox error={task.error} />
    {unknownMissing.length > 0 && <p role="alert">仍需补充：{unknownMissing.join("、")}。请重新读取建议核对。</p>}
    {bindingMissing && <p role="alert">来源绑定尚不完整，请重新读取建议后确认。</p>}
    {stale && <p role="alert">建议已过期或回执尚未确认。已保留信息供核对，请重新读取建议，不会自动再次提交。</p>}
    <div className="source-authority-actions">
      {alreadyConfirmed ? <button type="submit" disabled>已确认，无需重复提交</button>
        : <button className="primary" type="submit" disabled={!ready || task.busy}>{task.busy ? "正在核对并提交…" : "确认替代事实"}</button>}
      {(stale || bindingMissing || unknownMissing.length > 0) && <button type="button" disabled={task.busy} onClick={reload}>重新读取建议</button>}
      <button type="button" disabled={task.busy} onClick={cancel}>关闭核对</button>
    </div>
  </form>;
}

function CreateAuthorityForm({ spaceId, deny, complete, cancel, openSource, cancelOpen, records, showRecord }: {
  spaceId: string; deny: () => void; complete: () => void; cancel: () => void; openSource: (id: string) => void; cancelOpen: () => void;
  records: SourceAuthorityRecord[]; showRecord: (id: string) => void;
}) {
  const [choices, setChoices] = useState<Partial<Record<Endpoint, Choice>>>({});
  const [pickerKey, setPickerKey] = useState(0);
  const [effectiveFrom, setEffectiveFrom] = useState("");
  const [scope, setScope] = useState<"" | "full" | "partial">("");
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [selectionError, setSelectionError] = useState<Error>();
  const [receiptMismatch, setReceiptMismatch] = useState(false);
  const task = useAuthorityAction(deny);
  const duplicate = matchingRecord(records, { predecessor_version_id: choices.predecessor?.version.id ?? null,
    successor_version_id: choices.successor?.version.id ?? null, evidence_version_id: choices.evidence?.version.id ?? null,
    effective_from: effectiveFrom, scope });
  const ready = endpoints.every(([endpoint]) => !!choices[endpoint])
    && choices.predecessor?.version.id !== choices.successor?.version.id
    && validDate(effectiveFrom) && !!scope && !!reason.trim() && confirmed && !receiptMismatch && !duplicate;
  function select(endpoint: Endpoint, resource?: Resource, version?: Version) {
    cancelOpen();
    setConfirmed(false); task.clearError(); setSelectionError(undefined);
    const valid = resource && resource.space_id === spaceId && resource.kind === "document"
      && !resource.suspended && !resource.deleted_at && version && version.resource_id === resource.id;
    setChoices(previous => ({ ...previous, [endpoint]: valid ? { resource, version } : undefined }));
    if ((resource && (resource.space_id !== spaceId || resource.kind !== "document")) || (version && !valid))
      setSelectionError(new Error("请选择当前空间的可读文档及其具体版本，知识页不能代替证据原件。"));
  }
  async function submit() {
    if (!ready || task.busy) return;
    await task.run(async (signal, write) => {
      try {
        // Picker selections are not an authorization cache. Recheck each exact source before the explicit write.
        await Promise.all(endpoints.map(async ([endpoint]) => {
          const previous = choices[endpoint]!;
          const current = await readSource(previous.version.id, spaceId, signal);
          if (current.resource.id !== previous.resource.id || current.resource.revision !== previous.resource.revision
              || current.resource.access_epoch !== previous.resource.access_epoch || current.version.revision !== previous.version.revision
              || current.version.content_sha256 !== previous.version.content_sha256)
            throw new Error("来源版本、内容或权限已变化，请重新选择三份版本并核对证据。");
        }));
      } catch (error) {
        if (!signal.aborted) { setChoices({}); setPickerKey(key => key + 1); setConfirmed(false); }
        throw error;
      }
      signal.throwIfAborted();
      const payload = { space_id: spaceId, predecessor_version_id: choices.predecessor!.version.id,
        successor_version_id: choices.successor!.version.id, evidence_version_id: choices.evidence!.version.id,
        effective_from: effectiveFrom, scope, reason: reason.trim() };
      try {
        const result = await write<SourceAuthorityRecord>("/source-authority", payload);
        signal.throwIfAborted();
        try {
          checkedRecord(result, spaceId);
          if (result.state !== "ACTIVE" || Object.entries(payload).some(([key, value]) => result[key as keyof SourceAuthorityRecord] !== value))
            throw new Error("确认回执与提交内容不一致，结果尚未确认，请刷新关系核对。");
        } catch (error) { setReceiptMismatch(true); setConfirmed(false); throw error; }
        complete();
      } catch (error) {
        if (!signal.aborted && error instanceof ApiError && [409, 412, 422].includes(error.status)) {
          setChoices({}); setPickerKey(key => key + 1); setConfirmed(false);
        }
        throw error;
      }
    });
  }
  return <form className="source-authority-form" aria-label="确认替代事实表单" onSubmit={event => { event.preventDefault(); void submit(); }}>
    <h3>核对并确认替代事实</h3>
    <fieldset disabled={task.busy}>
      <div className="source-authority-sources">{endpoints.map(([endpoint, label]) => <section key={`${endpoint}:${pickerKey}`} aria-label={label}>
        <h4>{label}</h4>
        <VersionBlockPicker blockRequired={false} onSelect={(resource, version) => select(endpoint, resource, version)} />
        {choices[endpoint] && <button type="button" onClick={() => openSource(choices[endpoint]!.version.id)}>核对所选{label}</button>}
      </section>)}</div>
      <ErrorBox error={selectionError} />
      {choices.predecessor && choices.successor && choices.predecessor.version.id === choices.successor.version.id
        && <p role="alert">旧规则与后继规则不能选择同一个版本。</p>}
      <div className="source-authority-meta">
        <Field label="替代起始日期（含当日）"><input type="date" required value={effectiveFrom} onChange={event => { setEffectiveFrom(event.target.value); setConfirmed(false); }} /></Field>
        <Field label="替代范围"><select required value={scope} onChange={event => { setScope(event.target.value as typeof scope); setConfirmed(false); }}>
          <option value="">请选择范围</option><option value="full">全部替代（full）</option><option value="partial">部分替代（partial）</option>
        </select></Field>
      </div>
      <Field label="证据说明与确认原因" hint="说明证据中的替代条款；部分替代时写明适用范围。"><textarea required rows={3} value={reason}
        onChange={event => { setReason(event.target.value); setConfirmed(false); }} /></Field>
      <label className="source-authority-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />
        <span>我已核对所选确切版本的证据内容、替代范围和起始日期，仅确认来源替代事实，不认定全文现行有效或专家审核通过。</span></label>
    </fieldset>
    <ErrorBox error={task.error} />
    {duplicate && <p role="status">已确认，无需重复提交。<button type="button" onClick={() => showRecord(duplicate.id)}>查看已确认要素</button></p>}
    <div className="source-authority-actions"><button className="primary" type="submit" disabled={!ready || task.busy}>{task.busy ? "正在核对并提交…" : "提交确认"}</button>
      <button type="button" disabled={task.busy} onClick={cancel}>取消确认</button></div>
  </form>;
}

function RevokeAuthorityForm({ id, spaceId, deny, complete, cancel }: {
  id: string; spaceId: string; deny: () => void; complete: () => void; cancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [conflict, setConflict] = useState(false);
  const task = useAuthorityAction(deny);
  const loaded = useLoad(async signal => {
    try { return checkedRecord(await get<SourceAuthorityRecord>(endpointPath(id), signal), spaceId, id); }
    catch (error) { if (!signal.aborted && permissionFailure(error)) deny(); throw error; }
  }, [id, spaceId]);
  const record = !loaded.loading && !conflict ? loaded.data : undefined;
  const ready = !!record && record.state === "ACTIVE" && !!reason.trim() && confirmed && !task.busy;
  const reload = () => { setConfirmed(false); setConflict(false); task.clearError(); loaded.reload(); };
  async function submit() {
    if (!ready || !record) return;
    await task.run(async (signal, write) => {
      try {
        const result = await write<SourceAuthorityRecord>(`${endpointPath(id)}/revoke`, { reason: reason.trim() }, record.revision);
        signal.throwIfAborted();
        try {
          checkedRecord(result, spaceId, id);
          if (result.state !== "REVOKED" || result.revision <= record.revision || result.revoke_reason !== reason.trim())
            throw new Error("撤销回执尚未确认，请重新读取记录核对状态。");
        } catch (error) { setConflict(true); setConfirmed(false); throw error; }
        complete();
      } catch (error) {
        if (!signal.aborted && error instanceof ApiError && [409, 412].includes(error.status)) { setConflict(true); setConfirmed(false); }
        throw error;
      }
    });
  }
  return <form className="source-authority-form" aria-label="撤销事实表单" onSubmit={event => { event.preventDefault(); void submit(); }}>
    <h3>撤销替代事实</h3>
    <p>撤销将停用这条治理记录并保留历史，不删除任何文档，也不改写历史答案或引文。</p>
    {loaded.loading && <Loading label="正在读取当前记录与修订号…" />}
    <ErrorBox error={loaded.error} retry={reload} />
    {record && <>
      <p>{record.predecessor_title} → {record.successor_title} · 证据：{record.evidence_title} · 修订 {record.revision}</p>
      <dl className="source-authority-meta">
        <div><dt>替代起始日期（含当日）</dt><dd>{record.effective_from}</dd></div>
        <div><dt>替代范围</dt><dd>{record.scope === "full" ? "全部替代（full）" : "部分替代（partial）"}</dd></div>
        {endpoints.map(([endpoint, label]) => <div key={endpoint}><dt>{label}确切版本</dt><dd><code>{record[`${endpoint}_version_id`]}</code></dd></div>)}
      </dl>
      <p className="source-authority-reason"><strong>原确认原因：</strong>{record.reason}</p>
      {record.validation_note && <p>{record.validation_note}</p>}
    </>}
    {record?.state === "REVOKED" && <p>记录已撤销，无需重复提交。</p>}
    <fieldset disabled={task.busy}>
      <Field label="撤销原因"><textarea required rows={3} value={reason} onChange={event => { setReason(event.target.value); setConfirmed(false); }} /></Field>
      <label className="source-authority-confirm"><input type="checkbox" checked={confirmed} disabled={!record || record.state !== "ACTIVE"}
        onChange={event => setConfirmed(event.target.checked)} /><span>我确认停用上述治理记录，保留文档和撤销历史。</span></label>
    </fieldset>
    <ErrorBox error={task.error} />
    {conflict && <p role="alert">记录已变化或回执尚未确认。撤销原因已保留，请重新读取记录并再次确认。</p>}
    <div className="source-authority-actions">
      <button type="submit" disabled={!ready}>{task.busy ? "正在撤销…" : "确认撤销"}</button>
      <button type="button" disabled={task.busy || loaded.loading} onClick={reload}>重新读取记录</button>
      <button type="button" disabled={task.busy} onClick={cancel}>取消撤销</button>
    </div>
  </form>;
}
