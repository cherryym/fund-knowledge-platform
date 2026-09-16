import type { Kind, Resource, Version } from "./types";

export type WikiPage = {
  id: string; name: string; kind: Kind; knowledge_type: string;
  category: string; tags: string[]; version_id: string | null; version_no: number | null;
  state: string; excerpt: string; updated_at: string | null; link_count: number; backlink_count: number;
  source_mode?: "published" | "unverified_draft";
  node_role?: WikiNodeRole | null;
};
export type WikiCategory = { path: string; name: string; count: number };
export type WikiTaxonomy = { space_id: string; revision: number; categories: WikiCategory[]; truncated: boolean };
export type CategoryOperation = "create" | "rename" | "delete";

export function categoryPath(value: string): string {
  const text = value.normalize("NFKC").trim();
  const parts = text.split("/").map((part) => part.trim());
  if (!text || text.length > 200 || parts.length > 8 || parts.some((part) => !part || part === "." || part === "..")
    || /[<>\u0000-\u001f\\]/.test(text)) throw new Error("分类路径最多 200 字、8 层；请用 / 分隔，不含空层、特殊标记或 .、..。");
  return parts.join("/");
}

export function categoryDeletionReason(taxonomy: WikiTaxonomy, path: string): string | null {
  const category = taxonomy.categories.find((item) => item.path === path);
  if (!category) return "该分类已变化或不在当前可见目录中，请刷新目录。";
  if (category.count > 0) return "该分类含有知识页，请先调整知识归类；不能连同内容一起删除。";
  if (taxonomy.categories.some((item) => item.path.startsWith(`${path}/`))) return "该分类包含子分类，请先整理子分类。";
  return null; // The server still checks retained documents, hidden resources and truncated descendants.
}

export function categoryWrite(operation: CategoryOperation, spaceId: string, path: string,
  newPath: string, taxonomy: WikiTaxonomy) {
  const target = categoryPath(path);
  if (operation === "create") return { method: "POST", body: { space_id: spaceId, path: target } };
  if (!taxonomy.categories.some((item) => item.path === target)) throw new Error("分类目录已变化，请先刷新。");
  if (operation === "delete") {
    const reason = categoryDeletionReason(taxonomy, target);
    if (reason) throw new Error(reason);
    return { method: "DELETE", body: { space_id: spaceId, path: target }, revision: taxonomy.revision };
  }
  const renamed = categoryPath(newPath);
  if (renamed === target || renamed.startsWith(`${target}/`)) throw new Error("新分类不能与原路径相同，也不能移入自身子目录。");
  return { method: "PATCH", body: { space_id: spaceId, path: target, new_path: renamed }, revision: taxonomy.revision };
}
export type WikiWorkspaceData = {
  pages: WikiPage[]; categories: WikiCategory[]; tags: { name: string; count: number }[];
  stats: Record<string, number>; mode: "wiki"; truncated: boolean;
  next_cursor?: string | null;
  reader?: { resource: Resource; version: Version; links: WikiLinksData };
  readers?: Record<string, { resource: Resource; version: Version; links: WikiLinksData }>;
  graph?: WikiGraphData;
  maintenance?: Record<string, {aliases: string[]; canonical_key?: string | null; canonical_resource_id: string | null}>;
};
export type WikiVerification = {
  verification_status?: string; evidence_count?: number; explanation?: string | null;
  citation_precision?: "DOCUMENT" | null;
};
export type WikiLink = {
  id: string; name: string; kind: Kind; version_id: string | null; relation_type: string; status: string;
} & WikiVerification;
export type WikiLinksData = {
  outgoing: WikiLink[]; incoming: WikiLink[]; sources: WikiLink[]; unresolved: ({ title: string } & WikiVerification)[];
  truncated?: boolean;
};
export type WikiNode = {
  id: string; label: string; kind: string; knowledge_type: string; category: string;
  state: string; version_id: string | null;
  source_mode?: "published" | "unverified_draft";
  node_role?: WikiNodeRole | null;
};
export type WikiEdge = {
  id: string; source: string; target: string; type: string;
  origin: "citation" | "relation" | "wikilink" | "semantic"; state: string;
} & WikiVerification;
export type WikiGraphData = {
  nodes: WikiNode[]; edges: WikiEdge[]; truncated: boolean; total_visible_nodes: number; matched_visible_nodes?: number;
  total_visible_edges?: number; matched_visible_edges?: number; semantic_relation_count?: number;
  node_role_counts?: Partial<Record<WikiNodeRole | "topic_or_source", number>>;
};
export type WikiSelection = { id: string; version_id: string | null; name: string };
export type WikiModelSelection = { connection_id: string; model_id: string };
export type WikiModelOption = WikiModelSelection & {
  connection_name: string; model_name: string; kind: string; configured: boolean; allow_document_transfer: boolean;
};
export type WikiBuildRequest = {
  space_id: string; source_resource_ids: string[]; model_selection: WikiModelSelection;
  max_pages: number; consent: true;
  source_mode?: "published" | "unverified_draft";
  granularity?: "topic" | "knowledge_points";
  compilation_type?: WikiCompilationType;
};
export type WikiCompilationType = "topic" | "atomic_rule" | "scenario" | "sop";
export type WikiLaunch = { build: boolean; sourceId?: string; focusId?: string; graph?: boolean; error?: string };

