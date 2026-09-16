// Synthetic profile metadata only. These fixtures never load a model or a database.
export const qwen = {
  id: "qwen", name: "Qwen 检索", model: "Qwen/Qwen3-Embedding-0.6B", dimensions: 1024,
  fingerprint: "a".repeat(64), is_default: true, available: true, state: "READY", brand: "qwen",
  bm25: true, reranker_model: "Qwen/Qwen3-Reranker-0.6B", model_ready: true,
  index_ready: true, index_state: "READY", can_index: true,
};
export const bge = {
  ...qwen, id: "bge", name: "BGE 检索", model: "BAAI/bge-m3", fingerprint: "b".repeat(64),
  is_default: false, brand: "baai", reranker_model: "BAAI/bge-reranker-v2-m3",
};
export const profiles = (items = [qwen, bge], defaultId = "qwen") => ({
  enabled: true, default_profile_id: defaultId, items,
});
export const selection = item => ({ profile_id: item.id, fingerprint: item.fingerprint });
export const frozenSelection = item => ({ ...selection(item), model: item.model, dimensions: item.dimensions });
export const retrievalKey = (user, space) => `fund-kb:retrieval-profile:v1:${encodeURIComponent(user)}:${encodeURIComponent(space)}`;
