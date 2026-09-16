import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { Field } from "./ui";

export type CapabilityField = { key: string; label: string; type: "string" | "number" | "integer" | "boolean" | "date" | "json"; required: boolean; description: string };
export type CapabilityStep = { id: string; title: string; kind: "agent" | "human"; instructions: string; depends_on: string[];
  required_tools: string[]; outputs: CapabilityField[]; checks: string[]; risk: "read_only" | "draft_write" | "external_write" | "financial_action" };
export type CapabilityDefinition = { schema_version: 1; name: string; description: string; triggers: string[]; limitations: string[];
  inputs: CapabilityField[]; steps: CapabilityStep[]; deliverables: string[]; source_version_ids: string[]; source_scope: "reference" | "formal" };
export type CapabilityBinding = { version_id: string; resource_id?: string; space_id?: string; title?: string; revision?: number; access_epoch?: number; content_sha256?: string; block_ids?: string[] };
export type CapabilityDetail = { resource_id: string; version_id: string; revision: number; version_no: number; state: string; name: string;
  definition: CapabilityDefinition | null; manifest_sha256: string | null; content_sha256: string | null; source_bindings: CapabilityBinding[];
  permissions: { can_edit: boolean; can_trial: boolean; can_run: boolean; can_export: boolean; can_create_revision?: boolean }; notes: string[]; space_id?: string };
export type CapabilitySummary = { resource_id: string; version_id: string; revision?: number; version_no: number; state: string; name: string;
  description?: string; input_count?: number; step_count?: number; source_count?: number; structured?: boolean; space_id?: string };
export type CapabilityRun = { id: string; space_id: string; owner_id: string; version_id: string; capability_name: string; revision: number;
  state: "WAITING_AGENT" | "WAITING_HUMAN" | "COMPLETED" | "BLOCKED" | "FAILED" | "CANCELLED"; mode: "trial" | "guided";
  inputs: Record<string, unknown>; manifest_sha256: string; created_at: string; updated_at: string;
  steps: { id: string; title: string; kind: "agent" | "human"; state: string; outputs: Record<string, unknown>; note: string;
    reported_by?: string | null; reviewed_by?: string | null; report_channel?: string; output_fields?: CapabilityField[] }[]; deliverables: string[]; notes: string[] };
export type CapabilityNext = { run_id: string; revision: number; state: CapabilityRun["state"]; inputs: Record<string, unknown>;
  ready_steps: CapabilityStep[]; waiting_human_steps: CapabilityStep[]; previous_outputs: Record<string, Record<string, unknown>>; notes: string[] };
export const capabilityBoundary = "能力定义和试运行提供任务指导。Agent 使用自身已有工具执行并报告结果；完成状态仅表示定义内步骤及所需人工检查已完成，不认证资金、过账、交易执行或专家业务正确性。";
export const riskLabels = { read_only: "只读核对", draft_write: "形成草稿", external_write: "外部系统写入", financial_action: "资金或交易动作" };
export const runLabels: Record<string, string> = { WAITING_AGENT: "等待 Agent", WAITING_HUMAN: "待人工核对", COMPLETED: "定义流程已完成", BLOCKED: "阻塞", FAILED: "失败", CANCELLED: "已取消", REPORTED: "已收到步骤报告", ACCEPTED: "人工已接受", REJECTED: "人工已退回", PENDING: "等待依赖", READY: "可领取" };
export const fieldTypes = ["string", "number", "integer", "boolean", "date", "json"] as const;
export const fieldLabels = { string: "文字", number: "数值", integer: "整数", boolean: "是／否", date: "日期", json: "结构化 JSON" };
export const identifier = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
export const lines = (text: string) => text.split("\n").map(item => item.trim()).filter(Boolean);
export const stringList = (value: unknown): value is string[] => Array.isArray(value) && value.every(item => typeof item === "string");
export const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
export const revision = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) > 0;
export const validDate = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
export const isAccessError = (error: unknown) => error instanceof ApiError && [401, 403, 404].includes(error.status);
export const pathId = (value: string) => encodeURIComponent(value);
export function stableJson(value: unknown): string {
  const ordered = (item: unknown): unknown => Array.isArray(item) ? item.map(ordered)
    : object(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, ordered(item[key])])) : item;
  return JSON.stringify(ordered(value));
}

