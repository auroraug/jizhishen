"""OpenAI-compatible LLM and MinerU adapters selected by runtime stage routes."""
from __future__ import annotations

import hashlib
import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .model_config import effective_api_key, load_config, public_config, resolve_route


def _base(value: str) -> str:
    return value.rstrip("/")


def _openai_base(value: str) -> str:
    # An OpenAI-compatible base URL is the complete API root supplied by the
    # operator. Some providers use /v1 while others use roots such as /api/v3.
    return _base(value)


def _openai_headers(provider: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    secret = effective_api_key(provider)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def provider_status():
    return public_config()["providers"]


async def test_provider(provider_id: str):
    config = load_config()
    provider = next((p for p in config["providers"] if p["id"] == provider_id), None)
    if not provider:
        return {"ok": False, "message": "供应商不存在", "status_code": 404}
    started = time.perf_counter()
    try:
        if not provider.get("base_url"):
            raise RuntimeError("Base URL 未配置")
        if provider["kind"] == "mineru":
            if not effective_api_key(provider):
                raise RuntimeError("MinerU Token 未配置")
            return {"ok": True, "latency_ms": 0, "message": "Token 和服务地址已配置；上传任务时在线鉴权"}
        if provider["kind"] == "mineru-local":
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.get(_base(provider["base_url"]) + "/health")
        else:
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.get(_openai_base(provider["base_url"]) + "/models",
                    headers=_openai_headers(provider))
        response.raise_for_status()
        return {"ok": True, "latency_ms": round((time.perf_counter()-started)*1000), "message": "连接正常"}
    except Exception as exc:
        return {"ok": False, "latency_ms": round((time.perf_counter()-started)*1000), "message": str(exc)[:300]}


async def test_provider_config(provider: dict[str, Any]):
    """Test an unsaved provider form without mutating runtime configuration."""
    provider={**provider,"base_url":str(provider.get("base_url") or "").rstrip("/")}
    started=time.perf_counter()
    try:
        if not provider.get("base_url"):raise RuntimeError("Base URL 未配置")
        kind=provider.get("kind") or "openai"
        if kind=="mineru":
            if not effective_api_key(provider):raise RuntimeError("MinerU Token 未配置")
            return {"ok":True,"latency_ms":0,"message":"Token 与服务地址已配置"}
        if kind=="mineru-local":
            async with httpx.AsyncClient(timeout=12) as client:response=await client.get(_base(provider["base_url"])+"/health")
        else:
            async with httpx.AsyncClient(timeout=12) as client:response=await client.get(_openai_base(provider["base_url"])+"/models",headers=_openai_headers(provider))
        response.raise_for_status();payload=response.json() if response.content else {}
        ids=[str(x.get("id")) for x in payload.get("data",[]) if isinstance(x,dict) and x.get("id")]
        requested=str(provider.get("model_id") or ((provider.get("models") or [""])[0]) or "")
        if kind=="openai" and requested:
            async with httpx.AsyncClient(timeout=60) as client:
                probe=await client.post(_openai_base(provider["base_url"])+"/chat/completions",headers=_openai_headers(provider),
                    json={"model":requested,"messages":[{"role":"user","content":"Reply OK"}],"temperature":0,"max_tokens":1})
            probe.raise_for_status()
        return {"ok":True,"latency_ms":round((time.perf_counter()-started)*1000),"message":"连接正常",
                "models":ids,"model_accepted":True,
                "server_model_ids":ids}
    except Exception as exc:
        return {"ok":False,"latency_ms":round((time.perf_counter()-started)*1000),"message":str(exc)[:300]}


async def discover_models(provider_id: str) -> dict[str, Any]:
    config = load_config()
    provider = next((p for p in config["providers"] if p["id"] == provider_id), None)
    if not provider or provider["kind"] != "openai":
        raise ValueError("只有 OpenAI-compatible 供应商支持模型发现")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(_openai_base(provider["base_url"]) + "/models",
            headers=_openai_headers(provider))
        response.raise_for_status()
        data = response.json()
    raw_items=[str(item.get("id")) for item in data.get("data", []) if item.get("id")]
    # llama.cpp commonly advertises the absolute GGUF path even when its API
    # accepts a stable alias. Never leak that host-specific path into the model
    # catalog: it is a deployment detail, not the operator-facing Model ID.
    def request_id(value: str) -> str:
        if value.lower().endswith(".gguf") and ("/" in value or "\\" in value):
            return Path(value).stem
        return value
    items=list(dict.fromkeys(request_id(value) for value in raw_items))
    return {"items":items,"raw_items":raw_items,"raw_count":len(raw_items),
            "normalized":items!=raw_items,"normalization":"absolute GGUF path → filename stem" if items!=raw_items else None}


async def chat_with_trace(messages, purpose="fast", json_mode=False, max_tokens=2400,
                          stage: str | None = None, route_overrides: dict[str, Any] | None = None,
                          temperature: float = .1):
    stage = stage or ("semantic_consistency" if purpose == "reasoning" else "general_extraction")
    provider, model = resolve_route(stage, route_overrides)
    if provider["kind"] != "openai":
        raise RuntimeError(f"阶段 {stage} 需要 OpenAI-compatible LLM，当前为 {provider['kind']}")
    if not provider.get("base_url") or not model:
        raise RuntimeError(f"阶段 {stage} 的 Base URL 或模型名未配置")
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    request_hash = hashlib.sha256(json.dumps({"url": _openai_base(provider["base_url"]), "payload": payload},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    started_at = datetime.now().isoformat(timespec="milliseconds")
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(_openai_base(provider["base_url"]) + "/chat/completions",
            headers=_openai_headers(provider), json=payload)
        status_code = response.status_code
        response.raise_for_status()
        data = response.json()
    duration_ms = round((time.perf_counter()-started)*1000)
    usage = data.get("usage") or {}
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    safe_headers = {k: v for k, v in response.headers.items() if k.lower() in {"content-type", "x-request-id", "request-id", "date"}}
    return {
        "content": content, "provider": provider["name"], "provider_id": provider["id"], "model": model,
        "duration_ms": duration_ms, "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"), "usage": usage,
        "request_hash": request_hash, "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="milliseconds"),
        "request": {"method": "POST", "url": _openai_base(provider["base_url"]) + "/chat/completions",
                    "headers": {"Authorization": "[REDACTED]", "Content-Type": "application/json"}, "json": payload},
        "raw_response": data, "response_headers": safe_headers, "status_code": status_code,
        "finish_reason": choice.get("finish_reason"), "assistant_message": message,
    }


async def openai_chat(messages, purpose="fast", json_mode=False):
    return (await chat_with_trace(messages, purpose, json_mode))["content"]


def mineru_provider(provider_id: str | None = None) -> tuple[dict[str, Any], str]:
    """Resolve a concrete MinerU provider without depending on a later route change.

    Parse jobs persist ``provider_id`` and pass it back while polling.  This is
    important because operators may switch the global OCR route while an
    immutable parse version is still running.
    """
    if provider_id:
        config = load_config()
        provider = next((item for item in config["providers"] if item["id"] == provider_id), None)
        if not provider:
            raise RuntimeError(f"MinerU 供应商 {provider_id} 不存在")
        route = config.get("stage_routes", {}).get("ocr", {})
        model = str(route.get("model") if route.get("provider_id")==provider_id else "") or str((provider.get("models") or [""])[0])
    else:
        provider, model = resolve_route("ocr")
    if not str(provider.get("kind", "")).startswith("mineru"):
        raise RuntimeError(f"OCR 阶段必须使用 MinerU，当前为 {provider.get('kind')}")
    if provider["kind"] == "mineru-local":
        model = model or "vlm-engine"
        if model != "vlm-engine":
            raise RuntimeError("本地 MinerU 仅允许 vlm-engine；hybrid-engine 已禁用以避免 OOM")
    return provider, model or "vlm"


async def mineru_submit_url(file_url: str, model_version=None):
    provider, configured_model = mineru_provider()
    if provider["kind"] != "mineru":
        raise RuntimeError("本地 MinerU 不支持 URL 提交，请上传文件")
    base = _base(provider["base_url"])
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(base + "/extract/task",
            headers={"Authorization": f"Bearer {effective_api_key(provider)}", "Content-Type": "application/json"},
            json={"url": file_url, "model_version": model_version or configured_model})
        response.raise_for_status()
        return response.json()


async def mineru_upload_file(path: Path, data_id: str, model_version=None):
    provider, configured_model = mineru_provider()
    if provider["kind"] != "mineru":
        return await mineru_upload_files([(path, data_id)], model_version, provider_id=provider["id"])
    token = effective_api_key(provider)
    if not token:
        raise RuntimeError("MinerU Token 未配置")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"files": [{"name": path.name, "data_id": data_id, "is_ocr": True}],
               "model_version": model_version or configured_model, "enable_table": True,
               "enable_formula": True, "language": "ch"}
    async with httpx.AsyncClient(timeout=60) as client:
        created = await client.post(_base(provider["base_url"]) + "/file-urls/batch", headers=headers, json=payload)
        created.raise_for_status()
        result = created.json()
        if result.get("code") != 0:
            raise RuntimeError(result.get("msg", "MinerU 上传链接申请失败"))
        info = result["data"]
        uploaded = await client.put(info["file_urls"][0], content=path.read_bytes(), headers={})
        uploaded.raise_for_status()
    return {"batch_id": info["batch_id"], "state": "waiting-file", "provider": provider["id"],
            "model": model_version or configured_model,
            "request": {"files": [{"name": path.name, "data_id": data_id, "is_ocr": True}], "model_version": model_version or configured_model},
            "response": {"batch_id": info["batch_id"], "upload_status": uploaded.status_code}}


async def mineru_upload_files(files: list[tuple[Path, str]], model_version=None, upload_concurrency: int = 8,
                              provider_id: str | None = None):
    """Submit one real MinerU batch and upload its local files with bounded concurrency."""
    if not files or len(files) > 200:
        raise ValueError("MinerU 批次文件数必须为 1-200")
    provider, configured_model = mineru_provider(provider_id)
    if provider["kind"] == "mineru-local":
        return await mineru_local_submit_files(files, provider, model_version or configured_model)
    token = effective_api_key(provider)
    if not token:
        raise RuntimeError("MinerU Token 未配置")
    selected_model = model_version or configured_model
    request_files = [{"name": path.name, "data_id": data_id, "is_ocr": True} for path, data_id in files]
    payload = {"files": request_files, "model_version": selected_model, "enable_table": True,
               "enable_formula": True, "language": "ch"}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=90) as client:
        created = await client.post(_base(provider["base_url"]) + "/file-urls/batch", headers=headers, json=payload)
        created.raise_for_status()
        result = created.json()
        if result.get("code") != 0:
            raise RuntimeError(result.get("msg", "MinerU 批量上传链接申请失败"))
        info = result["data"]
        if len(info.get("file_urls") or []) != len(files):
            raise RuntimeError("MinerU 返回的上传链接数量与文件数量不一致")
        semaphore = asyncio.Semaphore(max(1, min(int(upload_concurrency), 16)))
        async def upload_one(index, path, url):
            async with semaphore:
                response = await client.put(url, content=path.read_bytes(), headers={})
                response.raise_for_status()
                return {"index": index, "name": path.name, "status_code": response.status_code}
        uploads = await asyncio.gather(*[upload_one(i, item[0], info["file_urls"][i]) for i, item in enumerate(files)])
    return {"batch_id": info["batch_id"], "state": "waiting-file", "provider": provider["id"],
            "model": selected_model, "request": {"files": request_files, "model_version": selected_model,
            "enable_table": True, "enable_formula": True, "language": "ch"},
            "response": {"batch_id": info["batch_id"], "uploads": uploads}}


