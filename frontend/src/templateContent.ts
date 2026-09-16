import type { Content, Version } from "./types";
export function contentFromTemplate(
  title: string,
  knowledgeType: string,
  template?: Version | null,
): Content {
  return {
    title,
    knowledge_type: knowledgeType,
    applicability: structuredClone(template?.applicability ?? {}),
    required_facts: [...(template?.required_facts ?? [])],
    // A template does not itself establish the legal validity of the new knowledge.
    legal_status: "UNKNOWN",
    valid_from: null,
    valid_to: null,
    blocks: structuredClone(template?.blocks ?? []).map((block, ordinal) => ({
      ...block,
      ordinal,
    })),
  };
}
