import { createContext, useContext } from "react";
import type { Me, Resource, Space } from "./types";

export type Navigation = "documents" | "knowledge" | "assistant" | "templates" | "tasks" | "trash" | "cases" | "models" | "settings";
export type AppContextType = {
  me: Me; space: Space; refresh: number; bump: () => void;
  navigate: (page: Navigation) => void; notify: (message: string) => void;
  openResource: (resource: Resource, tab?: string, versionId?: string, blockId?: string) => void;
  openVersion: (versionId: string, blockId?: string) => void;
  ask: (resource?: Resource) => void;
  openWikiTitle?: (title: string) => Promise<void> | void;
  setNavigationGuard?: (guard: (() => boolean) | null) => void;
  selectSpace?: (id: string) => void;
};

// Keep the context identity outside frequently hot-updated visual components.
// Motion/UI updates must not give consumers a different context than the provider.
export const AppContext = createContext<AppContextType | null>(null);
export function useApp() {
  const value = useContext(AppContext);
  if (!value) throw new Error("工作空间尚未就绪");
  return value;
}
