import { lazy, Suspense, useEffect, useState } from "react";
import { ApiError, get, query } from "./api";
import { ResourcePicker } from "./Pickers";
import type { Resource, Version } from "./types";
import { Badge, ErrorBox, Loading, Notice, useApp, useLoad } from "./ui";
import { CapabilitiesEditor, definitionFromKnowledge, readCapabilitySource } from "./CapabilitiesEditor";
import { CapabilitiesRuns, CapabilityTrialForm } from "./CapabilitiesRuns";
import { CapabilitiesAccess } from "./CapabilitiesAccess";
import { CapabilityFlow, capabilityBoundary, checkedDetail, definitionErrors, isAccessError, object, pathId, saveCapabilityFile, stableJson, stringList, useCapabilitiesTask,
  type CapabilityDefinition, type CapabilityDetail } from "./CapabilitiesShared";
import "./capabilities.css";

const LegacyTemplates = lazy(() => import("./ResourcesPage").then(module => ({ default: module.ResourcesPage })));
type Catalog = { items: CapabilityDetail[]; legacy_items?: { resource_id: string; name: string }[]; can_edit: boolean; notes: string[] };

export function CapabilitiesPage() {
  const app = useApp();
  return <CapabilitiesScope key={JSON.stringify([app.me.id, app.space.id, app.space.revision, app.space.roles, app.me.is_admin, app.refresh])} />;
}

