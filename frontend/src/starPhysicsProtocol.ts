import type { GraphRenderBudget, WikiGraphData } from "./knowledgeTypes";
import type { StarBounds } from "./starGraphPhysics";
import type { StarPhysics } from "./starPhysicsSettings";

export type StarPhysicsMode = "running" | "paused" | "quiet";

/** init/configure must advance generation; all other commands must match it.
 * Hidden, blurred and offscreen states are represented by mode: 'paused'. */
export type StarPhysicsCommand = { generation: number } & (
  | { type: "init"; graph: WikiGraphData; physics: StarPhysics; focusId?: string; budget?: GraphRenderBudget; mode: StarPhysicsMode }
  | { type: "mode"; mode: StarPhysicsMode }
  | { type: "pin"; id: string; x: number; y: number }
  | { type: "release"; id: string; reheat: boolean }
  | { type: "configure"; physics: StarPhysics }
  | { type: "replay" }
  | { type: "dispose" }
);

export type StarPhysicsFrame = {
  type: "frame";
  generation: number;
  /** Starts at 1 for each accepted generation, including the initial snapshot. */
  sequence: number;
  /** Interleaved x,y in prepareStarGraph(...).nodes order; caller owns this buffer. */
  positions: Float32Array;
  /** Cumulative real d3 ticks for this engine; configure/replay do not reset it. */
  ticks: number;
  /** True only when the current running mode has further cooling work. */
  hot: boolean;
  bounds: StarBounds;
};
export type StarPhysicsError = { type: "error"; generation: number; message: string };
export type StarPhysicsEvent = StarPhysicsFrame | StarPhysicsError;
export type StarPhysicsClock = {
  now(): number;
  setTimeout(callback: () => void, delay: number): unknown;
  clearTimeout(handle: unknown): void;
};
export type StarPhysicsSession = { send(command: StarPhysicsCommand): void; dispose(): void };

export { createStarPhysicsSession } from "./starGraphPhysics";
