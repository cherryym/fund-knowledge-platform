import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { get, query } from "./api";
import { useApp } from "./ui";
import { BrandIcon } from "./BrandIcon";
import "./retrieval-profiles.css";

export type RetrievalSelection = { profile_id: string; fingerprint: string };
export type FrozenRetrievalSelection = RetrievalSelection & { model: string; dimensions: number };
export type RetrievalProfileOption = {
  id: string; name: string; model: string; dimensions: number; fingerprint: string;
  is_default: boolean; available: boolean; state: string; brand: string; bm25: boolean; reranker_model: string;
  model_ready?: boolean; index_ready?: boolean; can_index?: boolean;
  embedding_status?: string; reranker_status?: string; index_state?: string;
};
type ProfilesResponse = { default_profile_id: string | null; items: RetrievalProfileOption[]; enabled: boolean };
const changedEvent = "fund-kb:retrieval-profile-preference";
const volatile = new Map<string, string | null>();
export const retrievalPreferenceKey = (user: string, space: string) =>
  `fund-kb:retrieval-profile:v1:${encodeURIComponent(user)}:${encodeURIComponent(space)}`;

function read(key: string) {
  if (volatile.has(key)) return volatile.get(key) ?? null;
  try { return window.localStorage.getItem(key); } catch { return null; }
}
function validId(value: unknown): value is string {
  return typeof value === "string" && /^[a-z0-9][a-z0-9_-]{0,63}$/.test(value);
}

/** Preferences contain an ID only; the latest server supplies model/fingerprint. */
export function useRetrievalProfile() {
  const app = useApp();
  const key = retrievalPreferenceKey(app.me.id, app.space.id);
  const subscribe = useCallback((notify: () => void) => {
    const own = (event: Event) => { if ((event as CustomEvent).detail?.key === key) notify(); };
    const other = (event: StorageEvent) => {
      if (event.key !== null && event.key !== key) return;
      volatile.delete(key); notify();
    };
    window.addEventListener(changedEvent, own); window.addEventListener("storage", other);
    return () => { window.removeEventListener(changedEvent, own); window.removeEventListener("storage", other); };
  }, [key]);
  const snapshot = useCallback(() => read(key), [key]);
  const saved = useSyncExternalStore(subscribe, snapshot, () => null);
  const preferredId = validId(saved) ? saved : null;
  const [nonce, setNonce] = useState(0);
  const identity = JSON.stringify([key, app.space.roles, app.refresh, nonce]);
  const [state, setState] = useState<{ identity: string; data?: ProfilesResponse; error?: Error }>({ identity: "" });
  useEffect(() => {
    const controller = new AbortController();
    get<ProfilesResponse>(`/retrieval/profiles?${query({ space_id: app.space.id })}`, controller.signal)
      .then(data => {
        if (!data || !Array.isArray(data.items) || typeof data.enabled !== "boolean" ||
            (data.enabled ? !validId(data.default_profile_id) : data.default_profile_id !== null || data.items.length > 0) ||
            new Set(data.items.map(item => item?.id)).size !== data.items.length ||
            data.items.some(item => !item || !validId(item.id) || typeof item.fingerprint !== "string" ||
              !/^[a-f0-9]{64}$/.test(item.fingerprint) || typeof item.available !== "boolean" ||
              typeof item.model !== "string" || !item.model || typeof item.name !== "string" ||
              !Number.isSafeInteger(item.dimensions) || item.dimensions <= 0 ||
              [item.model_ready, item.index_ready, item.can_index].some(value => value !== undefined && typeof value !== "boolean"))) {
          throw new Error("检索方案响应不完整，请刷新后重试。");
        }
        if (!controller.signal.aborted) setState({ identity, data });
      }).catch(error => {
        if (controller.signal.aborted) return;
        const code = (error as { status?: number }).status;
        if (code === 404 || code === 501) setState({ identity, data: { default_profile_id: null, items: [], enabled: false } });
        else setState({ identity, error: error instanceof Error ? error : new Error(String(error)) });
      });
    return () => controller.abort();
  }, [identity, app.space.id]);
  const current = state.identity === identity ? state : undefined;
  const data = current?.data;
  const selectedId = preferredId ?? data?.default_profile_id ?? null;
  const selected = data?.items.find(item => item.id === selectedId);
  const selection = useMemo<RetrievalSelection | undefined>(() => selected ? {
    profile_id: selected.id, fingerprint: selected.fingerprint,
  } : undefined, [selected?.id, selected?.fingerprint]);
  const choose = useCallback((id: string | null) => {
    if (id !== null && !validId(id)) return;
    try {
      if (id === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, id);
      volatile.delete(key);
    } catch { volatile.set(key, id); }
    window.dispatchEvent(new window.CustomEvent(changedEvent, { detail: { key } }));
  }, [key]);
  // A registered profile can be indexed before it is available for retrieval.
  const indexBlockedReason = !current ? "正在读取检索方案，请稍候。"
    : current.error ? "检索方案读取失败，请刷新后再提交。"
      : preferredId && !data?.enabled ? "当前服务不支持已选的检索方案，请恢复服务器默认或刷新。"
        : data?.enabled && !selected ? "所选检索方案已移除或停用，请重新选择。"
          : "";
  const blockedReason = indexBlockedReason || (selected?.model_ready === false
    ? "所选检索方案的模型运行前提尚未就绪，请刷新状态或选择可用方案。"
    : selected && (!selected.available || selected.index_ready === false)
      ? "所选检索方案的索引尚未就绪，请选择可用方案。" : "");
  return { options: data?.items ?? [], selected, selection, selectedId, enabled: data?.enabled ?? false,
    loading: !current, error: current?.error, blockedReason, indexBlockedReason, choose,
    reload: () => setNonce(value => value + 1), preferredId };
}

