// Pure read rendering: no editor engine, graph runtime or build dialog imports.
import { memo } from "react";
import { Link } from "@phosphor-icons/react";
import type { Block } from "./types";
import { richHtml } from "./richText";
import { purposeLabels } from "./Pickers";
import { ErrorBox, readable, useApp, useTask } from "./ui";

export const blockNames = {
  heading: "标题",
  paragraph: "正文",
  list: "列表",
  table: "表格",
  step: "操作步骤",
  warning: "注意事项",
  formula: "公式",
  image: "图片引用",
  attachment: "附件引用",
};
export function blockText(block: Block) {
  if (block.block_type === "table")
    return [
      block.data.columns,
      ...(Array.isArray(block.data.rows) ? block.data.rows : []),
    ]
      .map((r) => (Array.isArray(r) ? r.join(" | ") : readable(r)))
      .join("\n");
  if (block.block_type === "list")
    return ((block.data.items as string[]) ?? []).join("\n");
  if (block.block_type === "step")
    return ["action", "owner_role", "output", "verification"]
      .map((k) => readable(block.data[k]))
      .join("\n");
  return readable(
    block.data.text ?? block.data.expression_text ?? block.data.caption,
  );
}
/** One entry per cited source version, in first-cited order: a paragraph citing whole sections can carry hundreds of
 * source blocks from a handful of documents. Opens the first cited block of each document. */
export function citationGroups(citations: Block["citations"]) {
  const groups = new Map<string, { version_id: string; block_id: string; purposes: string[]; count: number }>();
  for (const citation of citations) {
    const group = groups.get(citation.version_id);
    if (!group) {
      groups.set(citation.version_id, { version_id: citation.version_id, block_id: citation.block_id,
        purposes: [citation.purpose], count: 1 });
    } else {
      group.count += 1;
      if (!group.purposes.includes(citation.purpose)) group.purposes.push(citation.purpose);
    }
  }
  return [...groups.values()];
}
export const BlockView = memo(function BlockView({
  block,
  highlighted = false,
  compact = false,
  sourceNames,
}: {
  block: Block;
  highlighted?: boolean;
  compact?: boolean;
  /** Source titles by version id, where the caller already holds the page's readable sources. */
  sourceNames?: Readonly<Record<string, string>>;
}) {
  const app = useApp();
  const wikiLinkTask = useTask();
  const d = block.data;
  const Heading = ("h" + Math.max(1, Math.min(6, Number(d.level ?? 2)))) as
    "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
  return (
    <article
      id={`block-${block.block_id}`}
      className={`content-block ${compact ? "compact-block" : ""} ${highlighted ? "highlight-block" : ""}`}
      onClick={event=>{
        const target=(event.target as Element).closest<HTMLElement>("[data-wiki-title]");
        if(target){event.preventDefault();event.stopPropagation();if(!wikiLinkTask.busy)void wikiLinkTask.run(async()=>{await app.openWikiTitle?.(target.dataset.wikiTitle??"");});}
      }}
      onKeyDown={event=>{
        if(!["Enter"," "].includes(event.key))return;
        const target=(event.target as Element).closest<HTMLElement>("[data-wiki-title]");
        if(target){event.preventDefault();event.stopPropagation();if(!wikiLinkTask.busy)void wikiLinkTask.run(async()=>{await app.openWikiTitle?.(target.dataset.wikiTitle??"");});}
      }}
    >
      <ErrorBox error={wikiLinkTask.error}/>
      {!compact && (
        <small className="block-locator">
          {blockNames[block.block_type]}{" "}
          {readable(
            block.locator.label ??
              block.locator.source_page ??
              block.locator.cell,
          )}
        </small>
      )}
      {block.block_type === "heading" ? (
        <Heading
          dangerouslySetInnerHTML={{
            __html: richHtml(readable(d.text), d.text_format, true),
          }}
        />
      ) : block.block_type === "paragraph" || block.block_type === "warning" ? (
        <div
          className={`rendered-rich-text ${block.block_type === "warning" ? "warning-text" : ""}`}
          dangerouslySetInnerHTML={{
            __html: richHtml(readable(d.text), d.text_format),
          }}
        />
      ) : block.block_type === "list" ? (
        d.ordered ? (
          <ol>
            {((d.items as string[]) ?? []).map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ol>
        ) : (
          <ul>
            {((d.items as string[]) ?? []).map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        )
      ) : block.block_type === "table" ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {((d.columns as string[]) ?? []).map((c, i) => (
                  <th key={i}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {((d.rows as string[][]) ?? []).map((row, i) => (
                <tr key={i}>
                  {row.map((cell, j) => (
                    <td key={j}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : block.block_type === "step" ? (
        <div className="sop-render">
          <p>{readable(d.action)}</p>
          <dl>
            <dt>责任角色</dt>
            <dd>{readable(d.owner_role)}</dd>
            <dt>输出</dt>
            <dd>{readable(d.output)}</dd>
            <dt>验证要求</dt>
            <dd>{readable(d.verification)}</dd>
          </dl>
        </div>
      ) : ["attachment", "image"].includes(block.block_type) ? (
        <button
          type="button"
          className="text-button"
          onClick={() => app.openVersion(readable(d.version_id))}
        >
          <Link />
          {readable(d.caption) || "查看受控附件"}
        </button>
      ) : (
        <p>{blockText(block)}</p>
      )}
      {block.citations.length > 0 && (
        <div className="citation-links">
          {citationGroups(block.citations).map((group, i) => (
            <button
              type="button"
              key={group.version_id}
              className="citation-chip"
              onClick={() => app.openVersion(group.version_id, group.block_id)}
            >
              <Link size={13} />
              {sourceNames?.[group.version_id] ? `《${sourceNames[group.version_id]}》` : `来源 ${i + 1}`}
              {" · "}{group.purposes.map((purpose) => purposeLabels[purpose] ?? purpose).join("、")}
              {group.count > 1 && ` · ${group.count}段`}
            </button>
          ))}
        </div>
      )}
    </article>
  );
});
