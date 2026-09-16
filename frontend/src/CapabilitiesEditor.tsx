import { useEffect, useState } from "react";
import { ApiError, get } from "./api";
import { VersionBlockPicker } from "./Pickers";
import type { Resource, Version } from "./types";
import { ErrorBox, Field, Notice, useApp } from "./ui";
import { CapabilityFlow, checkedDetail, definitionErrors, fieldLabels, fieldTypes, lines, pathId, riskLabels, stableJson, useCapabilitiesTask,
  type CapabilityDefinition, type CapabilityDetail, type CapabilityField, type CapabilityStep } from "./CapabilitiesShared";

function LineList({ label, value, change }: { label: string; value: string[]; change: (value: string[]) => void }) {
  const [text, setText] = useState(value.join("\n"));
  useEffect(() => { if (JSON.stringify(lines(text)) !== JSON.stringify(value)) setText(value.join("\n")); }, [JSON.stringify(value)]);
  return <Field label={label} hint="每行一项"><textarea aria-label={label} rows={2} value={text}
    onChange={event => { setText(event.target.value); change(lines(event.target.value)); }} /></Field>;
}

function FieldDefinitions({ label, fields, change }: { label: string; fields: CapabilityField[]; change: (fields: CapabilityField[]) => void }) {
  const update = (index: number, patch: Partial<CapabilityField>) => change(fields.map((field, i) => i === index ? { ...field, ...patch } : field));
  return <section className="capabilities-fields" aria-label={label}>
    <div className="capabilities-row"><h4>{label}</h4><button type="button" onClick={() => change([...fields,
      { key: "", label: "", type: "string", required: true, description: "" }])}>添加{label}</button></div>
    {fields.map((field, index) => <div className="capabilities-field-row" key={index}>
      <Field label="字段标识"><input aria-label={`${label} ${index + 1} 标识`} value={field.key} onChange={event => update(index, { key: event.target.value })} /></Field>
      <Field label="显示名称"><input aria-label={`${label} ${index + 1} 名称`} value={field.label} onChange={event => update(index, { label: event.target.value })} /></Field>
      <Field label="字段类型"><select aria-label={`${label} ${index + 1} 类型`} value={field.type} onChange={event => update(index, { type: event.target.value as CapabilityField["type"] })}>
        {fieldTypes.map(type => <option key={type} value={type}>{fieldLabels[type]}</option>)}</select></Field>
      <label className="capabilities-check"><input type="checkbox" checked={field.required} onChange={event => update(index, { required: event.target.checked })} />必填</label>
      <Field label="填写说明"><input aria-label={`${label} ${index + 1} 说明`} value={field.description} onChange={event => update(index, { description: event.target.value })} /></Field>
      <button type="button" onClick={() => change(fields.filter((_, i) => i !== index))}>删除{label} {index + 1}</button>
    </div>)}
    {!fields.length && <p className="capabilities-muted">尚未声明{label}。</p>}
  </section>;
}

export function definitionFromKnowledge(version: Version): CapabilityDefinition {
  const blocks = version.blocks.filter(block => block.block_type === "step");
  if (!blocks.length) throw new Error("这份已发布知识没有结构化步骤。请先补充操作步骤，或使用通用工作底稿逐项编辑；不会从普通段落编造流程。");
  const text = (value: unknown) => typeof value === "string" ? value : "";
  return { schema_version: 1, name: version.title, description: `从已发布知识“${version.title}”沉淀的任务指导，需核对步骤、风险与交付要求。`,
    triggers: ["适用于所绑定知识明确描述的业务场景"], limitations: ["来源步骤不等于外部操作授权；尚未明确的工具、依赖与风险需人工补充"], inputs: [],
    steps: blocks.map((block, index) => ({ id: `source_step_${index + 1}`, title: `原文步骤 ${index + 1}`, kind: "human", risk: "read_only",
      instructions: text(block.data.action) + (text(block.data.owner_role) ? `\n原文责任角色：${text(block.data.owner_role)}` : ""),
      depends_on: [], required_tools: [], outputs: text(block.data.output) ? [{ key: "result", label: "步骤输出", type: "string", required: true, description: text(block.data.output) }] : [],
      checks: text(block.data.verification) ? [text(block.data.verification)] : [] })),
    deliverables: blocks.map(block => text(block.data.output)).filter(Boolean), source_version_ids: [version.id], source_scope: "reference" };
}

