import { useEffect, useMemo, useRef, useState } from "react";
import { useListFlip } from "./useListFlip";
import {
  ArrowClockwise,
  ArrowSquareOut,
  CheckCircle,
  CloudArrowDown,
  Crown,
  Key,
  Lightning,
  MagnifyingGlass,
  PencilSimple,
  Plus,
  Power,
  ShieldCheck,
  X,
} from "@phosphor-icons/react";
import { api, del, get, post, query } from "./api";
import type { Job } from "./types";
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  FormModal,
  Loading,
  Modal,
  Motion,
  MotionItem,
  Notice,
  useApp,
  useLoad,
  useTask,
} from "./ui";
import { BrandIcon } from "./BrandIcon";
import {
  connectionConfigured,
  connectionNeedsSecret,
  connectionState,
  connectionWrite,
  modelBrandName,
  modelBrands,
  modelSourceLabels,
  protocolLabels,
  publicConnection,
  redactConnectionError,
  sourceKindLabels,
  publicOAuthState,
  safeOAuthUrl,
  type ModelOAuthState,
  type ModelOAuthChallenge,
  type ConnectionDraft,
  type ModelConnection,
  type ModelProtocol,
  type ModelProvider,
  type ModelProviderCatalog,
} from "./models.types";
import "./models-ui.css";

const dateLabel = (date?: string | null) =>
  date && Number.isFinite(Date.parse(date))
    ? new Date(date).toLocaleString("zh-CN", { hour12: false })
    : "尚未同步";
const sourceUrl = (url?: string | null) => {
  try {
    const parsed = new URL(url ?? "");
    return ["https:", "http:"].includes(parsed.protocol)
      ? parsed.href
      : undefined;
  } catch {
    return undefined;
  }
};
type TrackedJob = {
  connectionId: string;
  connectionName: string;
  operation: "sync" | "test";
  job: Job;
};
function initialDraft(
  providers: ModelProvider[],
  connection?: ModelConnection,
  chosen?: ModelProvider,
): ConnectionDraft {
  const provider =
    chosen ??
    providers.find((item) => item.id === connection?.provider_id) ??
    providers[0];
  return {
    name: connection?.name ?? provider?.name ?? "",
    provider_id: connection?.provider_id ?? provider?.id ?? "",
    protocol: connection?.protocol ?? provider?.protocol ?? "openai",
    base_url: connection?.base_url ?? provider?.base_url ?? "",
    enabled: connection?.enabled ?? true,
    credential_mode:
      connection?.credential_mode ??
      (provider?.kind === "subscription" ? "chatgpt_oauth" : provider?.kind === "local" ? "none" : "encrypted"),
    api_key_env: connection?.api_key_env ?? "",
    allow_document_transfer: connection?.allow_document_transfer ?? false,
    custom_models:
      connection?.models
        .filter((model) => model.source === "custom")
        .map(({ id, name, brand }) => ({ id, name, brand })) ?? [],
  };
}

