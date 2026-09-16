import type { Block } from "./types";

export const blockNames: Record<Block["block_type"], string> = {
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
export function createBlock(type: Block["block_type"]): Block {
  const data =
    type === "heading"
      ? { level: 2, text: "" }
      : type === "list"
        ? { ordered: false, items: [""] }
        : type === "table"
          ? { columns: ["项目", "核对要求"], rows: [["", ""]] }
          : type === "step"
            ? { action: "", owner_role: "", output: "", verification: "" }
            : type === "formula"
              ? { expression_text: "", unit: "", calculator_ref: null }
              : ["image", "attachment"].includes(type)
                ? { version_id: "", caption: "" }
                : { text: "" };
  return {
    block_id: crypto.randomUUID(),
    ordinal: 0,
    block_type: type,
    data,
    locator: {},
    citations: [],
  };
}
export function reorderBlock(
  blocks: Block[],
  id: string,
  offset: number,
): Block[] {
  const from = blocks.findIndex((b) => b.block_id === id),
    to = from + offset;
  if (from < 0 || to < 0 || to >= blocks.length) return blocks;
  const next = [...blocks];
  [next[from], next[to]] = [next[to], next[from]];
  return next;
}
export function insertBlock(
  blocks: Block[],
  type: Block["block_type"],
  afterId?: string,
): { blocks: Block[]; id: string } {
  const fresh = createBlock(type),
    index = afterId ? blocks.findIndex((b) => b.block_id === afterId) : -1;
  const position = index < 0 ? blocks.length : index + 1;
  return {
    blocks: [...blocks.slice(0, position), fresh, ...blocks.slice(position)],
    id: fresh.block_id,
  };
}
