"""Runtime-editable model configuration.

The MVP intentionally stores user-entered credentials as plain text on the local
server. API responses are always masked and trace payloads are redacted.
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .db import DATA_DIR

CONFIG_PATH = Path(os.getenv("MODEL_CONFIG_PATH", str(DATA_DIR / "model-config.json")))

LLM_STAGES = [
    "document_classification", "general_extraction", "payment_extraction",
    "change_extraction", "contract_extraction", "semantic_consistency",
    "policy_retrieval", "text2sql", "result_summary", "visual_general",
    "visual_table_or_chart", "visual_seal_signature", "visual_form_checkbox",
]
ALL_STAGES = [*LLM_STAGES, "ocr"]


def _default_config() -> dict[str, Any]:
    cloud_url = os.getenv("CLOUD_LLM_BASE_URL", "")
    cloud_key = os.getenv("CLOUD_LLM_API_KEY", "")
    fast = os.getenv("CLOUD_LLM_FAST_MODEL", "")
    reasoning = os.getenv("CLOUD_LLM_REASONING_MODEL", fast)
    providers = [
        {
            "id": "cloud_llm", "name": "云端 OpenAI 兼容模型", "kind": "openai",
            "base_url": cloud_url, "api_key": cloud_key, "api_key_env": "CLOUD_LLM_API_KEY",
            "enabled": bool(cloud_url), "source": "environment",
            "models": [m for m in dict.fromkeys([fast, reasoning]) if m],
        },
        {
            "id": "mineru_cloud", "name": "MinerU 云端 OCR", "kind": "mineru",
            "base_url": os.getenv("MINERU_BASE_URL", "https://mineru.net/api/v4"),
            "api_key": os.getenv("MINERU_TOKEN", ""), "api_key_env": "MINERU_TOKEN",
            "enabled": bool(os.getenv("MINERU_TOKEN")), "source": "environment",
            "models": [os.getenv("MINERU_MODEL_VERSION", "vlm")],
        },
        {
            "id": "mineru_local", "name": "MinerU 本地 OCR（VLM）", "kind": "mineru-local",
            "base_url": os.getenv("LOCAL_MINERU_BASE_URL", "http://host.docker.internal:8000"), "api_key": "",
            "enabled": bool(os.getenv("LOCAL_MINERU_BASE_URL")), "source": "environment", "models": ["vlm-engine"],
        },
    ]
    default_fast = fast or ""
    routes = {stage: {"provider_id": "cloud_llm", "model": default_fast} for stage in LLM_STAGES}
    for stage in ("semantic_consistency", "policy_retrieval", "result_summary"):
        routes[stage] = {"provider_id": "cloud_llm", "model": reasoning or default_fast}
    routes["ocr"] = {"provider_id": "mineru_cloud", "model": os.getenv("MINERU_MODEL_VERSION", "vlm")}
    return {"schema_version": 1, "providers": providers, "stage_routes": routes}


def _normalize(config: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    result["schema_version"] = 1
    result.setdefault("providers", [])
    result.setdefault("stage_routes", {})
    for provider in result["providers"]:
        provider["id"] = re.sub(r"[^a-zA-Z0-9_-]", "_", str(provider.get("id") or "provider"))[:48]
        provider["name"] = str(provider.get("name") or provider["id"])[:100]
        provider["kind"] = provider.get("kind") if provider.get("kind") in {"openai", "mineru", "mineru-local"} else "openai"
        provider["base_url"] = str(provider.get("base_url") or "").rstrip("/")
        provider["api_key"] = str(provider.get("api_key") or "")
        provider["models"] = list(dict.fromkeys(str(x) for x in provider.get("models", []) if str(x).strip()))
        provider["enabled"] = bool(provider.get("enabled", True))
        provider.setdefault("source", "user")
    ids = {p["id"] for p in result["providers"]}
    if len(ids) != len(result["providers"]):
        raise ValueError("供应商 ID 不能重复")
    default_llm=next((p["id"] for p in result["providers"] if p["kind"]=="openai"),None)
    default_ocr=next((p["id"] for p in result["providers"] if p["kind"].startswith("mineru")),None)
    for stage in ALL_STAGES:
        route = result["stage_routes"].setdefault(stage, {})
        route.setdefault("provider_id", default_ocr if stage == "ocr" else default_llm)
        route.setdefault("model", "vlm" if stage == "ocr" else "")
        selected = next((p for p in result["providers"] if p["id"] == route["provider_id"]), None)
        if not selected:
            raise ValueError(f"阶段 {stage} 引用了不存在的供应商 {route['provider_id']}")
        if stage == "ocr" and not selected["kind"].startswith("mineru"):
            raise ValueError("OCR 阶段只能选择 MinerU 供应商")
        if stage == "ocr" and selected["kind"] == "mineru-local":
            if str(route.get("model") or "vlm-engine") != "vlm-engine":
                raise ValueError("本地 MinerU 只允许 vlm-engine；hybrid-engine/hybrid-http-client 已禁用以避免 OOM")
            route["model"] = "vlm-engine"
        if stage != "ocr" and selected["kind"] != "openai":
            raise ValueError(f"阶段 {stage} 只能选择 OpenAI-compatible LLM")
    return result


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        save_config(_default_config())
    try:
        return _normalize(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        # Never destroy a malformed operator-owned file. The UI can repair it via PUT.
        return _normalize(_default_config())


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize(config)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="model-config-", suffix=".tmp", dir=CONFIG_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(normalized, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, CONFIG_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return normalized


def effective_api_key(provider: dict[str, Any]) -> str:
    return str(provider.get("api_key") or os.getenv(str(provider.get("api_key_env") or ""), ""))


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:3]}••••{value[-4:]}"


def public_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    result = copy.deepcopy(config or load_config())
    for provider in result["providers"]:
        secret = effective_api_key(provider)
        provider["api_key"] = ""
        provider["api_key_masked"] = mask_secret(secret)
        if provider["kind"] == "mineru":
            provider["configured"] = bool(provider.get("base_url") and secret)
        else:
            # OpenAI-compatible local runtimes commonly require no API key;
            # connectivity is verified separately by the provider test action.
            provider["configured"] = bool(provider.get("base_url"))
    return result


def merge_public_update(incoming: dict[str, Any]) -> dict[str, Any]:
    """Preserve an existing key when the browser submits an empty key field."""
    current = load_config()
    old = {p["id"]: p for p in current["providers"]}
    candidate = copy.deepcopy(incoming)
    for provider in candidate.get("providers", []):
        if not provider.get("api_key") and provider.get("id") in old:
            provider["api_key"] = old[provider["id"]].get("api_key", "")
            provider.setdefault("api_key_env", old[provider["id"]].get("api_key_env", ""))
    return save_config(candidate)


def snapshot(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    config = load_config()
    routes = copy.deepcopy(config["stage_routes"])
    for stage, value in (overrides or {}).items():
        if stage in ALL_STAGES and isinstance(value, dict):
            routes[stage] = {**routes.get(stage, {}), **value}
    providers = []
    for item in config["providers"]:
        provider = copy.deepcopy(item)
        provider["api_key"] = "[REDACTED]" if effective_api_key(provider) else ""
        providers.append(provider)
    return {"schema_version": config["schema_version"], "providers": providers, "stage_routes": routes}


def resolve_route(stage: str, overrides: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    config = load_config()
    route = {**config["stage_routes"].get(stage, {}), **((overrides or {}).get(stage, {}))}
    provider = next((p for p in config["providers"] if p["id"] == route.get("provider_id")), None)
    if not provider:
        raise RuntimeError(f"阶段 {stage} 未配置有效供应商")
    if not provider.get("enabled"):
        raise RuntimeError(f"供应商 {provider['name']} 已停用")
    return provider, str(route.get("model") or "")
