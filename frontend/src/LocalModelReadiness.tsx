import { useEffect, useRef, useState } from "react";
import { api, get, query } from "./api";
import { ErrorBox, useApp, useTask } from "./ui";
import type { RetrievalSelection } from "./retrievalProfiles";

export type ModelRuntimeState = {runtime_id: string; process_id: number; scope: string; policy: string; supported: boolean;
  state: string; phase: string; attempts: number; self_tested: boolean; started_at?: string | null;
  completed_at?: string | null; elapsed_ms?: number | null; error_code?: string | null};
type RuntimeResponse = {space_id: string; model_runtime: ModelRuntimeState; can_prepare: boolean;
  retrieval_selection?: RetrievalSelection};
const labels: Record<string,string> = {NOT_LOADED:"待准备", LOADING:"加载与自检中", READY:"推理就绪",
  FAILED:"准备失败", CLOSED:"服务已关闭", NOT_APPLICABLE:"未启用本地预热"};
const phases: Record<string,string> = {waiting_slot:"等待本机模型准备队列", preparing:"准备本地模型",
  loading_embedding:"加载嵌入模型并执行本地自检", loading_reranker:"加载重排模型并执行本地自检",
  self_test_complete:"完成自检", ready:"嵌入与重排自检已通过", failed:"自检未通过", closed:"运行进程正在关闭", idle:"首次查询或点击按钮时准备"};
const errors: Record<string,string> = {LOCAL_MODEL_FILE_MISSING_OR_INVALID:"本地模型文件缺失或格式不正确",
  LOCAL_MODEL_HASH_MISMATCH:"模型文件校验未通过", LOCAL_DEPENDENCY_UNAVAILABLE:"运行依赖未安装完整",
  LOCAL_MEMORY_UNAVAILABLE:"可用内存不足", REQUESTED_MPS_UNAVAILABLE:"当前设备的MPS不可用",
  MODEL_WARMUP_FAILED:"本地模型准备失败，请核对运行环境", MODEL_WARMUP_CLOSED:"模型服务正在关闭",
  WARMUP_EMBEDDING_INVALID:"嵌入模型自检输出无效", WARMUP_RERANK_INVALID:"重排模型自检输出无效"};

export function LocalModelReadiness({initial, selection, queryPending, onState, onReady}: {
  initial: ModelRuntimeState; selection?: RetrievalSelection; queryPending: boolean;
  onState: (state: ModelRuntimeState) => void; onReady: () => void;
}) {
  const app = useApp();
  const [value,setValue] = useState<RuntimeResponse>({space_id:app.space.id,model_runtime:initial,can_prepare:false});
  const [error,setError] = useState<Error>();
  const [revision,setRevision] = useState(0);
  const task = useTask();
  const callbacks = useRef({onState,onReady}); callbacks.current = {onState,onReady};
  const previous = useRef(initial);
  const actions = useRef(new Set<AbortController>());
  useEffect(() => () => { for (const controller of actions.current) controller.abort(); actions.current.clear(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const response = await get<RuntimeResponse>(`/retrieval/model-runtime?${query({space_id:app.space.id,
          ...(selection ? {profile_id:selection.profile_id} : {})})}`,controller.signal);
        if (controller.signal.aborted) return;
        if (response.space_id !== app.space.id || !response.model_runtime || typeof response.can_prepare !== "boolean"
          || (selection && (response.retrieval_selection?.profile_id !== selection.profile_id
            || response.retrieval_selection?.fingerprint !== selection.fingerprint))) throw new Error("模型准备状态与当前方案不一致。");
        const current = response.model_runtime;
        setValue(response); setError(undefined); callbacks.current.onState(current);
        if (current.state === "READY" && current.self_tested === true
          && (previous.current.state !== "READY" || previous.current.runtime_id !== current.runtime_id)) callbacks.current.onReady();
        previous.current = current;
        if (current.state === "LOADING") timer = setTimeout(() => void poll(),1000);
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason : new Error("模型状态读取失败"));
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  },[app.me.id,app.space.id,selection?.profile_id,selection?.fingerprint,queryPending,revision]);
  function prepare() {
    if (!value.can_prepare || task.busy || value.model_runtime.state === "LOADING" || value.model_runtime.state === "READY") return;
    void task.run(async () => {
      const controller = new AbortController(); actions.current.add(controller);
      try {
        await api("/retrieval/model-warmup",{method:"POST",signal:controller.signal,body:{space_id:app.space.id,
          retry:value.model_runtime.state === "FAILED", ...(selection ? {retrieval_selection:selection} : {})}});
        if (!controller.signal.aborted) setRevision(v=>v+1);
      } finally { actions.current.delete(controller); }
    });
  }
  const runtime = value.model_runtime;
  const ready = runtime.state === "READY" && runtime.self_tested === true;
  return <section className="retrieval-update" aria-label="本地模型准备" data-model-runtime={error ? "UNKNOWN" : runtime.state}>
    <p role="status"><strong>本地推理：{error ? "状态读取失败" : ready ? "推理就绪" : runtime.state === "READY" ? "自检状态待核对" : labels[runtime.state] ?? "状态未知"}</strong>
      {!error && <> · {phases[runtime.phase] ?? runtime.phase}</>}</p>
    {runtime.state === "LOADING" && <p className="retrieval-meta">查询会等待同一个准备任务，准备完成后继续；不会重复加载或跳过重排。</p>}
    {runtime.state === "FAILED" && !error && <p className="retrieval-failure">{errors[runtime.error_code ?? ""] ?? "本地模型自检失败"} · {runtime.error_code}</p>}
    <p className="retrieval-meta">当前进程 {runtime.process_id} · {runtime.policy === "auto" ? "默认方案自动预热，其他方案按需" : "按需预热"} · 准备尝试 {runtime.attempts} 次。
      自检只使用合成文本，不读取业务资料、不调用收费答疑模型。加载完成后保留至进程退出。</p>
    {value.can_prepare && !ready && <button type="button" onClick={prepare} disabled={task.busy || runtime.state === "LOADING" || !!error}>
      {task.busy ? "正在提交…" : runtime.state === "FAILED" ? "重试预热" : "预热本地模型"}</button>}
    <ErrorBox error={error} retry={()=>setRevision(v=>v+1)} /><ErrorBox error={task.error} />
  </section>;
}
