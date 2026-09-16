export type ModelProviderKind = "direct" | "gateway" | "local" | "custom" | "subscription";
export type ModelProtocol =
  "openai" | "responses" | "anthropic" | "gemini" | "ollama" | "codex_app_server";
export type CredentialMode = "encrypted" | "env" | "none" | "chatgpt_oauth";
export type BridgeCapabilities = { auth: boolean; model_list: boolean; inference: boolean;
  auth_blocked_reason: string | null; inference_blocked_reason: string | null;
  browser_callback_reachable: boolean; live_verified: boolean };
export type ModelOAuthState = { connection_id: string; owner_user_id: string; connection_revision: number;
  oauth_revision: number; auth_epoch: number; state: string; attempt_id: string | null;
  active_job_id: string | null; account: {type: "chatgpt"; plan_type: string | null} | null;
  checked_at: string | null; last_error_code: string | null; capabilities: BridgeCapabilities };
export type ModelOAuthChallenge = { attempt_id: string; flow: "device_code" | "browser";
  verification_url: string | null; authorization_url: string | null; user_code: string | null;
  local_expires_at: string };
export type ModelSelection = { connection_id: string; model_id: string };
export type ProviderModel = {
  id: string;
  name: string;
  brand: string;
  flagship?: boolean;
  source_url?: string;
};
export type ModelProvider = {
  id: string;
  name: string;
  kind: ModelProviderKind;
  protocol: ModelProtocol;
  base_url: string;
  icon: string;
  models: ProviderModel[];
  verified_at?: string | null;
  source_url?: string;
};
export type ModelProviderCatalog = {
  items: ModelProvider[];
  live_verified: boolean;
};
export type ConnectionModel = ProviderModel & {
  context_length?: number | null;
  source: "preset" | "synced" | "custom";
};
export type ModelConnection = {
  id: string;
  owner_user_id: string;
  capabilities?: BridgeCapabilities;
  oauth_state?: ModelOAuthState;
  space_id: string;
  name: string;
  provider_id: string;
  protocol: ModelProtocol;
  base_url: string;
  enabled: boolean;
  credential_mode: CredentialMode;
  api_key_env?: string | null;
  credential_present: boolean;
  allow_document_transfer: boolean;
  revision: number;
  status: string;
  last_error_code?: string | null;
  last_synced_at?: string | null;
  models: ConnectionModel[];
};
export type ModelOption = {
  connection_id: string;
  connection_name: string;
  provider_id: string;
  kind: ModelProviderKind;
  protocol: ModelProtocol;
  model_id: string;
  model_name: string;
  brand: string;
  flagship?: boolean;
  configured: boolean;
  allow_document_transfer: boolean;
  owner_user_id: string;
  connection_revision: number;
  selectable?: boolean;
  blocked_reason?: string | null;
};
export type ModelOptions = {
  items: ModelOption[];
  default: ModelSelection | null;
};
export type ConnectionDraft = {
  name: string;
  provider_id: string;
  base_url: string;
  protocol: ModelProtocol;
  enabled: boolean;
  credential_mode: CredentialMode;
  api_key_env: string;
  allow_document_transfer: boolean;
  custom_models: { id: string; name: string; brand: string }[];
};
export type ConnectionWrite = Omit<ConnectionDraft, "api_key_env"> & {
  space_id?: string;
  api_key?: string;
  api_key_env?: string;
};
export const sourceKindLabels: Record<ModelProviderKind, string> = {
  subscription: "ChatGPT / Codex 订阅",
  direct: "厂商 API",
  gateway: "中转网关",
  local: "本地服务",
  custom: "自定义服务",
};
export const protocolLabels: Record<ModelProtocol, string> = {
  codex_app_server: "官方 Codex app-server",
  openai: "OpenAI Chat Completions 兼容",
  responses: "OpenAI Responses 原生",
  anthropic: "Anthropic Messages 原生",
  gemini: "Gemini 原生",
  ollama: "Ollama 原生",
};
export const modelSourceLabels: Record<ConnectionModel["source"], string> = {
  preset: "目录预置",
  synced: "账号已同步",
  custom: "手工登记",
};
export const modelBrands: Record<string, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  claude: "Claude",
  google: "Google",
  gemini: "Gemini",
  deepseek: "DeepSeek",
  qwen: "通义千问",
  baai: "智源研究院 BAAI",
  moonshot: "Moonshot",
  kimi: "Kimi",
  zhipu: "智谱",
  glm: "GLM",
  doubao: "豆包",
  minimax: "MiniMax",
  mistral: "Mistral",
  xai: "xAI",
  grok: "Grok",
  openrouter: "OpenRouter",
  siliconflow: "硅基流动",
  ollama: "Ollama",
  lmstudio: "LM Studio",
};
const brandAliases: Record<string, string> = {
  "chatgpt-codex": "openai",
  gemini: "google",
  claude: "anthropic",
  kimi: "moonshot",
  glm: "zhipu",
  "z-ai": "zhipu",
  "z.ai": "zhipu",
  "lm-studio": "lmstudio",
  lm_studio: "lmstudio",
  alibaba: "qwen",
  "tongyi-qianwen": "qwen",
  "x.ai": "xai",
  grok: "xai",
  "silicon-flow": "siliconflow",
};
export function normalizeModelBrand(brand?: string | null): string {
  const key = (brand ?? "").trim().toLowerCase();
  return Object.hasOwn(modelBrands, key)
    ? key
    : Object.hasOwn(brandAliases, key)
      ? brandAliases[key]
      : key;
}
export function modelBrandName(brand?: string | null): string {
  const key = normalizeModelBrand(brand);
  return Object.hasOwn(modelBrands, key)
    ? modelBrands[key]
    : !key || key === "unknown"
      ? "未标注厂商"
      : brand?.trim() || "未标注厂商";
}
export function providerIconPath(brand?: string | null): string | null {
  const key = normalizeModelBrand(brand);
  return Object.hasOwn(modelBrands, key)
    ? "/provider-icons/" + key + ".svg"
    : null;
}
export const sameModel = (
  a: ModelSelection | null | undefined,
  b: ModelSelection | null | undefined,
) =>
  a === b ||
  Boolean(
    a && b && a.connection_id === b.connection_id && a.model_id === b.model_id,
  );