function CapabilitiesScope() {
  const [tab, setTab] = useState("catalog");
  const [runId, setRunId] = useState<string>();
  const [blocked, setBlocked] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const deny = () => { setBlocked(true); setRunId(undefined); setTab(current => current === "access" ? "catalog" : current); };
  useEffect(() => { window.addEventListener("session-expired", deny); return () => window.removeEventListener("session-expired", deny); }, []);
  return <>
    <header className="page-heading"><div><h1>Agent能力中心</h1><p>将已沉淀知识转为可维护的工作流，记录 Agent 报告与人工核对。</p></div></header>
    <nav className="tabs" aria-label="能力中心页面">{[["catalog", "能力目录"], ["runs", "运行记录"], ["access", "Agent接入"], ["legacy", "旧方案模板"]].map(([id, label]) =>
      <button key={id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>{label}</button>)}</nav>
    <div className="standard-page capabilities-page" key={epoch}>
      {blocked && tab !== "access" ? <div className="capabilities-surface"><p role="alert">当前身份、知识库或对象权限已失效，旧数据与一次性凭据已清除。</p>
        <p>仍可明确进入“Agent接入”，由服务端重新核对是否允许查看或撤销本人的凭据；这不恢复内容访问。</p>
        <button type="button" onClick={() => { setBlocked(false); setEpoch(value => value + 1); }}>重新读取当前知识库</button></div>
        : tab === "catalog" ? <CapabilitiesCatalog deny={deny} started={id => { setRunId(id); setTab("runs"); }} legacy={() => setTab("legacy")} />
        : tab === "runs" ? <CapabilitiesRuns key={runId ?? "list"} initialId={runId} deny={deny} />
        : tab === "access" ? <CapabilitiesAccess deny={deny} />
        : <><Notice>旧普通模板保留原正文与原使用方式。未结构化模板不会自动转换为 Agent 能力。</Notice><Suspense fallback={<Loading />}><LegacyTemplates kind="template" /></Suspense></>}
    </div>
  </>;
}

function CapabilitiesCatalog({ deny, started, legacy }: { deny: () => void; started: (id: string) => void; legacy: () => void }) {
  const app = useApp();
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<{ resourceId: string; versionId?: string }>();
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<CapabilityDefinition>();
  const [knowledge, setKnowledge] = useState<Resource>();
  const [localRefresh, setLocalRefresh] = useState(0);
  const task = useCapabilitiesTask(deny);
  const loaded = useLoad(async signal => {
    try {
      const value = await get<Catalog>(`/capabilities?${query({ space_id: app.space.id })}`, signal);
      if (!value || !Array.isArray(value.items) || typeof value.can_edit !== "boolean" || !stringList(value.notes)) throw new Error("能力目录或编辑权限尚未确认。");
      value.items.forEach(item => checkedDetail(item, app.space.id));
      if (new Set(value.items.map(item => item.resource_id)).size !== value.items.length) throw new Error("能力目录包含重复标识。");
      return value;
    } catch (error) { if (!signal.aborted && isAccessError(error)) deny(); throw error; }
  }, [app.space.id, localRefresh]);
  const canEdit = !loaded.loading && !loaded.error && loaded.data?.can_edit === true;
  const starter = useLoad(async signal => {
    if (!creating || !canEdit) return null;
    try {
      const value = await get<{ definition: CapabilityDefinition; state: string }>(`/capabilities/starter?${query({ space_id: app.space.id })}`, signal);
      if (!value || value.state !== "EXAMPLE_NOT_SAVED" || definitionErrors(value.definition).length) throw new Error("工作底稿起点尚未完整就绪。");
      return value;
    } catch (error) { if (!signal.aborted && isAccessError(error)) deny(); throw error; }
  }, [creating, canEdit, app.space.id]);
  const refresh = () => { setCreating(false); setDraft(undefined); setKnowledge(undefined); setLocalRefresh(value => value + 1); };
  const created = (detail: CapabilityDetail) => { refresh(); setSelected({ resourceId: detail.resource_id, versionId: detail.version_id }); };
  async function createStarter() {
    if (!canEdit || !starter.data || task.busy) return;
    await task.run(async (signal, write) => {
      const definition = structuredClone(starter.data!.definition);
      const detail = checkedDetail(await write<CapabilityDetail>("/capabilities", { space_id: app.space.id, definition }), app.space.id);
      signal.throwIfAborted();
      if (detail.state !== "DRAFT" || stableJson(detail.definition) !== stableJson(definition)) throw new Error("创建回执与所选草稿定义不一致，未将它视为成功沉淀。");
      created(detail);
    });
  }
  async function importKnowledge() {
    if (!canEdit || !knowledge?.active_version_id || knowledge.kind !== "knowledge") return;
    const resourceId = knowledge.id, versionId = knowledge.active_version_id;
    await task.run(async signal => {
      const source = await readCapabilitySource(versionId, app.space.id, signal, "knowledge");
      signal.throwIfAborted();
      if (source.resource.id !== resourceId) throw new Error("发布知识与所选对象不一致。");
      setDraft(definitionFromKnowledge(source.version)); setCreating(false);
    });
  }
  const items = loaded.data?.items.filter(item => !filter || [item.name, item.definition?.description ?? ""].some(value => value.includes(filter))) ?? [];
  return <>
    <div className="capabilities-toolbar"><input aria-label="搜索能力" type="search" placeholder="按用途或名称查找能力" value={filter} onChange={event => setFilter(event.target.value)} />
      <button className="primary" disabled={!canEdit || task.busy} onClick={() => { setCreating(true); setSelected(undefined); setDraft(undefined); }}>沉淀新能力</button>
      <button onClick={() => { setSelected(undefined); refresh(); }}>刷新能力目录</button></div>
    <Notice>{capabilityBoundary}</Notice>
    <ErrorBox error={loaded.error} retry={refresh} /><ErrorBox error={task.error} />
    {loaded.loading && <Loading label="正在读取完整可见能力目录…" />}
    {!loaded.loading && loaded.data && !canEdit && <p>当前可查看能力；沉淀和维护须由服务端授予编辑权限。</p>}
    {creating && canEdit && <section className="capabilities-surface"><h2>选择沉淀起点</h2><div className="capabilities-grid-two">
      <article className="capabilities-choice"><h3>工作底稿起点</h3><p>{starter.data?.definition.description ?? "读取平台提供的工作底稿定义；创建后仍为 DRAFT。"}</p>
        {starter.loading && <Loading label="正在读取起点定义…" />}<ErrorBox error={starter.error} retry={starter.reload} />
        {starter.data && <CapabilityFlow steps={starter.data.definition.steps} />}
        <button className="primary" disabled={!starter.data || task.busy} onClick={() => void createStarter()}>用工作底稿创建草稿</button>
        <button disabled={!starter.data || task.busy} onClick={() => { if (starter.data) { setDraft(structuredClone(starter.data.definition)); setCreating(false); } }}>先调整定义</button></article>
      <article className="capabilities-choice"><h3>从已发布知识沉淀</h3><p>只复制真实结构化 step 内容；没有结构化步骤时需要补充，不自动编造。</p>
        <ResourcePicker label="选择已发布知识" value={knowledge?.id ?? ""} change={setKnowledge} />
        {knowledge && (knowledge.kind !== "knowledge" || !knowledge.active_version_id) && <p role="alert">请选择已发布的知识页。</p>}
        <button disabled={!knowledge?.active_version_id || knowledge.kind !== "knowledge" || task.busy} onClick={() => void importKnowledge()}>带入已发布知识步骤</button></article>
    </div><button disabled={task.busy} onClick={() => setCreating(false)}>关闭沉淀入口</button></section>}
    {draft && canEdit && <><Notice>来源只带入明确的 action、责任角色、输出和核对要求。导入步骤暂按人工核对配置；请核对工具、依赖和风险，再保存草稿。</Notice>
      <CapabilitiesEditor key={JSON.stringify(draft)} initial={draft} deny={deny} saved={created} close={() => setDraft(undefined)} /></>}
    {selected && <CapabilityDetailPane key={selected.resourceId + ":" + (selected.versionId ?? "current") + ":" + localRefresh}
      selection={selected} canCreateRevision={canEdit} deny={deny} started={started} changed={created} close={() => setSelected(undefined)} />}
    {!selected && !draft && !loaded.loading && loaded.data && <div className="capabilities-cards">{items.map(item => <article className="capabilities-card" key={item.resource_id}>
      <div className="capabilities-row"><h2>{item.name}</h2><Badge value={item.state} /></div><p>{item.definition?.description || "尚未结构化，请通过旧模板入口查看。"}</p>
      <dl className="capabilities-metrics"><div><dt>版本</dt><dd>V{item.version_no}</dd></div><div><dt>输入要素</dt><dd>{item.definition?.inputs.length ?? "未提供"}</dd></div>
        <div><dt>工作步骤</dt><dd>{item.definition?.steps.length ?? "未提供"}</dd></div><div><dt>绑定来源</dt><dd>{item.source_bindings.length}</dd></div></dl>
      {item.definition && <p className="capabilities-muted">{item.definition.inputs.map(field => field.label + (field.required ? "（必填）" : "")).join("、") || "无声明输入"}</p>}
      <button onClick={() => setSelected({ resourceId: item.resource_id, versionId: item.version_id })}>查看能力</button>
    </article>)}</div>}
    {!loaded.loading && loaded.data && !items.length && <p className="capabilities-empty">尚无匹配的结构化能力。可以从通用工作底稿或已发布知识开始沉淀。</p>}
    {!!loaded.data?.legacy_items?.length && <p>另有 {loaded.data.legacy_items.length} 份旧普通模板，正文保持原样。<button onClick={legacy}>查看旧方案模板</button></p>}
    {loaded.data?.notes.map((note, index) => <p className="capabilities-muted" key={index}>{note}</p>)}
  </>;
}

function CapabilityDetailPane({ selection, canCreateRevision, deny, changed, started, close }: {
  selection: { resourceId: string; versionId?: string }; canCreateRevision: boolean; deny: () => void; changed: (value: CapabilityDetail) => void;
  started: (id: string) => void; close: () => void;
}) {
  const app = useApp();
  const [edit, setEdit] = useState(false);
  const [trial, setTrial] = useState(false);
  const [copy, setCopy] = useState(false);
  const [copyReason, setCopyReason] = useState("");
  const [skill, setSkill] = useState<{ filename: string; skill_markdown: string; workflow_json: Record<string, unknown>; connector_markdown: string }>();
  const task = useCapabilitiesTask(deny);
  const loaded = useLoad(async signal => {
    try { return checkedDetail(await get<CapabilityDetail>(selection.versionId ? `/capability-versions/${pathId(selection.versionId)}`
      : `/capabilities/${pathId(selection.resourceId)}`, signal), app.space.id, selection); }
    catch (error) { if (!signal.aborted && isAccessError(error)) deny(); throw error; }
  }, [selection.resourceId, selection.versionId]);
  const detail = loaded.data, definition = detail?.definition;
  function exportSkill() {
    if (!detail?.permissions.can_export || task.busy) return;
    setSkill(undefined);
    void task.run(async signal => {
      const value = await get<NonNullable<typeof skill>>(`/capability-versions/${pathId(detail.version_id)}/skill`, signal); signal.throwIfAborted();
      if (!value || value.filename !== "SKILL.md" || typeof value.skill_markdown !== "string" || !value.skill_markdown.trim()
          || typeof value.connector_markdown !== "string" || !value.connector_markdown.trim() || !object(value.workflow_json))
        throw new Error("技能包回执不完整，请重新读取。");
      setSkill(value);
    });
  }
  async function copyRevision() {
    if (!detail || !canCreateRevision || !copyReason.trim() || task.busy) return;
    await task.run(async (signal, write) => {
      const result = await write<Version>(`/resources/${pathId(detail.resource_id)}/versions`, {
        title: detail.name, change_reason: copyReason.trim(), change_kind: "UPDATE", base_version_id: detail.version_id,
      });
      signal.throwIfAborted();
      if (result.resource_id !== detail.resource_id || result.state !== "DRAFT" || result.base_version_id !== detail.version_id) throw new Error("修订回执未确认是所选版本的 COPY 草稿，请重新读取。");
      const current = checkedDetail(await get<CapabilityDetail>(`/capability-versions/${pathId(result.id)}`, signal), app.space.id, { resourceId: detail.resource_id, versionId: result.id });
      signal.throwIfAborted(); changed(current);
    });
  }
  return <section className="capabilities-surface" aria-label="能力详情">
    <div className="capabilities-row"><h2>{detail?.name ?? "能力详情"}</h2><button disabled={task.busy} onClick={close}>关闭能力详情</button></div>
    {loaded.loading && <Loading />}<ErrorBox error={loaded.error} retry={loaded.reload} /><ErrorBox error={task.error} />
    {detail && definition && <>
      <div className="capabilities-row"><Badge value={detail.state} /><span>V{detail.version_no} · 修订 {detail.revision}</span><span>{definition.source_scope === "reference" ? "资料辅助 · 未专业核验" : "正式范围仍需服务端准入"}</span></div>
      <p>{definition.description}</p><CapabilityFlow steps={definition.steps} />
      <div className="capabilities-row"><button disabled={!detail.permissions.can_edit || detail.state !== "DRAFT"} onClick={() => setEdit(true)}>编辑能力定义</button>
        <button disabled={!detail.permissions.can_trial && !detail.permissions.can_run} onClick={() => setTrial(true)}>试运行能力</button>
        <button disabled={!detail.permissions.can_export || task.busy} onClick={exportSkill}>导出技能包</button>
        {detail.state !== "DRAFT" && <button disabled={!canCreateRevision || task.busy} onClick={() => setCopy(true)}>新建 COPY 草稿修订</button>}
        <button disabled={task.busy} onClick={() => void task.run(async signal => {
          const resource = await get<Resource>(`/resources/${pathId(detail.resource_id)}`, signal); signal.throwIfAborted();
          if (resource.id !== detail.resource_id || resource.space_id !== app.space.id) throw new Error("版本对象与当前知识库不一致。");
          app.openResource(resource, "review", detail.version_id);
        })}>版本与原审核流程</button></div>
      {copy && <form className="capabilities-inset" aria-label="新建能力修订" onSubmit={event => { event.preventDefault(); void copyRevision(); }}>
        <p>复制所选版本建立新 DRAFT，保留原版本。审核发布继续使用原流程。</p>
        <label className="field"><span>修订原因</span><textarea aria-label="修订原因" value={copyReason} disabled={task.busy} onChange={event => setCopyReason(event.target.value)} /></label>
        <button type="submit" disabled={task.busy || !copyReason.trim()}>创建修订草稿</button><button type="button" disabled={task.busy} onClick={() => setCopy(false)}>取消修订</button></form>}
      {edit && detail.permissions.can_edit && detail.state === "DRAFT" && <CapabilitiesEditor key={detail.version_id + ":" + detail.revision} initial={definition} detail={detail} deny={deny} saved={changed} close={() => setEdit(false)} />}
      {trial && <CapabilityTrialForm detail={detail} deny={deny} started={run => started(run.id)} close={() => setTrial(false)} />}
      <div className="capabilities-grid-two"><section><h3>输入与交付</h3><ul>{definition.inputs.map(field => <li key={field.key}>{field.label} · {field.type} · {field.required ? "必填" : "可选"}：{field.description}</li>)}</ul>
        <ul>{definition.deliverables.map((value, i) => <li key={i}>{value}</li>)}</ul></section><section><h3>使用边界</h3><ul>{[...definition.triggers, ...definition.limitations].map((value, i) => <li key={i}>{value}</li>)}</ul></section></div>
      <section><h3>步骤指导与核对要求</h3>{definition.steps.map(step => <details className="capabilities-step-detail" key={step.id}><summary>{step.title} · {step.kind === "human" ? "人工" : "Agent"}</summary>
        <p className="capabilities-prose">{step.instructions}</p><p>工具声明：{step.required_tools.join("、") || "无"}</p><ul>{step.checks.map((value, i) => <li key={i}>{value}</li>)}</ul>
        <ul>{step.outputs.map(field => <li key={field.key}>{field.label}（{field.type}，{field.required ? "必填" : "可选"}）：{field.description}</li>)}</ul></details>)}</section>
      <section><h3>冻结来源</h3>{detail.source_bindings.length ? <ul className="capabilities-source-list">{detail.source_bindings.map(source => <li key={source.version_id}>
        <span>{source.title ?? "已绑定来源"}</span><code>{source.version_id}</code><small>{source.content_sha256 ?? "摘要由服务端冻结"}</small></li>)}</ul> : <p>没有绑定来源，不能视为已具备业务证据。</p>}</section>
      <details className="capabilities-advanced"><summary>完整机器定义与摘要</summary><p>Manifest SHA-256：<code>{detail.manifest_sha256 ?? "未提供"}</code></p>
        <p>内容 SHA-256：<code>{detail.content_sha256 ?? "未提供"}</code></p><pre>{JSON.stringify({ definition, source_bindings: detail.source_bindings }, null, 2)}</pre></details>
      {skill && <section className="capabilities-inset" aria-label="可下载技能包"><h3>技能包已准备</h3><p>导出不等于安装、连接或外部操作授权。请由 Agent 使用者自行配置接入。</p>
        <div className="capabilities-row"><button onClick={() => saveCapabilityFile(skill.filename || "SKILL.md", skill.skill_markdown)}>下载 SKILL.md</button>
          <button onClick={() => saveCapabilityFile("workflow.json", JSON.stringify(skill.workflow_json, null, 2))}>下载 workflow.json</button>
          <button onClick={() => saveCapabilityFile("connector.md", skill.connector_markdown)}>下载接入说明</button></div>
        <details><summary>查看 SKILL.md</summary><pre>{skill.skill_markdown}</pre></details></section>}
      {detail.notes.map((note, index) => <p className="capabilities-muted" key={index}>{note}</p>)}
    </>}
  </section>;
}