/** Hash-only app navigation. No URL fetch, external destination or arbitrary resource path. */
export function wikiLaunch(hash: string): WikiLaunch {
  const [route, params = ""] = hash.split("?", 2);
  if (route !== "#/knowledge") return { build: false };
  const query = new URLSearchParams(params);
  const isId = (value: string) => /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value);
  const source = query.get("build_source");
  if (source !== null) {
    if (query.getAll("build_source").length !== 1 || !isId(source))
      return { build: true, error: "来源标识无效，请从当前空间的文档列表重新选择。" };
    return { build: true, sourceId: source.toLowerCase() };
  }
  if (query.get("graph") === "1" && query.has("focus")) {
    const focus = query.get("focus")!;
    if (!isId(focus) || query.getAll("focus").length !== 1) return { build: false, error: "关联图谱的内容标识无效，请从来源文档重新打开。" };
    return { build: false, graph: true, focusId: focus.toLowerCase() };
  }
  return { build: query.get("build") === "1" };
}
export type CategoryBranch = { path: string; name: string; count: number | null; children: CategoryBranch[] };

export const WIKI_SOURCE_LIMIT = 8;
export const WIKI_LOCATOR_LIMIT = 20;
export const WIKI_QUERY_LIMIT = 300;
export const wikiNodeRoleLabels = {
  concept: "概念", asset: "资产", rule: "规则", method: "方法", parameter: "参数",
  condition: "条件", exception: "例外", procedure: "流程",
} as const;
export type WikiNodeRole = keyof typeof wikiNodeRoleLabels;

/** Validate before requests; URLSearchParams encodes the accepted text separately. */
export function wikiSearchQuery(value = ""): string {
  const text = value.trim();
  if (text.length > WIKI_QUERY_LIMIT || /[\u0000-\u001f\u007f]/.test(text))
    throw new Error(`搜索词最多 ${WIKI_QUERY_LIMIT} 字，不能含控制字符。`);
  return text;
}

export function wikiGraphParameters({ spaceId, focusId, depth = 1, category, search, role = "" }: {
  spaceId: string; focusId?: string; depth?: number; category?: string; search?: string; role?: string;
}) {
  if (role && !Object.hasOwn(wikiNodeRoleLabels, role)) throw new Error("节点角色无效，请重新选择。");
  if (!Number.isInteger(depth) || depth < 0 || depth > 3) throw new Error("局部图谱深度必须为 0–3 的整数。");
  return { space_id: spaceId, focus_id: focusId, depth, category, q: wikiSearchQuery(search),
    node_role: role || undefined };
}
export const wikiTypeLabels: Record<string, string> = {
  source: "来源文档", knowledge: "知识页", wiki: "知识页", document: "来源文档", template: "方案模板",
  term: "术语", faq: "常见问题", rule: "业务规则", sop: "操作规程", scenario: "业务场景", case: "业务案例",
  solution_template: "方案模板",
};
export const wikiRelationLabels: Record<string, string> = {
  WIKI_LINK: "双链", CITES: "引用", EXPLAINS: "解释", APPLIES_TO: "适用于", REQUIRES: "要求",
  EXCEPTION_OF: "例外", DEPENDS_ON: "依赖", SUPERSEDES: "替代", RULE: "规范依据",
  FACT: "业务事实", CASE: "案例", CALCULATION: "计算依据", INTERNAL_OPINION: "内部意见",
};
export const wikiRelationDescriptions = {
  EXPLAINS: "起点解释终点的含义或机制。",
  APPLIES_TO: "起点适用于终点所指的对象或场景。",
  REQUIRES: "起点要求终点所指的前提或事项。",
  EXCEPTION_OF: "起点是终点的一种例外情形。",
  DEPENDS_ON: "起点的成立或执行依赖终点。",
} as const;

export function wikiRelationTitle(type: string, verification?: string, precision?: WikiVerification["citation_precision"]): string {
  if (precision === "DOCUMENT") return "书目关联 · 待核验（未逐段定位）";
  return `${wikiRelationLabels[type] || type}${verification === "PROPOSED" ? " · 待核验"
    : verification ? ` · 核验状态：${verification}` : ""}`;
}

export function wikiEvidenceText(count?: number, precision?: WikiVerification["citation_precision"]): string {
  if (precision === "DOCUMENT") return "";
  return count !== undefined && Number.isSafeInteger(count) && count >= 0 ? `原文定位 ${count} 处` : "";
}