async def mineru_batch_result(batch_id: str, provider_id: str | None = None):
    provider, _ = mineru_provider(provider_id)
    if provider["kind"] == "mineru-local":
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(_base(provider["base_url"]) + f"/tasks/{batch_id}")
            response.raise_for_status()
            return response.json()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(_base(provider["base_url"]) + f"/extract-results/batch/{batch_id}",
            headers={"Authorization": f"Bearer {effective_api_key(provider)}"})
        response.raise_for_status()
        return response.json()


async def mineru_local_submit_files(files: list[tuple[Path, str]], provider: dict[str, Any],
                                    backend: str = "vlm-engine") -> dict[str, Any]:
    """Submit one asynchronous local MinerU task.

    ``hybrid-engine`` and ``hybrid-http-client`` are intentionally not exposed.
    The full ZIP response is requested so the normalizer receives Markdown,
    content-list JSON, model/middle JSON, images and the original document.
    """
    if backend != "vlm-engine":
        raise RuntimeError("本地 MinerU 仅允许 vlm-engine；hybrid-engine 已禁用以避免 OOM")
    fields = {
        "backend": "vlm-engine", "effort": "medium", "formula_enable": "true",
        "table_enable": "true", "image_analysis": "true", "return_md": "true",
        "return_middle_json": "true", "return_model_output": "true",
        "return_content_list": "true", "return_images": "true",
        "response_format_zip": "true", "return_original_file": "true",
    }
    multipart = [("files", (path.name, path.read_bytes(), "application/octet-stream")) for path, _ in files]
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(_base(provider["base_url"]) + "/tasks", data=fields, files=multipart)
        response.raise_for_status()
        result = response.json()
    task_id = str(result.get("task_id") or "")
    if not task_id:
        raise RuntimeError("本地 MinerU 未返回 task_id")
    safe_request = {**fields, "files": [{"name": path.name, "data_id": data_id,
        "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path, data_id in files]}
    return {"batch_id": task_id, "task_id": task_id, "state": result.get("status") or "pending",
            "provider": provider["id"], "provider_kind": provider["kind"], "model": "vlm-engine",
            "duration_ms": round((time.perf_counter()-started)*1000), "request": safe_request,
            "response": result}


async def mineru_local_result(task_id: str, provider_id: str) -> tuple[bytes, dict[str, Any]]:
    provider, model = mineru_provider(provider_id)
    if provider["kind"] != "mineru-local" or model != "vlm-engine":
        raise RuntimeError("任务不属于本地 MinerU vlm-engine")
    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.get(_base(provider["base_url"]) + f"/tasks/{task_id}/result")
        response.raise_for_status()
    return response.content, {"status_code": response.status_code,
        "content_type": response.headers.get("content-type"), "content_length": len(response.content),
        "etag": response.headers.get("etag")}