function ConnectionForm({
  providers,
  connection,
  provider: chosen,
  close,
  saved,
}: {
  providers: ModelProvider[];
  connection?: ModelConnection;
  provider?: ModelProvider;
  close: () => void;
  saved: (connection: ModelConnection) => void;
}) {
  const app = useApp();
  const [draft, setDraft] = useState(() =>
    initialDraft(providers, connection, chosen),
  );
  const credentialInput = useRef<HTMLInputElement>(null);
  const task = useTask();
  const provider = providers.find((item) => item.id === draft.provider_id);
  const oauth = draft.provider_id === "chatgpt-codex";
  const envReferences = useLoad(
    (signal) => draft.credential_mode === "env"
      ? get<{items: {name: string}[]; can_enter_env_reference: boolean}>("/model-credential-references", signal)
      : Promise.resolve({items: [], can_enter_env_reference: false}),
    [app.me.id, draft.credential_mode],
  );
  const set = <K extends keyof ConnectionDraft>(
    key: K,
    value: ConnectionDraft[K],
  ) => setDraft((current) => ({ ...current, [key]: value }));
  const changeProvider = (providerId: string) => {
    const selected = providers.find((item) => item.id === providerId);
    if (!selected) return;
    if (credentialInput.current) credentialInput.current.value = "";
    setDraft((current) => ({
      ...current,
      provider_id: selected.id,
      name:
        current.name === provider?.name || !current.name
          ? selected.name
          : current.name,
      protocol: selected.protocol,
      base_url: selected.base_url,
      credential_mode: selected.kind === "subscription" ? "chatgpt_oauth" : selected.kind === "local" ? "none" : "encrypted",
      api_key_env: "",
      allow_document_transfer: false,
      custom_models: [],
    }));
  };
  return (
    <Modal
      title={connection ? "编辑模型连接" : "添加模型连接"}
      close={close}
      busy={task.busy}
      wide
    >
      {({ requestClose, complete }) => (
        <form
          autoComplete="off"
          onSubmit={(event) => {
            event.preventDefault();
            void task.run(async () => {
              const secret = credentialInput.current?.value.trim() ?? "";
              const body = connectionWrite(
                draft,
                app.space.id,
                secret,
                connection,
              );
              try {
                const result = await api<ModelConnection>(
                  connection
                    ? "/model-connections/" + connection.id
                    : "/model-connections",
                  {
                    method: connection ? "PATCH" : "POST",
                    body,
                    revision: connection?.revision,
                  },
                );
                saved(publicConnection(result));
                complete();
              } catch (error) {
                throw redactConnectionError(error, secret);
              } finally {
                if (credentialInput.current) credentialInput.current.value = "";
                delete body.api_key;
              }
            });
          }}
        >
          <div className="modal-body models-connection-form">
            <section className="model-form-section">
              <h3>服务来源</h3>
              <div className="form-grid">
                <Field label="提供商">
                  <select
                    required
                    value={draft.provider_id}
                    disabled={task.busy || (!!connection && connection.provider_id === "chatgpt-codex")}
                    onChange={(event) => changeProvider(event.target.value)}
                  >
                    <option value="">选择提供商</option>
                    {providers.map((item) => (
                      <option value={item.id} key={item.id}>
                        {item.name} · {sourceKindLabels[item.kind]}
                      </option>
                    ))}
                    {draft.provider_id && !provider && (
                      <option value={draft.provider_id}>
                        {modelBrandName(draft.provider_id)}（已有连接）
                      </option>
                    )}
                  </select>
                </Field>
                <Field label="连接名称">
                  <input
                    required
                    maxLength={200}
                    value={draft.name}
                    onChange={(event) => set("name", event.target.value)}
                    placeholder="例如：团队主力模型"
                  />
                </Field>
              </div>
              {provider && (
                <div className="model-provider-inline">
                  <BrandIcon brand={provider.id} size={26} />
                  <div>
                    <strong>{provider.name}</strong>
                    <small>
                      {sourceKindLabels[provider.kind]} ·{" "}
                      {provider.kind === "gateway"
                        ? "通过此网关访问不同厂商模型"
                        : provider.kind === "local"
                          ? "使用已运行的本地服务，不下载模型权重"
                          : "按所选协议访问模型服务"}
                    </small>
                  </div>
                </div>
              )}
              {!oauth && <div className="form-grid">
                <Field label="服务基础地址">
                  <input
                    required
                    type="url"
                    autoComplete="off"
                    value={draft.base_url}
                    onChange={(event) => set("base_url", event.target.value)}
                    placeholder="https://…"
                  />
                </Field>
                <Field label="接口协议">
                  <select
                    value={draft.protocol}
                    onChange={(event) =>
                      set("protocol", event.target.value as ModelProtocol)
                    }
                  >
                    {Object.entries(protocolLabels).map(([id, label]) => (
                      <option key={id} value={id}>
                        {label}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>}
            </section>
            <section className="model-form-section">
              <h3>
                <Key size={18} />
                凭据配置
              </h3>
              <div
                className="model-segments"
                role="group"
                aria-label="凭据方式"
              >
                {(oauth ? [["chatgpt_oauth", "官方 ChatGPT OAuth"]] : [
                  ["encrypted", "网页录入 · 服务端加密"],
                  ["env", "环境变量引用"],
                  ["none", "无需密钥"],
                ]).map(([id, label]) => (
                  <button
                    type="button"
                    key={id}
                    className={draft.credential_mode === id ? "active" : ""}
                    aria-pressed={draft.credential_mode === id}
                    onClick={() => {
                      if (credentialInput.current)
                        credentialInput.current.value = "";
                      set(
                        "credential_mode",
                        id as ConnectionDraft["credential_mode"],
                      );
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {oauth && <Notice>保存后可通过官方设备码或受支持的浏览器流程登录。ChatGPT Pro 属于订阅访问，不提供通用 API 余额。严格文本隔离尚未验证，推理暂不可用。</Notice>}
              {draft.credential_mode === "encrypted" && (
                <>
                  <Field
                    label={
                      connection?.credential_present &&
                      connection.credential_mode === "encrypted" &&
                      !connectionNeedsSecret(draft, connection)
                        ? "替换 API 密钥（留空保留现有凭据）"
                        : "API 密钥（启用连接时必填）"
                    }
                  >
                    <input
                      ref={credentialInput}
                      type="password"
                      name="model_connection_secret"
                      required={connectionNeedsSecret(draft, connection)}
                      autoComplete="new-password"
                      spellCheck={false}
                      maxLength={8192}
                      placeholder={
                        connection?.credential_present
                          ? "已保存的密钥不会回显"
                          : "提交后由服务端加密保存"
                      }
                    />
                  </Field>
                  <p className="model-muted-note">
                    密钥只提交给服务端，不写入浏览器本地存储。提交后输入框清空；服务端加密配置不可用时会明确报错。暂不填写时，请关闭下方“启用此连接”。
                  </p>
                </>
              )}
              {draft.credential_mode === "env" && (
                <Field
                  label="环境变量名称"
                  hint="仅允许部署管理员或预分配给本人的变量名称；不显示环境变量值。"
                >
                  {envReferences.data?.can_enter_env_reference ? <input
                    required
                    pattern="FKB_[A-Z0-9_]+"
                    autoComplete="off"
                    value={draft.api_key_env}
                    onChange={(event) => set("api_key_env", event.target.value)}
                    placeholder="FKB_PROVIDER_API_KEY"
                  /> : <select required value={draft.api_key_env} onChange={event => set("api_key_env", event.target.value)}>
                    <option value="">选择已分配给本人的引用</option>
                    {(envReferences.data?.items ?? []).map(item => <option key={item.name} value={item.name}>{item.name}</option>)}
                  </select>}
                  <ErrorBox error={envReferences.error} retry={envReferences.reload} />
                </Field>
              )}
              {draft.credential_mode === "none" && (
                <Notice>
                  适用于无需身份凭据的本地服务；若所选服务要求密钥，连接测试会返回配置或认证错误。
                </Notice>
              )}
            </section>
            <section className="model-form-section">
              <h3>
                <ShieldCheck size={18} />
                本人连接与资料传输
              </h3>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={draft.enabled}
                  onChange={(event) => set("enabled", event.target.checked)}
                />
                启用此连接
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={draft.allow_document_transfer}
                  onChange={(event) =>
                    set("allow_document_transfer", event.target.checked)
                  }
                />
                允许将本人可访问库内、经本次操作授权的文档上下文发送到此服务
              </label>
              <p className="model-muted-note">
                资料传输默认关闭。同步模型与连接探测不包含业务文档，知识构建和答疑仍须遵守来源权限。
              </p>
            </section>
            {!oauth && <details
              className="model-custom-models"
              open={
                draft.custom_models.length > 0 || provider?.kind === "local"
              }
            >
              <summary>
                手工登记模型
                {provider?.kind === "local"
                  ? "（仅登记已部署模型）"
                  : "（同步接口未返回时使用）"}
              </summary>
              <div className="form-stack">
                {draft.custom_models.map((model, i) => (
                  <div className="model-custom-row" key={i}>
                    <Field label="模型 ID">
                      <input
                        required
                        value={model.id}
                        placeholder="服务实际使用的模型 ID"
                        onChange={(event) =>
                          set(
                            "custom_models",
                            draft.custom_models.map((item, n) =>
                              n === i
                                ? { ...item, id: event.target.value }
                                : item,
                            ),
                          )
                        }
                      />
                    </Field>
                    <Field label="显示名称">
                      <input
                        required
                        value={model.name}
                        onChange={(event) =>
                          set(
                            "custom_models",
                            draft.custom_models.map((item, n) =>
                              n === i
                                ? { ...item, name: event.target.value }
                                : item,
                            ),
                          )
                        }
                      />
                    </Field>
                    <Field label="模型厂商">
                      <select
                        value={model.brand}
                        onChange={(event) =>
                          set(
                            "custom_models",
                            draft.custom_models.map((item, n) =>
                              n === i
                                ? { ...item, brand: event.target.value }
                                : item,
                            ),
                          )
                        }
                      >
                        <option value="unknown">其他 / 未标注</option>
                        {Object.entries(modelBrands).map(([id, name]) => (
                          <option value={id} key={id}>
                            {name}
                          </option>
                        ))}
                        {model.brand &&
                          !modelBrands[model.brand] &&
                          model.brand !== "unknown" && (
                            <option value={model.brand}>{model.brand}</option>
                          )}
                      </select>
                    </Field>
                    <button
                      type="button"
                      className="icon-button danger"
                      aria-label={"移除手工模型 " + (i + 1)}
                      onClick={() =>
                        set(
                          "custom_models",
                          draft.custom_models.filter((_, n) => n !== i),
                        )
                      }
                    >
                      <X />
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  onClick={() =>
                    set("custom_models", [
                      ...draft.custom_models,
                      { id: "", name: "", brand: "unknown" },
                    ])
                  }
                >
                  <Plus />
                  登记模型
                </button>
              </div>
            </details>}
            <ErrorBox error={task.error} />
          </div>
          <footer className="modal-foot">
            <span className="model-muted-note">
              保存配置不会自动请求模型服务
            </span>
            <button type="button" disabled={task.busy} onClick={requestClose}>
              取消
            </button>
            <button className="primary" disabled={task.busy}>
              {task.busy ? "正在保存…" : "保存连接"}
            </button>
          </footer>
        </form>
      )}
    </Modal>
  );
}

function OAuthPanel({connection}: {connection: ModelConnection}) {
  const app = useApp();
  const root = `/model-connections/${connection.id}/oauth`;
  const [state, setState] = useState<ModelOAuthState>();
  const [challenge, setChallenge] = useState<ModelOAuthChallenge>();
  const [error, setError] = useState<Error>();
  const [busy, setBusy] = useState(false);
  const [nonce, setNonce] = useState(0);
  const lastState = useRef<string | undefined>(undefined);
  useEffect(() => {
    const controller = new AbortController();
    let working = false;
    setChallenge(undefined);
    const read = async () => {
      if (working) return;
      working = true;
      try {
        const value = publicOAuthState(await get<ModelOAuthState>(root, controller.signal));
        if (controller.signal.aborted) return;
        setState(value);
        // A successful transition refreshes connection metadata once. A remount
        // which already starts authenticated must not trigger a reload loop.
        if (value.state === "AUTHENTICATED" && lastState.current !== undefined && lastState.current !== value.state) app.bump();
        lastState.current = value.state;
        setError(undefined);
        if (value.state === "PENDING" && value.attempt_id) {
          const fresh = await get<ModelOAuthChallenge>(root + "/challenge?" + query({attempt_id: value.attempt_id}), controller.signal);
          if (controller.signal.aborted) return;
          setChallenge({attempt_id: fresh.attempt_id, flow: fresh.flow,
            verification_url: safeOAuthUrl(fresh.verification_url) ?? null,
            authorization_url: safeOAuthUrl(fresh.authorization_url) ?? null,
            user_code: fresh.user_code, local_expires_at: fresh.local_expires_at});
          if (!value.active_job_id) await api<Job>(root + "/refresh", {method: "POST", revision: value.oauth_revision,
            body: {connection_revision: value.connection_revision}, signal: controller.signal});
        } else setChallenge(undefined);
      } catch (e) {
        if (!controller.signal.aborted) { setError(e as Error); setChallenge(undefined); }
      } finally { working = false; }
    };
    void read();
    const interval = setInterval(() => void read(), 2500);
    return () => {controller.abort(); clearInterval(interval);};
  }, [root, connection.revision, app.me.id, nonce]);
  const perform = async (operation: "start" | "refresh" | "cancel" | "logout", flow = "device_code") => {
    if (!state || busy) return;
    setBusy(true); setError(undefined); setChallenge(undefined);
    try {
      await api<Job>(root + "/" + operation, {method: "POST", revision: state.oauth_revision,
        body: {connection_revision: state.connection_revision,
          ...(operation === "start" ? {flow} : {}), ...(operation === "cancel" ? {attempt_id: state.attempt_id} : {})}});
      setNonce(value => value + 1);
    } catch (e) {setError(e as Error);} finally {setBusy(false);}
  };
  const labels: Record<string,string> = {DISABLED:"连接已停用", SIGNED_OUT:"未登录", STARTING:"正在准备登录",
    PENDING:"等待本人完成官方授权", AUTHENTICATED:"已登录", CANCELLING:"正在取消", CANCELLED:"已取消",
    EXPIRED:"登录已过期", SIGNING_OUT:"正在退出", ERROR:"需检查登录状态"};
  const pending = ["STARTING", "PENDING", "CANCELLING", "SIGNING_OUT"].includes(state?.state ?? "");
  const canStart = state?.capabilities.auth && !pending && state.state !== "AUTHENTICATED" && connection.enabled;
  const url = safeOAuthUrl(challenge?.verification_url ?? challenge?.authorization_url);
  return <section className="model-oauth-panel" aria-label="官方 ChatGPT 登录">
    <h3>官方 ChatGPT / Codex 登录</h3>
    <p role="status">{state ? labels[state.state] ?? "状态未知" : "读取登录状态…"}
      {state?.account?.plan_type ? ` · ${state.account.plan_type}` : ""}</p>
    <p>{state?.capabilities.inference
      ? "已启用隔离文本推理：不提供文件、命令、浏览器或插件工具。资料调用仍需单独授权；使用此账号的 Codex 订阅限额。"
      : "推理暂不可用：尚未验证全部工具与文件读取都能禁用。订阅访问不等于 API 余额。"}</p>
    {state && !state.capabilities.auth && <Notice>登录适配未就绪：{state.capabilities.auth_blocked_reason}。需配置独立身份目录、官方可执行文件和加密托管。</Notice>}
    <div className="inline-actions">
      <button type="button" disabled={!canStart || busy} onClick={() => void perform("start")}>使用设备码登录</button>
      {state?.capabilities.browser_callback_reachable && <button type="button" disabled={!canStart || busy} onClick={() => void perform("start", "browser")}>使用浏览器登录</button>}
      <button type="button" disabled={!state?.capabilities.auth || busy || !!state?.active_job_id} onClick={() => void perform("refresh")}>刷新登录状态</button>
      {state?.attempt_id && ["STARTING","PENDING"].includes(state.state) && <button type="button" disabled={busy} onClick={() => void perform("cancel")}>取消登录</button>}
      {state && ["AUTHENTICATED","ERROR"].includes(state.state) && <button type="button" disabled={busy || !state.capabilities.auth} onClick={() => void perform("logout")}>退出此连接</button>}
    </div>
    {challenge && url && Date.parse(challenge.local_expires_at) > Date.now() && <div className="model-oauth-challenge">
      {challenge.user_code && <p>在官方页面输入设备码：<code>{challenge.user_code}</code></p>}
      <a href={url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">打开官方授权页面 <ArrowSquareOut /></a>
      <small>本次登录窗口截止：{dateLabel(challenge.local_expires_at)}</small>
    </div>}
    {state?.last_error_code && <small>{state.last_error_code}</small>}
    <ErrorBox error={error} retry={() => setNonce(value => value + 1)} />
  </section>;
}

export function ModelsPage() {
  const listRoot = useRef<HTMLDivElement>(null);
  const app = useApp();
  const [tab, setTab] = useState("connections");
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string>();
  const [editing, setEditing] = useState<{
    connection?: ModelConnection;
    provider?: ModelProvider;
  }>();
  const [stopping, setStopping] = useState<ModelConnection>();
  const [testing, setTesting] = useState<ModelConnection>();
  const [tracked, setTracked] = useState<TrackedJob[]>([]);
  const [pollError, setPollError] = useState<Error>();
  const [pollNonce, setPollNonce] = useState(0);
  const task = useTask();
  const canManage = true; // Backend enforces owner; every active signed-in user can manage their own.
  const providers = useLoad(
    (signal) => get<ModelProviderCatalog>("/model-providers", signal),
    [app.space.id],
  );
  const connections = useLoad(
    async (signal) =>
      (
        await get<{ items: ModelConnection[] }>(
          "/model-connections",
          signal,
        )
      ).items.map(publicConnection),
    [app.me.id, app.refresh],
  );
  const catalog = providers.data?.items ?? [];
  const providerFor = (connection: ModelConnection) =>
    catalog.find((provider) => provider.id === connection.provider_id);
  const current =
    connections.data?.find((connection) => connection.id === selectedId) ??
    connections.data?.[0];
  const filtered = useMemo(
    () =>
      (connections.data ?? []).filter(
        (connection) =>
          (filter === "all" ||
            catalog.find((provider) => provider.id === connection.provider_id)
              ?.kind === filter) &&
          (!search.trim() ||
            [connection.name, connection.provider_id, connection.base_url].some(
              (text) =>
                text
                  .toLocaleLowerCase()
                  .includes(search.trim().toLocaleLowerCase()),
            )),
      ),
    [connections.data, catalog, filter, search],
  );
  useListFlip(listRoot, `${tab}:${filtered.map(connection => connection.id).join(",")}`, ".model-connection-row[data-flip-id]");
  const pendingIds = tracked
    .filter((item) => ["QUEUED", "RUNNING"].includes(item.job.state))
    .map((item) => item.job.id)
    .join(",");
  useEffect(() => {
    setTracked([]);
    setSelectedId(undefined);
    setEditing(undefined);
    setStopping(undefined);
    setTesting(undefined);
  }, [app.space.id, app.me.id]);
  useEffect(() => {
    if (!pendingIds) return;
    const controller = new AbortController();
    let active = false;
    const poll = async () => {
      if (active) return;
      active = true;
      try {
        const results = await Promise.allSettled(
          pendingIds
            .split(",")
            .map((id) => get<Job>("/jobs/" + id, controller.signal)),
        );
        if (controller.signal.aborted) return;
        const updates: Job[] = [];
        let error: Error | undefined;
        for (const result of results) {
          if (result.status === "fulfilled") updates.push(result.value);
          else error = result.reason as Error;
        }
        setTracked((items) =>
          items.map((item) => ({
            ...item,
            job: updates.find((job) => job.id === item.job.id) ?? item.job,
          })),
        );
        setPollError(error);
        if (updates.some((job) => !["QUEUED", "RUNNING"].includes(job.state))) {
          connections.reload();
          app.bump();
        }
      } finally {
        active = false;
      }
    };
    void poll();
    const timer = setInterval(() => void poll(), 1800);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [pendingIds, pollNonce]);
  const jobPending = (connectionId: string) =>
    tracked.some(
      (item) =>
        item.connectionId === connectionId &&
        ["QUEUED", "RUNNING"].includes(item.job.state),
    );
  async function startJob(
    connection: ModelConnection,
    operation: "sync" | "test",
    modelId?: string,
  ) {
    const job = await api<Job>(
      "/model-connections/" +
        connection.id +
        (operation === "sync" ? "/sync-models" : "/test"),
      {method: "POST", revision: connection.revision,
        body: operation === "test" ? (modelId ? { model_id: modelId } : {}) : undefined},
    );
    setTracked((items) => [
      {
        connectionId: connection.id,
        connectionName: connection.name,
        operation,
        job,
      },
      ...items,
    ]);
    app.notify(
      operation === "sync" ? "已创建模型同步任务" : "已创建连接探测任务",
    );
  }
  return (
    <section className="models-page">
      <header className="page-heading">
        <div>
          <h1>我的模型服务</h1>
          <p>管理本人的 API、本地服务与官方 ChatGPT 登录，可用于本人获准访问的知识库。</p>
        </div>
        <div className="inline-actions">
          <button
            type="button"
            onClick={() => {
              providers.reload();
              connections.reload();
            }}
          >
            <ArrowClockwise />
            刷新
          </button>
          <button
            type="button"
            className="primary"
            disabled={!canManage || providers.loading || !catalog.length}
            onClick={() => setEditing({})}
          >
            <Plus />
            添加连接
          </button>
        </div>
      </header>
      <nav className="tabs" aria-label="模型服务页面">
        <button
          type="button"
          className={tab === "connections" ? "active" : ""}
          onClick={() => setTab("connections")}
        >
          我的连接
          {connections.data && (
            <span className="model-tab-count">{connections.data.length}</span>
          )}
        </button>
        <button
          type="button"
          className={tab === "providers" ? "active" : ""}
          onClick={() => setTab("providers")}
        >
          提供商目录
        </button>
      </nav>
      <div className="models-page-body" ref={listRoot}>
        <ErrorBox error={task.error} />
        {tab === "providers" ? (
          <>
            <div className="models-catalog-heading">
              <h2>选择模型来源</h2>
              <p>
                以下是有来源依据的目录预置。旗舰标识和型号可选不代表当前账号已经授权或完成实服务验证。
              </p>
            </div>
            <ErrorBox error={providers.error} retry={providers.reload} />
            {providers.loading ? (
              <Loading label="读取提供商目录…" />
            ) : !catalog.length ? (
              <Empty
                title="提供商目录暂不可用"
                detail="请刷新，或等待服务端提供模型目录。"
              />
            ) : (
              <div className="models-provider-grid">
                {catalog.map((provider, index) => (
                  <MotionItem
                    active={index < 6}
                    identity={provider.id}
                    className="models-provider-card"
                    key={provider.id}
                  >
                    <div className="model-provider-card-head">
                      <BrandIcon brand={provider.id} size={31} />
                      <div>
                        <h3>{provider.name}</h3>
                        <span>{sourceKindLabels[provider.kind]}</span>
                      </div>
                    </div>
                    <p>{protocolLabels[provider.protocol]}</p>
                    <div className="models-preset-list">
                      {provider.kind !== "local" &&
                        !provider.models.some((model) => model.flagship) && (
                          <span>配置后同步账号型号，或手工登记模型</span>
                        )}
                      {provider.kind === "local" ? (
                        <span>仅列出实际同步或手工登记的模型</span>
                      ) : (
                        provider.models
                          .filter((model) => model.flagship)
                          .slice(0, 3)
                          .map((model) => (
                            <span key={model.id}>
                              <BrandIcon
                                brand={model.brand}
                                size={15}
                                decorative
                              />
                              {model.name}
                              <Crown size={12} />
                            </span>
                          ))
                      )}
                    </div>
                    <footer>
                      <span className="model-state neutral">目录预置</span>
                      <button
                        type="button"
                        disabled={!canManage}
                        onClick={() => setEditing({ provider })}
                      >
                        配置连接
                      </button>
                    </footer>
                    {sourceUrl(provider.source_url) && (
                      <a
                        className="model-catalog-source"
                        href={sourceUrl(provider.source_url)}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        目录依据
                        <ArrowSquareOut size={12} />
                      </a>
                    )}
                    <small className="model-muted-note">
                      {provider.verified_at
                        ? "目录核对：" + provider.verified_at.slice(0, 10)
                        : "目录内容待核验"}
                    </small>
                  </MotionItem>
                ))}
              </div>
            )}
          </>
        ) : (
          <>
            <div className="models-list-toolbar">
              <label className="model-picker-search">
                <MagnifyingGlass size={18} />
                <input
                  aria-label="搜索模型连接"
                  placeholder="搜索连接名称或提供商"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </label>
              <div
                className="model-segments"
                role="group"
                aria-label="筛选连接来源"
              >
                {[["all", "全部"], ...Object.entries(sourceKindLabels)].map(
                  ([id, label]) => (
                    <button
                      key={id}
                      type="button"
                      className={filter === id ? "active" : ""}
                      aria-pressed={filter === id}
                      onClick={() => setFilter(id)}
                    >
                      {label}
                    </button>
                  ),
                )}
              </div>
            </div>
            <ErrorBox error={connections.error} retry={connections.reload} />
            <ErrorBox error={providers.error} retry={providers.reload} />
            {connections.loading ? (
              <Loading label="读取空间模型连接…" />
            ) : !connections.data?.length ? (
              <Empty
                title="还没有配置模型连接"
                detail="选择厂商 API、中转网关或已运行的本地服务。保存配置后，可主动同步模型并测试连接。"
              >
                <button
                  type="button"
                  disabled={!canManage || !catalog.length}
                  onClick={() => setEditing({})}
                >
                  <Plus />
                  添加首个连接
                </button>
              </Empty>
            ) : (
              <div className="models-connection-layout">
                <div className="models-connections-list">
                  {!filtered.length ? (
                    <Empty
                      title="没有匹配的连接"
                      detail="调整关键词或来源筛选。"
                    />
                  ) : (
                    filtered.map((connection) => {
                      const state = connectionState(connection);
                      const provider = providerFor(connection);
                      return (
                        <button
                          type="button"
                          className={
                            "model-connection-row " +
                            (current?.id === connection.id ? "selected" : "")
                          }
                          key={connection.id}
                          aria-pressed={current?.id === connection.id}
                          data-flip-id={`connection-${connection.id}`}
                          onClick={() => setSelectedId(connection.id)}
                        >
                          <BrandIcon
                            brand={connection.provider_id}
                            size={29}
                            decorative
                          />
                          <span className="model-connection-title">
                            <strong>{connection.name}</strong>
                            <small>
                              {provider?.name ??
                                modelBrandName(connection.provider_id)}{" "}
                              · {sourceKindLabels[provider?.kind ?? "custom"]}
                            </small>
                            <small>
                              {connection.models.length} 个模型 ·{" "}
                              {connection.credential_mode === "encrypted"
                                ? "服务端加密凭据"
                                : connection.credential_mode === "env"
                                  ? "环境变量引用"
                                  : "无需密钥"}
                            </small>
                          </span>
                          <span className={"model-state " + state.tone}>
                            {state.label}
                          </span>
                        </button>
                      );
                    })
                  )}
                </div>
                {current && (
                  <Motion
                    identity={current.id}
                    className="models-connection-detail"
                  >
                    <header>
                      <div className="model-provider-inline">
                        <BrandIcon brand={current.provider_id} size={34} />
                        <div>
                          <h2>{current.name}</h2>
                          <small>
                            {providerFor(current)?.name ??
                              modelBrandName(current.provider_id)}{" "}
                            ·{" "}
                            {
                              sourceKindLabels[
                                providerFor(current)?.kind ?? "custom"
                              ]
                            }
                          </small>
                        </div>
                      </div>
                      <span
                        className={
                          "model-state " + connectionState(current).tone
                        }
                      >
                        {connectionState(current).label}
                      </span>
                    </header>
                    {current.protocol === "codex_app_server" && <OAuthPanel key={current.id + app.me.id} connection={current} />}
                    <div className="models-detail-actions">
                      <button
                        type="button"
                        disabled={!canManage || task.busy}
                        onClick={() => setEditing({ connection: current })}
                      >
                        <PencilSimple />
                        编辑连接
                      </button>
                      <button
                        type="button"
                        disabled={
                          !canManage ||
                          !connectionConfigured(current) ||
                          jobPending(current.id) ||
                          task.busy
                        }
                        onClick={() =>
                          void task.run(() => startJob(current, "sync"))
                        }
                      >
                        <CloudArrowDown />
                        同步模型
                      </button>
                      <button
                        type="button"
                        disabled={
                          (current.protocol === "codex_app_server" && !current.capabilities?.inference) || !canManage ||
                          !connectionConfigured(current) ||
                          jobPending(current.id) ||
                          task.busy
                        }
                        onClick={() => setTesting(current)}
                      >
                        <Lightning />
                        测试连接
                      </button>
                      {(
                        <button
                          type="button"
                          className="danger"
                          disabled={!canManage || task.busy}
                          onClick={() => setStopping(current)}
                        >
                          <Power />
                          {current.enabled ? "停用" : "移除连接"}
                        </button>
                      )}
                    </div>
                    <dl className="model-connection-metadata">
                      <div>
                      <dt>接口协议</dt>
                      <dd>{protocolLabels[current.protocol]}</dd>
                      </div>
                      <div>
                      <dt>服务地址</dt>
                      <dd>{current.protocol === "codex_app_server" ? "官方隔离 bridge" : current.base_url}</dd>
                      </div>
                      <div>
                      <dt>凭据状态</dt>
                      <dd>
                        {current.credential_mode === "none"
                          ? "此连接设为无需密钥"
                          : current.credential_present
                            ? "已配置（内容不回显）"
                            : "尚未配置"}
                        {current.credential_mode === "env" &&
                          current.api_key_env && (
                            <small>{current.api_key_env}</small>
                          )}
                      </dd>
                      </div>
                      <div>
                      <dt>资料传输</dt>
                      <dd>
                        {current.allow_document_transfer
                          ? "已明确授权"
                          : "未授权外发文档上下文"}
                      </dd>
                      </div>
                      <div>
                      <dt>最近同步</dt>
                      <dd>{dateLabel(current.last_synced_at)}</dd>
                      </div>
                    </dl>
                    {current.last_error_code && (
                      <Notice>最近任务需检查：{current.last_error_code}</Notice>
                    )}
                    <div className="models-model-list-heading">
                      <h3>
                        此连接下的模型 <span>{current.models.length}</span>
                      </h3>
                      <small>
                        同步列表代表账号目录可见，模型调用仍以实际请求结果为准。
                      </small>
                    </div>
                    {current.models.length ? (
                      <div className="models-account-models">
                        {current.models.map((model) => (
                          <article
                            className="models-account-model"
                            key={model.id}
                          >
                            <BrandIcon
                              brand={model.brand}
                              size={23}
                              decorative
                            />
                            <div>
                              <strong>
                                {model.name}
                                {model.flagship && (
                                  <span className="model-flagship">
                                    <Crown size={12} />
                                    旗舰
                                  </span>
                                )}
                              </strong>
                              <small>
                                {modelBrandName(model.brand)}
                                {providerFor(current)?.kind === "gateway" &&
                                  " · 经 " +
                                    (providerFor(current)?.name ??
                                      current.provider_id) +
                                    " 中转"}
                              </small>
                              <small className="model-id">{model.id}</small>
                            </div>
                            <span className="model-state neutral">
                              {modelSourceLabels[model.source] ?? "来源未确认"}
                            </span>
                            {model.context_length != null && (
                              <small>
                                {model.context_length.toLocaleString()} 上下文
                              </small>
                            )}
                          </article>
                        ))}
                      </div>
                    ) : (
                      <Empty
                        title="还没有登记模型"
                        detail="同步此连接的模型列表，或在编辑连接时手工登记已部署的模型。"
                      />
                    )}
                  </Motion>
                )}
              </div>
            )}
          </>
        )}
        {tracked.length > 0 && (
          <section className="models-jobs">
            <header>
              <h3>本次后台任务</h3>
              <button
                type="button"
                className="text-button"
                onClick={() => app.navigate("tasks")}
              >
                查看全部任务
              </button>
            </header>
            <ErrorBox
              error={pollError}
              retry={() => setPollNonce((value) => value + 1)}
            />
            {tracked.map((item) => (
              <div className="model-job-row" key={item.job.id}>
                <span>
                  {item.operation === "sync" ? (
                    <CloudArrowDown size={19} />
                  ) : (
                    <Lightning size={19} />
                  )}
                </span>
                <div>
                  <strong>
                    {item.connectionName} ·{" "}
                    {item.operation === "sync" ? "同步模型" : "连接探测"}
                  </strong>
                  <small>{item.job.stage || "等待后台处理"}</small>
                  {item.job.error_code && (
                    <p className="text-red">{item.job.error_code}</p>
                  )}
                  {item.job.state === "SUCCEEDED" && (
                    <small>
                      {item.operation === "sync"
                        ? "同步任务完成，已刷新连接模型列表。"
                        : "本次固定探测通过，不代表专业准确性或所有型号均可用。"}
                    </small>
                  )}
                </div>
                <Badge value={item.job.state} />
                {["QUEUED", "RUNNING"].includes(item.job.state) && (
                  <button
                    type="button"
                    disabled={task.busy}
                    onClick={() =>
                      void task.run(async () => {
                        const job = await post<Job>(
                          "/jobs/" + item.job.id + "/cancel",
                        );
                        setTracked((items) =>
                          items.map((value) =>
                            value.job.id === job.id ? { ...value, job } : value,
                          ),
                        );
                      })
                    }
                  >
                    取消任务
                  </button>
                )}
                {item.job.state === "FAILED" && (
                  <button
                    type="button"
                    disabled={task.busy}
                    onClick={() =>
                      void task.run(async () => {
                        const job = await post<Job>(
                          "/jobs/" + item.job.id + "/retry",
                        );
                        setTracked((items) =>
                          items.map((value) =>
                            value.job.id === job.id ? { ...value, job } : value,
                          ),
                        );
                      })
                    }
                  >
                    <ArrowClockwise />
                    重试任务
                  </button>
                )}
              </div>
            ))}
          </section>
        )}
      </div>
      {editing && (
        <ConnectionForm
          providers={catalog}
          connection={editing.connection}
          provider={editing.provider}
          close={() => setEditing(undefined)}
          saved={(connection) => {
            setSelectedId(connection.id);
            setTab("connections");
            app.bump();
            connections.reload();
            app.notify("模型连接已保存，尚未自动发起模型请求");
          }}
        />
      )}
      {stopping && (
        <FormModal
          title={stopping.enabled ? "停用模型连接" : "移除模型连接"}
          label="移除并清除凭据配置"
          danger
          close={() => setStopping(undefined)}
          submit={async () => {
            await del("/model-connections/" + stopping.id, stopping.revision);
            app.bump();
            connections.reload();
            app.notify("连接已移除，历史运行记录保留");
          }}
        >
          <p>
            将移除「{stopping.name}
            」，停用该连接并清除保存的凭据配置。历史咨询与审计记录保留。
          </p>
          <Notice>
            再次使用时需新建连接并配置凭据。环境变量本身不会由此操作删除。
          </Notice>
        </FormModal>
      )}
      {testing && (
        <FormModal
          title="测试模型连接"
          label="发起固定探测"
          close={() => setTesting(undefined)}
          submit={async (data) => {
            await startJob(
              testing,
              "test",
              String(data.get("model_id") ?? "") || undefined,
            );
          }}
        >
          <div className="model-provider-inline">
            <BrandIcon brand={testing.provider_id} size={28} />
            <strong>{testing.name}</strong>
          </div>
          <Field label="探测模型">
            <select name="model_id">
              <option value="">由服务选择默认模型</option>
              {testing.models.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.name} · {model.id}
                </option>
              ))}
            </select>
          </Field>
          <Notice>
            将向所选服务发送固定、无业务文档的测试内容。结果通过后台任务返回；成功不代表业务答案准确性已验证。
          </Notice>
        </FormModal>
      )}
    </section>
  );
}
export default ModelsPage;
