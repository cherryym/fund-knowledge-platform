"""Official Qwen reranker pins, independently obtained from HF metadata 2026-09-23."""

QWEN_RERANKER_SPEC = {
    "repo": "Qwen/Qwen3-Reranker-4B",
    "revision": "22e683669bc0f0bd69640a1354a6d0aebcfeede5",
    "weights": ("model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"),
    "files": {
        "config.json": (727, "git:b0d564fb244100a8bbcb6499da05485440bae0d2"),
        "generation_config.json": (214, "git:e4f1d3193e99a3e5d7047edf56e93a6e933fa31b"),
        "tokenizer_config.json": (9706, "git:7345216a0785dc7086e8c245b2a9d3896ce2b756"),
        "special_tokens_map.json": (613, "git:ac23c0aaa2434523c494330aeb79c58395378103"),
        "tokenizer.json": (11422654, "sha256:aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"),
        "vocab.json": (2776833, "git:4783fe10ac3adce15ac8f358ef5462739852c569"),
        "merges.txt": (1671853, "git:31349551d90c7606f325fe0f11bbb8bd5fa0d7c7"),
        "model.safetensors.index.json": (32819, "git:c8dc285661c390e68d4864b34cbe7ec6f755b776"),
        "chat_template.jinja": (741, "git:63b97f268a9ddb9c6b34e6d7d8ef531d1fee9cf4"),
        "config_sentence_transformers.json": (325, "git:c6f8a7e0240028086cbce6b5c018874cf48cbeb7"),
        "sentence_bert_config.json": (362, "git:813a773d3bf36aa61b1760d80070c9d72c87e270"),
        "modules.json": (280, "git:008ceadb8aca5de2810344509a4ac73352f23428"),
        "1_LogitScore/config.json": (57, "git:d2c77fa4fc4e2bdba0fbd2caa97f7272f7c9e967"),
        "model-00001-of-00002.safetensors": (4058781760, "sha256:cf2e87cbf71fa628961532232e04dd6c19702a0a057f5e2aff95ea1aca4fd488"),
        "model-00002-of-00002.safetensors": (3984833200, "sha256:78946d22b7f6456ea7a5358dbdf3982de36c5bac1f166a5fd58e18e31db8048a"),
    },
}

DEFAULT_RERANK_INSTRUCTION = "Given a question, retrieve passages containing relevant rules, conditions, exceptions, and procedures."