export function definitionErrors(value: unknown): string[] {
  if (!object(value) || value.schema_version !== 1 || typeof value.name !== "string" || !value.name.trim()
      || value.name.length > 300 || typeof value.description !== "string" || !value.description.trim()
      || !["triggers", "limitations", "deliverables", "source_version_ids"].every(key => stringList(value[key]) && (value[key] as string[]).every(item => !!item.trim()))
      || !["reference", "formal"].includes(String(value.source_scope)) || !Array.isArray(value.inputs) || !Array.isArray(value.steps))
    return ["能力定义缺少名称、描述、输入或步骤等规范字段。"];
  const errors: string[] = [];
  const extra = (item: Record<string, unknown>, allowed: string[]) => Object.keys(item).some(key => !allowed.includes(key));
  if (extra(value, ["schema_version", "name", "description", "triggers", "limitations", "inputs", "steps", "deliverables", "source_version_ids", "source_scope"]))
    errors.push("能力定义包含不支持的字段；高级 JSON 不能扩展执行权限。");
  if (!(value.triggers as string[]).length || !(value.deliverables as string[]).length) errors.push("请补充使用时机和交付物。");
  const checkFields = (fields: unknown, label: string) => {
    if (!Array.isArray(fields)) { errors.push(`${label}字段无效。`); return; }
    const keys = new Set<string>();
    for (const field of fields) {
      if (!object(field) || extra(field, ["key", "label", "type", "required", "description"]) || typeof field.key !== "string" || !identifier.test(field.key) || typeof field.label !== "string" || !field.label.trim()
          || !fieldTypes.includes(field.type as CapabilityField["type"]) || typeof field.required !== "boolean" || typeof field.description !== "string")
        errors.push(`${label}需填写合法标识、名称、类型和说明。`);
      else if (keys.has(field.key)) errors.push(`${label}标识重复：${field.key}`);
      else keys.add(field.key);
    }
  };
  checkFields(value.inputs, "输入");
  if (!value.steps.length) errors.push("请补充至少一个结构化步骤，不会从普通段落编造执行流程。");
  const ids = new Set<string>();
  let completeShape = true;
  for (const step of value.steps) {
    if (!object(step) || extra(step, ["id", "title", "kind", "instructions", "depends_on", "required_tools", "outputs", "checks", "risk"])
        || typeof step.id !== "string" || !identifier.test(step.id) || typeof step.title !== "string" || !step.title.trim()
        || !["agent", "human"].includes(String(step.kind)) || typeof step.instructions !== "string" || !step.instructions.trim()
        || !stringList(step.depends_on) || !stringList(step.required_tools) || !stringList(step.checks)
        || !Object.keys(riskLabels).includes(String(step.risk))) {
      errors.push("每个步骤需具备合法标识、标题、指导内容、类型、依赖、工具和风险级别。"); completeShape = false; continue;
    }
    if (ids.has(step.id)) errors.push(`步骤标识重复：${step.id}`);
    ids.add(step.id); checkFields(step.outputs, `步骤 ${step.title} 的输出`);
    if (!step.checks.length || step.checks.some(item => !item.trim())) errors.push(`步骤 ${step.title} 需填写至少一项核对要求。`);
    if (new Set(step.required_tools).size !== step.required_tools.length || step.required_tools.some(item => !item.trim())) errors.push(`步骤 ${step.title} 的工具要求不能重复或为空。`);
  }
  if (completeShape) {
    const steps = value.steps as CapabilityStep[];
    for (const step of steps) {
      if (new Set(step.depends_on).size !== step.depends_on.length || step.depends_on.some(id => !ids.has(id) || id === step.id))
        errors.push(`步骤 ${step.title} 的依赖缺失、重复或指向自身。`);
    }
    const remaining = new Map(steps.map(step => [step.id, new Set(step.depends_on)]));
    while (remaining.size) {
      const ready = [...remaining].filter(([, dependencies]) => dependencies.size === 0).map(([id]) => id);
      if (!ready.length) { errors.push("步骤依赖存在循环或不可满足的依赖。"); break; }
      for (const id of ready) { remaining.delete(id); for (const dependencies of remaining.values()) dependencies.delete(id); }
    }
    if (steps.some(step => ["external_write", "financial_action"].includes(step.risk) && step.kind !== "human"))
      errors.push("外部写入或资金动作只允许人工步骤；定义本身不授予外部操作权限。");
  }
  if (new Set(value.source_version_ids as string[]).size !== (value.source_version_ids as string[]).length)
    errors.push("来源版本不可重复。");
  if ((value.source_version_ids as string[]).some(id => !/^[a-f\d]{8}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{12}$/i.test(id)))
    errors.push("来源必须是通过版本选择器取得的确切版本标识。");
  return [...new Set(errors)];
}

