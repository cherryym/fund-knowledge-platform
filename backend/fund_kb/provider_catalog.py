"""Official-source catalog snapshot, NOT proof of account access or a live invocation."""
from __future__ import annotations

from copy import deepcopy

CATALOG_DATE = "2026-09-07"


def _models(brand, source, entries):
    return [{"id": model_id, "name": name, "brand": brand, "flagship": flagship,
        "source_url": source, "availability": "UNKNOWN"} for model_id, name, flagship in entries]


def _provider(id, name, kind, protocol, base_url, source_url, entries=(), verified=False):
    return {"id": id, "name": name, "kind": kind, "protocol": protocol, "base_url": base_url,
        "icon": id, "models": _models(id, source_url, entries),
        "verified_at": CATALOG_DATE if verified else None, "source_url": source_url}


CATALOG = [
    _provider("chatgpt-codex", "ChatGPT / Codex 官方登录", "subscription", "codex_app_server", "",
        "https://learn.chatgpt.com/docs/app-server#auth-endpoints"),
    _provider("openai", "OpenAI", "direct", "responses", "https://api.openai.com/v1",
        "https://developers.openai.com/api/docs/models", [
            ("gpt-6-astra", "GPT-6 Astra", True), ("gpt-5.6-sol", "GPT-5.6 Sol", True),
            ("gpt-5.6-terra", "GPT-5.6 Terra", False), ("gpt-5.6-luna", "GPT-5.6 Luna", False)], True),
    _provider("anthropic", "Anthropic", "direct", "anthropic", "https://api.anthropic.com/v1",
        "https://platform.claude.com/docs/en/models/overview", [
            ("claude-fable-5-1", "Claude Fable 5.1", True), ("claude-opus-5", "Claude Opus 5", True),
            ("claude-sonnet-5", "Claude Sonnet 5", False),
            ("claude-haiku-4-5-20251001", "Claude Haiku 4.5", False)], True),
    _provider("google", "Google Gemini", "direct", "gemini", "https://generativelanguage.googleapis.com/v1beta",
        "https://ai.google.dev/gemini-api/docs/models", [
            ("gemini-3.8-flash", "Gemini 3.8 Flash", True), ("gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview", True),
            ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite", False)], True),
    _provider("deepseek", "DeepSeek", "direct", "openai", "https://api.deepseek.com", "https://api-docs.deepseek.com/quick_start/pricing/",
        [("deepseek-v4-pro", "DeepSeek V4 Pro", True), ("deepseek-v4-flash", "DeepSeek V4 Flash", False)], True),
    _provider("qwen", "通义千问 Qwen", "direct", "openai", "https://dashscope.aliyuncs.com/compatible-mode/v1", "https://help.aliyun.com/zh/model-studio/text-generation-model",
        [("qwen3.8-max", "Qwen3.8 Max", True), ("qwen3.8-max-0902", "Qwen3.8 Max 0902", False),
            ("qwen3.7-plus", "Qwen3.7 Plus", False), ("qwen3.8-flash", "Qwen3.8 Flash", False)], True),
    _provider("moonshot", "Moonshot / Kimi", "direct", "openai", "https://api.moonshot.cn/v1", "https://platform.kimi.com/docs/models",
        [("kimi-k3", "Kimi K3", True), ("kimi-k2.7-code", "Kimi K2.7 Code", False)], True),
    _provider("zhipu", "智谱 GLM", "direct", "openai", "https://open.bigmodel.cn/api/paas/v4", "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.2",
        [("glm-5.2", "GLM-5.2", True)], True),
    _provider("doubao", "豆包 / 火山方舟", "direct", "openai", "https://ark.cn-beijing.volces.com/api/v3", "https://github.com/volcengine/ark-runtime-python",
        [("doubao-seed-2-1-pro-260628", "Doubao Seed 2.1 Pro", True)], True),
    _provider("minimax", "MiniMax", "direct", "openai", "https://api.minimax.io/v1", "https://platform.minimax.io/docs/api-reference/text-openai-api",
        [("MiniMax-M3", "MiniMax M3", True), ("MiniMax-M2.7", "MiniMax M2.7", False)], True),
    _provider("mistral", "Mistral AI", "direct", "openai", "https://api.mistral.ai/v1", "https://docs.mistral.ai/models/mistral-medium-3-5-26-04",
        [("mistral-medium-3-5", "Mistral Medium 3.5", True)], True),
    _provider("xai", "xAI / Grok", "direct", "openai", "https://api.x.ai/v1", "https://docs.x.ai/developers/models",
        [("grok-4.6", "Grok 4.6", True)], True),
    _provider("openrouter", "OpenRouter", "gateway", "openai", "https://openrouter.ai/api/v1", "https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties", verified=True),
    _provider("siliconflow", "硅基流动 SiliconFlow", "gateway", "openai", "https://api.siliconflow.com/v1", "https://docs.siliconflow.com/en/api-reference/models/get-model-list", verified=True),
    _provider("ollama", "Ollama", "local", "ollama", "http://127.0.0.1:11434", "https://docs.ollama.com/api/chat", verified=True),
    _provider("lmstudio", "LM Studio", "local", "openai", "http://127.0.0.1:1234/v1", "https://lmstudio.ai/docs/developer/openai-compat", verified=True),
    _provider("local-openai", "本地兼容服务", "local", "openai", "http://127.0.0.1:8000/v1", "https://docs.vllm.ai/en/latest/serving/online_serving/", verified=True),
    _provider("custom", "自定义兼容服务", "custom", "openai", "", "https://platform.openai.com/docs/api-reference/chat"),
]
PROTOCOLS = {"openai", "responses", "anthropic", "gemini", "ollama", "codex_app_server"}

PARAMETER_NOTES = {
    "chatgpt-codex": "官方 app-server 账号登录与模型目录；严格文本隔离尚未验证，推理阻断。ChatGPT 套餐不等于 Platform API 余额。",
    "openai": "Responses优先；GPT-6 Astra只使用low及以上reasoning，不发送temperature/top_p/logprobs；工具调用需Responses且本应用不执行工具。",
    "anthropic": "原生Messages，system独立字段，x-api-key与anthropic-version；当前文本适配通过提示与应用JSON校验约束输出。",
    "google": "原生generateContent，systemInstruction与generationConfig；JSON通过responseMimeType，API密钥放请求头。",
    "deepseek": "V4支持Chat/Responses；不强制采样参数，保留其服务端思考默认值，JSON模式带明确JSON指令。",
    "qwen": "按地区控制台设置兼容端点；本适配为非流式JSON请求，支持时关闭enable_thinking以避免非流式限制。",
    "moonshot": "当前预置Kimi K3；K2.5和moonshot-v1已于2026-08-31下线；不注入旧版本采样参数。",
    "doubao": "预置使用官方SDK中的完整带日期模型ID；独立推理接入点ep-*可手工添加，账户权限仍需同步/测试。",
    "minimax": "M3使用max_completion_tokens和reasoning_split；不发送未核验的response_format，JSON由提示与应用校验约束。",
    "openrouter": "中转厂商与模型品牌分列；同步模型参数能力，只有支持response_format时发送该参数。",
    "siliconflow": "默认官方国际端点；部署人可显式设置所用地区端点；仅同步text类模型，不自动挑选图像或嵌入模型。",
    "ollama": "原生/api/chat、/api/tags，stream=false、format=json、options.num_predict；只列已安装/手工配置模型，不下载权重。",
    "lmstudio": "OpenAI兼容Chat/Responses；模型ID取实际同步列表，不预置不存在的本地模型。",
    "local-openai": "通用本地OpenAI兼容服务，可配置vLLM/oMLX等已运行服务；只同步或手工配置模型，复用本地主机白名单，不下载权重。",
    "custom": "协议由管理员明确选择；未知Chat能力不强加response_format，使用JSON提示并做应用层校验。",
}
for _item in CATALOG:
    _item["parameter_notes"] = PARAMETER_NOTES.get(_item["id"], "使用已选协议的文本参数；不注入未核验的采样、工具或推理参数。")
    _item["account_availability"] = "UNKNOWN"
    if _item["id"] == "local-openai":
        _item["icon"] = "local"
    if _item["id"] == "chatgpt-codex":
        _item["icon"] = "openai"
        _item["verified_at"] = "2026-09-08"


def provider_by_id(provider_id):
    return next((deepcopy(x) for x in CATALOG if x["id"] == provider_id), None)


def public_catalog():
    return {"items": deepcopy(CATALOG), "live_verified": False}


def preset_models(provider_id):
    provider = provider_by_id(provider_id)
    return [{"id": model["id"], "name": model["name"], "brand": model["brand"],
        "flagship": model["flagship"], "source": "preset"} for model in (provider or {}).get("models", [])]
