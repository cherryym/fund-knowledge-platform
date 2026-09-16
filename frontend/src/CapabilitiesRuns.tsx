import { useState } from "react";
import { ApiError, get, query } from "./api";
import { Badge, ErrorBox, Field, Loading, Notice, readable, useApp, useLoad } from "./ui";
import { CapabilityFlow, CapabilityValueFields, capabilityBoundary, checkedDetail, checkedRun, isAccessError, object, parseCapabilityValues,
  pathId, revision, runLabels, stableJson, stringList, useCapabilitiesTask, type CapabilityBinding, type CapabilityDetail, type CapabilityNext, type CapabilityRun, type CapabilityStep } from "./CapabilitiesShared";

export function CapabilityTrialForm({ detail, deny, started, close }: { detail: CapabilityDetail; deny: () => void; started: (run: CapabilityRun) => void; close: () => void }) {
  const app = useApp();
  const [mode, setMode] = useState<"trial" | "guided">(detail.permissions.can_trial ? "trial" : "guided");
  const [values, setValues] = useState<Record<string, string>>({});
  const [label, setLabel] = useState("");
  const [attempted, setAttempted] = useState(false);
  const [stale, setStale] = useState(false);
  const task = useCapabilitiesTask(deny);
  const parsed = parseCapabilityValues(detail.definition?.inputs ?? [], values);
  const inputErrors = [...parsed.errors, ...([...label.trim()].length > 200 ? ["Agent 标记最多 200 个字符。"] : [])];
  const allowed = mode === "trial" ? detail.permissions.can_trial : detail.permissions.can_run;
  async function submit() {
    setAttempted(true); if (!allowed || !detail.definition || inputErrors.length || task.busy || stale) return;
    const inputs = parsed.values;
    await task.run(async (signal, write) => {
      const current = checkedDetail(await get<CapabilityDetail>(`/capability-versions/${pathId(detail.version_id)}`, signal), app.space.id, { resourceId: detail.resource_id, versionId: detail.version_id });
      signal.throwIfAborted();
      if (!(mode === "trial" ? current.permissions.can_trial : current.permissions.can_run)) throw new ApiError(403, "CAPABILITY_RUN_FORBIDDEN", "当前版本无运行权限。");
      if (current.revision !== detail.revision || current.manifest_sha256 !== detail.manifest_sha256) { setStale(true); throw new Error("能力版本已变化，请重新读取后填写本次输入。"); }
      const result = checkedRun(await write<CapabilityRun>("/capability-runs", { version_id: current.version_id, inputs, mode,
        ...(label.trim() ? { agent_label: label.trim() } : {}) }), app.space.id, app.me.id);
      signal.throwIfAborted();
      if (result.version_id !== current.version_id || result.manifest_sha256 !== current.manifest_sha256 || result.mode !== mode
          || stableJson(result.inputs) !== stableJson(inputs) || !["WAITING_AGENT", "WAITING_HUMAN"].includes(result.state))
        throw new Error("运行回执与所选能力版本不一致。");
      started(result);
    });
  }
  return <form className="capabilities-inset" aria-label="能力试运行输入" onSubmit={event => { event.preventDefault(); void submit(); }}>
    <h3>准备试运行输入</h3><p>创建运行只生成指导记录，随后由 Agent 使用自身工具完成步骤并报告；本页不会自动调用模型或业务系统。</p>
    <fieldset disabled={task.busy || stale}>
      <Field label="运行方式"><select aria-label="运行方式" value={mode} onChange={event => setMode(event.target.value as typeof mode)}>
        <option value="trial" disabled={!detail.permissions.can_trial}>试运行</option><option value="guided" disabled={!detail.permissions.can_run}>已发布能力指导运行</option></select></Field>
      <CapabilityValueFields fields={detail.definition?.inputs ?? []} values={values} change={(key, value) => setValues(previous => ({ ...previous, [key]: value }))} />
      <Field label="Agent 标记（可选）" hint="最多 200 个字符，不代表 Agent 已连接"><input aria-label="Agent 标记" value={label} onChange={event => setLabel(event.target.value)} /></Field>
    </fieldset><ErrorBox error={task.error} />
    {attempted && inputErrors.length > 0 && <ul role="alert">{inputErrors.map(error => <li key={error}>{error}</li>)}</ul>}
    <div className="capabilities-row"><button className="primary" type="submit" disabled={!allowed || task.busy || stale}>创建本次运行</button>
      <button type="button" disabled={task.busy} onClick={close}>取消试运行输入</button></div>
  </form>;
}