export function checkedDetail(value: CapabilityDetail, spaceId: string, expected?: { resourceId?: string; versionId?: string }) {
  if (!value || !value.resource_id || !value.version_id || !revision(value.revision) || !revision(value.version_no)
      || typeof value.name !== "string" || !stringList(value.notes) || !Array.isArray(value.source_bindings)
      || (value.space_id !== undefined && value.space_id !== spaceId)
      || (expected?.resourceId && value.resource_id !== expected.resourceId) || (expected?.versionId && value.version_id !== expected.versionId)
      || !value.permissions || ["can_edit", "can_trial", "can_run", "can_export"].some(key => typeof value.permissions[key as keyof typeof value.permissions] !== "boolean"))
    throw new Error("能力响应或权限与当前选择不一致，请重新读取。");
  if (value.definition !== null && definitionErrors(value.definition).length) throw new Error("能力定义结构未通过检查，请通过原版本入口核对。");
  if (value.source_bindings.some(source => !source || typeof source.version_id !== "string" || !source.version_id)
      || (value.definition !== null && (typeof value.manifest_sha256 !== "string" || !value.manifest_sha256 || typeof value.content_sha256 !== "string" || !value.content_sha256)))
    throw new Error("能力摘要或来源绑定不完整。");
  return value;
}

export function checkedRun(value: CapabilityRun, spaceId: string, userId: string, id?: string) {
  if (!value || !value.id || (id && value.id !== id) || value.space_id !== spaceId || value.owner_id !== userId
      || !value.version_id || !revision(value.revision) || !["WAITING_AGENT", "WAITING_HUMAN", "COMPLETED", "BLOCKED", "FAILED", "CANCELLED"].includes(value.state)
      || !["trial", "guided"].includes(value.mode) || typeof value.manifest_sha256 !== "string" || !object(value.inputs)
      || !Array.isArray(value.steps) || !stringList(value.notes) || !stringList(value.deliverables))
    throw new Error("运行响应与当前用户、知识库或任务不一致，请重新读取。");
  if (value.steps.some(step => !step || typeof step.id !== "string" || typeof step.title !== "string" || !["agent", "human"].includes(step.kind)
      || typeof step.state !== "string" || !object(step.outputs) || typeof step.note !== "string") || new Set(value.steps.map(step => step.id)).size !== value.steps.length)
    throw new Error("运行步骤回执结构不完整。");
  return value;
}

export function useCapabilitiesTask(deny: () => void, failed?: (error: unknown) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error>();
  const controller = useRef<AbortController | null>(null);
  const operation = useRef<{ signature: string; key: string } | undefined>(undefined);
  useEffect(() => () => controller.current?.abort(), []);
  async function run(action: (signal: AbortSignal, write: <T>(path: string, body: unknown, version?: number, method?: "POST" | "PUT") => Promise<T>) => Promise<void>) {
    if (controller.current) return;
    const current = new AbortController(); controller.current = current; setBusy(true); setError(undefined);
    const write = async <T,>(path: string, body: unknown, version?: number, method: "POST" | "PUT" = "POST") => {
      current.signal.throwIfAborted();
      const signature = JSON.stringify([path, body, version, method]);
      if (operation.current?.signature !== signature) operation.current = { signature, key: crypto.randomUUID() };
      try {
        const result = await api<T>(path, { method, body, revision: version, key: operation.current.key, signal: current.signal });
        operation.current = undefined; return result;
      } catch (failure) {
        if (failure instanceof ApiError && failure.status >= 400 && failure.status < 500) operation.current = undefined;
        throw failure;
      }
    };
    try { await action(current.signal, write); }
    catch (failure) {
      if (!current.signal.aborted) {
        failed?.(failure);
        if (isAccessError(failure)) deny();
        else setError(failure instanceof Error ? failure : new Error("操作未完成，请重新核对。"));
      }
    } finally {
      if (!current.signal.aborted) setBusy(false);
      if (controller.current === current) controller.current = null;
      current.abort();
    }
  }
  return { busy, error, run, clearError: () => setError(undefined), cancel: () => { controller.current?.abort(); controller.current = null; setBusy(false); } };
}

