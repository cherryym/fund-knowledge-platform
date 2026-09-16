import { memo, useCallback, useMemo, type RefObject } from "react";
import { ArrowSquareOut } from "@phosphor-icons/react";
import { graphGroup, wikiTypeLabels, type WikiGraphData, type WikiNode } from "./knowledgeTypes";
import { STAR_HEIGHT, STAR_WIDTH, starRadius, type StarNodeElements } from "./starGraphEngine";
import { defaultStarTheme, nodeStarColor } from "./starGraphTheme";

// These are mount-time fallbacks only. The runtime owns live SVG presentation,
// including colors, radius, labels and transforms; UI state must not reconcile it.
const INITIAL_THEME = defaultStarTheme();
const DUST = Array.from({ length: 22 }, (_, index) => ({ x: 22 + (index * 163.7) % 906,
  y: 18 + (index * 97.3) % 578, radius: index % 4 === 0 ? 1.4 : 0.8 }));

type StarSceneProps = {
  graph: WikiGraphData;
  svg: RefObject<SVGSVGElement | null>;
  viewport: RefObject<SVGGElement | null>;
  starLayer: RefObject<SVGGElement | null>;
  edgeLayer: RefObject<SVGGElement | null>;
  nodes: Map<string, StarNodeElements>;
  edges: Map<string, SVGPathElement>;
};

const StarSvgEdge = memo(function StarSvgEdgeView({ id, elements }: {
  id: string; elements: Map<string, SVGPathElement>;
}) {
  const bindEdge = useCallback((element: SVGPathElement | null) => {
    if (element) elements.set(id, element); else elements.delete(id);
  }, [id, elements]);
  return <path data-star-edge={id} ref={bindEdge} fill="none" stroke={INITIAL_THEME.edge}
    strokeWidth={INITIAL_THEME.physics.edgeWidth} vectorEffect="non-scaling-stroke" />;
});

const StarSvgNode = memo(function StarSvgNodeView({ node, degree, hasHalo, elements }: {
  node: WikiNode; degree: number; hasHalo: boolean; elements: Map<string, StarNodeElements>;
}) {
  const id = node.id;
  const bindNode = useCallback((element: SVGGElement | null) => {
    if (element) elements.set(id, { position: element, glyph: element.querySelector<SVGGElement>(".star-glyph")!,
      dot: element.querySelector<SVGCircleElement>(".star-dot")!, text: element.querySelector<SVGTextElement>("text")!,
      halo: element.querySelector<SVGCircleElement>(".star-halo") || undefined });
    else elements.delete(id);
    // Halo membership is structural: rebind only when its DOM is added/removed,
    // so the runtime never keeps a reference to a detached halo after data changes.
  }, [id, elements, hasHalo]);
  const radius = starRadius(degree, INITIAL_THEME.physics.nodeSize);
  const color = nodeStarColor(INITIAL_THEME, id, graphGroup(node));
  return <g data-star-node={id} className="star-node" ref={bindNode}>
    <title>{node.label}</title><g className="star-glyph">
      {hasHalo && <circle className="star-halo" aria-hidden="true" r={radius * 2.7} fill={color} opacity="0.1" />}
      <circle className="star-dot" r={radius} fill={color} />
    </g>
    <text className="star-label" x="14" y="4">{node.label.length > 24 ? `${node.label.slice(0, 23)}…` : node.label}</text>
  </g>;
});

/** Topology-only boundary: never pass selection, phase or live theme here. */
export const StarScene = memo(function StarSceneView({ graph, svg, viewport, starLayer, edgeLayer, nodes, edges }: StarSceneProps) {
  const degrees = useMemo(() => {
    const values = new Map(graph.nodes.map((node) => [node.id, new Set<string>()]));
    for (const edge of graph.edges) { values.get(edge.source)?.add(edge.target); values.get(edge.target)?.add(edge.source); }
    return values;
  }, [graph]);
  const haloIds = useMemo(() => new Set([...graph.nodes].sort((a, b) => (degrees.get(b.id)?.size || 0)
    - (degrees.get(a.id)?.size || 0)).slice(0, 24).map((node) => node.id)), [graph, degrees]);
  return <svg ref={svg} className="star-surface" viewBox={`0 0 ${STAR_WIDTH} ${STAR_HEIGHT}`} preserveAspectRatio="xMidYMid meet" aria-hidden="true"
    data-star-ticks="0" data-star-frames="0" data-star-clock="stopped">
    <g className="star-dust" data-decorative="true" aria-hidden="true">{DUST.map((dust, index) => <circle key={index} cx={dust.x} cy={dust.y} r={dust.radius} />)}</g>
    <g ref={viewport}>
      <g ref={edgeLayer} className="star-edge-layer">{graph.edges.map((edge) => <StarSvgEdge key={edge.id} id={edge.id} elements={edges} />)}</g>
      <g ref={starLayer}>{graph.nodes.map((node) => <StarSvgNode key={node.id} node={node} degree={degrees.get(node.id)?.size || 0}
        hasHalo={haloIds.has(node.id)} elements={nodes} />)}</g>
    </g>
  </svg>;
});

export const StarNodeRow = memo(function StarNodeRowView({ node, selected, color, onSelect, onOpen }: {
  node: WikiNode; selected: boolean; color: string; onSelect: (id: string) => void; onOpen: (node: WikiNode) => void;
}) {
  return <li className={selected ? "active" : ""}>
    <button type="button" className="star-list-select" onClick={() => onSelect(node.id)} aria-pressed={selected}>
      <i style={{ background: color }} /><span>{node.label}</span><small>{wikiTypeLabels[node.knowledge_type] || "知识页"}</small>
    </button>
    <button type="button" className="star-list-open" aria-label={`打开${node.label}`} onClick={() => onOpen(node)}><ArrowSquareOut /></button>
  </li>;
});