export function RetrievalProfilePicker({ value, disabled = false, compact = false, allowUnavailable = false }: {
  value: ReturnType<typeof useRetrievalProfile>; disabled?: boolean; compact?: boolean; allowUnavailable?: boolean;
}) {
  if (!value.loading && !value.error && !value.enabled && !value.preferredId) return null;
  return <div className={`retrieval-profile-picker${compact ? " compact" : ""}`}>
    <label>
      <span>检索方案</span>
      {value.selected && <BrandIcon brand={value.selected.brand} size={18} />}
      <select aria-label="选择检索方案" value={value.selectedId ?? ""}
        disabled={disabled || value.loading || !!value.error}
        onChange={event => value.choose(event.target.value)}>
        {!value.selected && <option value={value.selectedId ?? ""} disabled>
          {value.loading ? "读取中…" : value.preferredId ? "原检索方案不可用" : "请选择检索方案"}
        </option>}
        {value.options.map(item => <option key={item.id} value={item.id}
          disabled={!allowUnavailable && (!item.available || item.model_ready === false || item.index_ready === false)}>
          {item.name}{item.is_default ? " · 默认" : ""}{item.available ? "" : " · 尚未就绪"}
        </option>)}
      </select>
    </label>
    {!compact && value.selected && <span className="retrieval-profile-description">
      {value.selected.dimensions.toLocaleString()} 维 · BM25 混合召回 · 保留 Wiki / 图谱联动
      {value.selected.model_ready !== undefined && <> · 模型运行前提：{value.selected.model_ready ? "就绪" : "未就绪"}</>}
      {value.selected.index_ready !== undefined && <> · 索引：{value.selected.index_ready ? "就绪" : "未就绪"}</>}
    </span>}
    {value.preferredId && <button type="button" disabled={disabled} onClick={() => value.choose(null)}>恢复默认</button>}
    {value.error && <button type="button" disabled={disabled} onClick={value.reload}>刷新检索方案</button>}
    {!compact && value.blockedReason && !value.loading && <span role="status">{value.blockedReason}</span>}
  </div>;
}