export function parseCapabilityValues(fields: CapabilityField[], draft: Record<string, string>) {
  const values: [string, unknown][] = [], errors: string[] = [];
  for (const field of fields) {
    const raw = draft[field.key] ?? "";
    if (!raw.trim()) { if (field.required) errors.push(`请填写${field.label}。`); continue; }
    let value: unknown = raw;
    if (field.type === "boolean") { if (!["true", "false"].includes(raw)) errors.push(`${field.label}请选择是或否。`); else value = raw === "true"; }
    else if (["number", "integer"].includes(field.type)) {
      value = Number(raw);
      if (!Number.isFinite(value) || (field.type === "integer" && !Number.isSafeInteger(value))) errors.push(`${field.label}不是有效${field.type === "integer" ? "整数" : "数值"}。`);
    } else if (field.type === "date" && !validDate(raw)) errors.push(`${field.label}不是有效日期。`);
    else if (field.type === "json") { try { value = JSON.parse(raw); } catch { errors.push(`${field.label}不是有效 JSON。`); } }
    values.push([field.key, value]);
  }
  return { values: Object.fromEntries(values), errors };
}

export function CapabilityValueFields({ fields, values, change }: { fields: CapabilityField[]; values: Record<string, string>; change: (key: string, value: string) => void }) {
  return <div className="capabilities-inputs">{fields.map(field => <Field key={field.key} label={field.label + (field.required ? "（必填）" : "（可选）")} hint={field.description}>
    {field.type === "boolean" ? <select aria-label={field.label} value={values[field.key] ?? ""} onChange={event => change(field.key, event.target.value)}>
      <option value="">请选择</option><option value="true">是</option><option value="false">否</option></select>
      : field.type === "json" || field.type === "string" ? <textarea aria-label={field.label} rows={field.type === "json" ? 4 : 2}
        value={values[field.key] ?? ""} onChange={event => change(field.key, event.target.value)} />
        : <input aria-label={field.label} type={field.type === "date" ? "date" : "number"} step={field.type === "integer" ? "1" : "any"}
          value={values[field.key] ?? ""} onChange={event => change(field.key, event.target.value)} />}
  </Field>)}</div>;
}

export function CapabilityFlow({ steps }: { steps: CapabilityStep[] }) {
  return <ol className="capabilities-flow" aria-label="工作流步骤与依赖">{steps.map(step => <li key={step.id}>
    <div><span>{step.kind === "human" ? "人工检查" : "Agent 步骤"}</span><strong>{step.title}</strong></div>
    <p>{step.depends_on.length ? "依赖：" + step.depends_on.map(id => steps.find(item => item.id === id)?.title ?? id).join("、") : "起始步骤"}</p>
    <small>{riskLabels[step.risk] ?? "风险待补充"}</small>
  </li>)}</ol>;
}

export const capabilityStarter = (): CapabilityDefinition => ({ schema_version: 1, name: "通用工作底稿", description: "依据已绑定来源整理工作底稿，交由人工核对。",
  triggers: ["需要形成有来源依据、可核对的工作底稿"], limitations: ["不提供资金、交易或过账工具；外部操作需另行授权"],
  inputs: [{ key: "task_goal", label: "本次工作目标", type: "string", required: true, description: "说明需要核对的问题与交付范围" }],
  steps: [{ id: "prepare", title: "整理输入与依据", kind: "agent", instructions: "核对用户输入及已绑定来源，记录适用条件、来源定位和不确定事项。",
    depends_on: [], required_tools: ["read_bound_sources"], outputs: [{ key: "workpaper", label: "工作底稿", type: "string", required: true, description: "底稿正文或可核对的产物位置及来源定位" }],
    checks: ["来源版本与本次任务匹配", "明确区分事实、推断与待核事项"], risk: "read_only" },
  { id: "review", title: "人工核对交付物", kind: "human", instructions: "查看真实交付物与来源，记录接受或退回理由。", depends_on: ["prepare"], required_tools: [], outputs: [], checks: ["交付物符合任务目标"], risk: "read_only" }],
  deliverables: ["可核对的工作底稿及人工检查记录"], source_version_ids: [], source_scope: "reference" });

export function saveCapabilityFile(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = filename.replace(/[\\/:*?"<>|\x00-\x1f]/g, "_");
  anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 0);
}
