import { lazy, Suspense, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  ArrowClockwise, ArrowLeft, ArrowRight, BookOpen, CaretDown, CaretRight, FileText,
  FolderSimple, Graph, Link, MagnifyingGlass, PencilSimple, Plus, Sparkle, Tag, Trash, X,
} from "@phosphor-icons/react";
import { api, get, query } from "./api";
import { BlockView } from "./BlockView";
import { DocumentCanvas } from "./DocumentCanvas";
import { ModelPicker } from "./ModelPicker";
import { WikiCompilationControls } from "./WikiCompilationControls";
import { WikiBuildCoverage } from "./WikiBuildCoverage";
import { useListFlip } from "./useListFlip";
import { WikiNavigationResizeHandle } from "./WikiNavigationResizeHandle";
import { useWikiSnapshot } from "./useWikiSnapshot";
import { readWikiView, writeWikiView } from "./wikiSessionCache";
import { observeWikiReady, wikiOpenTime } from "./wikiPerformance";
import type { Job, Page, Resource, Version } from "./types";
import { AppContext, Badge, Empty, ErrorBox, Field, Loading, Modal, Motion, Notice, dateText, labels, useApp, useLoad, useTask } from "./ui";
import {
  boundedGraph, buildRequest, categoryDeletionReason, categoryPath, categoryTree, categoryWrite, wikiLaunch,
  wikiEvidenceText, wikiGraphParameters, wikiNodeRoleLabels, wikiRelationDescriptions, wikiRelationLabels, wikiRelationTitle,
  wikiSearchQuery, wikiTypeLabels, WIKI_QUERY_LIMIT, WIKI_SOURCE_LIMIT,
  type CategoryBranch, type CategoryOperation, type WikiTaxonomy, type WikiGraphData, type WikiLink, type WikiLinksData, type WikiModelOption,
  type WikiModelSelection, type WikiNode, type WikiSelection, type WikiVerification, type WikiWorkspaceData, type WikiCompilationType,
} from "./knowledgeTypes";
import "./knowledge-workspace.css";

const CreateResourceDialog = lazy(() => import("./ResourcesPage").then(m => ({default:m.CreateResourceDialog})));
const KnowledgeGraph = lazy(() => import("./KnowledgeGraph").then(m => ({default:m.KnowledgeGraph})));
const WikiMaintenanceDialog = lazy(() => import("./WikiMaintenanceDialog").then(m => ({default:m.WikiMaintenanceDialog})));

export function WikiCategoryDialog({ operation, path, close, saved }: {
  operation: CategoryOperation; path: string; close: () => void;
  saved: (result: WikiTaxonomy, operation: CategoryOperation, oldPath: string, newPath: string) => void;
}) {
  const app = useApp();
  const [value, setValue] = useState(operation === "create" ? (path ? `${path}/` : "") : path);
  const [confirmed, setConfirmed] = useState(false);
  const task = useTask();
  const taxonomy = useLoad((signal) => get<WikiTaxonomy>(`/wiki/taxonomy?${query({ space_id: app.space.id })}`, signal), [app.space.id]);
  const title = { create: "新建知识分类", rename: "重命名或移动分类", delete: "删除空分类" }[operation];
  const blocked = operation === "delete" && taxonomy.data ? categoryDeletionReason(taxonomy.data, path) : null;
  return <Modal title={title} close={close} busy={task.busy}>
    <form className="wiki-category-form" onSubmit={(event) => {
      event.preventDefault();
      task.run(async () => {
        if (!taxonomy.data) throw new Error("请先读取最新分类目录。");
        if (operation === "delete" && !confirmed) throw new Error("请确认仅删除此空分类。");
        const request = categoryWrite(operation, app.space.id, operation === "create" ? value : path, value, taxonomy.data);
        const result = await api<WikiTaxonomy>("/wiki/categories", request);
        if (result?.space_id !== app.space.id || !Number.isInteger(result.revision) || !Array.isArray(result.categories))
          throw new Error("服务未返回有效分类目录，请重新读取后确认操作结果。");
        saved(result, operation, path, operation === "delete" ? "" : categoryPath(value));
        close();
      });
    }}>
      <div className="modal-body form-stack">
        {taxonomy.loading && <Loading label="正在读取分类目录版本…" />}
        <ErrorBox error={taxonomy.error} retry={taxonomy.reload} />
        {operation !== "create" && <p className="wiki-category-current">当前分类：<strong>{path}</strong></p>}
        {operation !== "delete" && <Field label={operation === "create" ? "分类路径" : "新分类路径"} hint="使用 / 创建层级，例如 运营 / 估值 / 债券。最多 8 层。">
          <input autoFocus required maxLength={200} value={value} disabled={task.busy}
            onChange={(event) => setValue(event.target.value)} aria-label={operation === "create" ? "新分类路径" : "重命名后的分类路径"} placeholder="运营/估值" />
        </Field>}
        {operation === "rename" && <Notice>重命名会同步更新本分类、子分类与其下资源的归类。服务端会逐项核对管理权限；目标路径已存在时会拒绝合并。</Notice>}
        {operation === "delete" && <>
          <Notice>仅删除空目录。含知识页、子分类、来源文档或回收站保留资源的分类，服务端会阻止删除。</Notice>
          {blocked && <p className="wiki-build-error" role="status">{blocked}</p>}
          <label className="wiki-build-consent"><input type="checkbox" checked={confirmed} disabled={Boolean(blocked) || task.busy}
            onChange={(event) => setConfirmed(event.target.checked)} />确认删除分类「{path}」，不删除其中的任何资源。</label>
        </>}
        {taxonomy.data?.truncated && <Notice>分类目录已截断。操作仍以服务端完整目录和资源检查为准。</Notice>}
        <ErrorBox error={task.error} />
        {task.error && <button type="button" disabled={task.busy} onClick={taxonomy.reload}><ArrowClockwise />读取最新目录后再确认</button>}
      </div>
      <footer className="modal-foot"><button type="button" onClick={close} disabled={task.busy}>取消</button>
        <button type="submit" className={operation === "delete" ? "danger solid" : "primary"}
          disabled={task.busy || !taxonomy.data || taxonomy.loading || Boolean(blocked) || (operation === "delete" && !confirmed)}>
          {operation === "delete" ? <Trash /> : operation === "create" ? <Plus /> : <PencilSimple />}
          {task.busy ? "正在保存…" : operation === "delete" ? "删除空分类" : "保存分类"}</button></footer>
    </form>
  </Modal>;
}