export function filterModelOptions(
  options: ModelOption[],
  search: string,
  kind: string,
  flagshipOnly: boolean,
) {
  const needle = search.trim().toLocaleLowerCase();
  return options.filter(
    (option) =>
      (kind === "all" || option.kind === kind) &&
      (!flagshipOnly || option.flagship === true) &&
      (!needle ||
        [
          option.model_name,
          option.model_id,
          option.connection_name,
          option.provider_id,
          modelBrandName(option.brand),
        ].some((value) => value.toLocaleLowerCase().includes(needle))),
  );
}
export function connectionConfigured(connection: ModelConnection) {
  return (
    connection.enabled &&
    (connection.credential_mode === "none" || connection.credential_present)
  );
}
export function connectionState(connection: ModelConnection): {
  label: string;
  tone: "neutral" | "green" | "amber" | "red";
} {
  if (!connection.enabled) return { label: "已停用", tone: "neutral" };
  if (connection.protocol === "codex_app_server" && connection.last_error_code)
    return {label:"订阅连接需检查", tone:"red"};
  if (connection.protocol === "codex_app_server") return {
    label: connection.oauth_state?.state === "AUTHENTICATED"
      ? connection.capabilities?.inference ? connection.status === "TESTED" ? "订阅推理已验证" : "已登录 · 可测试推理" : "已登录 · 推理不可用"
      : "官方登录 · 待授权",
    tone: connection.capabilities?.inference && connection.status === "TESTED" ? "green" : "amber",
  };
  if (!connectionConfigured(connection))
    return { label: "未配置", tone: "amber" };
  if (connection.last_error_code) return { label: "需检查连接", tone: "red" };
  if (
    ["VERIFIED", "TESTED", "CONNECTED", "TEST_SUCCEEDED"].includes(
      connection.status.toUpperCase(),
    )
  )
    return { label: "连接探测通过", tone: "green" };
  if (connection.status.toUpperCase() === "SYNCED")
    return { label: "已同步 · 未验证", tone: "neutral" };
  return { label: "已配置 · 未验证", tone: "neutral" };
}
// Pick public fields only. Unexpected response fields can never be spread into
// component state, an edit payload, a debug view or a model-option label.
export function publicConnection(value: ModelConnection): ModelConnection {
  return {
    id: value.id,
    owner_user_id: value.owner_user_id,
    capabilities: value.capabilities ? {auth: value.capabilities.auth, model_list: value.capabilities.model_list,
      inference: value.capabilities.inference === true, auth_blocked_reason: value.capabilities.auth_blocked_reason,
      inference_blocked_reason: value.capabilities.inference_blocked_reason,
      browser_callback_reachable: value.capabilities.browser_callback_reachable, live_verified: false} : undefined,
    oauth_state: value.oauth_state ? publicOAuthState(value.oauth_state) : undefined,
    space_id: value.space_id,
    name: value.name,
    provider_id: value.provider_id,
    protocol: value.protocol,
    base_url: value.base_url,
    enabled: value.enabled,
    credential_mode: value.credential_mode,
    api_key_env: value.api_key_env,
    credential_present: value.credential_present,
    allow_document_transfer: value.allow_document_transfer,
    revision: value.revision,
    status: value.status,
    last_error_code: value.last_error_code,
    last_synced_at: value.last_synced_at,
    models: (value.models ?? []).map((model) => ({
      id: model.id,
      name: model.name,
      brand: model.brand,
      flagship: model.flagship,
      source_url: model.source_url,
      source: model.source,
      context_length: model.context_length,
    })),
  };
}
export function connectionNeedsSecret(
  draft: ConnectionDraft,
  previous?: ModelConnection,
): boolean {
  if (!draft.enabled || draft.credential_mode !== "encrypted") return false;
  return (
    !previous?.credential_present ||
    previous.credential_mode !== "encrypted" ||
    draft.provider_id !== previous.provider_id ||
    draft.protocol !== previous.protocol ||
    draft.base_url.trim().replace(/\/+$/, "") !==
      previous.base_url.trim().replace(/\/+$/, "")
  );
}
export function connectionWrite(
  draft: ConnectionDraft,
  spaceId: string,
  secret: string,
  previous?: ModelConnection,
): ConnectionWrite {
  if (!draft.name.trim()) throw new Error("请填写连接名称。");
  if (draft.provider_id === "chatgpt-codex") return {
    ...(previous ? {} : {space_id: spaceId}), name: draft.name.trim(), provider_id: draft.provider_id,
    protocol: "codex_app_server", base_url: "", credential_mode: "chatgpt_oauth", enabled: draft.enabled,
    allow_document_transfer: draft.allow_document_transfer, custom_models: [],
  };
  let base: URL;
  try {
    base = new URL(draft.base_url);
  } catch {
    throw new Error("请填写完整的服务地址。");
  }
  if (
    !["http:", "https:"].includes(base.protocol) ||
    base.username ||
    base.password ||
    base.search ||
    base.hash
  )
    throw new Error(
      "服务地址须为 HTTP(S) 基础地址，不包含账号、密钥、查询参数或片段。",
    );
  if (
    draft.credential_mode === "env" &&
    !/^FKB_[A-Z0-9_]+$/.test(draft.api_key_env.trim())
  )
    throw new Error(
      "环境变量引用须以 FKB_ 开头，仅使用大写字母、数字和下划线。",
    );
  if (connectionNeedsSecret(draft, previous) && !secret.trim())
    throw new Error(
      previous?.credential_present
        ? "厂商、协议或服务地址已更换，请重新输入对应密钥。"
        : "启用此连接需要 API 密钥；也可关闭“启用此连接”后暂存配置。",
    );
  const models = draft.custom_models.map((model) => ({
    id: model.id.trim(),
    name: model.name.trim(),
    brand: model.brand.trim(),
  }));
  if (models.length > 1000)
    throw new Error("每个连接最多手工登记 1000 个模型。");
  if (models.some((model) => !model.id || !model.name || !model.brand))
    throw new Error("手工模型请完整填写模型 ID、名称和厂商。");
  if (
    models.some(
      (model) =>
        !/^[A-Za-z0-9][A-Za-z0-9._:/+@-]*$/.test(model.id) ||
        model.id.length > 512 ||
        model.name.length > 300 ||
        model.brand.length > 100,
    )
  )
    throw new Error(
      "模型 ID 请使用服务提供的英文或数字标识；检查模型 ID、名称和厂商的长度。",
    );
  if (new Set(models.map((model) => model.id)).size !== models.length)
    throw new Error("手工模型 ID 不能重复。");
  return {
    ...(previous ? {} : { space_id: spaceId }),
    name: draft.name.trim(),
    provider_id: draft.provider_id,
    base_url: draft.base_url.trim(),
    protocol: draft.protocol,
    enabled: draft.enabled,
    credential_mode: draft.credential_mode,
    allow_document_transfer: draft.allow_document_transfer,
    custom_models: models,
    ...(draft.credential_mode === "env"
      ? { api_key_env: draft.api_key_env.trim() }
      : {}),
    ...(draft.credential_mode === "encrypted" && secret.trim()
      ? { api_key: secret.trim() }
      : {}),
  };
}

