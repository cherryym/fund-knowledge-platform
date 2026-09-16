import type { StarPhysicsCommand, StarPhysicsFrame, StarPhysicsMode } from "./starPhysicsProtocol";
import { createStarPhysicsSession } from "./starGraphPhysics";
import type { WikiGraphData, GraphRenderBudget } from "./knowledgeTypes";
import type { StarPhysics } from "./starPhysicsSettings";

type Command = StarPhysicsCommand extends infer T ? T extends { generation: number } ? Omit<T, "generation"> : never : never;
let nextGeneration = 0;
export function createStarPhysicsClient(options: {
  graph: WikiGraphData; physics: StarPhysics; focusId?: string; budget?: GraphRenderBudget;
  initialMode?: StarPhysicsMode;
  onFrame(frame: StarPhysicsFrame): void; onBackend(mode: "worker" | "main-thread"): void;
  workerFactory?: () => Worker;
}) {
  let generation = ++nextGeneration;
  let disposed = false, sequence = -1, worker: Worker | null = null;
  let fallback: ReturnType<typeof createStarPhysicsSession> | null = null;
  let timeout: ReturnType<typeof setTimeout> | undefined;
  let mode: StarPhysicsMode = options.initialMode || "paused";
  let physics = options.physics;
  let latest: Float32Array | null = null;
  const pins = new Map<string, { x: number; y: number }>();
  const init = (): Extract<StarPhysicsCommand, { type: "init" }> => ({ type: "init", generation, graph: options.graph, physics,
    focusId: options.focusId, budget: options.budget, mode });
  const accept = (frame: StarPhysicsFrame) => {
    if (disposed || frame.generation !== generation || !Number.isSafeInteger(frame.sequence) || frame.sequence < 1 || frame.sequence <= sequence) return;
    if (!(frame.positions instanceof Float32Array) || frame.positions.length !== options.graph.nodes.length * 2
      || !frame.positions.every(Number.isFinite)) return;
    sequence = frame.sequence; latest = frame.positions;
    if (timeout !== undefined) clearTimeout(timeout); timeout = undefined;
    options.onFrame(frame);
  };
  function stopWorker() {
    if (timeout !== undefined) clearTimeout(timeout); timeout = undefined;
    if (worker) { worker.onmessage = null; worker.onerror = null; worker.onmessageerror = null; worker.terminate(); worker = null; }
  }
  function useFallback() {
    if (disposed || fallback) return;
    stopWorker(); sequence = -1;
    const positions = latest;
    let seeding = Boolean(positions);
    fallback = createStarPhysicsSession(event => { if (event.type === "frame" && !seeding) accept(event); });
    fallback.send({ ...init(), mode: positions ? "paused" : mode });
    // Preserve the latest valid graph when a worker fails; do not jump to seeds.
    if (positions) options.graph.nodes.forEach((node, i) => {
      fallback!.send({ type: "pin", generation, id: node.id, x: positions[2 * i], y: positions[2 * i + 1] });
      fallback!.send({ type: "release", generation, id: node.id, reheat: false });
    });
    for (const [id, point] of pins) fallback.send({ type: "pin", generation, id, ...point });
    seeding = false; fallback.send({ type: "mode", generation, mode });
    options.onBackend("main-thread");
  }
  try {
    worker = options.workerFactory ? options.workerFactory() : new Worker(new URL("./starPhysics.worker.ts", import.meta.url), { type: "module", name: "FundKB Graph Physics" });
    const activeWorker = worker;
    worker.onmessage = event => {
      if (worker !== activeWorker || fallback || event.data?.generation !== generation || disposed) return;
      if (event.data.type === "error") useFallback(); else if (event.data.type === "frame") accept(event.data);
    };
    worker.onerror = event => { event.preventDefault(); useFallback(); };
    worker.onmessageerror = () => useFallback();
    timeout = setTimeout(useFallback, 2000);
    worker.postMessage(init()); options.onBackend("worker");
  } catch { useFallback(); }
  return {
    send(command: Command) {
      if (disposed) return;
      if (command.type === "mode") mode = command.mode;
      if (command.type === "configure") { physics = command.physics; generation = ++nextGeneration; sequence = -1; }
      if (command.type === "pin") pins.set(command.id, { x: command.x, y: command.y });
      if (command.type === "release") pins.delete(command.id);
      const message = { ...command, generation } as StarPhysicsCommand;
      if (fallback) fallback.send(message);
      else try { worker?.postMessage(message); } catch { useFallback(); }
    },
    dispose() { if (disposed) return; disposed = true; stopWorker(); fallback?.dispose(); fallback = null; pins.clear(); latest = null; },
  };
}