export function WikiAssignCategoryDialog({ resource, close, saved }: { resource: Resource; close: () => void; saved: () => void }) {
  const app = useApp();
  const [value, setValue] = useState(resource.category);
  const touched = useRef(false);
  const task = useTask();
  const loaded = useLoad(async (signal) => {
    const [current, taxonomy] = await Promise.all([
      get<Resource>(`/resources/${encodeURIComponent(resource.id)}`, signal),
      get<WikiTaxonomy>(`/wiki/taxonomy?${query({ space_id: app.space.id })}`, signal),
    ]);
    if (current.space_id !== app.space.id || current.kind === "document") throw new Error("此处只调整当前空间知识页的分类。");
    return { current, taxonomy };
  }, [resource.id, app.space.id]);
  useEffect(() => {
    if (loaded.data && !touched.current) setValue(loaded.data.current.category);
  }, [loaded.data]);
  const listId = `wiki-category-options-${resource.id}`;
  return <Modal title="调整知识归类" close={close} busy={task.busy}>
    <form onSubmit={(event) => {
      event.preventDefault();
      task.run(async () => {
        if (!loaded.data) throw new Error("请先读取最新资源信息。");
        const nextCategory = categoryPath(value);
        const result = await api<Resource>(`/resources/${encodeURIComponent(resource.id)}`, { method: "PATCH",
          revision: loaded.data.current.revision, body: { category: nextCategory } });
        if (result?.id !== resource.id || result.category !== nextCategory)
          throw new Error("资源归类返回结果不一致，请重新读取资源信息确认。");
        saved();
        close();
      });
    }}>
      <div className="modal-body form-stack"><p>{resource.name}</p>
        {loaded.loading && <Loading label="正在读取资源信息…" />}<ErrorBox error={loaded.error} retry={loaded.reload} />
        <Field label="归属分类" hint="可选择已有分类，或输入新的层级路径。保存只更新资源分类，正文仍沿用原版本。">
          <input autoFocus list={listId} aria-label="知识页归属分类" value={value} maxLength={200} required disabled={task.busy}
            onChange={(event) => { touched.current = true; setValue(event.target.value); }} />
          <datalist id={listId}>{loaded.data?.taxonomy.categories.map((item) => <option key={item.path} value={item.path} />)}</datalist>
        </Field>
        {loaded.data && <small>服务端当前分类：{loaded.data.current.category}</small>}
        <ErrorBox error={task.error} />
        {task.error && <button type="button" disabled={task.busy} onClick={loaded.reload}><ArrowClockwise />重新读取资源信息</button>}
      </div>
      <footer className="modal-foot"><button type="button" onClick={close} disabled={task.busy}>取消</button>
        <button type="submit" className="primary" disabled={task.busy || loaded.loading || !loaded.data}><FolderSimple />{task.busy ? "正在保存…" : "保存归类"}</button></footer>
    </form>
  </Modal>;
}

function CategoryTree({ branches, selected, choose }: {
  branches: CategoryBranch[]; selected: string; choose: (path: string) => void;
}) {
  return <ul className="wiki-category-tree">{branches.map((branch) =>
    <CategoryItem key={branch.path} branch={branch} selected={selected} choose={choose} />)}</ul>;
}
function CategoryItem({ branch, selected, choose }: {
  branch: CategoryBranch; selected: string; choose: (path: string) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  return <li>
    <div className={`wiki-category-row ${selected === branch.path ? "active" : ""}`}>
      {branch.children.length > 0 ? <button type="button" className="wiki-category-disclosure"
        aria-label={`${expanded ? "收起" : "展开"}${branch.name}子分类`} aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}>{expanded ? <CaretDown /> : <CaretRight />}</button>
        : <span className="wiki-category-spacer" />}
      <button type="button" aria-current={selected === branch.path ? "true" : undefined} title={branch.path}
        onClick={() => choose(branch.path)}><FolderSimple /><span>{branch.name}</span>
        {branch.count !== null && <small>{branch.count}</small>}</button>
    </div>
    {expanded && branch.children.length > 0 && <CategoryTree branches={branch.children} selected={selected} choose={choose} />}
  </li>;
}

function GraphLocatorResults({ search, category, locate }: { search: string; category?: string; locate: (id: string) => void }) {
  const app = useApp();
  const found = useLoad(async (signal) => get<WikiWorkspaceData>(`/wiki/workspace?${query({
    space_id: app.space.id, q: wikiSearchQuery(search), category,
  })}`, signal), [app.space.id, app.me.id, app.refresh, search, category]);
  const pages = found.data?.pages.filter((page) => page.kind !== "document") || [];
  return <div className="wiki-graph-locator-results" aria-label="节点定位结果" aria-busy={found.loading}>
    {found.loading && <Loading label="正在查找可见知识节点…" />}
    <ErrorBox error={found.error} retry={found.reload} />
    {found.data && <>
      <p role="status">本次找到 {pages.length} 条可见知识
        {found.data.truncated && " · 结果尚未完整"}。</p>
      {!pages.length && <p>当前搜索与分类下没有匹配的可见知识，请调整搜索或分类。</p>}
      <ul>{pages.map((page) => <li key={page.id}><button type="button" onClick={() => locate(page.id)}
        aria-label={`查看「${page.name}」的局部图谱`}><span>{page.name}</span>
        <small>{page.category} · {labels[page.state] || page.state} · 查看局部图谱</small></button></li>)}</ul>
    </>}
  </div>;
}

function GraphLocator({ category, locate }: { category?: string; locate: (id: string) => void }) {
  const [input, setInput] = useState("");
  const [submitted, setSubmitted] = useState("");
  return <div className="wiki-graph-locator">
    <form role="search" aria-label="定位可见知识节点" onSubmit={(event) => { event.preventDefault(); setSubmitted(input.trim()); }}>
      <label className="wiki-search"><MagnifyingGlass /><input aria-label="定位知识节点" placeholder="查找未绘制的知识节点"
        maxLength={WIKI_QUERY_LIMIT} value={input} onChange={(event) => { setInput(event.target.value); setSubmitted(""); }} /></label>
      <button type="submit" disabled={!input.trim()}>查找节点</button>
    </form>
    <p>搜索当前分类内的全部可见知识。选中后打开局部图，并重置图谱搜索、分类和角色以查看相邻内容。</p>
    {submitted && <GraphLocatorResults key={submitted} search={submitted} category={category} locate={locate} />}
  </div>;
}

function WikiRelationAnnotation({ type, relation, contentState, semantic = false }: {
  type: string; relation: WikiVerification; contentState?: string; semantic?: boolean;
}) {
  // Semantic projections and document-only associations remain unverified if PROPOSED is omitted.
  const proposed = semantic || relation.verification_status === "PROPOSED" || relation.citation_precision === "DOCUMENT";
  const explanation = typeof relation.explanation === "string" ? relation.explanation.trim() : "";
  const evidence = wikiEvidenceText(relation.evidence_count, relation.citation_precision);
  return <>
    <small><span className={proposed ? "wiki-proposed-badge" : undefined}>{wikiRelationTitle(type, proposed ? "PROPOSED" : relation.verification_status, relation.citation_precision)}</span>
      {contentState ? ` · 关联内容：${labels[contentState] || contentState}` : ""}{evidence && ` · ${evidence}`}</small>
    {explanation && <small className="wiki-relation-explanation">关系解释：{explanation}</small>}
  </>;
}

function WikiSemanticRelations({ data }: { data: WikiGraphData }) {
  const relations = useMemo(() => data.edges.filter((edge) => edge.origin === "semantic" || edge.verification_status === "PROPOSED" || edge.citation_precision === "DOCUMENT"), [data]);
  const hasBibliography = relations.some((edge) => edge.citation_precision === "DOCUMENT");
  const names = useMemo(() => new Map(data.nodes.map((node) => [node.id, node.label])), [data]);
  if (!relations.length) return null;
  return <details className="wiki-semantic-relations">
    <summary>{hasBibliography ? "关系草稿（含书目关联）" : "语义关系草稿"} · 待核验（{relations.length} 条）</summary>
    <p>{hasBibliography ? "书目关联仅保留笔记与来源文档的关联，未逐段定位，不构成已定位的事实引用。当前图中的待核验关系"
      : "仅展示当前图中模型提出的关系，"}不作为正式依赖或发布关系。解释与原文定位数量不代表已经核验。</p>
    <ul aria-label={hasBibliography ? "待核验关系条目" : "待核验语义关系条目"}>{relations.map((edge) => <li key={edge.id}>
      <strong>{names.get(edge.source)} → {names.get(edge.target)}</strong>
      <WikiRelationAnnotation type={edge.type} relation={edge} semantic={edge.origin === "semantic"} />
    </li>)}</ul>
  </details>;
}

