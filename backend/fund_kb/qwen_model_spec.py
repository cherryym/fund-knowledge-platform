"""Official public Qwen4B files pinned from the Hugging Face API on 2026-09-12."""

QWEN4B_SPEC = {
    "repo": "Qwen/Qwen3-Embedding-4B",
    "revision": "5cf2132abc99cad020ac570b19d031efec650f2b",
    "weights": ("model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"),
    "files": {
        "config.json": (727, "git:8b4b87fc69023e7a224eb6563753aaf3223d8b98"),
        "tokenizer_config.json": (7256, "git:df3a9d96759529ca1006eb6db024bbb099a97578"),
        "tokenizer.json": (11422947, "sha256:83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d"),
        "vocab.json": (2776833, "git:4783fe10ac3adce15ac8f358ef5462739852c569"),
        "merges.txt": (1671853, "git:31349551d90c7606f325fe0f11bbb8bd5fa0d7c7"),
        "model.safetensors.index.json": (30431, "git:3d736ef26714eee0abde3e05104ee1b3ec26c974"),
        "1_Pooling/config.json": (313, "git:81de5602eacbce382009c5af7a23085871801d8f"),
        "config_sentence_transformers.json": (215, "git:76aef3ade63553ebb698fe3c2a3264040ed093f8"),
        "modules.json": (349, "git:952a9b81c0bfd99800fabf352f69c7ccd46c5e43"),
        "model-00001-of-00002.safetensors": (4965826464, "sha256:e70bfe3c970523fb7ef4eddffed2254ce3f1e7150c3de2af4342de129dd756f8"),
        "model-00002-of-00002.safetensors": (3077765624, "sha256:ed1b87c8e9eb7e535a1a155e4fd00d9f4dba80e58a6db48a4c9f82cede7079c1"),
    },
}

QWEN_QUERY_INSTRUCTION = (
    "Instruct: Given a fund operations or valuation question, retrieve source passages that provide "
    "relevant rules, applicability conditions, exceptions, calculations and accounting procedures.\nQuery: "
)
