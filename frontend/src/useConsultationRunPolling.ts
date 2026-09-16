import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, get } from "./api";
import type { Run } from "./types";

export const RUN_POLL_MS = 1600;
const pending = (run: Pick<Run, "state">) => ["QUEUED", "RUNNING"].includes(run.state);
const terminal = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
type Progress = Pick<Run, "thread_id" | "job_id" | "state" | "phase" | "attempt" | "invalidated" |
  "model_snapshot" | "question_analysis" | "error_code"> & {
  progress_version: 1; run_id: string; source_check: "current_access"; created_at?: string;
};

/** Consume a bounded SSE snapshot, not an EventSource connection or automatic reconnect. */
export function parseRunProgress(text: string, run: Run): Progress | null {
  const records = text.trim().startsWith("{") ? [text] : text.replace(/\r\n/g, "\n").split("\n\n")
    .map(event => event.split("\n").filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n")).filter(Boolean);
  if (!records.length) throw new Error("进度响应缺少当前状态，请重新读取。");
  const data = JSON.parse(records.at(-1)!);
  if (!data || data.run_id !== run.id) throw new Error("进度响应与当前咨询不一致。");
  // Old stage-only events cannot safely replace the current preview/ACL receipt.
  if (data.progress_version === undefined) return null;
  if (data.progress_version !== 1 || data.source_check !== "current_access" || data.thread_id !== run.thread_id ||
      data.job_id !== run.job_id || typeof data.invalidated !== "boolean" ||
      (!pending(data) && !terminal.has(data.state))) throw new Error("进度响应缺少有效的当前访问回执。");
  return { ...data, phase: data.phase ?? data.stage ?? null };
}

export function replaceRun(previous: Run[], next: Run): Run[] {
  const index = previous.findIndex(run => run.id === next.id && run.thread_id === next.thread_id);
  if (index < 0 || JSON.stringify(previous[index]) === JSON.stringify(next)) return previous;
  const result = [...previous];
  result[index] = next;
  return result;
}

type Entry = { run: Run; controller: AbortController; timer?: ReturnType<typeof setTimeout>; reading: boolean };
export function useConsultationRunPolling({ scope, runs, enabled, onRun, withdrawPreview }: {
  scope: string; runs: Run[]; enabled: boolean;
  onRun: (run: Run) => void; withdrawPreview: (id: string) => void;
}) {
  const session = useMemo(() => ({ scope, entries: new Map<string, Entry>(), paused: new Set<string>(),
    latest: new Map<string, Run>(), stopped: false, enabled: false, legacy: false }), [scope]);
  const current = useRef(session);
  current.current = session;
  const permitted = useRef(enabled);
  permitted.current = enabled;
  const callbacks = useRef({ onRun, withdrawPreview });
  callbacks.current = { onRun, withdrawPreview };
  const [errors, setErrors] = useState<{ session: typeof session; items: Record<string, Error> }>({ session, items: {} });
  const [finalReads, setFinalReads] = useState<{ session: typeof session; items: Record<string, string> }>({ session, items: {} });
  const pendingKey = runs.filter(pending).map(run => run.id).join(",");
  const live = (entry: Entry) => current.current === session && permitted.current && !session.stopped && !entry.controller.signal.aborted;
  function stop(id: string) {
    const entry = session.entries.get(id);
    if (entry) { entry.controller.abort(); clearTimeout(entry.timer); session.entries.delete(id); }
  }
  function errorFor(id: string, error?: Error) {
    if (current.current !== session || session.stopped) return;
    setErrors(previous => {
      const items = previous.session === session ? previous.items : {};
      if (!error && !items[id]) return previous;
      const next = { ...items };
      if (error) next[id] = error; else delete next[id];
      return { session, items: next };
    });
  }
  async function read(entry: Entry, full = false, propagate = false): Promise<Run | undefined> {
    if (!live(entry) || entry.reading) return;
    entry.reading = true;
    const started = Date.now();
    let retry = true;
    let finalRead = false;
    try {
      let next: Run | undefined;
      if (!full && !session.legacy) {
        let progress: Progress | null = null;
        try {
          progress = parseRunProgress(await api<string>(`/runs/${encodeURIComponent(entry.run.id)}/events`,
            { response: "text", signal: entry.controller.signal }), entry.run);
        } catch (error) {
          if (!(error instanceof ApiError && [404, 501].includes(error.status))) throw error;
        }
        if (!live(entry)) return;
        if (!progress) session.legacy = true;
        else if (terminal.has(progress.state)) {
          finalRead = true;
          setFinalReads(previous => ({ session, items: {
            ...(previous.session === session ? previous.items : {}), [entry.run.id]: progress.state,
          } }));
          errorFor(entry.run.id);
          callbacks.current.withdrawPreview(entry.run.id);
        }
        else next = { ...entry.run, state: progress.state, phase: progress.phase, attempt: progress.attempt,
          created_at: progress.created_at ?? entry.run.created_at,
          invalidated: progress.invalidated, error_code: progress.error_code ?? null,
          model_snapshot: progress.model_snapshot ?? null,
          question_analysis: progress.invalidated ? undefined : progress.question_analysis,
          answer: null, evidence_diagnostic: undefined, failure_diagnostic: undefined };
      }
      if (!next) next = await get<Run>(`/runs/${encodeURIComponent(entry.run.id)}`, entry.controller.signal);
      if (!live(entry)) return;
      if (next.id !== entry.run.id || next.thread_id !== entry.run.thread_id || next.job_id !== entry.run.job_id)
        throw new Error("答疑响应与当前咨询不一致。");
      entry.run = next;
      session.latest.set(next.id, next);
      callbacks.current.onRun(next);
      errorFor(next.id);
      setFinalReads(previous => {
        if (previous.session !== session || !previous.items[next.id]) return previous;
        const items = { ...previous.items }; delete items[next.id];
        return { session, items };
      });
      retry = pending(next);
      return next;
    } catch (error) {
      if (!live(entry)) return;
      callbacks.current.withdrawPreview(entry.run.id);
      errorFor(entry.run.id, error instanceof Error ? error : new Error(String(error)));
      // Access denials are not a reason to try a different endpoint or keep polling.
      retry = !finalRead && !(error instanceof ApiError && [401, 403, 404].includes(error.status));
      if (!retry) session.paused.add(entry.run.id);
      if (propagate) throw error;
    } finally {
      entry.reading = false;
      if (live(entry) && retry && session.enabled && !session.paused.has(entry.run.id))
        entry.timer = setTimeout(() => void read(entry), Math.max(0, RUN_POLL_MS - (Date.now() - started)));
    }
  }
  function start(run: Run, full = false, propagate = false) {
    stop(run.id);
    const entry: Entry = { run, controller: new AbortController(), reading: false };
    session.entries.set(run.id, entry);
    return read(entry, full, propagate);
  }
  useEffect(() => {
    session.stopped = false;
    return () => { session.stopped = true; for (const id of session.entries.keys()) stop(id); };
  }, [session]);
  useEffect(() => {
    session.enabled = enabled;
    const active = new Map(runs.filter(pending).map(run => [run.id, run]));
    for (const id of session.entries.keys()) if (!enabled || !active.has(id)) stop(id);
    if (!enabled) { session.paused.clear(); return; }
    for (const [id, run] of active) if (!session.entries.has(id) && !session.paused.has(id)) void start(run);
  }, [session, enabled, pendingKey]);
  return {
    errors: errors.session === session ? errors.items : {},
    finalReads: finalReads.session === session ? finalReads.items : {},
    clearErrors() {
      setErrors(previous => previous.session === session && !Object.keys(previous.items).length
        ? previous : { session, items: {} });
      setFinalReads(previous => previous.session === session && !Object.keys(previous.items).length
        ? previous : { session, items: {} });
    },
    pause(id: string) { session.paused.add(id); stop(id); },
    resume(run: Run) {
      session.paused.delete(run.id);
      const latest = session.latest.get(run.id) ?? run;
      if (session.enabled && pending(latest) && !session.entries.has(run.id)) void start(latest);
    },
    readNow(run: Run) {
      session.paused.delete(run.id);
      return start(run, true, true);
    },
  };
}
