import type { Resource, Version } from "./types";

/** Status must describe the same version number that the list displays. */
export function resourceDisplayState(resource: Resource, latest?: Pick<Version, "id" | "state">): string {
  if (resource.deleted_at) return "DELETED";
  if (resource.suspended) return "SUSPENDED";
  if (latest && latest.id !== resource.active_version_id) return latest.state;
  if (resource.active_version_id) return "PUBLISHED";
  return latest?.state ?? "未确认";
}