export function CapabilitiesRuns({ initialId, deny }: { initialId?: string; deny: () => void }) {
  const app = useApp();
  const [selected, setSelected] = useState(initialId);
  const loaded = useLoad(async signal => {
    try {
      type Summary = Pick<CapabilityRun, "id" | "space_id" | "version_id" | "capability_name" | "revision" | "state" | "mode" | "created_at" | "updated_at">;
      const value = await get<{ items: Summary[] }>(`/capability-runs?${query({ space_id: app.space.id })}`, signal);
      if (!value || !Array.isArray(value.items)) throw new Error("运行列表不完整。");
      if (value.items.some(item => !item || !item.id || item.space_id !== app.space.id || !item.version_id || !revision(item.revision)
          || typeof item.capability_name !== "string" || !["WAITING_AGENT", "WAITING_HUMAN", "COMPLETED", "BLOCKED", "FAILED", "CANCELLED"].includes(item.state)
          || !["trial", "guided"].includes(item.mode)) || new Set(value.items.map(item => item.id)).size !== value.items.length)
        throw new Error("运行列表与当前知识库不一致。");
      return value;
    } catch (error) { if (!signal.aborted && isAccessError(error)) deny(); throw error; }
  }, [app.space.id, app.me.id]);
  return <>
    <div className="capabilities-row"><h2>我的运行记录</h2><button onClick={() => { setSelected(undefined); loaded.reload(); }}>刷新运行列表</button></div>
    <Notice>{capabilityBoundary}</Notice><ErrorBox error={loaded.error} retry={loaded.reload} />{loaded.loading && <Loading />}
    <div className="capabilities-run-layout"><aside className="capabilities-run-list" aria-label="个人运行列表">
      {loaded.data?.items.map(run => <button className={selected === run.id ? "selected" : ""} key={run.id} onClick={() => setSelected(run.id)}>
        <strong>{run.capability_name}</strong><span>{runLabels[run.state]} · {run.mode === "trial" ? "试运行" : "指导运行"}</span><small>{run.created_at}</small></button>)}
      {!loaded.loading && loaded.data && !loaded.data.items.length && <p>暂无个人运行记录。</p>}
    </aside>{selected ? <CapabilityRunDetail key={selected} id={selected} deny={deny} changed={loaded.reload} />
      : <div className="capabilities-empty">选择一条运行，查看下一步、步骤报告或人工核对要求。</div>}</div>
  </>;
}

type NextProjection = CapabilityNext;
function stepIds(values: NextProjection["ready_steps"], steps: CapabilityStep[], kind: "agent" | "human") {
  if (!Array.isArray(values)) throw new Error("下一步响应缺少步骤列表。");
  const ids = values.map(value => value?.id);
  if (new Set(ids).size !== ids.length || values.some(value => !value || typeof value.id !== "string"
      || !steps.some(step => step.id === value.id && step.kind === kind && stableJson(step) === stableJson(value))))
    throw new Error("下一步与本次冻结能力的步骤类型不一致。");
  return ids;
}
type RunSources = { run_id: string; scope: string; source_bindings: CapabilityBinding[];
  records: { resource_id: string; version_id: string; block_id: string; ordinal: number; title: string; text: string; locator: unknown; content_sha256: string; legal_status: string }[]; notes: string[] };
const previousOutputs = (run: CapabilityRun) => Object.fromEntries(run.steps.filter(step => ["REPORTED", "ACCEPTED"].includes(step.state)).map(step => [step.id, step.outputs]));
const displayValue = (value: unknown) => typeof value === "string" ? value : JSON.stringify(value, null, 2);

