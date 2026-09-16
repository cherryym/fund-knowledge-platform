import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowClockwise, ArrowsOut, Minus, Palette, Pause, Play, Plus, X } from "@phosphor-icons/react";
import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import { Empty, ErrorBox, Motion, Notice } from "./ui";
import { graphGroup, wikiTypeLabels, type GraphRenderBudget, type WikiGraphData, type WikiNode } from "./knowledgeTypes";
import { useMotionPreferences } from "./motionPreferences";
import { mountStarGraph, prepareStarGraph,
  type StarRuntime, type StarNodeElements, type StarPhase } from "./starGraphEngine";
import { nodeStarColor, STAR_GROUPS, STAR_PHYSICS_LIMITS, starColor, useStarGraphTheme,
  type StarGraphTheme, type StarPhysics } from "./starGraphTheme";
import { StarNodeRow, StarScene } from "./StarScene";
import { StarCanvasScene } from "./StarCanvasScene";
import { mountStarCanvas } from "./starCanvasRuntime";
import { GraphHeightResizeHandle } from "./GraphHeightResizeHandle";
import "./star-graph.css";

gsap.registerPlugin(useGSAP);
export type KnowledgeGraphProps = {
  data: WikiGraphData; onOpenNode: (node: WikiNode) => void; focusId?: string; label?: string; preferenceKey?: string;
  heightPreferenceKey?: string;
  /** Compatibility hint only; all valid returned graph data is now retained. */
  renderBudget?: GraphRenderBudget;
  /** Emits bounded, non-business draw diagnostics only on the synthetic QA page. */
  diagnostics?: boolean;
};
const phaseNames: Record<StarPhase, string> = { settling: "星点收束中", settled: "布局已收束", paused: "已暂停", quiet: "静态布局" };
const physicsLabels: Record<keyof StarPhysics, string> = {
  nodeSize: "节点大小", edgeWidth: "连线粗细", center: "中心吸引", repel: "节点斥力", distance: "连接距离",
  linkStrength: "关联牵引力", damping: "运动阻尼",
};

function ColorField({ label, value, change }: { label: string; value: string; change: (value: string) => void }) {
  const [draft, setDraft] = useState(value);
  const [invalid, setInvalid] = useState(false);
  useEffect(() => { setDraft(value); setInvalid(false); }, [value]);
  return <label className="star-color-field"><span>{label}</span><div>
    <input type="color" aria-label={`${label}选色器`} value={value} onChange={(event) => change(event.target.value)} />
    <input type="text" aria-label={`${label}色值`} value={draft} maxLength={7} spellCheck={false} aria-invalid={invalid}
      onChange={(event) => { setDraft(event.target.value); setInvalid(false); }} onBlur={() => {
        const color = starColor(draft); if (color) change(color); else { setInvalid(true); setDraft(value); }
      }} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); event.currentTarget.blur(); } }} />
  </div>{invalid && <small role="status">请输入 #RRGGBB 或 #RGB。</small>}</label>;
}

