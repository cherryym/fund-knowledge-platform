import type { Me, Page, Part, Upload, Job } from "./types";
import { clearDocumentSessionCache } from "./documentSessionCache";
import { clearWikiSessionCache } from "./wikiSessionCache";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public traceId = "",
  ) {
    super(message);
  }
}
let csrf = "";
let sessionUser = "";
const etags = new Map<string, string>();
const downloadNames = new Map<string, string>();
const inflightKeys = new Map<string, string>();
const base = "/api/v1";
export const apiUrl = (path: string) => `${base}${path}`;
export function clearSession() {
  clearDocumentSessionCache();
  clearWikiSessionCache();
  sessionUser = "";
  csrf = "";
  etags.clear();
  downloadNames.clear();
  inflightKeys.clear();
}
export function setSession(me: Me) {
  if (sessionUser !== me.id || csrf !== me.csrf_token) { clearDocumentSessionCache(); clearWikiSessionCache(); }
  sessionUser = me.id;
  csrf = me.csrf_token;
}
export const revisionTag = (revision: number) => `"${revision}"`;
export const query = (
  values: Record<string, string | number | boolean | undefined | null>,
) => {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values))
    if (value !== undefined && value !== null && value !== "")
      params.set(key, String(value));
  return params.toString();
};
type Options = {
  method?: string;
  body?: unknown;
  revision?: number;
  etagPath?: string;
  signal?: AbortSignal;
  response?: "json" | "text" | "blob";
  key?: string;
};
export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const method = options.method ?? "GET";
  const isWrite = !["GET", "HEAD"].includes(method);
  const headers = new Headers({
    Accept: options.response === "text" ? "text/html" : "application/json",
  });
  if (isWrite && csrf) headers.set("X-CSRF-Token", csrf);
  // Reuse the same key after an ambiguous network/server failure. Successful and
  // definite client-error responses retire it; requests are never auto-replayed.
  const operation = `${method}:${path}:${options.body instanceof Blob ? options.body.size : JSON.stringify(options.body)}`;
  if (isWrite) {
    const key =
      options.key ?? inflightKeys.get(operation) ?? crypto.randomUUID();
    inflightKeys.set(operation, key);
    headers.set("Idempotency-Key", key);
  }
  if (options.revision !== undefined)
    headers.set("If-Match", revisionTag(options.revision));
  else if (options.etagPath && etags.has(options.etagPath))
    headers.set("If-Match", etags.get(options.etagPath)!);
  let body: BodyInit | undefined;
  if (options.body instanceof Blob) {
    body = options.body;
    headers.set("Content-Type", "application/octet-stream");
  } else if (options.body !== undefined) {
    body = JSON.stringify(options.body);
    headers.set("Content-Type", "application/json");
  }
  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      method,
      headers,
      body,
      credentials: "include",
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new ApiError(
      0,
      "NETWORK_ERROR",
      "无法连接服务，请检查网络后重试。操作结果尚未确认。",
    );
  }
  if (response.ok || (response.status >= 400 && response.status < 500))
    inflightKeys.delete(operation);
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    if (response.status === 401) { clearDocumentSessionCache(); clearWikiSessionCache(); }
    if (method === "GET" && [403,404].includes(response.status)
      && /^\/(?:resources\/|versions\/|wiki\/workspace|wiki\/pages\/)/.test(path)) clearWikiSessionCache();
    if (response.status === 401 && path !== "/me" && !path.startsWith("/auth/"))
      window.dispatchEvent(new Event("session-expired"));
    const conflict =
      response.status === 412
        ? "内容已被其他人更新，请保留当前编辑，重新读取最新版本后再保存。"
        : "";
    throw new ApiError(
      response.status,
      value.code ?? `HTTP_${response.status}`,
      conflict ||
        value.message ||
        (typeof value.detail === "string"
          ? value.detail
          : `请求失败（${response.status}）`),
      value.trace_id,
    );
  }
  const etag = response.headers.get("ETag");
  if (etag) etags.set(path, etag);
  const disposition = response.headers.get("Content-Disposition");
  if (disposition) {
    const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    const plain = disposition.match(/filename="([^"]+)"/i)?.[1];
    try {
      const name = encoded ? decodeURIComponent(encoded) : plain;
      if (name) downloadNames.set(path, name.replace(/[\\/]/g, "_"));
    } catch {
      /* Keep the supplied fallback filename. */
    }
  }
  if (response.status === 204) return undefined as T;
  if (options.response === "blob") return (await response.blob()) as T;
  if (options.response === "text") return (await response.text()) as T;
  return (await response.json()) as T;
}
export const get = <T>(path: string, signal?: AbortSignal) =>
  api<T>(path, { signal });
export const post = <T>(path: string, body?: unknown, revision?: number) =>
  api<T>(path, { method: "POST", body, revision });
export const patch = <T>(path: string, body: unknown, revision: number) =>
  api<T>(path, { method: "PATCH", body, revision });
export const del = (path: string, revision?: number) =>
  api<void>(path, { method: "DELETE", revision });
export async function allPages<T>(
  path: string,
  signal?: AbortSignal,
): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | null = null;
  const seen = new Set<string>();
  do {
    if (signal?.aborted) throw new DOMException("Request no longer needed", "AbortError");
    const page: Page<T> = await get(
      `${path}${path.includes("?") ? "&" : "?"}${query({ limit: 100, cursor })}`,
      signal,
    );
    if (signal?.aborted) throw new DOMException("Request no longer needed", "AbortError");
    items.push(...page.items);
    cursor = page.next_cursor;
    if (cursor && seen.has(cursor))
      throw new Error("分页游标重复，请刷新后重试。");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return items;
}
export async function download(path: string, filename: string) {
  const blob = await api<Blob>(path, { response: "blob" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = downloadNames.get(path) ?? filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
export async function sha256(blob: Blob) {
  const bytes = new Uint8Array(
    await crypto.subtle.digest("SHA-256", await blob.arrayBuffer()),
  );
  return Array.from(bytes, (x) => x.toString(16).padStart(2, "0")).join("");
}
export async function sendParts(
  file: File,
  upload: Upload,
  onProgress: (percent: number) => void,
  signal: AbortSignal,
): Promise<Job> {
  const current = await get<Upload>(`/uploads/${upload.id}`, signal);
  if (current.state !== "OPEN")
    throw new Error(`上传会话为 ${current.state}，请创建新上传。`);
  const parts: Part[] = [];
  let transferred = 0;
  for (let partNo = 1; partNo <= current.part_count; partNo++) {
    signal.throwIfAborted();
    const part = file.slice(
      (partNo - 1) * current.part_size,
      Math.min(partNo * current.part_size, file.size),
    );
    const hash = await sha256(part);
    const existing = current.completed_parts.find(
      (p) =>
        p.part_no === partNo && p.sha256 === hash && p.size_bytes === part.size,
    );
    const result =
      existing ??
      (await api<Part>(`/uploads/${current.id}/parts/${partNo}`, {
        method: "PUT",
        body: part,
        signal,
      }));
    parts.push(result);
    transferred += part.size;
    onProgress(Math.floor((transferred / file.size) * 100));
  }
  signal.throwIfAborted();
  return api<Job>(`/uploads/${current.id}/complete`, {
    method: "POST",
    body: { parts },
    signal,
  });
}