export function GraphQuery({ focusId, category, search, onOpenNode, onFocusChange, initialData }: {
  focusId?: string; category?: string; search?: string; onOpenNode: (node: WikiNode) => void; onFocusChange?: (id: string) => void;
  initialData?: WikiGraphData;
}) {
  const app = useApp();
  const [depth, setDepth] = useState(1);
  const [role, setRole] = useState("");
  const [entry, setEntry] = useState("");
  const consumed = useRef<WikiGraphData | undefined>(undefined);
  const remote = useLoad(async (signal) => {
    if (initialData && !focusId && !role && consumed.current !== initialData) {
      await Promise.resolve();
      if (!signal.aborted) consumed.current = initialData;
      return initialData;
    }
    return get<WikiGraphData>(`/wiki/graph?${query(wikiGraphParameters({
      spaceId: app.space.id, focusId, depth, category, search, role,
    }))}`, signal);
  }, [app.space.id, app.me.id, app.refresh, focusId, depth, category, search, role, initialData]);
  const seed = !focusId && !role ? initialData : undefined;
  const graph = {...remote, data: remote.data ?? (remote.loading ? seed : undefined), loading: remote.loading && !seed};
  const drawing = useMemo(() => graph.data ? boundedGraph(graph.data) : undefined, [graph.data]);
  function locate(id: string) { setRole(""); setEntry(""); onFocusChange?.(id); }
  const count = (value?: number) => value !== undefined && Number.isSafeInteger(value) && value >= 0 ? value : "未知";
  return <div className="wiki-graph-query">
    <div className="wiki-local-depth">
      <span>{focusId ? `局部中心：${drawing?.nodes.find((node) => node.id === focusId)?.label || "当前选中内容"}` : "当前空间的可见知识与来源"}</span>
      <Field label="节点角色"><select aria-label="筛选图谱节点角色" value={role} onChange={(event) => setRole(event.target.value)}>
        <option value="">全部角色</option>{Object.entries(wikiNodeRoleLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></Field>
      {focusId && <Field label="展开深度"><select aria-label="局部图谱展开深度" value={depth} onChange={(event) => setDepth(Number(event.target.value))}>
        <option value={1}>一层关系</option><option value={2}>两层关系</option><option value={3}>三层关系</option>
      </select></Field>}
    </div>
    {onFocusChange && <GraphLocator key={`${app.space.id}:${app.me.id}:${app.refresh}:${category || ""}:${focusId || ""}`} category={category} locate={locate} />}
    {graph.loading && <Loading label="正在读取可见关系…" />}
    <ErrorBox error={graph.error} retry={graph.reload} />
    {drawing && <>
      <p className="wiki-graph-coverage" role="status">当前绘制 {drawing.nodes.length}/{count(drawing.total_visible_nodes)} 节点、{drawing.edges.length} 条线；全空间 {count(drawing.total_visible_edges)} 条关系、{count(drawing.semantic_relation_count)} 条语义草稿
        <span className="wiki-graph-scope">筛选后 {count(drawing.matched_visible_nodes)} 节点、{count(drawing.matched_visible_edges)} 条关系。
          节点分母与全空间总数均仅含当前有权访问的内容。</span></p>
      {(drawing.truncated || (Number.isSafeInteger(drawing.matched_visible_nodes) && drawing.matched_visible_nodes! > drawing.nodes.length)
        || (Number.isSafeInteger(drawing.matched_visible_edges) && drawing.matched_visible_edges! > drawing.edges.length)) &&
        <p className="wiki-truncated" role="status">图谱数据尚未完整，未显示不代表不存在。节点和关系不再设固定显示上限。
          <button type="button" onClick={graph.reload}>重新读取完整图谱</button></p>}
      <div className="wiki-graph-relation-help">
        <span className="wiki-proposed-badge">模型提出的语义关系待核验</span>
        <details><summary>关系类型说明</summary><p>按起点 → 终点理解；关系类型和证据条数不代表关系已经核验。双链表示页面链接，引用表示来源指向。</p>
          <dl>{Object.entries(wikiRelationDescriptions).map(([type, description]) => <div key={type}>
            <dt>{wikiRelationLabels[type]} <code>{type}</code></dt><dd>{description}</dd></div>)}</dl>
        </details>
      </div>
      {onFocusChange && drawing.nodes.length > 0 && <form className="wiki-graph-entry" onSubmit={(event) => {
        event.preventDefault(); if (drawing.nodes.some((node) => node.id === entry)) locate(entry);
      }}>
        <select aria-label="选择已绘制节点进入局部图" value={drawing.nodes.some((node) => node.id === entry) ? entry : ""} onChange={(event) => setEntry(event.target.value)}>
          <option value="">从已绘制节点进入局部图</option>{drawing.nodes.map((node) => <option key={node.id} value={node.id}>{node.label}</option>)}
        </select><button type="submit" disabled={!drawing.nodes.some((node) => node.id === entry)}>查看节点局部图</button>
      </form>}
      <Suspense fallback={<Loading label="正在加载图谱渲染器…" />}>
        <KnowledgeGraph data={drawing} focusId={focusId} onOpenNode={onOpenNode} preferenceKey={app.space.id}
          heightPreferenceKey={JSON.stringify([app.me.id, app.space.id, focusId ? "local" : "global"])}
          label={focusId ? "当前内容的局部关系图谱" : "知识空间全局图谱"} />
      </Suspense>
      <WikiSemanticRelations key={`${focusId || ""}:${category || ""}:${search || ""}:${role}`} data={drawing} />
    </>}
  </div>;
}

function WikiConnections({ id, open, resolve, initialData }: {
  id: string; open: (link: WikiLink) => void; resolve: (title: string) => void;
  initialData?: WikiLinksData;
}) {
  const app = useApp();
  const consumed = useRef<WikiLinksData | undefined>(undefined);
  const remote = useLoad(async (signal) => {
    if (initialData && consumed.current !== initialData) {
      await Promise.resolve();
      if (!signal.aborted) consumed.current = initialData;
      return initialData;
    }
    return get<WikiLinksData>(`/wiki/pages/${encodeURIComponent(id)}/links`, signal);
  }, [id, app.space.id, app.refresh, initialData]);
  const links = {...remote, data: remote.data ?? (remote.loading ? initialData : undefined),
    loading: remote.loading && !initialData};
  const visibleLinks = links.data ? [...links.data.outgoing, ...links.data.incoming, ...links.data.sources] : [];
  const hasBibliography = visibleLinks.some((link) => link.citation_precision === "DOCUMENT");
  return <aside className="wiki-connections" aria-label="当前知识页的链接与来源">
    <div className="wiki-section-title"><Link /><h3>链接与依据</h3><small>仅展示当前可访问内容</small></div>
    {links.loading && <Loading label="正在核对链接…" />}
    <ErrorBox error={links.error} retry={links.reload} />
    {links.data?.truncated && <Notice>链接列表已截断；此处仅展示本次返回的部分关系，不能据此判断其他链接不存在。</Notice>}
    {(hasBibliography || visibleLinks.some((link) => link.verification_status === "PROPOSED")) &&
      <p className="wiki-relation-draft-note">{hasBibliography
        ? "书目关联未逐段定位，不构成已定位的事实引用；待核验关系不作为正式依赖或发布关系。"
        : "待核验语义关系仅为草稿投影，不作为正式依赖或发布关系。"}</p>}
    {links.data && <div className="wiki-connection-grid">
      {([ ["sources", "来源依据"], ["outgoing", "出链"], ["incoming", "反向链接"] ] as const).map(([key, title]) =>
        <details key={key} open><summary>{title}<span>{links.data![key].length}</span></summary>
          {links.data![key].length ? <ul>{links.data![key].map((link, index) => <li key={`${link.id}:${link.version_id}:${index}`}>
            <button type="button" onClick={() => open(link)}><span>{link.kind === "document" ? <FileText /> : <BookOpen />}{link.name}</span>
              <WikiRelationAnnotation type={link.relation_type} relation={link} contentState={link.status} /></button>
          </li>)}</ul> : <p>当前没有可见{title}。</p>}
        </details>)}
      <details open><summary>未解析链接<span>{links.data.unresolved.length}</span></summary>
        {links.data.unresolved.length ? <><p>可能尚未建立、名称有歧义，或当前无访问权限。</p>
          <ul>{links.data.unresolved.map((link, index) => <li key={`${link.title}:${index}`}>
            <button type="button" className="wiki-unresolved-link" onClick={() => resolve(link.title)}>[[{link.title}]]
              <WikiRelationAnnotation type="重新解析" relation={link} /></button>
          </li>)}</ul></> : <p>当前没有未解析链接。</p>}
      </details>
    </div>}
  </aside>;
}

function WikiReader({ selected, back, canBack, open, openNode, resolve, initialData }: {
  selected: WikiSelection; back: () => void; canBack: boolean;
  open: (link: WikiLink) => void; openNode: (node: WikiNode) => void; resolve: (title: string) => void;
  initialData?: WikiWorkspaceData["reader"];
}) {
  const app = useApp();
  const [tab, setTab] = useState<"read" | "graph">("read");
  const [assignCategory, setAssignCategory] = useState(false);
  const resolver = useRef(resolve);
  resolver.current = resolve;
  const readingApp = useMemo(() => ({ ...app, openWikiTitle: (title: string) => resolver.current(title) }), [app]);
  const consumed = useRef<WikiWorkspaceData["reader"]>(undefined);
  const validInitial = initialData?.resource.id === selected.id && initialData.version.resource_id === selected.id
    && initialData.version.id === selected.version_id ? initialData : undefined;
  const remote = useLoad(async (signal) => {
    if (initialData && consumed.current !== initialData && initialData.resource.id === selected.id
      && initialData.version.resource_id === selected.id && initialData.version.id === selected.version_id) {
      await Promise.resolve();
      if (!signal.aborted) consumed.current = initialData;
      return initialData;
    }
    // A list-supplied version can be read in parallel; both endpoints authorize
    // independently and their association is checked before displaying either.
    const [resource, knownVersion] = await Promise.all([
      get<Resource>(`/resources/${encodeURIComponent(selected.id)}`, signal),
      selected.version_id ? get<Version>(`/versions/${encodeURIComponent(selected.version_id)}`, signal) : Promise.resolve(null),
    ]);
    const versionId = selected.version_id || resource.active_version_id;
    if (!versionId) return { resource, version: null, links: undefined as WikiLinksData | undefined };
    const version = knownVersion || await get<Version>(`/versions/${encodeURIComponent(versionId)}`, signal);
    if (version.resource_id !== resource.id) throw new Error("知识版本归属不一致，请重新读取。");
    return { resource, version, links: undefined as WikiLinksData | undefined };
  }, [selected.id, selected.version_id, app.refresh, initialData]);
  const loaded = {...remote, data: remote.data ?? (remote.loading ? validInitial : undefined),
    loading: remote.loading && !validInitial};
  return <section className="wiki-reader" aria-label="知识页阅读区">
    <div className="wiki-reader-toolbar">
      <button type="button" disabled={!canBack} onClick={back} aria-label="返回上一个知识页"><ArrowLeft /></button>
      <div className="wiki-reader-path"><span>{loaded.data?.resource.category || "知识空间"}</span><strong>{loaded.data?.resource.name || selected.name}</strong></div>
      {loaded.data && <><button type="button" onClick={() => setAssignCategory(true)}><FolderSimple />调整归类</button>
        <button type="button" onClick={() => app.openResource(loaded.data!.resource, "edit", loaded.data!.version?.id)}>
        <PencilSimple />编辑知识</button></>}
    </div>
    {loaded.loading && <Loading label="正在读取知识正文…" />}
    <ErrorBox error={loaded.error} retry={loaded.reload} />
    {loaded.data && <>
      <div className="wiki-reader-meta">
        <div className="wiki-view-switch" role="group" aria-label="知识页视图">
          <button type="button" aria-pressed={tab === "read"} onClick={() => setTab("read")}><BookOpen />阅读</button>
          <button type="button" aria-pressed={tab === "graph"} onClick={() => setTab("graph")}><Graph />局部图谱</button>
        </div>
        {loaded.data.version && <span>v{loaded.data.version.version_no} · {wikiTypeLabels[loaded.data.version.knowledge_type] || "知识页"}
          <Badge value={loaded.data.version.state} /></span>}
      </div>
      {loaded.data.resource.suspended && <Notice>本知识页已暂停自动引用。历史正文不代表当前可用于答疑。</Notice>}
      {tab === "graph" ? <GraphQuery focusId={selected.id} onOpenNode={openNode} /> : loaded.data.version ?
        <div className="wiki-reading-body">
          <AppContext.Provider value={readingApp}>
            <DocumentCanvas title={loaded.data.version.title} blocks={loaded.data.version.blocks} editable={false}
              renderBlock={(block, highlighted) => <BlockView block={block} highlighted={highlighted} compact />} />
          </AppContext.Provider>
        </div> : <Empty title="当前没有可读的知识版本" detail="可在版本详情中查看权限允许的草稿、复核进度与发布状态。" />}
      <WikiConnections id={selected.id} open={open} resolve={resolve} initialData={loaded.data.links} />
      {assignCategory && <WikiAssignCategoryDialog resource={loaded.data.resource} close={() => setAssignCategory(false)}
        saved={() => { loaded.reload(); app.bump(); app.notify("知识归类已更新。"); }} />}
    </>}
  </section>;
}

/** All network side effects go through the established API/CSRF/idempotency client. */
export function WikiBuildDialog({ close, initialSourceId, launchError }: { close: () => void; initialSourceId?: string; launchError?: string }) {
  const app = useApp();
  const [selected, setSelected] = useState<string[]>(initialSourceId ? [initialSourceId] : []);
  const [selection, setSelection] = useState<WikiModelSelection | null>(null);
  const [maxPages, setMaxPages] = useState(6);
  const [consent, setConsent] = useState(false);
  const [sourceMode, setSourceMode] = useState<"published" | "unverified_draft">("published");
  const [granularity, setGranularity] = useState<"topic" | "knowledge_points">("topic");
  const [compilationType, setCompilationType] = useState<WikiCompilationType>("topic");
  const [sourceSearch, setSourceSearch] = useState("");
  const [sourceQuery, setSourceQuery] = useState("");
  const [sourceCursors, setSourceCursors] = useState<string[]>([]);
  const [sourceNames, setSourceNames] = useState<Record<string, string>>({});
  const [job, setJob] = useState<Job | null>(null);
  const [pollError, setPollError] = useState<Error>();
  const [pollAgain, setPollAgain] = useState(0);
  const task = useTask();
  const completed = useRef(false);
  const actions = useRef(app);
  actions.current = app;
  useEffect(() => {
    const timer = setTimeout(() => { setSourceQuery(sourceSearch.trim()); setSourceCursors([]); }, 250);
    return () => clearTimeout(timer);
  }, [sourceSearch]);
  const sourceCursor = sourceCursors.at(-1);
  const sources = useLoad((signal) => get<Page<Resource>>(`/resources?${query({ space_id: app.space.id, kind: "document",
    q: sourceQuery, limit: 50, cursor: sourceCursor })}`, signal), [app.space.id, app.refresh, sourceQuery, sourceCursor]);
  const options = useLoad((signal) => get<{ items: WikiModelOption[] }>(`/model-options?${query({ space_id: app.space.id })}`, signal),
    [app.space.id, app.refresh]);
  const initialSource = useLoad(async (signal) => {
    if (!initialSourceId) return null;
    const resource = await get<Resource>(`/resources/${encodeURIComponent(initialSourceId)}`, signal);
    if (resource.kind !== "document" || resource.space_id !== app.space.id
      || !(sourceMode === "unverified_draft" ? resource.latest_state === "DRAFT" : resource.active_version_id)
      || resource.deleted_at || resource.suspended) throw new Error("预选来源尚不可构建；请确认当前空间、来源发布和停用状态。");
    return resource;
  }, [app.space.id, initialSourceId, app.refresh, sourceMode]);
  useEffect(() => {
    if (initialSource.data) setSourceNames((value) => ({ ...value, [initialSource.data!.id]: initialSource.data!.name }));
  }, [initialSource.data]);
  const model = options.data?.items.find((option) => option.connection_id === selection?.connection_id && option.model_id === selection.model_id);
  useEffect(() => { setConsent(false); }, [selection?.connection_id, selection?.model_id, selected.join("|")]);
  useEffect(() => {
    if (!job?.id) return;
    if (["SUCCEEDED", "FAILED", "CANCELLED"].includes(job.state)) {
      if (job.state === "SUCCEEDED" && !completed.current) { completed.current = true; actions.current.bump(); }
      return;
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setPollError(undefined);
    const poll = async () => {
      if (controller.signal.aborted) return;
      try {
        if (document.visibilityState === "hidden") { timer = setTimeout(poll, 2500); return; }
        const fresh = await get<Job>(`/jobs/${encodeURIComponent(job.id)}`, controller.signal);
        if (controller.signal.aborted) return;
        setJob(fresh);
        if (fresh.state === "SUCCEEDED" && !completed.current) {
          completed.current = true;
          actions.current.bump();
          actions.current.notify("Wiki 构建任务完成，新知识仍需人工复核。");
        }
        if (!["SUCCEEDED", "FAILED", "CANCELLED"].includes(fresh.state)) timer = setTimeout(poll, 2500);
      } catch (failure) { if (!controller.signal.aborted) setPollError(failure instanceof Error ? failure : new Error("任务状态读取失败。")); }
    };
    timer = setTimeout(poll, 1200);
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
    // One cancellable poll chain per job; status changes do not restart it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.id, pollAgain]);
  const documents = sources.data?.items.filter((resource) => resource.kind === "document") || [];
  const initialPending = Boolean(initialSourceId && selected.includes(initialSourceId) && (initialSource.loading || initialSource.error));
  const canSubmit = !task.busy && !initialPending && selected.length > 0 && consent && model?.configured && model.allow_document_transfer;
  return <Modal title="从来源文档构建 Wiki" close={close} wide busy={task.busy}>
    {job ? <div className="wiki-build-result" role="status">
      <Badge value={job.state} />
      <h3>{job.state === "SUCCEEDED" ? "Wiki 草稿构建完成" : job.state === "FAILED" ? "Wiki 构建未完成" : job.state === "CANCELLED" ? "构建已取消" : "构建任务已提交"}</h3>
      <p>{job.state === "SUCCEEDED" ? "生成内容保留来源引用，需人工核对后按既有流程复核与发布。" : "以后台任务状态为准；提交任务不表示已调用模型或已生成知识。"}</p>
      <small>任务 {job.id} · {job.stage || "等待执行"}</small>
      {job.error_code && <p className="wiki-build-error">{job.error_code} · 请在任务中心查看原因后再处理。</p>}
      <WikiBuildCoverage job={job} continueBatch={() => {setJob(null); setConsent(false); completed.current = false;}} />
      <ErrorBox error={pollError} retry={() => setPollAgain((value) => value + 1)} />
      <footer className="modal-foot"><button type="button" onClick={() => { close(); app.navigate("tasks"); }}>查看任务详情<ArrowRight /></button>
        <button type="button" className="primary" onClick={close}>返回知识空间</button></footer>
    </div> : <form className="wiki-build-form" onSubmit={(event) => {
      event.preventDefault();
      task.run(async () => {
        const body = buildRequest(app.space.id, selected, model, maxPages, consent, sourceMode, granularity, compilationType);
        const result = await api<Job>("/wiki/builds", { method: "POST", body });
        if (!result?.id || result.kind !== "COMPILE" || !["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"].includes(result.state))
          throw new Error("服务没有返回有效构建任务，未确认构建成功。");
        setJob(result);
      });
    }}>
      <p className="wiki-build-intro">{sourceMode === "unverified_draft"
        ? "从可编辑的来源草稿构建待核验 Wiki 与图谱。原件和来源状态不变，生成内容禁止用于正式答疑或直接发布。"
        : "选择已发布来源，让模型整理有依据的知识页与双链。原始文件和已有人工草稿会保留。"}</p>
      <Field label="构建模式"><select aria-label="构建模式" value={sourceMode} disabled={task.busy} onChange={(event) => {
        setSourceMode(event.target.value as "published" | "unverified_draft"); setSelected([]); setConsent(false);
        setGranularity("topic"); setCompilationType("topic");
      }}><option value="published">已核验来源构建</option><option value="unverified_draft">待核验 Wiki / 图谱（仅浏览编辑）</option></select></Field>
      {launchError && <Notice>{launchError}</Notice>}
      {initialSourceId && selected.includes(initialSourceId) && <ErrorBox error={initialSource.error} retry={initialSource.reload} />}
      <fieldset className="wiki-source-picker" disabled={task.busy}><legend>1. 选择来源文档 <small>{selected.length}/{WIKI_SOURCE_LIMIT}</small></legend>
        <input aria-label="查找构建来源" placeholder="搜索当前空间的来源文档" value={sourceSearch} onChange={(event) => setSourceSearch(event.target.value)} />
        {selected.length > 0 && <div className="wiki-selected-sources" aria-label="已选构建来源">{selected.map((id) =>
          <button type="button" key={id} onClick={() => setSelected((items) => items.filter((item) => item !== id))}>
            {sourceNames[id] || "已选来源"}<X aria-label="移除来源" /></button>)}</div>}
        {sources.loading && <Loading label="正在读取来源文档…" />}<ErrorBox error={sources.error} retry={sources.reload} />
        <div className="wiki-source-options">{documents.map((resource) => {
          const eligible = Boolean((sourceMode === "unverified_draft" ? resource.latest_state === "DRAFT" : resource.active_version_id)
            && !resource.deleted_at && !resource.suspended);
          const checked = selected.includes(resource.id);
          return <label key={resource.id} className={!eligible ? "unavailable" : ""}>
            <input type="checkbox" checked={checked} disabled={!eligible || (!checked && selected.length >= WIKI_SOURCE_LIMIT)}
              onChange={() => { setSourceNames((value) => ({ ...value, [resource.id]: resource.name }));
                setSelected((value) => checked ? value.filter((id) => id !== resource.id) : [...value, resource.id]); }} />
            <FileText /><span>{resource.name}<small>{eligible
              ? sourceMode === "unverified_draft" ? "来源草稿 · 仅整理待核验知识，提交时重验正文与权限" : "已发布 · 提交时再次核验扫描、有效性与权限"
              : sourceMode === "unverified_draft" ? "需要可编辑且未停用的来源草稿" : "需先完成来源复核与发布，且未被停用"}</small></span>
          </label>;
        })}</div>
        {!sources.loading && !sources.error && !documents.length && <p>没有匹配来源；请先在文档中心上传、复核并发布。</p>}
        <div className="wiki-source-pagination"><span>每页最多 50 份来源 · 已选项跨页保留</span>
          <button type="button" disabled={sources.loading || !sourceCursors.length} onClick={() => setSourceCursors((items) => items.slice(0, -1))}>上一页</button>
          <button type="button" disabled={sources.loading || !sources.data?.next_cursor || sourceCursors.includes(sources.data.next_cursor)}
            onClick={() => { if (sources.data?.next_cursor) setSourceCursors((items) => [...items, sources.data!.next_cursor!]); }}>下一页</button>
        </div>
      </fieldset>
      <fieldset disabled={task.busy}><legend>2. 选择模型与生成范围</legend><ModelPicker spaceId={app.space.id} value={selection}
        onChange={setSelection} requireTransfer required disabled={task.busy} />
        <ErrorBox error={options.error} retry={options.reload} />
        <WikiCompilationControls value={compilationType} disabled={task.busy}
          onChange={value => { setCompilationType(value); setConsent(false); }} />
        {sourceMode === "unverified_draft" && <Field label="生成粒度" hint="知识点与语义关系从所选来源提炼，仍需核验；生成知识点数量受下方页数上限约束。">
          <select aria-label="生成粒度" value={granularity} onChange={(event) => {
            setGranularity(event.target.value as "topic" | "knowledge_points"); setConsent(false);
            if (event.target.value === "knowledge_points") setCompilationType("atomic_rule");
          }}><option value="topic">专题概览</option><option value="knowledge_points">知识点与语义关系</option></select>
        </Field>}
        <Field label="最多生成知识页数" hint="1–12 页；这是数量上限，不是必须生成的页数。">
          <input type="number" min={1} max={12} required value={maxPages} onChange={(event) => setMaxPages(Number(event.target.value))} />
        </Field>
      </fieldset>
      <Notice>{model?.kind === "local" ? "文档正文、来源定位和已有知识标题将发送到所选本地模型服务；请确认地址与访问范围。"
        : "构建会把所选文档的正文、来源定位及已有知识标题发送给模型服务。经中转网关调用时，内容也会经过该网关。"}
        连接已配置不代表真实调用已验证；生成内容只进入待复核草稿。</Notice>
      {model && !model.allow_document_transfer && <p className="wiki-build-error" role="status">该连接未获准接收文档，请由管理员先确认外发范围。</p>}
      <label className="wiki-build-consent"><input type="checkbox" checked={consent} disabled={!selection || task.busy}
        onChange={(event) => setConsent(event.target.checked)} />我确认将本次所选来源发送给所选模型，用于生成 Wiki 草稿。</label>
      <ErrorBox error={task.error} />
      <footer className="modal-foot"><button type="button" onClick={close} disabled={task.busy}>取消</button>
        <button type="submit" className="primary" disabled={!canSubmit}><Sparkle />{task.busy ? "正在提交…" : "提交 Wiki 构建"}</button></footer>
    </form>}
  </Modal>;
}

function WorkspaceContent() {
  const listRoot = useRef<HTMLDivElement>(null);
  const navigationId = useId();
  const app = useApp();
  const viewKey = JSON.stringify([app.me.id, app.space.id, app.space.revision, [...(app.space.roles || [])].sort()]);
  type SavedView = {search: string; category: string; tag: string; kind: string; status: string;
    view: "pages" | "graph"; graphFocus?: string; selected: WikiSelection | null; history: WikiSelection[]; scrollTop?: number};
  const savedView = useRef(readWikiView<SavedView>(viewKey));
  const [launch, setLaunch] = useState(() => wikiLaunch(typeof window === "undefined" ? "" : window.location.hash));
  const [search, setSearch] = useState(savedView.current?.search || "");
  const [debounced, setDebounced] = useState(savedView.current?.search.trim() || "");
  const [category, setCategory] = useState(savedView.current?.category || "");
  const [tag, setTag] = useState(savedView.current?.tag || "");
  const [kind, setKind] = useState(savedView.current?.kind || "");
  const [status, setStatus] = useState(savedView.current?.status || "");
  const [view, setView] = useState<"pages" | "graph">(launch.graph ? "graph" : savedView.current?.view || "pages");
  const [graphFocus, setGraphFocus] = useState<string | undefined>(launch.focusId || savedView.current?.graphFocus);
  const [selected, setSelected] = useState<WikiSelection | null>(savedView.current?.selected || null);
  const [history, setHistory] = useState<WikiSelection[]>(savedView.current?.history || []);
  const [dialog, setDialog] = useState<"new" | "build" | "maintenance" | null>(launch.build ? "build" : null);
  const [categoryOperation, setCategoryOperation] = useState<CategoryOperation | null>(null);
  const [treeOpen, setTreeOpen] = useState(false);
  const task = useTask();
  const canManageCategories = app.me.is_admin || app.space.roles?.includes("admin");
  const measurement = useRef<{start:number;kind:'open'|'switch'} | null>({start:wikiOpenTime(),kind:'open'});
  useEffect(()=>{void import('./KnowledgeGraph');},[]);
  useEffect(() => {
    const changed = () => {
      const next = wikiLaunch(window.location.hash);
      if (next.build) { setLaunch(next); setDialog("build"); }
      else if (next.graph) { setLaunch(next); setGraphFocus(next.focusId); setView("graph"); setDialog(null); }
    };
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  useEffect(() => { const timer = setTimeout(() => setDebounced(search.trim()), 250); return () => clearTimeout(timer); }, [search]);
  const snapshotKey = `${viewKey}:${JSON.stringify([debounced, category, tag, kind, status])}`;
  const workspace = useWikiSnapshot<WikiWorkspaceData>(snapshotKey, String(app.refresh), async (signal) => {
    // React StrictMode may clean up its first effect immediately. Do not send
    // that abandoned full-library request: cancelling HTTP cannot undo work
    // already running in the server's read transaction.
    await Promise.resolve();
    if (signal.aborted) throw new DOMException("Aborted", "AbortError");
    const data = await get<WikiWorkspaceData>(`/wiki/workspace?${query({
      space_id: app.space.id, q: wikiSearchQuery(debounced), category, tag, kind, status, hydrate: true,
    })}`, signal);
    if (data.readers && data.pages.some(page => {
      const reader=data.readers![page.id];
      return !reader || reader.resource.id!==page.id || reader.version.resource_id!==page.id
        || reader.version.id!==page.version_id || reader.links.truncated;
    })) throw new Error('知识快照的正文或链接不完整，请重新读取；不会作为完整缓存使用。');
    return data;
  });
  useEffect(() => {
    writeWikiView(viewKey, {search, category, tag, kind, status, view, graphFocus, selected, history,
      scrollTop: readWikiView<SavedView>(viewKey)?.scrollTop || 0});
  }, [viewKey, search, category, tag, kind, status, view, graphFocus, selected, history]);
  useEffect(() => {
    if (workspace.data && savedView.current?.scrollTop) {
      const list = listRoot.current?.querySelector<HTMLElement>(".wiki-page-index > ul");
      if (list) list.scrollTop = savedView.current.scrollTop;
      savedView.current = undefined;
    }
  }, [workspace.data]);
  // Laplace's workspace response carries the authorized taxonomy before q /
  // category / tag filtering. Reuse it instead of scanning the entire wiki twice.
  const taxonomy = workspace;
  const tree = useMemo(() => categoryTree(taxonomy.data?.categories || []), [taxonomy.data]);
  const pages = workspace.data?.pages.filter((page) => page.kind !== "document") || [];
  const historicalSourcePages = pages.filter((page) => page.source_mode === "unverified_draft" || page.tags.includes("unverified-sources"));
  const hasPendingHistory = historicalSourcePages.some((page) => ["DRAFT", "IN_REVIEW"].includes(page.state));
  const hasPublishedHistory = historicalSourcePages.some((page) => ["APPROVED", "ACTIVE", "PUBLISHED"].includes(page.state));
  const pageCount = (value?: number) => value !== undefined && Number.isSafeInteger(value) && value >= 0 ? value : "未知";
  useListFlip(listRoot, `${view}:${pages.map(page => page.id).join(",")}`, ".wiki-page-index li[data-flip-id]");
  const current = selected || (pages[0] ? { id: pages[0].id, name: pages[0].name, version_id: pages[0].version_id } : null);
  useEffect(()=>{
    const root=listRoot.current?.closest<HTMLElement>('.knowledge-workspace');
    if(!workspace.data||workspace.loading||!root||!measurement.current)return;
    const pending=measurement.current;
    return observeWikiReady(root,{pages:pages.length,readers:Object.keys(workspace.data.readers||{}).length,
      nodes:workspace.data.graph?.nodes.length||0,mode:view},pending.start,pending.kind,
      ()=>{if(measurement.current===pending)measurement.current=null;});
  },[workspace.data,workspace.loading,current?.id,view]);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  function choose(next: WikiSelection) {
    measurement.current={start:performance.now(),kind:'switch'};
    controller.current?.abort();
    if (current && (current.id !== next.id || current.version_id !== next.version_id)) setHistory((items) => [...items.slice(-29), current]);
    setSelected(next);
    setView("pages");
    task.clearError();
  }
  function openLink(link: WikiLink) {
    if (link.kind === "document") {
      if (link.version_id) app.openVersion(link.version_id);
      else task.run(async () => app.openResource(await get<Resource>(`/resources/${encodeURIComponent(link.id)}`)));
    } else choose({ id: link.id, name: link.name, version_id: link.version_id });
  }
  function openNode(node: WikiNode) {
    openLink({ id: node.id, name: node.label, kind: node.kind === "source" ? "document" : node.kind as Resource["kind"],
      version_id: node.version_id, relation_type: "", status: node.state });
  }
  function resolve(title: string) {
    const target = title.normalize("NFKC").trim().toLocaleLowerCase();
    const local = Object.values(workspace.data?.readers || {}).filter(({resource,version}) =>
      [resource.name, version.title, ...(workspace.data?.maintenance?.[resource.id]?.aliases
          ?? (resource.tags || []).filter(t => t.startsWith("alias:")).map(t => t.slice(6))),
        workspace.data?.maintenance?.[resource.id]?.canonical_key || ""]
        .some(name => name.normalize("NFKC").trim().toLocaleLowerCase() === target));
    const resolved = [...new Set(local.map(item => workspace.data?.maintenance?.[item.resource.id]?.canonical_resource_id || item.resource.id))];
    const canonical = resolved.length === 1 ? workspace.data?.readers?.[resolved[0]] : undefined;
    if (canonical) { choose({id:canonical.resource.id,name:canonical.resource.name,version_id:canonical.version.id}); return; }
    task.run(async () => {
      controller.current?.abort();
      const active = new AbortController();
      controller.current = active;
      try {
        const result = await get<{ resource_id: string; version_id: string; title: string }>(`/wiki/resolve?${query({ space_id: app.space.id, title })}`, active.signal);
        const resource = await get<Resource>(`/resources/${encodeURIComponent(result.resource_id)}`, active.signal);
        if (!active.signal.aborted) {
          if (resource.kind === "document") app.openResource(resource, undefined, result.version_id);
          else choose({ id: result.resource_id, version_id: result.version_id, name: result.title });
        }
      } catch (error) { if (!active.signal.aborted) throw error; }
    });
  }
  function clearFilters() { setSearch(""); setCategory(""); setTag(""); setKind(""); setStatus(""); setSelected(null); }
  function openBuild() { setLaunch({ build: true }); setDialog("build"); }
  function savedCategory(_taxonomy: WikiTaxonomy, operation: CategoryOperation, oldPath: string, newPath: string) {
    if (operation === "delete") setCategory(oldPath.includes("/") ? oldPath.slice(0, oldPath.lastIndexOf("/")) : "");
    else setCategory(newPath);
    setSelected(null);
    workspace.reload();
    app.bump();
    app.notify(operation === "create" ? "分类已创建。" : operation === "rename" ? "分类与相关资源归类已更新。" : "空分类已删除。");
  }
  return <Motion className="page-root knowledge-workspace" identity={app.space.id} stagger={false}>
    <header className="page-heading"><div><h1>知识空间</h1><p>从知识页出发，沿着双链与来源深入理解。</p></div>
      <div className="wiki-heading-actions"><button type="button" onClick={() => setDialog("maintenance")}><Link />知识维护</button>
        <button type="button" onClick={openBuild}><Sparkle />从文档构建 Wiki</button>
        <button type="button" className="primary" onClick={() => setDialog("new")}><Plus />新建知识</button></div>
    </header>
    {launch.error && !dialog && <Notice>{launch.error}</Notice>}
    {historicalSourcePages.length > 0 && <Notice><span className="wiki-source-mode-banner">
      {hasPendingHistory ? hasPublishedHistory
        ? "当前列表包含仍处于草稿/待复核的 Wiki，也包含已确认发布的 Wiki。"
        : "当前列表仍包含草稿/待复核 Wiki，应按当前状态及既有权限流程处理。"
        : hasPublishedHistory ? "当前列表包含已确认发布、但保留待核验来源生成历史的 Wiki。"
        : "当前列表包含保留待核验来源生成历史的 Wiki。"}
      保留生成时来源提示，发布不代表独立专业复核；当前状态见条目/管理员确认记录。
    </span></Notice>}
    <div className="wiki-workbench" ref={listRoot}>
      <aside id={navigationId} className={`wiki-navigation ${treeOpen ? "is-open" : ""}`} aria-label="知识分类和标签">
        <WikiNavigationResizeHandle ownerId={app.me.id} spaceId={app.space.id} panelId={navigationId}/>
        <div className="wiki-section-title"><BookOpen /><strong>{app.space.name}</strong></div>
        <nav aria-label="知识分类"><button type="button" className={`wiki-all-pages ${!category ? "active" : ""}`}
          aria-current={!category ? "true" : undefined} onClick={() => { setCategory(""); setSelected(null); }}><BookOpen />全部知识页</button>
          <h2>分类目录</h2>
          {canManageCategories && <div className="wiki-taxonomy-actions" role="group" aria-label="管理分类目录">
            <button type="button" onClick={() => setCategoryOperation("create")} title={category ? "在当前分类下创建子分类" : "创建分类"}><Plus />新建</button>
            <button type="button" onClick={() => setCategoryOperation("rename")} disabled={!category}>重命名</button>
            <button type="button" onClick={() => setCategoryOperation("delete")} disabled={!category} aria-label="删除当前空分类"><Trash /></button>
          </div>}
          {taxonomy.loading && <Loading label="读取分类…" />}<ErrorBox error={taxonomy.error} retry={taxonomy.reload} />
          <CategoryTree branches={tree} selected={category} choose={(path) => { setCategory(path); setSelected(null); setTreeOpen(false); }} />
          {!taxonomy.loading && !taxonomy.error && !tree.length && <p className="wiki-navigation-empty">新知识的分类会显示在这里。</p>}
        </nav>
        <div className="wiki-tags"><h2><Tag />标签</h2>
          <button type="button" aria-pressed={!tag} onClick={() => { setTag(""); setSelected(null); }}>全部标签</button>
          {taxonomy.data?.tags.filter((item) => !item.name.startsWith("alias:")).map((item) => <button key={item.name} type="button"
            aria-pressed={tag === item.name} onClick={() => { setTag(tag === item.name ? "" : item.name); setSelected(null); }}>
            <span>#{item.name}</span><small>{item.count}</small></button>)}
        </div>
        <button type="button" className="wiki-source-navigation" onClick={() => app.navigate("documents")}><FileText />管理来源文档<ArrowRight /></button>
      </aside>
      <section className="wiki-main" aria-label="知识浏览工作台">
        <div className="wiki-browser-toolbar">
          <button type="button" className="wiki-mobile-taxonomy" onClick={() => setTreeOpen((value) => !value)} aria-expanded={treeOpen}><FolderSimple />分类</button>
          <label className="wiki-search"><MagnifyingGlass /><input value={search} onChange={(event) => { setSearch(event.target.value); setSelected(null); }}
            placeholder="搜索知识标题、正文与双链" aria-label="搜索知识空间" maxLength={WIKI_QUERY_LIMIT} />
            {search && <button type="button" aria-label="清空知识搜索" onClick={() => setSearch("")}><X /></button>}</label>
          <div className="wiki-view-switch" role="group" aria-label="知识空间视图"><button type="button" aria-pressed={view === "pages"} onClick={() => setView("pages")}><BookOpen />知识页</button>
            <button type="button" aria-pressed={view === "graph" && !graphFocus} onClick={() => { measurement.current={start:performance.now(),kind:'switch'}; setGraphFocus(undefined); setView("graph"); }}><Graph />全局图谱</button></div>
          <button type="button" aria-label="刷新知识空间" onClick={() => app.bump()}><ArrowClockwise /></button>
          {workspace.refreshing && !workspace.loading && <small className="muted" role="status">已载入完整快照，后台核对更新中…</small>}
        </div>
        <div className="wiki-filterbar"><span>{category || "全部分类"}{tag && ` / #${tag}`}</span>
          {view === "pages" ? <><select aria-label="筛选知识资源类型" value={kind} onChange={(event) => { setKind(event.target.value); setSelected(null); }}>
            <option value="">全部类型</option><option value="knowledge">知识页</option><option value="template">方案模板</option></select>
            <select aria-label="筛选知识状态" value={status} onChange={(event) => { setStatus(event.target.value); setSelected(null); }}>
              <option value="">全部可见状态</option><option value="PUBLISHED">已发布</option><option value="DRAFT">草稿</option><option value="IN_REVIEW">待复核</option>
            </select></> : <small>图谱按分类与搜索筛选，标签/状态筛选仅用于知识页列表。</small>}
          {view === "graph" && graphFocus && <button type="button" className="text-button" onClick={() => setGraphFocus(undefined)}>局部关联视图 · 返回全局图谱</button>}
          {(search || category || tag || kind || status) && <button type="button" className="text-button" onClick={clearFilters}><X />清除筛选</button>}
        </div>
        <ErrorBox error={task.error} />
        {workspace.error && workspace.data && <Notice>更新暂未完成，当前显示上次成功读取的快照。编辑与答疑仍由服务端重新检查权限和最新来源。</Notice>}
        {view === "graph" ? workspace.loading ? <Loading label="正在完整读取知识空间…" /> : <GraphQuery focusId={graphFocus} category={category} search={debounced} onOpenNode={openNode}
          initialData={workspace.data?.graph}
          onFocusChange={(id) => { setSearch(""); setDebounced(""); setCategory(""); setGraphFocus(id); }} /> : <div className="wiki-pages-layout">
          <section className="wiki-page-index" aria-label="知识页列表" aria-busy={workspace.loading}>
            <div className="wiki-index-heading"><strong>{debounced ? "搜索结果" : "知识页"}</strong><span>当前 {pages.length} / 全库 {pageCount(workspace.data?.stats?.total_pages)} 篇
              {Boolean(debounced || category || tag || kind || status) && ` · 筛选匹配 ${pageCount(workspace.data?.stats?.matched_pages)} 篇`}
              {workspace.data?.truncated ? " · 部分结果" : ""}</span></div>
            {workspace.loading && <Loading label="正在查找知识…" />}<ErrorBox error={workspace.error} retry={workspace.reload} />
            {workspace.data?.truncated && <p className="wiki-truncated" role="status">结果已截断，请细化分类、标签或搜索。</p>}
            {!workspace.loading && !workspace.error && !pages.length && <Empty title="没有匹配知识页" detail="调整筛选条件，或从已发布来源构建 Wiki。" />}
            <ul onScroll={(event) => { const saved = readWikiView<SavedView>(viewKey); if(saved)writeWikiView(viewKey,{...saved,scrollTop:event.currentTarget.scrollTop}); }}>{pages.map((page) => <li key={page.id} data-flip-id={`wiki-${page.id}`}><button type="button" className={current?.id === page.id ? "active" : ""}
              aria-current={current?.id === page.id ? "page" : undefined} onClick={() => choose({ id: page.id, version_id: page.version_id, name: page.name })}>
              <span className="wiki-page-type"><BookOpen />{wikiTypeLabels[page.knowledge_type] || "知识页"}<Badge value={page.state} /></span>
              <strong>{page.name}</strong><p>{page.excerpt || "打开知识页阅读内容与依据。"}</p>
              <span className="wiki-page-foot"><span><Link />{page.link_count} 出链 · {page.backlink_count} 反链</span><time>{dateText(page.updated_at || undefined)}</time></span>
            </button></li>)}</ul>
          </section>
          {current ? <WikiReader key={`${app.space.id}:${current.id}:${current.version_id}`} selected={current} canBack={history.length > 0}
            initialData={workspace.data?.readers?.[current.id] || workspace.data?.reader}
            back={() => { const previous = history.at(-1); if (previous) { setSelected(previous); setHistory((items) => items.slice(0, -1)); } }}
            open={openLink} openNode={openNode} resolve={resolve} /> : <div className="wiki-start"><BookOpen weight="light" size={46} />
              <h2>把资料连接成可复核的知识</h2><p>选择左侧知识页开始阅读，或从已发布文档构建有来源的 Wiki。</p>
              <button type="button" onClick={openBuild}><Sparkle />从文档构建 Wiki</button></div>}
        </div>}
      </section>
    </div>
    {dialog === "new" && <Suspense fallback={<Loading label="正在加载新建知识表单…" />}>
      <CreateResourceDialog kind="knowledge" defaultCategory={category} close={() => setDialog(null)} />
    </Suspense>}
    {dialog === "build" && <WikiBuildDialog key={launch.sourceId || "manual"} initialSourceId={launch.sourceId}
      launchError={launch.error} close={() => setDialog(null)} />}
    {dialog === "maintenance" && <Suspense fallback={<Loading label="正在加载知识维护…" />}>
      <WikiMaintenanceDialog pages={pages} initialId={current?.id} close={() => setDialog(null)} />
    </Suspense>}
    {categoryOperation && <WikiCategoryDialog operation={categoryOperation} path={category} close={() => setCategoryOperation(null)} saved={savedCategory} />}
  </Motion>;
}

export function KnowledgeWorkspace() {
  const app = useApp();
  return <WorkspaceContent key={app.space.id} />;
}
