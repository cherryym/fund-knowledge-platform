/** Local Vite-only QA entry. Not imported by the application or shipped as an entry in dist. */
import { StrictMode, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { KnowledgeGraph } from "./KnowledgeGraph";
import { AmbientEffects } from "./AmbientEffects";
import { createStarfieldFixture, fixtureCount, STAR_FIXTURE_COUNTS } from "./starfieldFixture";
import "./styles.css";
import "./readability.css";
import "./ambient-effects.css";

function StarfieldPerf() {
  const root = useRef<HTMLDivElement>(null);
  const [count, setCount] = useState(() => fixtureCount(new URLSearchParams(location.search).get("nodes")));
  const [opened, setOpened] = useState("");
  const data = useMemo(() => createStarfieldFixture(count), [count]);
  return <div ref={root} data-ambient-shell="true" style={{ minHeight: "100vh" }}>
    <AmbientEffects rootRef={root} routeKey="synthetic-performance" />
    <main className="main-shell" style={{ maxWidth: 1220, margin: "0 auto", padding: 24 }}>
      <header className="page-heading" style={{ padding: "8px 0 18px" }}><div><h1 style={{ fontSize: 24 }}>星图性能验收</h1>
        <p>合成数据，不调用模型、不读取业务文档、不写入知识库。本页不作为生产入口打包。</p></div>
        <label>节点数量 <select aria-label="合成节点数量" value={count} onChange={e => {
          const next = fixtureCount(e.target.value); setCount(next); setOpened("");
          const url = new URL(location.href); url.searchParams.set("nodes", String(next)); history.replaceState(null, "", url);
        }}>
          {STAR_FIXTURE_COUNTS.map(option => <option key={option} value={option}>{option}</option>)}</select></label>
      </header>
      <section style={{ padding: 20, background: "#fff", border: "1px solid #e2eaf3", borderRadius: 12 }}>
        <p role="status" data-fixture-selection style={{ margin: "0 0 14px", color: "#334766", minHeight: 24 }}>
          {opened ? `已打开合成节点：${opened}` : "请连续缩放、反向缩放、拖动空白处，再快速切换星点。点击结果会显示在这里。"}</p>
        <KnowledgeGraph key={count} data={data} diagnostics renderBudget={import.meta.env.DEV ? "synthetic-300" : "standard"}
          label="合成知识星图 · 性能验收" onOpenNode={node => setOpened(node.label)} />
      </section>
    </main>
  </div>;
}
const mount = document.getElementById("root");
if (mount) createRoot(mount).render(<StrictMode><StarfieldPerf /></StrictMode>);