// GET /versions does not expose historical release presence. This is a preliminary
// readability/state check; capability create/update recheck actual release records and hashes.
export async function readCapabilitySource(id: string, spaceId: string, signal: AbortSignal, kind?: "knowledge") {
  const version = await get<Version>(`/versions/${pathId(id)}`, signal); signal.throwIfAborted();
  if (version.id !== id || !version.resource_id) throw new Error("来源版本与选择不一致。");
  const resource = await get<Resource>(`/resources/${pathId(version.resource_id)}`, signal); signal.throwIfAborted();
  if (resource.space_id !== spaceId || resource.id !== version.resource_id || resource.suspended || resource.deleted_at
      || !["document", "knowledge"].includes(resource.kind) || version.state !== "APPROVED" || (kind && resource.kind !== kind))
    throw new Error("来源必须是当前知识库可读、已批准的文档或知识版本。请先通过原审核流程；发布记录和摘要在保存时由服务端核对。");
  return { resource, version };
}

export function CapabilitiesEditor({ initial, detail, deny, saved, close }: {
  initial: CapabilityDefinition; detail?: CapabilityDetail; deny: () => void; saved: (value: CapabilityDetail) => void; close: () => void;
}) {
  const app = useApp();
  const [definition, setDefinition] = useState<CapabilityDefinition>(() => structuredClone(initial));
  const [sourceTitles, setSourceTitles] = useState<Record<string, string>>(() => Object.fromEntries((detail?.source_bindings ?? []).map(source => [source.version_id, source.title ?? source.version_id])));
  const [addingSource, setAddingSource] = useState(false);
  const [sourceCandidate, setSourceCandidate] = useState<{ resource: Resource; version: Version }>();
  const [sourceError, setSourceError] = useState<Error>();
  const [attempted, setAttempted] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [advanced, setAdvanced] = useState("");
  const [advancedError, setAdvancedError] = useState<Error>();
  const task = useCapabilitiesTask(deny);
  const errors = definitionErrors(definition);
  const canSave = !detail || (detail.state === "DRAFT" && detail.permissions.can_edit);
  const update = (patch: Partial<CapabilityDefinition>) => setDefinition(previous => ({ ...previous, ...patch }));
  const updateStep = (index: number, patch: Partial<CapabilityStep>) => setDefinition(previous => {
    const old = previous.steps[index].id;
    return { ...previous, steps: previous.steps.map((step, i) => {
      const next = i === index ? { ...step, ...patch } : step;
      return { ...next, depends_on: patch.id !== undefined ? next.depends_on.map(id => id === old ? patch.id! : id) : next.depends_on };
    }) };
  });
  async function save() {
    setAttempted(true); if (errors.length || !canSave || task.busy || conflict) return;
    const body = structuredClone(definition);
    await task.run(async (signal, write) => {
      try {
        if (detail) {
          const current = checkedDetail(await get<CapabilityDetail>(`/capability-versions/${pathId(detail.version_id)}`, signal), app.space.id, { versionId: detail.version_id });
          signal.throwIfAborted();
          if (!current.permissions.can_edit || current.state !== "DRAFT") throw new ApiError(403, "CAPABILITY_READ_ONLY", "当前版本不可编辑。");
          if (current.revision !== detail.revision || current.manifest_sha256 !== detail.manifest_sha256
              || stableJson(current.source_bindings) !== stableJson(detail.source_bindings)) {
            setConflict(true); throw new Error("版本或来源绑定已变化。当前编辑已保留，请关闭并重新读取后核对，不会覆盖新修订。");
          }
        }
        await Promise.all(body.source_version_ids.map(id => readCapabilitySource(id, app.space.id, signal)));
        signal.throwIfAborted();
        const result = checkedDetail(detail ? await write<CapabilityDetail>(`/capabilities/${pathId(detail.resource_id)}`,
          { version_id: detail.version_id, definition: body }, detail.revision, "PUT")
          : await write<CapabilityDetail>("/capabilities", { space_id: app.space.id, definition: body }), app.space.id,
          detail ? { resourceId: detail.resource_id, versionId: detail.version_id } : undefined);
        signal.throwIfAborted();
        if (result.state !== "DRAFT" || !result.definition || stableJson(result.definition) !== stableJson(body))
          throw new Error("保存回执与草稿定义不一致，请重新读取确认结果。");
        saved(result);
      } catch (error) { if (!signal.aborted && error instanceof ApiError && [409, 412].includes(error.status)) setConflict(true); throw error; }
    });
  }
  return <form className="capabilities-editor capabilities-surface" aria-label="能力定义编辑器" onSubmit={event => { event.preventDefault(); void save(); }}>
    <div className="capabilities-row"><h2>{detail ? "编辑能力草稿" : "沉淀新能力"}</h2><span>保存为 DRAFT · 不自动审核发布</span></div>
    <Notice>输入、步骤、工具和核对要求构成可维护的工作流。工具要求仅是声明；未连接 Agent 时不会执行。外部写入和金融动作仅允许人工步骤。</Notice>
    <fieldset disabled={!canSave || task.busy || conflict}>
      <section><h3>基本信息</h3><div className="capabilities-grid-two">
        <Field label="能力名称"><input aria-label="能力名称" maxLength={300} value={definition.name} onChange={event => update({ name: event.target.value })} /></Field>
        <Field label="来源使用范围"><select aria-label="来源使用范围" value={definition.source_scope} onChange={event => update({ source_scope: event.target.value as CapabilityDefinition["source_scope"] })}>
          <option value="reference">资料辅助 · 保留未专业核验提示</option><option value="formal">正式范围 · 仍需服务端准入检查</option></select></Field>
      </div><Field label="交付目标与适用场景"><textarea aria-label="交付目标与适用场景" rows={3} value={definition.description} onChange={event => update({ description: event.target.value })} /></Field>
        <LineList label="何时使用" value={definition.triggers} change={triggers => update({ triggers })} />
        <LineList label="不适用与人工处理边界" value={definition.limitations} change={limitations => update({ limitations })} /></section>
      <FieldDefinitions label="输入要素" fields={definition.inputs} change={inputs => update({ inputs })} />
      <section><div className="capabilities-row"><h3>步骤与依赖</h3><button type="button" onClick={() => {
        let number = definition.steps.length + 1; while (definition.steps.some(step => step.id === `step_${number}`)) number++;
        update({ steps: [...definition.steps, { id: `step_${number}`, title: "新增步骤", kind: "agent", risk: "read_only", instructions: "", depends_on: [], required_tools: [], outputs: [], checks: [] }] });
      }}>添加步骤</button></div><CapabilityFlow steps={definition.steps} />
        {definition.steps.map((step, index) => <section className="capabilities-step-editor" key={index} aria-label={`步骤 ${index + 1}`}>
          <div className="capabilities-grid-two"><Field label="步骤标识"><input aria-label={`步骤 ${index + 1} 标识`} value={step.id} onChange={event => updateStep(index, { id: event.target.value })} /></Field>
            <Field label="步骤标题"><input aria-label={`步骤 ${index + 1} 标题`} value={step.title} onChange={event => updateStep(index, { title: event.target.value })} /></Field>
            <Field label="处理者"><select aria-label={`步骤 ${index + 1} 处理者`} value={step.kind} onChange={event => updateStep(index, { kind: event.target.value as CapabilityStep["kind"] })}>
              <option value="agent">Agent 执行并报告</option><option value="human">人工处理与核对</option></select></Field>
            <Field label="风险与动作"><select aria-label={`步骤 ${index + 1} 风险`} value={step.risk} onChange={event => updateStep(index, { risk: event.target.value as CapabilityStep["risk"] })}>
              {Object.entries(riskLabels).map(([risk, label]) => <option key={risk} value={risk} disabled={step.kind !== "human" && ["external_write", "financial_action"].includes(risk)}>{label}</option>)}</select></Field></div>
          <Field label="任务指导"><textarea aria-label={`步骤 ${index + 1} 指导`} rows={3} value={step.instructions} onChange={event => updateStep(index, { instructions: event.target.value })} /></Field>
          <fieldset className="capabilities-dependencies"><legend>前置步骤</legend>{definition.steps.filter((_, i) => i !== index).map((other, i) => <label className="capabilities-check" key={i}>
            <input type="checkbox" checked={step.depends_on.includes(other.id)} onChange={event => updateStep(index, { depends_on: event.target.checked ? [...step.depends_on, other.id] : step.depends_on.filter(id => id !== other.id) })} />{other.title}（{other.id}）</label>)}
            {step.depends_on.filter(id => !definition.steps.some(other => other.id === id)).map(id => <p key={id} role="alert">缺失依赖：{id} <button type="button" onClick={() => updateStep(index, { depends_on: step.depends_on.filter(value => value !== id) })}>移除此依赖</button></p>)}</fieldset>
          <LineList label={`步骤 ${index + 1} 工具要求`} value={step.required_tools} change={required_tools => updateStep(index, { required_tools })} />
          <FieldDefinitions label={`步骤 ${index + 1} 输出`} fields={step.outputs} change={outputs => updateStep(index, { outputs })} />
          <LineList label={`步骤 ${index + 1} 核对要求`} value={step.checks} change={checks => updateStep(index, { checks })} />
          <button type="button" onClick={() => update({ steps: definition.steps.filter((_, i) => i !== index) })}>删除步骤 {index + 1}</button>
        </section>)}</section>
      <LineList label="最终交付物" value={definition.deliverables} change={deliverables => update({ deliverables })} />
      <section><div className="capabilities-row"><h3>绑定已发布来源</h3><button type="button" onClick={() => setAddingSource(true)}>添加来源版本</button></div>
        <p>可保留历史已发布版本。选择器只核对可读性与批准状态；是否有发布记录、摘要是否一致，由保存时的服务端检查，不因选择而发布。</p>
        {!definition.source_version_ids.length && <p>尚未绑定来源。不能把空来源或资料辅助范围当成专业核验或执行许可。</p>}
        <ul className="capabilities-source-list">{definition.source_version_ids.map(id => <li key={id}><span>{sourceTitles[id] ?? id}</span><code>{id}</code>
          <button type="button" onClick={() => update({ source_version_ids: definition.source_version_ids.filter(value => value !== id) })}>移除来源</button></li>)}</ul>
        {addingSource && <div className="capabilities-inset"><VersionBlockPicker blockRequired={false} onSelect={(resource, version) => { setSourceCandidate(resource && version ? { resource, version } : undefined); setSourceError(undefined); }} />
          <button type="button" disabled={!sourceCandidate} onClick={() => {
            const candidate = sourceCandidate; if (!candidate) return;
            if (candidate.resource.space_id !== app.space.id || candidate.version.resource_id !== candidate.resource.id
                || !["document", "knowledge"].includes(candidate.resource.kind) || candidate.version.state !== "APPROVED"
                || candidate.resource.deleted_at || candidate.resource.suspended) { setSourceError(new Error("请选择当前知识库已发布的确切来源版本。")); return; }
            update({ source_version_ids: [...new Set([...definition.source_version_ids, candidate.version.id])] });
            setSourceTitles(previous => ({ ...previous, [candidate.version.id]: candidate.version.title })); setAddingSource(false); setSourceCandidate(undefined);
          }}>绑定所选来源</button><button type="button" onClick={() => { setAddingSource(false); setSourceCandidate(undefined); }}>取消选择来源</button><ErrorBox error={sourceError} /></div>}
      </section>
      <details className="capabilities-advanced"><summary>高级：完整定义 JSON</summary><p>仅用于完整导入和检查；不会把 JSON 当作可执行代码。</p>
        <textarea aria-label="高级定义 JSON" rows={12} value={advanced} placeholder="粘贴完整 definition" onChange={event => setAdvanced(event.target.value)} />
        <button type="button" onClick={() => setAdvanced(JSON.stringify(definition, null, 2))}>载入当前定义 JSON</button>
        <button type="button" onClick={() => { try { const next = JSON.parse(advanced); const issues = definitionErrors(next); if (issues.length) throw new Error(issues.join(" ")); setDefinition(next); setAdvancedError(undefined); }
          catch (error) { setAdvancedError(error instanceof Error ? error : new Error("JSON 无效。")); } }}>应用 JSON 到编辑器</button><ErrorBox error={advancedError} /></details>
    </fieldset>
    {attempted && errors.length > 0 && <div className="capabilities-errors" role="alert"><strong>仍需补充或修正</strong><ul>{errors.map(error => <li key={error}>{error}</li>)}</ul></div>}
    <ErrorBox error={task.error} />
    <div className="capabilities-row"><button className="primary" type="submit" disabled={!canSave || task.busy || conflict}>{task.busy ? "正在保存…" : detail ? "保存草稿" : "创建能力草稿"}</button>
      <button type="button" disabled={task.busy} onClick={close}>关闭编辑</button></div>
  </form>;
}
