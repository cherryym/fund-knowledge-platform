import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ArrowDown,
  ArrowUp,
  BookOpen,
  Check,
  DotsSixVertical,
  Eye,
  Link,
  List,
  MagnifyingGlass,
  PencilSimple,
  Plus,
  SidebarSimple,
  Trash,
  X,
} from "@phosphor-icons/react";
import type { Block } from "./types";
import { blockNames, insertBlock, reorderBlock } from "./blockOperations";
import { RichTextSession, RichTextToolbar } from "./RichTextSession";
import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import { useMotionPreferences } from "./motionPreferences";
import { attachDocumentProgress } from "./documentScroll";

export type DocumentPosition = { scrollTop: number; active?: string };
type Props = {
  title: string;
  blocks: Block[];
  editable?: boolean;
  onTitle?: (title: string) => void;
  onChange?: (blocks: Block[]) => void;
  renderBlock: (block: Block, highlighted: boolean) => ReactNode;
  renderEditor?: (block: Block) => ReactNode;
  renderCitations?: (block: Block) => ReactNode;
  onCite?: (id: string) => void;
  highlightedId?: string;
  position?: DocumentPosition;
};
function searchable(block: Block) {
  return Object.values(block.data)
    .flat(3)
    .filter((v) => typeof v === "string")
    .join(" ");
}
export function DocumentCanvas(props: Props) {
  const { effective } = useMotionPreferences();
  const {
    title,
    blocks,
    editable = false,
    onChange,
    onTitle,
    renderBlock,
    renderEditor,
    renderCitations,
    onCite,
    highlightedId,
  } = props;
  const [active, setActive] = useState<string | undefined>(
    props.position?.active,
  );
  const [reading, setReading] = useState(false);
  const [inspector, setInspector] = useState(false);
  const [outlineOpen, setOutlineOpen] = useState(false);
  const [insertOpen, setInsertOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [matchIndex, setMatchIndex] = useState(0);
  const progress = useRef<HTMLDivElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const beforeMove = useRef(new Map<string, number>());
  const inserted = useRef<string | undefined>(undefined);
  const scroll = useRef<HTMLDivElement>(null);
  const positions = useRef(new Map<string, HTMLElement>());
  useEffect(() => {
    if (props.position && scroll.current)
      scroll.current.scrollTop = props.position.scrollTop;
    if (scroll.current && progress.current)
      return attachDocumentProgress(scroll.current, progress.current, scroll.current.querySelector(".document-paper"));
  }, []);
  useEffect(() => {
    if (props.position) props.position.active = active;
  }, [active, props.position]);
  const order = blocks.map((b) => b.block_id).join(":");
  useGSAP(
    () => {
      if (effective === "quiet") {
        beforeMove.current.clear();
        inserted.current = undefined;
        return;
      }
      for (const [id, top] of beforeMove.current) {
        const element = positions.current.get(id);
        if (element) {
          const delta = top - element.getBoundingClientRect().top;
          if (Math.abs(delta) > 1)
            gsap.fromTo(
              element,
              { y: delta },
              {
                y: 0,
                duration: 0.28,
                ease: "power2.out",
                clearProps: "transform",
              },
            );
        }
      }
      beforeMove.current.clear();
      if (inserted.current) {
        const element = positions.current.get(inserted.current);
        if (element)
          gsap.fromTo(
            element,
            { autoAlpha: 0, y: 8 },
            {
              autoAlpha: 1,
              y: 0,
              duration: 0.3,
              ease: "power2.out",
              clearProps: "transform,opacity,visibility",
            },
          );
        inserted.current = undefined;
      }
    },
    { scope: root, dependencies: [order, effective], revertOnUpdate: true },
  );
  const selected = blocks.find((b) => b.block_id === active);
  const currentIndex = blocks.findIndex((b) => b.block_id === active);
  const headings = useMemo(
    () => blocks.filter((b) => b.block_type === "heading"),
    [blocks],
  );
  const matches = useMemo(
    () =>
      query.trim()
        ? blocks.filter((b) =>
            searchable(b).toLowerCase().includes(query.trim().toLowerCase()),
          )
        : [],
    [blocks, query],
  );
  const matchedIds = useMemo(() => new Set(matches.map(block => block.block_id)), [matches]);
  const canEdit = editable && !reading;
  const jump = (id: string) =>
    positions.current.get(id)?.scrollIntoView({
      block: "center",
      behavior: effective === "quiet"
        ? "instant"
        : "smooth",
    });
  useEffect(() => {
    if (highlightedId) jump(highlightedId);
  }, [highlightedId]);
  function add(type: Block["block_type"]) {
    const result = insertBlock(blocks, type, active);
    inserted.current = result.id;
    onChange?.(result.blocks);
    setActive(result.id);
    setReading(false);
    setInsertOpen(false);
    requestAnimationFrame(() => jump(result.id));
  }
  function move(offset: number) {
    if (active) {
      for (const b of [selected, blocks[currentIndex + offset]]) {
        if (b) {
          const element = positions.current.get(b.block_id);
          if (element)
            beforeMove.current.set(
              b.block_id,
              element.getBoundingClientRect().top,
            );
        }
      }
      onChange?.(reorderBlock(blocks, active, offset));
    }
  }
  function style(value: string) {
    if (
      !selected ||
      !["paragraph", "heading", "warning"].includes(selected.block_type)
    )
      return;
    const { level: _level, ...data } = selected.data;
    onChange?.(
      blocks.map((b) =>
        b.block_id === active
          ? {
              ...b,
              block_type: value.startsWith("h")
                ? "heading"
                : (value as "paragraph" | "warning"),
              data: value.startsWith("h")
                ? { ...data, level: Number(value.slice(1)) }
                : data,
            }
          : b,
      ),
    );
  }
  return (
    <RichTextSession>
      <div
        ref={root}
        className={`document-workbench ${canEdit ? "editing-document" : "reading-document"}`}
      >
        <div className="document-commandbar">
          <button
            type="button"
            className="mobile-outline-toggle"
            aria-label="打开章节目录"
            aria-expanded={outlineOpen}
            onClick={() => {
              setOutlineOpen((v) => !v);
              setInspector(false);
            }}
          >
            <List />
            目录
          </button>
          <div className="document-mode-switch">
            {editable ? (
              <>
                <button
                  type="button"
                  className={!reading ? "active" : ""}
                  onClick={() => setReading(false)}
                >
                  <PencilSimple />
                  编辑文稿
                </button>
                <button
                  type="button"
                  className={reading ? "active" : ""}
                  onClick={() => {
                    setReading(true);
                    setActive(undefined);
                  }}
                >
                  <Eye />
                  阅读预览
                </button>
              </>
            ) : (
              <span>
                <BookOpen />
                连续文稿
              </span>
            )}
          </div>
          <span className="document-counter">
            {blocks.length} 段
            {selected ? ` · 当前第 ${currentIndex + 1} 段` : ""}
          </span>
          {canEdit && (
            <>
              <select
                aria-label="段落样式"
                disabled={
                  !selected ||
                  !["heading", "paragraph", "warning"].includes(
                    selected.block_type,
                  )
                }
                value={
                  selected?.block_type === "heading"
                    ? `h${selected.data.level}`
                    : (selected?.block_type ?? "paragraph")
                }
                onChange={(e) => style(e.target.value)}
              >
                <option value="paragraph">正文</option>
                {[1, 2, 3, 4, 5, 6].map((n) => (
                  <option value={`h${n}`} key={n}>
                    标题 {n}
                  </option>
                ))}
                <option value="warning">注意事项</option>
              </select>
              <RichTextToolbar />
              <div className="document-insert">
                <button
                  type="button"
                  onClick={() => setInsertOpen((v) => !v)}
                  aria-expanded={insertOpen}
                >
                  <Plus />
                  插入
                </button>
                {insertOpen && (
                  <div
                    className="document-insert-menu"
                    role="menu"
                    aria-label="插入内容类型"
                  >
                    {Object.entries(blockNames).map(([type, name]) => (
                      <button
                        type="button"
                        role="menuitem"
                        key={type}
                        onClick={() => add(type as Block["block_type"])}
                      >
                        {name}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <button
                type="button"
                aria-label="段落属性与引用"
                title="段落属性与引用"
                disabled={!active}
                aria-pressed={inspector}
                onClick={() => setInspector((v) => !v)}
              >
                <SidebarSimple />
              </button>
            </>
          )}
        </div>
        <div
          className={`document-canvas-layout ${inspector && selected && canEdit ? "with-inspector" : ""}`}
        >
          <aside
            className={`document-outline ${outlineOpen ? "outline-open" : ""}`}
            aria-label="文稿章节目录"
          >
            <h3>
              <List />
              文稿目录
              <button
                type="button"
                className="mobile-outline-toggle"
                aria-label="收起章节目录"
                onClick={() => setOutlineOpen(false)}
              >
                <X />
              </button>
            </h3>
            <label className="document-find">
              <MagnifyingGlass />
              <input
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setMatchIndex(0);
                }}
                placeholder="查找本篇内容"
                aria-label="查找本篇内容"
              />
            </label>
            {query && (
              <div className="document-find-results">
                <span>{matches.length} 处匹配</span>
                <button
                  type="button"
                  disabled={!matches.length}
                  onClick={() => {
                    jump(matches[matchIndex % matches.length].block_id);
                    setMatchIndex((i) => i + 1);
                  }}
                >
                  下一处
                </button>
              </div>
            )}
            <button
              type="button"
              onClick={() =>
                scroll.current?.scrollTo({ top: 0, behavior: "instant" })
              }
            >
              文稿开头
            </button>
            {headings.map((b) => (
              <button
                type="button"
                key={b.block_id}
                className={active === b.block_id ? "active" : ""}
                style={{
                  paddingLeft:
                    10 + Math.min(3, Number(b.data.level ?? 1) - 1) * 9,
                }}
                onClick={() => {
                  jump(b.block_id);
                  setOutlineOpen(false);
                }}
              >
                {String(b.data.text || "未命名章节").replace(/[*_`]/g, "")}
              </button>
            ))}
            {!headings.length && <p>添加标题后会自动形成章节目录。</p>}
            <div className="document-outline-foot">
              <strong>{blocks.length}</strong> 个内容段落
              <br />
              连续阅读 · 不按段落分页
            </div>
          </aside>
          <div
            className="document-scroll"
            ref={scroll}
            role="region"
            aria-label="文稿正文"
            tabIndex={0}
            onScroll={(e) => {
              const el = e.currentTarget;
              if (props.position) props.position.scrollTop = el.scrollTop;
            }}
          >
            <div
              className="document-reading-progress"
              ref={progress}
              style={{ transform: "scaleX(0)" }}
            />
            <div className="document-paper">
              <header className="document-paper-head">
                <span>{canEdit ? "可编辑文稿" : "文稿内容"}</span>
                {editable && !reading ? (
                  <input
                    className="document-title"
                    aria-label="文稿标题"
                    value={title}
                    onChange={(e) => onTitle?.(e.target.value)}
                    maxLength={300}
                    required
                  />
                ) : (
                  <h1>{title}</h1>
                )}
                <p>
                  {canEdit
                    ? "点击正文即可编辑；格式与段落操作在上方工具栏。"
                    : "保留内容结构、来源定位与版本信息。"}
                </p>
              </header>
              {blocks.map((block, i) => {
                const isActive = canEdit && active === block.block_id;
                return (
                  <section
                    key={block.block_id}
                    ref={(element) => {
                      if (element)
                        positions.current.set(block.block_id, element);
                      else positions.current.delete(block.block_id);
                    }}
                    data-segment={block.block_id}
                    data-kind={block.block_type}
                    className={`document-segment ${isActive ? "is-editing" : ""} ${matchedIds.has(block.block_id) ? "find-match" : ""}`}
                  >
                    {canEdit && (
                      <button
                        type="button"
                        className="segment-handle"
                        aria-label={`编辑第 ${i + 1} 段`}
                        title={`第 ${i + 1} 段 · ${blockNames[block.block_type]}`}
                        onClick={() => {
                          setActive(block.block_id);
                          setInspector(true);
                        }}
                      >
                        <DotsSixVertical />
                      </button>
                    )}
                    {isActive ? (
                      <div
                        className="segment-editor"
                        data-testid="active-segment-editor"
                      >
                        {renderEditor?.(block)}
                      </div>
                    ) : (
                      <div
                        className="segment-render"
                        onClick={(e) => {
                          if (
                            canEdit &&
                            !(e.target as Element).closest("a,button")
                          )
                            setActive(block.block_id);
                        }}
                      >
                        {renderBlock(block, highlightedId === block.block_id)}
                      </div>
                    )}
                    {isActive && (
                      <div className="segment-footer">
                        <span>
                          第 {i + 1} 段 · {blockNames[block.block_type]}
                        </span>
                        <button
                          type="button"
                          onClick={() => {
                            setActive(undefined);
                            setInspector(false);
                          }}
                        >
                          <Check />
                          完成本段
                        </button>
                      </div>
                    )}
                  </section>
                );
              })}
              {!blocks.length && (
                <div className="document-empty">
                  <p>开始记录你的业务知识</p>
                  {editable && (
                    <button type="button" onClick={() => add("paragraph")}>
                      <Plus />
                      添加第一段
                    </button>
                  )}
                </div>
              )}
              {canEdit && (
                <button
                  type="button"
                  className="document-add-tail"
                  onClick={() => {
                  const fresh = insertBlock(blocks, "paragraph");
                  inserted.current = fresh.id;
                    onChange?.(fresh.blocks);
                    setActive(fresh.id);
                    requestAnimationFrame(() => jump(fresh.id));
                  }}
                >
                  <Plus />
                  在文稿末尾添加段落
                </button>
              )}
              <footer className="document-paper-foot">
                文稿结束 · 共 {blocks.length} 段
              </footer>
            </div>
          </div>
          {inspector && selected && canEdit && (
            <aside className="segment-inspector" aria-label="当前段落属性">
              <header>
                <h3>段落属性</h3>
                <button
                  type="button"
                  aria-label="关闭段落属性"
                  onClick={() => setInspector(false)}
                >
                  <X />
                </button>
              </header>
              <p>
                第 {currentIndex + 1} 段 · {blockNames[selected.block_type]}
              </p>
              <div className="inspector-actions">
                <button
                  type="button"
                  aria-label="上移当前段落"
                  disabled={currentIndex <= 0}
                  onClick={() => move(-1)}
                >
                  <ArrowUp />
                  上移
                </button>
                <button
                  type="button"
                  aria-label="下移当前段落"
                  disabled={currentIndex >= blocks.length - 1}
                  onClick={() => move(1)}
                >
                  <ArrowDown />
                  下移
                </button>
              </div>
              <h4>来源引用</h4>
              <button type="button" onClick={() => onCite?.(selected.block_id)}>
                <Link />
                添加引用
              </button>
              {renderCitations?.(selected)}
              <p className="inspector-note">
                仅改变线上文稿，不修改上传原件。保存后需按既有流程复核。
              </p>
              <button
                type="button"
                className="danger"
                onClick={() => {
                  if (
                    window.confirm(
                      "从当前草稿删除本段？未保存前可放弃整次更改。",
                    )
                  ) {
                    onChange?.(blocks.filter((b) => b.block_id !== active));
                    setActive(undefined);
                    setInspector(false);
                  }
                }}
              >
                <Trash />
                删除本段
              </button>
            </aside>
          )}
        </div>
      </div>
    </RichTextSession>
  );
}