function CapabilityRunDetail({ id, deny, changed }: { id: string; deny: () => void; changed: () => void }) {
  const app = useApp();
  const [epoch, setEpoch] = useState(0);
  const [next, setNext] = useState<{ value: NextProjection; ready: string[]; human: string[] }>();
  const [selected, setSelected] = useState("");
  const [outputs, setOutputs] = useState<Record<string, string>>({});
  const [status, setStatus] = useState<"reported" | "blocked" | "failed">("reported");
  const [note, setNote] = useState("");
  const [decision, setDecision] = useState<"accept" | "reject">("accept");
  const [reviewed, setReviewed] = useState(false);
  const [canceling, setCanceling] = useState(false);
  const [cancelReason, setCancelReason] = useState("");
  const [sources, setSources] = useState<RunSources>();
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [conflict, setConflict] = useState(false);
  const task = useCapabilitiesTask(deny, error => {
    setSources(undefined);
    if (error instanceof ApiError && [409, 412].includes(error.status)) {
      setConflict(true); setReviewed(false);
      if (/SOURCE|DEFINITION/.test(error.code)) setNext(undefined);
    }
  });
  const loaded = useLoad(async signal => {
    try {
      const run = checkedRun(await get<CapabilityRun>(`/capability-runs/${pathId(id)}`, signal), app.space.id, app.me.id, id);
      signal.throwIfAborted();
      const detail = checkedDetail(await get<CapabilityDetail>(`/capability-versions/${pathId(run.version_id)}`, signal), app.space.id, { versionId: run.version_id });
      if (!detail.definition || detail.manifest_sha256 !== run.manifest_sha256) throw new Error("运行定义或来源绑定已变化，不能沿用旧步骤继续执行。");
      if (run.steps.length !== detail.definition.steps.length || run.steps.some(row => {
        const step = detail.definition!.steps.find(step => step.id === row.id);
        return !step || step.kind !== row.kind || (row.output_fields !== undefined && stableJson(row.output_fields) !== stableJson(step.outputs));
      })) throw new Error("运行步骤或 output_fields 与冻结能力定义不一致，不能把字段要求当作实际产物。");
      return { run, detail, epoch };
    } catch (error) { if (!signal.aborted && isAccessError(error)) deny(); throw error; }
  }, [id, epoch]);
  const data = !loaded.loading && !loaded.error && loaded.data?.epoch === epoch ? loaded.data : undefined;
  const run = data?.run, definition = data?.detail.definition;
  const active = !!run && !["COMPLETED", "FAILED", "CANCELLED"].includes(run.state);
  const step = definition?.steps.find(item => item.id === selected);
  const refresh = () => {
    task.cancel(); task.clearError(); setNext(undefined); setSelected(""); setOutputs({}); setReviewed(false); setSources(undefined);
    setCanceling(false); setConflict(false); setFormErrors([]); setEpoch(value => value + 1);
  };
  async function readNext() {
    if (!run || !definition || !active || task.busy) return;
    setNext(undefined); setSelected(""); setReviewed(false); setOutputs({}); setFormErrors([]); setStatus("reported"); setDecision("accept");
    await task.run(async signal => {
      const value = await get<NextProjection>(`/capability-runs/${pathId(id)}/next`, signal); signal.throwIfAborted();
      if (!value || value.run_id !== run.id || value.revision !== run.revision || value.state !== run.state || !stringList(value.notes)
          || !object(value.inputs) || !object(value.previous_outputs) || stableJson(value.inputs) !== stableJson(run.inputs)
          || stableJson(value.previous_outputs) !== stableJson(previousOutputs(run))) {
        setConflict(true); throw new Error("运行状态已变化，请刷新运行后再读取下一步。");
      }
      const ready = stepIds(value.ready_steps, definition.steps, "agent"), human = stepIds(value.waiting_human_steps, definition.steps, "human");
      setNext({ value, ready, human }); setSelected(ready[0] ?? human[0] ?? ""); setNote("");
    });
  }
  async function mutate(kind: "report" | "review" | "cancel") {
    if (!run || task.busy || conflict || !active) return;
    if (kind !== "cancel" && (!next || !step)) return;
    const requireOutputs = step?.kind === "human" ? decision === "accept" : status === "reported";
    const parsed = requireOutputs ? parseCapabilityValues(step?.outputs ?? [], outputs) : { values: {}, errors: [] };
    const errors = kind === "report" ? [...parsed.errors, ...(status !== "reported" && !note.trim() ? ["请填写阻塞或失败原因。"] : [])]
      : kind === "review" ? [...(decision === "accept" ? parsed.errors : []), ...(!reviewed ? ["请明确核对实际交付物。"] : []), ...(!note.trim() ? ["请填写人工核对理由。"] : [])]
      : !cancelReason.trim() ? ["请填写取消原因。"] : [];
    setFormErrors(errors); if (errors.length) return;
    if (kind === "report" && (step?.kind !== "agent" || !next?.ready.includes(step.id))) return;
    if (kind === "review" && (step?.kind !== "human" || !next?.human.includes(step.id))) return;
    await task.run(async (signal, write) => {
      try {
        const current = checkedRun(await get<CapabilityRun>(`/capability-runs/${pathId(id)}`, signal), app.space.id, app.me.id, id);
        signal.throwIfAborted();
        if (current.revision !== run.revision || current.state !== run.state || current.manifest_sha256 !== run.manifest_sha256) { setConflict(true); throw new Error("运行修订已变化，请刷新后重新核对。"); }
        const path = kind === "report" ? `/capability-runs/${pathId(id)}/steps/${pathId(step!.id)}` : `/capability-runs/${pathId(id)}/${kind}`;
        const body = kind === "report" ? { status, outputs: parsed.values, note: note.trim() }
          : kind === "review" ? { step_id: step!.id, decision, note: note.trim(), outputs: decision === "accept" ? parsed.values : {} } : { reason: cancelReason.trim() };
        const result = checkedRun(await write<CapabilityRun>(path, body, run.revision), app.space.id, app.me.id, id);
        signal.throwIfAborted();
        if (result.revision <= run.revision || result.manifest_sha256 !== run.manifest_sha256) throw new Error("运行更新回执未确认，请重新读取。");
        refresh(); changed();
      } catch (error) { if (!signal.aborted && error instanceof ApiError && [409, 412].includes(error.status)) { setConflict(true); setReviewed(false); } throw error; }
    });
  }
  return <section className="capabilities-surface" aria-label="能力运行详情">
    <div className="capabilities-row"><h2>{run?.capability_name ?? "运行详情"}</h2><button onClick={refresh}>刷新当前运行</button></div>
    <ErrorBox error={loaded.error} retry={refresh} /><ErrorBox error={task.error} />{loaded.loading && <Loading />}
    {run && definition && <>
      <div className="capabilities-row"><span className="capabilities-status">{runLabels[run.state]}</span><span>{run.mode === "trial" ? "试运行" : "指导运行"} · 修订 {run.revision}</span></div>
      <p>运行：<code>{run.id}</code> · 冻结版本：<code>{run.version_id}</code></p><CapabilityFlow steps={definition.steps} />
      <section aria-label="本次运行输入"><h3>本次运行输入</h3><dl>{definition.inputs.map(field => <div key={field.key}><dt>{field.label}</dt>
        <dd className="capabilities-prose">{Object.hasOwn(run.inputs, field.key) ? displayValue(run.inputs[field.key]) : "未提供"}</dd></div>)}</dl>
        {!definition.inputs.length && <p>本能力未声明输入字段。</p>}</section>
      <div className="capabilities-row"><button disabled={!active || task.busy || conflict} onClick={() => void readNext()}>读取下一步</button>
        <button disabled={task.busy} onClick={() => { setSources(undefined); void task.run(async signal => {
          const value = await get<RunSources>(`/capability-runs/${pathId(id)}/sources`, signal); signal.throwIfAborted();
          if (!value || value.run_id !== run.id || value.scope !== definition.source_scope || !stringList(value.notes) || !Array.isArray(value.records)
              || !Array.isArray(value.source_bindings) || stableJson(value.source_bindings) !== stableJson(data!.detail.source_bindings)
              || definition.source_version_ids.some(id => !value.records.some(record => record.version_id === id))
              || new Set(value.records.map(record => record.version_id + ":" + record.block_id)).size !== value.records.length
              || value.records.some(record => {
                const binding = value.source_bindings.find(binding => binding.version_id === record?.version_id);
                return !record || !binding || !definition.source_version_ids.includes(record.version_id) || typeof record.block_id !== "string"
                  || (binding.resource_id !== undefined && binding.resource_id !== record.resource_id)
                  || (binding.block_ids !== undefined && !binding.block_ids.includes(record.block_id))
                  || typeof record.title !== "string" || typeof record.text !== "string" || !Number.isSafeInteger(record.ordinal)
                  || typeof record.content_sha256 !== "string" || !record.content_sha256 || typeof record.legal_status !== "string";
              })) throw new Error("来源响应与本次绑定版本不一致。");
          setSources(value);
        }); }}>读取绑定来源</button>
        <button disabled={!active || task.busy || conflict} onClick={() => setCanceling(true)}>取消运行</button></div>
      {conflict && <p role="alert">请刷新当前运行再决定下一步。不会自动重放报告或人工核对。</p>}
      <section><h3>已记录步骤</h3><ol className="capabilities-step-records">{run.steps.map(item => <li key={item.id}>
        <strong>{item.title}</strong><span>{item.kind === "human" ? "人工步骤" : "Agent 步骤"} · {runLabels[item.state] ?? item.state}</span>
        {item.note && <p className="capabilities-prose">{item.note}</p>}
        {item.reported_by && <small>报告者：{item.reported_by}{item.report_channel === "web_user" ? "（网页手动回传）" : item.report_channel === "agent_token" ? "（Agent 接入回传）" : ""}。收到报告不证明外部动作已执行。</small>}
        {item.reviewed_by && <small>人工核对者：{item.reviewed_by}</small>}
        {object(item.outputs) && Object.keys(item.outputs).length > 0 && <dl>{Object.entries(item.outputs).map(([key, value]) => <div key={key}><dt>{definition.steps.find(step => step.id === item.id)?.outputs.find(field => field.key === key)?.label ?? key}</dt>
          <dd className="capabilities-prose">{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</dd></div>)}</dl>}
      </li>)}</ol></section>
      {next && <section className="capabilities-inset"><h3>当前可处理步骤</h3>{next.value.notes.map((value, i) => <p key={i}>{value}</p>)}
        {Object.keys(next.value.previous_outputs).length > 0 && <section aria-label="前序步骤产物"><h4>前序步骤已回传的产物</h4>
          {Object.entries(next.value.previous_outputs).map(([id, values]) => <div key={id}><strong>{definition.steps.find(step => step.id === id)?.title}</strong>
            <dl>{Object.entries(values).map(([key, value]) => <div key={key}><dt>{definition.steps.find(step => step.id === id)?.outputs.find(field => field.key === key)?.label ?? key}</dt>
              <dd className="capabilities-prose">{displayValue(value)}</dd></div>)}</dl></div>)}
          <p className="capabilities-muted">这是已登记的报告内容，仍需核对真实产物和来源。</p></section>}
        <Field label="选择当前步骤"><select aria-label="选择当前步骤" value={selected} disabled={task.busy || conflict} onChange={event => {
          setSelected(event.target.value); setOutputs({}); setNote(""); setReviewed(false); setFormErrors([]); setStatus("reported"); setDecision("accept");
        }}>{[...next.ready, ...next.human].map(id => <option key={id} value={id}>{definition.steps.find(step => step.id === id)?.title}</option>)}</select></Field>
        {!selected && <p>暂时没有满足依赖的可处理步骤，请核对阻塞原因。</p>}
        {step && <form aria-label={step.kind === "human" ? "人工核对步骤" : "步骤报告表单"} onSubmit={event => { event.preventDefault(); void mutate(step.kind === "human" ? "review" : "report"); }}>
          <p className="capabilities-prose">{step.instructions}</p><p>需要的 Agent 工具：{step.required_tools.join("、") || "未声明"}</p>
          <ul>{step.checks.map((value, i) => <li key={i}>{value}</li>)}</ul>
          <fieldset disabled={task.busy || conflict}>
            {step.kind === "agent" ? <><Notice>这是手动回传步骤报告。请填写实际产物，不会触发 Agent 工具或把文字回传标记为已过账。</Notice>
              <Field label="报告状态"><select aria-label="报告状态" value={status} onChange={event => setStatus(event.target.value as typeof status)}>
                <option value="reported">已报告产物</option><option value="blocked">遇到阻塞</option><option value="failed">步骤失败</option></select></Field>
              {status === "reported" ? <CapabilityValueFields fields={step.outputs} values={outputs} change={(key, value) => setOutputs(previous => ({ ...previous, [key]: value }))} />
                : <p>本次只回传阻塞或失败原因；未完成的输出不作为成功产物提交。切回“已报告产物”可继续编辑。</p>}</>
              : <><Field label="人工核对决定"><select aria-label="人工核对决定" value={decision} onChange={event => { setDecision(event.target.value as typeof decision); setReviewed(false); }}>
                <option value="accept">接受交付物</option><option value="reject">退回处理</option></select></Field>
                <CapabilityValueFields fields={step.outputs} values={outputs} change={(key, value) => { setOutputs(previous => ({ ...previous, [key]: value })); setReviewed(false); }} /></>}
            <Field label={step.kind === "human" ? "人工核对理由" : status === "reported" ? "步骤报告说明（可选）" : "阻塞或失败原因（必填）"}><textarea aria-label="步骤说明" rows={3} value={note} onChange={event => { setNote(event.target.value); setReviewed(false); }} /></Field>
            {step.kind === "human" && <label className="capabilities-check"><input type="checkbox" aria-label="已核对实际交付物" checked={reviewed} onChange={event => setReviewed(event.target.checked)} />
              我已核对实际交付物及上述要求；此次记录不授权平台执行资金、过账或交易。</label>}
          </fieldset><button className="primary" type="submit" disabled={task.busy || conflict || (step.kind === "human" && !reviewed)}>{step.kind === "human" ? "提交人工核对" : "回传步骤报告"}</button>
        </form>}
      </section>}
      {canceling && <form className="capabilities-inset" aria-label="取消能力运行" onSubmit={event => { event.preventDefault(); void mutate("cancel"); }}>
        <p>取消平台中的指导记录不会撤销外部系统已经发生的动作。</p><Field label="取消运行原因"><textarea aria-label="取消运行原因" value={cancelReason} disabled={task.busy} onChange={event => setCancelReason(event.target.value)} /></Field>
        <button type="submit" disabled={!cancelReason.trim() || task.busy || conflict}>确认取消运行</button><button type="button" disabled={task.busy} onClick={() => setCanceling(false)}>保留运行</button></form>}
      {formErrors.length > 0 && <ul role="alert">{formErrors.map(error => <li key={error}>{error}</li>)}</ul>}
      {sources && <section className="capabilities-inset" aria-label="运行绑定来源"><h3>当前授权的来源原文</h3>
        {sources.notes.map((value, i) => <p key={i}>{value}</p>)}
        {sources.records.map((record, i) => <article className="capabilities-source-record" key={record.version_id + ":" + record.block_id + ":" + i}>
          <h4>{record.title}</h4><Badge value={record.legal_status} /><p className="capabilities-prose">{record.text}</p><small>{object(record.locator) ? readable(record.locator.label ?? record.locator.source_page ?? record.locator) : readable(record.locator)}</small>
          <details><summary>确切来源定位</summary><code>{record.version_id} · {record.block_id} · 顺序 {record.ordinal}</code>
            <p>原文块摘要（非整份版本摘要）：<code>{record.content_sha256}</code></p></details>
        </article>)}
      </section>}
      <section><h3>定义要求的交付物</h3><ul>{run.deliverables.map((value, i) => <li key={i}>{value}</li>)}</ul></section>
      {run.notes.map((value, i) => <p className="capabilities-muted" key={i}>{value}</p>)}
    </>}
  </section>;
}