export function KnowledgeGraph({ data, onOpenNode, focusId, label = "知识关系图谱", preferenceKey, heightPreferenceKey, renderBudget = "standard", diagnostics = false }: KnowledgeGraphProps) {
  const container = useRef<HTMLElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const canvasLabels = useRef<HTMLCanvasElement>(null);
  const useCanvas = typeof CanvasRenderingContext2D !== "undefined";
  const [backend, setBackend] = useState("");
  const viewport = useRef<SVGGElement>(null);
  const starLayer = useRef<SVGGElement>(null);
  const edgeLayer = useRef<SVGGElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const settingsPanel = useRef<HTMLElement>(null);
  const settingsTrigger = useRef<HTMLButtonElement>(null);
  const nodes = useRef(new Map<string, StarNodeElements>());
  const edges = useRef(new Map<string, SVGPathElement>());
  const runtime = useRef<StarRuntime | null>(null);
  const [selected, setSelected] = useState<string | null>(focusId || null);
  const [paused, setPaused] = useState(false);
  const [phase, setPhase] = useState<StarPhase>("settling");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [search, setSearch] = useState("");
  const { effective, reduced } = useMotionPreferences();
  const quiet = reduced || effective === "quiet";
  const preferences = useStarGraphTheme(preferenceKey);
  const current = useRef({ onOpenNode, paused, quiet, level: effective, theme: preferences.theme, selected });
  current.current = { onOpenNode, paused, quiet, level: effective, theme: preferences.theme, selected };
  const openNode = useCallback((node: WikiNode) => current.current.onOpenNode(node), []);
  const prepared = useMemo(() => {
    try { return { graph: prepareStarGraph(data, renderBudget), error: undefined }; }
    catch (error) { return { graph: { nodes: [], edges: [], truncated: false, total_visible_nodes: 0 } as WikiGraphData,
      error: error instanceof Error ? error : new Error("图谱数据无效。") }; }
  }, [data, renderBudget]);
  const graph = prepared.graph;
  const id = useId();
  useGSAP((_context, contextSafe) => {
    if (useCanvas && canvas.current && canvasLabels.current && graph.nodes.length) {
      const mounted = mountStarCanvas({ canvas: canvas.current, labels: canvasLabels.current, graph, focusId, renderBudget, diagnostics,
        theme: current.current.theme, paused: current.current.paused, quiet: current.current.quiet, level: current.current.level,
        selection: current.current.selected, onOpen: (node) => current.current.onOpenNode(node), onSelect: setSelected, onPhase: setPhase, onBackend: setBackend });
      runtime.current = mounted;
      return () => { mounted.dispose(); if (runtime.current === mounted) runtime.current = null; };
    }
    if (!svg.current || !viewport.current || !starLayer.current || !edgeLayer.current || !graph.nodes.length) return;
    const mounted = mountStarGraph({ surface: svg.current, viewport: viewport.current, starLayer: starLayer.current,
      edgeLayer: edgeLayer.current, nodes: nodes.current, edges: edges.current, graph, focusId, renderBudget,
      theme: current.current.theme, paused: current.current.paused, quiet: current.current.quiet, level: current.current.level,
      selection: current.current.selected, contextSafe: contextSafe!, onOpen: (node) => current.current.onOpenNode(node),
      onSelect: setSelected, onPhase: setPhase });
    runtime.current = mounted;
    return () => { mounted.dispose(); if (runtime.current === mounted) runtime.current = null; };
  }, { scope: container, dependencies: [graph, focusId, renderBudget, useCanvas, diagnostics], revertOnUpdate: true });
  useEffect(() => { runtime.current?.activity(paused, quiet, effective); }, [paused, quiet, effective, graph]);
  useEffect(() => { runtime.current?.theme(preferences.theme); }, [preferences.theme, graph]);
  useEffect(() => { runtime.current?.select(selected); }, [selected, graph]);
  useEffect(() => {
    if (!settingsOpen) return;
    const panel = settingsPanel.current;
    panel?.scrollIntoView?.({ block: "nearest", behavior: current.current.quiet ? "auto" : "smooth" });
    panel?.querySelector<HTMLInputElement>('input[type="text"]')?.focus({ preventScroll: true });
    return () => settingsTrigger.current?.focus({ preventScroll: true });
  }, [settingsOpen]);
  const selectedNode = graph.nodes.find((node) => node.id === selected);
  const visible = useMemo(() => graph.nodes.filter((node) => node.label.toLocaleLowerCase().includes(search.toLocaleLowerCase())), [graph, search]);
  const theme = preferences.theme;
  const update = (patch: Partial<StarGraphTheme>) => preferences.change({ ...theme, ...patch });
  return <section className="wiki-graph star-graph" ref={container} aria-label={label} data-star-level={effective}>
    <header className="star-toolbar"><div><strong>{label}</strong><small>{graph.nodes.length} 个节点 · {graph.edges.length} 条关系</small></div>
      <div className="star-tools">
        <button type="button" onClick={() => { setPaused(!paused); runtime.current?.activity(!paused, quiet, effective); }} disabled={quiet || !graph.nodes.length}
          aria-label={paused ? "继续图谱动态" : "暂停图谱动态"}>{paused ? <Play /> : <Pause />}{paused ? "继续" : "暂停"}</button>
        <button type="button" onClick={() => { setPaused(false); runtime.current?.activity(false, quiet, effective); runtime.current?.replay(); }} disabled={!graph.nodes.length}><ArrowClockwise />重播收束</button>
        <button ref={settingsTrigger} type="button" disabled={!graph.nodes.length} onClick={() => setSettingsOpen(!settingsOpen)} aria-expanded={settingsOpen} aria-controls={`${id}-settings`}><Palette />配色与布局</button>
      </div>
    </header>
    <div className="star-legend" aria-label="节点图例">{STAR_GROUPS.map((group) => <span key={group}><i style={{ background: theme.mode === "unified" ? theme.unified : theme.groups[group] }} />{wikiTypeLabels[group]}</span>)}
      <span className="star-phase" role="status">{graph.nodes.length ? phaseNames[phase] : prepared.error ? "图谱数据不可用" : "暂无可见节点"}</span></div>
    {backend && <p className="star-backend-note" aria-label="图谱渲染方式">{backend}{backend.includes("备用") ? " · 兼容模式，独立计算暂不可用" : " · 独立计算，连续绘制"}</p>}
    {prepared.error && <ErrorBox error={prepared.error} />}
    {graph.truncated && <Notice>图谱数据未完整返回或含无效引用，当前显示 {graph.nodes.length} 个节点与 {graph.edges.length} 条关系。
      {Number.isFinite(graph.matched_visible_nodes) && <>当前范围匹配 {graph.matched_visible_nodes} 个节点。</>}
      {Number.isFinite(graph.total_visible_nodes) && <>空间可见总数 {graph.total_visible_nodes}。</>}请重新读取完整图谱。</Notice>}
    {!graph.nodes.length && !prepared.error && <Empty title="当前范围还没有可见节点" detail="图谱只显示已授权的知识和来源；不会补造星点或关系。" />}
    {graph.nodes.length > 0 && <><div className="star-stage" ref={stage} id={`${id}-stage`}>
      {useCanvas ? <StarCanvasScene canvas={canvas} labels={canvasLabels} description={`${label}，${graph.nodes.length}个节点，${graph.edges.length}条关系。拖动星点牵引，松手恢复全图；方向键平移，加减键缩放。`} /> :
        <StarScene graph={graph} svg={svg} viewport={viewport} starLayer={starLayer} edgeLayer={edgeLayer}
          nodes={nodes.current} edges={edges.current} />}
      <div className="star-view-tools"><button type="button" aria-label="缩小图谱" onClick={() => runtime.current?.zoom(1 / 1.25)}><Minus /></button>
        <button type="button" aria-label="放大图谱" onClick={() => runtime.current?.zoom(1.25)}><Plus /></button>
        <button type="button" onClick={() => runtime.current?.fit()}><ArrowsOut />适应画布</button></div>
      <span className="star-stage-caption">拖动星点调整布局 · 空白处平移 · 滚轮缩放 · 单击打开知识</span>
    </div><GraphHeightResizeHandle stage={stage} panelId={`${id}-stage`} preferenceKey={heightPreferenceKey} /></>}
    {quiet && <p className="star-note">{reduced ? "系统已启用减少动态效果。" : "当前为安静模式。"}图谱使用有限静态布局，拖动、缩放与阅读仍可操作。</p>}
    {settingsOpen && stage.current && createPortal(<Motion className="star-settings-drawer" preset="overview" identity="star-settings">
      <section className="star-settings" ref={settingsPanel} id={`${id}-settings`} aria-label="图谱配色与布局设置"
        onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setSettingsOpen(false); } }}>
      <div className="star-settings-heading"><h3>定义你的知识星图</h3><button type="button" onClick={preferences.reset}>恢复默认</button>
        <button type="button" aria-label="关闭图谱配色" onClick={() => setSettingsOpen(false)}><X /></button></div>
      <p>配色仅改变显示，不改变知识状态、权限或关系。{preferences.persistence === "session" || !preferenceKey ? "本次会话已应用。" : "设置按当前空间保存。"}</p>
      <p>中心与当前焦点使用高亮色；单点覆盖优先于分组色和高亮色。</p>
      <div className="star-color-mode" role="group" aria-label="节点配色模式"><button type="button" aria-pressed={theme.mode === "group"} onClick={() => update({ mode: "group" })}>按类型分组</button>
        <button type="button" aria-pressed={theme.mode === "unified"} onClick={() => update({ mode: "unified" })}>统一节点颜色</button></div>
      <div className="star-color-grid">
        {theme.mode === "unified" ? <ColorField label="统一节点颜色" value={theme.unified} change={(unified) => update({ unified })} /> :
          STAR_GROUPS.map((group) => <ColorField key={group} label={`${wikiTypeLabels[group]}颜色`} value={theme.groups[group]}
            change={(color) => update({ groups: { ...theme.groups, [group]: color } })} />)}
        <ColorField label="连线颜色" value={theme.edge} change={(edge) => update({ edge })} />
        <ColorField label="焦点高亮颜色" value={theme.highlight} change={(highlight) => update({ highlight })} />
      </div>
      <div className="star-single-color"><label>单点颜色<select aria-label="选择要配色的节点" value={selectedNode?.id || ""} onChange={(event) => setSelected(event.target.value || null)}>
        <option value="">选择真实节点</option>{graph.nodes.map((node) => <option key={node.id} value={node.id}>{node.label}</option>)}</select></label>
        {selectedNode && <><ColorField label="选中节点颜色" value={nodeStarColor(theme, selectedNode.id, graphGroup(selectedNode))}
          change={(color) => update({ nodes: { ...theme.nodes, [selectedNode.id]: color } })} />
          <button type="button" onClick={() => { const next = { ...theme.nodes }; delete next[selectedNode.id]; update({ nodes: next }); }}>清除单点覆盖</button></>}
      </div>
      <label className="star-label-mode">可见标签<select aria-label="图谱标签显示" value={theme.labels} onChange={(event) => update({ labels: event.target.value as StarGraphTheme["labels"] })}>
        <option value="auto">自动分级 · 悬停显示标题</option><option value="all">显示全部标签</option><option value="none">仅使用下方节点列表</option></select></label>
      <div className="star-physics">{(Object.keys(STAR_PHYSICS_LIMITS) as (keyof StarPhysics)[]).map((key) => <label key={key}>
        <span>{physicsLabels[key]}<output>{theme.physics[key]}</output></span><input type="range" aria-label={physicsLabels[key]}
          min={STAR_PHYSICS_LIMITS[key][0]} max={STAR_PHYSICS_LIMITS[key][1]} step={STAR_PHYSICS_LIMITS[key][2]} value={theme.physics[key]}
          onChange={(event) => update({ physics: { ...theme.physics, [key]: Number(event.target.value) } })} /></label>)}</div>
    </section></Motion>, stage.current)}
    {graph.nodes.length > 0 && <details className="star-node-directory" open><summary>可访问的节点列表 · {graph.nodes.length}</summary>
      <input aria-label="筛选图谱节点列表" placeholder="查找节点名称" value={search} onChange={(event) => setSearch(event.target.value)} />
      <ul aria-label="图谱节点">{visible.map((node) => <StarNodeRow key={node.id} node={node} selected={selected === node.id}
        color={nodeStarColor(theme, node.id, graphGroup(node))} onSelect={setSelected} onOpen={openNode} />)}</ul>
      {!visible.length && <p>当前图谱中没有匹配名称。</p>}
    </details>}
  </section>;
}