/** Missing path ancestors are navigation groups, never fabricated graph nodes. Counts remain server counts. */
export function categoryTree(categories: WikiCategory[]): CategoryBranch[] {
  const roots: CategoryBranch[] = [];
  const byPath = new Map<string, CategoryBranch>();
  for (const category of categories) {
    const segments = category.path.split("/").map((item) => item.trim()).filter(Boolean);
    let siblings = roots;
    let path = "";
    for (const [index, name] of segments.entries()) {
      path = path ? `${path}/${name}` : name;
      let branch = byPath.get(path);
      if (!branch) {
        branch = { path, name, count: null, children: [] };
        byPath.set(path, branch);
        siblings.push(branch);
      }
      if (index === segments.length - 1) {
        branch.name = category.name || name;
        branch.count = category.count;
      }
      siblings = branch.children;
    }
  }
  function sort(items: CategoryBranch[]) {
    items.sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
    items.forEach((item) => sort(item.children));
  }
  sort(roots);
  return roots;
}

export function graphGroup(node: Pick<WikiNode, "kind" | "knowledge_type">): "source" | "wiki" | "term" | "template" {
  if (node.kind === "document" || node.kind === "source") return "source";
  if (node.knowledge_type === "term" || node.kind === "term") return "term";
  if (node.kind === "template" || node.knowledge_type === "solution_template") return "template";
  return "wiki";
}

/** Legacy budget names are retained for caller compatibility, not display caps. */
export type GraphRenderBudget = "standard" | "synthetic-300";
/** Keep every valid returned node/edge. Deduplicate and discard dangling links;
 * old optional limit/budget arguments must never truncate the displayed data. */
export function boundedGraph(data: WikiGraphData, _limit?: number, _budget?: GraphRenderBudget): WikiGraphData {
  const nodeIds = new Set<string>();
  const nodes = data.nodes.filter((node) => {
    if (!node || !node.id || nodeIds.has(node.id)) return false;
    nodeIds.add(node.id);
    return true;
  });
  const edgeIds = new Set<string>();
  const edges = data.edges.filter((edge) => {
    if (!edge || !edge.id || edgeIds.has(edge.id) || !nodeIds.has(edge.source) || !nodeIds.has(edge.target)) return false;
    edgeIds.add(edge.id);
    return true;
  });
  return {
    ...data, nodes, edges,
    truncated: data.truncated || nodes.length !== data.nodes.length || edges.length !== data.edges.length,
  };
}

export function graphElements(data: WikiGraphData) {
  return [
    ...data.nodes.map((node) => ({ group: "nodes" as const, data: { ...node, id: `node:${node.id}`, resource_id: node.id,
      group: graphGroup(node) } })),
    ...data.edges.map((edge) => ({ group: "edges" as const, data: { ...edge, id: `edge:${edge.id}`,
      source: `node:${edge.source}`, target: `node:${edge.target}` } })),
  ];
}

export function buildRequest(spaceId: string, sources: string[], model: WikiModelOption | undefined,
  maxPages: number, consent: boolean, sourceMode: "published" | "unverified_draft" = "published",
  granularity: "topic" | "knowledge_points" = "topic", compilationType?: WikiCompilationType): WikiBuildRequest {
  const ids = [...new Set(sources)];
  if (!ids.length || ids.length > WIKI_SOURCE_LIMIT) throw new Error(`请选择 1–${WIKI_SOURCE_LIMIT} 份来源文档。`);
  if (!["published", "unverified_draft"].includes(sourceMode)) throw new Error("来源构建模式无效。");
  if (!["topic", "knowledge_points"].includes(granularity)) throw new Error("生成粒度无效。");
  if (granularity === "knowledge_points" && sourceMode !== "unverified_draft")
    throw new Error("知识点与语义关系仅支持待核验构建模式。");
  if (!model?.configured) throw new Error("请选择已配置的模型连接；未配置服务不能构建 Wiki。");
  if (!model.allow_document_transfer) throw new Error("该模型连接尚未获准接收文档内容，请先由管理员确认。");
  if (!Number.isInteger(maxPages) || maxPages < 1 || maxPages > 12) throw new Error("生成页数必须是 1–12 的整数。");
  if (!consent) throw new Error("请明确确认本次向所选模型发送文档内容。");
  if (compilationType && !["topic", "atomic_rule", "scenario", "sop"].includes(compilationType))
    throw new Error("请选择有效的知识编译类型。");
  return { space_id: spaceId, source_resource_ids: ids,
    model_selection: { connection_id: model.connection_id, model_id: model.model_id }, max_pages: maxPages, consent: true,
    ...(sourceMode === "unverified_draft" ? {source_mode: sourceMode} : {}),
    ...(granularity === "knowledge_points" ? {granularity} : {}),
    ...(compilationType ? {compilation_type: compilationType} : {}) };
}