export function publicOAuthState(value: ModelOAuthState): ModelOAuthState {
  return {connection_id: value.connection_id, owner_user_id: value.owner_user_id,
    connection_revision: value.connection_revision, oauth_revision: value.oauth_revision,
    auth_epoch: value.auth_epoch, state: value.state, attempt_id: value.attempt_id,
    active_job_id: value.active_job_id, account: value.account ? {type: "chatgpt", plan_type: value.account.plan_type} : null,
    checked_at: value.checked_at, last_error_code: value.last_error_code,
    capabilities: {auth: value.capabilities.auth, model_list: value.capabilities.model_list, inference: value.capabilities.inference === true,
      auth_blocked_reason: value.capabilities.auth_blocked_reason,
      inference_blocked_reason: value.capabilities.inference_blocked_reason,
      browser_callback_reachable: value.capabilities.browser_callback_reachable, live_verified: false}};
}

export function safeOAuthUrl(value: string | null | undefined): string | undefined {
  try {
    const url = new URL(value ?? "");
    return url.protocol === "https:" && !url.username && !url.password && !url.hash &&
      (!url.port || url.port === "443") && ["auth.openai.com", "chatgpt.com", "auth0.openai.com"].includes(url.hostname)
      ? url.href : undefined;
  } catch { return undefined; }
}
export function redactConnectionError(error: unknown, secret = "") {
  const message =
    error instanceof Error ? error.message : "连接配置未能完成，请重试。";
  return new Error(
    secret ? message.replaceAll(secret, "[密钥已隐藏]") : message,
  );
}
