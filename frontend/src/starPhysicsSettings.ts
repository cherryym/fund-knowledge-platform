/** Pure settings shared by the physics worker and the theme/UI adapter. */
export type StarPhysics = { nodeSize: number; edgeWidth: number; center: number; repel: number; distance: number; linkStrength: number; damping: number };

// Keep these defaults and bounds compatible with the existing saved theme v1.
export const STAR_PHYSICS_LIMITS = {
  nodeSize: [0.65, 1.8, 0.05], edgeWidth: [0.4, 2.4, 0.05], center: [0.02, 0.2, 0.005],
  repel: [35, 320, 5], distance: [45, 180, 5],
  linkStrength: [0.02, 0.8, 0.01], damping: [0.15, 0.85, 0.01],
} as const;
export const STAR_DEFAULT_PHYSICS: StarPhysics = { nodeSize: 1, edgeWidth: 0.85, center: 0.075, repel: 135, distance: 100, linkStrength: 0.22, damping: 0.52 };

export function normalizeStarPhysics(value: unknown): StarPhysics {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  const result = { ...STAR_DEFAULT_PHYSICS };
  for (const key of Object.keys(STAR_PHYSICS_LIMITS) as (keyof StarPhysics)[]) {
    const candidate = source[key];
    if (typeof candidate === "number" && Number.isFinite(candidate))
      result[key] = Math.max(STAR_PHYSICS_LIMITS[key][0], Math.min(STAR_PHYSICS_LIMITS[key][1], candidate));
  }
  return result;
}
