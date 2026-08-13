"""Immutable published prompt versions and mutable drafts."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .db import db

DEFAULTS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "visual_general": (
        "你是工程资料视觉元素分析器。只描述图片中可直接观察到的工程相关内容，不推测身份、真伪或不可见信息。只返回 JSON。",
        "分析元素 {{element}}。返回 {\"description\":\"\",\"visible_text\":\"\",\"element_type\":\"image\",\"confidence\":0.0}。",
        {"type":"object","properties":{"description":{"type":"string"},"confidence":{"type":"number"}},"required":["description","confidence"]}),
    "visual_table_or_chart": (
        "你是工程图表视觉复核器。结构化表格主结果来自 MinerU HTML；你只处理图表或表格结构失败的视觉兜底，不得编造数值。只返回 JSON。",
        "分析元素 {{element}}。返回 {\"description\":\"\",\"visible_labels\":[],\"visible_values\":[],\"structure_warning\":\"\",\"confidence\":0.0}。",
        {"type":"object","properties":{"description":{"type":"string"},"confidence":{"type":"number"}},"required":["description","confidence"]}),
    "visual_seal_signature": (
        "你是工程资料印章签名可见性分析器。只能判断存在、位置、可见文字和关联区域；严禁鉴定真伪。只返回 JSON。",
        "分析元素 {{element}}。返回 {\"present\":false,\"visible_text\":\"\",\"related_party\":\"\",\"date_visible\":\"\",\"authenticity_assessed\":false,\"confidence\":0.0}。",
        {"type":"object","properties":{"present":{"type":"boolean"},"confidence":{"type":"number"}},"required":["present","confidence"]}),
    "visual_form_checkbox": (
        "你是工程表单视觉元素分析器。只识别勾选状态、手写可见内容和字段关联，不推测模糊内容。只返回 JSON。",
        "分析元素 {{element}}。返回 {\"field\":\"\",\"state\":\"unknown\",\"visible_text\":\"\",\"confidence\":0.0}。",
        {"type":"object","properties":{"state":{"type":"string"},"confidence":{"type":"number"}},"required":["state","confidence"]}),
    "document_classification": (
        "你是农村集体建设工程资料分类器。只能依据文件名和原文，将每份资料映射到给定标准阶段；不确定时标为其他资料。只返回 JSON。",
        "标准阶段：{{phases}}\n待分类资料：\n{{documents}}\n"
        "严格返回一个 JSON 对象，格式为：{\"documents\":[{\"document_id\":123,\"phase\":\"施工合同\",\"confidence\":0.95}]}。"
        "禁止把数组作为根节点；phase 必须逐字取自标准阶段列表。",
        {"type":"object","properties":{"documents":{"type":"array"}},"required":["documents"]}),
    "general_extraction": (
        "你是建设工程语义事实抽取器。COMPUTATION_POLICY_V1：模型禁止输出金额、比例、合计、差额、日期差、工期天数、阈值或任何派生计算结果；这些由 Python 从原文表格和标签确定性提取并计算。只能依据原文，不推测。quote 必须是原句；element_ids 必须逐字引用输入中的稳定元素 ID。只返回 JSON。",
        "只抽取不涉及运算的 scope_item、approval_record。不要输出 budget、max_price、award_amount、contract_amount、construction_period_days 或其他数值字段。\n"
        "严格返回 {\"facts\":[{\"field\":\"scope_item\",\"value\":\"原文范围描述\",\"quote\":\"原文\",\"page\":1,\"element_ids\":[\"P1-E0003\"],\"confidence\":0.95}]}；"
        "没有明确事实时 facts 为空。\n{{document}}",
        {"type":"object","properties":{"facts":{"type":"array"}},"required":["facts"]}),
    "payment_extraction": (
        "你是工程验收语义抽取器。COMPUTATION_POLICY_V1：模型禁止输出任何金额、比例、累计、合计、差额或派生计算结果。付款与结算数值由 Python 直接消费表格。只返回 JSON。",
        "只抽取 acceptance_record 的原文日期和结论；不要输出 payment_record、settlement_record 或任何数值字段。\n"
        "严格返回 {\"facts\":[{\"field\":\"acceptance_record\",\"value\":{\"date\":\"2025-01-01\",\"result\":\"合格\"},\"quote\":\"原文\",\"page\":1,\"element_ids\":[\"P1-E0003\"],\"confidence\":0.95}]}。\n{{document}}",
        {"type":"object","properties":{"facts":{"type":"array"}},"required":["facts"]}),
    "change_extraction": (
        "你是工程变更语义抽取器。COMPUTATION_POLICY_V1：模型禁止输出金额、比例、累计、合计、差额和阈值。不得把原合同清单项目臆测为新增项目。只返回 JSON。",
        "只抽取 change_record 的 change_no、proposed_date、approved_date、implemented_date、description、attachment_summary；禁止输出 amount。\n"
        "严格返回 {\"facts\":[{\"field\":\"change_record\",\"value\":{\"change_no\":\"BG-01\",\"description\":\"原文事项\"},\"quote\":\"原文\",\"page\":1,\"element_ids\":[\"P1-E0003\"],\"confidence\":0.95}]}。\n{{document}}",
        {"type":"object","properties":{"facts":{"type":"array"}},"required":["facts"]}),
    "contract_extraction": (
        "你是工程合同范围语义抽取器。COMPUTATION_POLICY_V1：模型禁止输出合同金额、比例、阈值、质保月数、工期天数或其他数值和派生计算结果。只能依据明确条款，不补齐缺失约定。只返回 JSON。",
        "只抽取 scope_item；不要输出 contract_amount、construction_period_days、contract_terms。\n"
        "严格返回 {\"facts\":[{\"field\":\"scope_item\",\"value\":\"合同范围原文\",\"quote\":\"原文\",\"page\":1,\"element_ids\":[\"P1-E0003\"],\"confidence\":0.95}]}。\n{{document}}",
        {"type":"object","properties":{"facts":{"type":"array"}},"required":["facts"]}),
    "semantic_consistency": (
        "你是工程合同语义一致性复核器。COMPUTATION_POLICY_V1：只判断范围语义，不得计算或输出金额、比例、差额、合计、日期差、阈值结果。只识别有直接原文支持的范围实质增减或无合理依据的重复计价；证据不足时 findings 必须为空。只返回 JSON。",
        "项目事实：{{facts}}\n资料原文：{{documents}}\n返回 {\"findings\":[]}，每项只包含 code,title,severity,summary,confidence,evidence，禁止 amount 和任何计算结果。",
        {"type":"object","properties":{"findings":{"type":"array"}},"required":["findings"]}),
    "policy_retrieval": (
        "你是政策检索查询改写器。根据异常生成短查询并对候选条款编号重排；不得编造制度。只返回 JSON。",
        "异常：{{findings}}\n候选政策：{{policies}}\n返回 {\"query\":\"...\",\"ranked_ids\":[]}。",
        {"type":"object","properties":{"query":{"type":"string"},"ranked_ids":{"type":"array"}},"required":["query","ranked_ids"]}),
    "text2sql": (
        "你是只读 SQLite 查询生成器。当前 MVP 没有自然语言数据库问答任务，本提示默认不执行。",
        "问题：{{question}}\n数据库结构：{{schema}}",
        {"type":"object","properties":{"sql":{"type":"string"}}}),
    "result_summary": (
        "你是工程审查结果摘要器。COMPUTATION_POLICY_V1：不得执行任何计算；只能逐字转述 Python 已生成的数值和结论，不得新增、换算、合计或推导。不得新增异常或改变规则结论。只返回 JSON。",
        "事实：{{facts}}\n异常：{{findings}}\n政策命中：{{policies}}\n返回 {\"summary\":\"...\",\"focus\":[]}。",
        {"type":"object","properties":{"summary":{"type":"string"},"focus":{"type":"array"}},"required":["summary","focus"]}),
}


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def seed_prompts() -> None:
    with db() as conn:
        for stage, (system, user, schema) in DEFAULTS.items():
            if conn.execute("SELECT 1 FROM prompt_versions WHERE stage=?", (stage,)).fetchone():
                continue
            conn.execute("""INSERT INTO prompt_versions(stage,version,status,is_current,system_prompt,user_prompt,json_schema,parser_version,created_at,published_at)
              VALUES(?,?,?,?,?,?,?,?,?,?)""", (stage, 1, "published", 1, system, user, json.dumps(schema, ensure_ascii=False), "json-v1", now(), now()))
        # Upgrade untouched legacy extraction prompts regardless of their numeric version.
        # Some deployed databases already promoted the old P?-B? contract to v2;
        # version numbers alone therefore cannot identify the schema.  Published
        # history remains immutable: migration always publishes a new version.
        for stage in ("general_extraction","payment_extraction","change_extraction","contract_extraction"):
            current=conn.execute("SELECT * FROM prompt_versions WHERE stage=? AND status='published' AND is_current=1 ORDER BY version DESC LIMIT 1",(stage,)).fetchone()
            if not current:continue
            user_prompt=current["user_prompt"] or ""
            legacy_schema=("element_ids" not in user_prompt and any(marker in user_prompt for marker in ("P1-B1","P?-B?",'"block"')))
            computation_legacy="COMPUTATION_POLICY_V1" not in (current["system_prompt"] or "")
            if not (legacy_schema or computation_legacy):continue
            system,user,schema=DEFAULTS[stage];version=conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM prompt_versions WHERE stage=?",(stage,)).fetchone()[0]
            conn.execute("UPDATE prompt_versions SET is_current=0 WHERE stage=?",(stage,))
            conn.execute("""INSERT INTO prompt_versions(stage,version,status,is_current,system_prompt,user_prompt,json_schema,parser_version,based_on_id,created_at,published_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(stage,version,"published",1,system,user,json.dumps(schema,ensure_ascii=False),"element-id-json-v2",current["id"],now(),now()))
        for stage in ("semantic_consistency","result_summary"):
            current=conn.execute("SELECT * FROM prompt_versions WHERE stage=? AND status='published' AND is_current=1 ORDER BY version DESC LIMIT 1",(stage,)).fetchone()
            if not current or "COMPUTATION_POLICY_V1" in (current["system_prompt"] or ""):continue
            system,user,schema=DEFAULTS[stage];version=conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM prompt_versions WHERE stage=?",(stage,)).fetchone()[0]
            conn.execute("UPDATE prompt_versions SET is_current=0 WHERE stage=?",(stage,))
            conn.execute("""INSERT INTO prompt_versions(stage,version,status,is_current,system_prompt,user_prompt,json_schema,parser_version,based_on_id,created_at,published_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(stage,version,"published",1,system,user,json.dumps(schema,ensure_ascii=False),"computation-policy-v1",current["id"],now(),now()))


def list_prompts() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM prompt_versions ORDER BY stage,version DESC").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["json_schema"] = json.loads(item["json_schema"] or "{}")
        result.append(item)
    return result


def published_prompt(stage: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM prompt_versions WHERE stage=? AND status='published' AND is_current=1 ORDER BY version DESC LIMIT 1", (stage,)).fetchone()
    if not row:
        raise RuntimeError(f"阶段 {stage} 没有已发布 Prompt")
    item = dict(row)
    item["json_schema"] = json.loads(item["json_schema"] or "{}")
    return item


def render(template: str, variables: dict[str, Any]) -> str:
    result = template
    for key, value in variables.items():
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        result = result.replace("{{" + key + "}}", value)
    return result


def create_draft(stage: str, based_on_id: int | None = None) -> dict[str, Any]:
    with db() as conn:
        source = conn.execute("SELECT * FROM prompt_versions WHERE id=?", (based_on_id,)).fetchone() if based_on_id else conn.execute(
            "SELECT * FROM prompt_versions WHERE stage=? ORDER BY version DESC LIMIT 1", (stage,)).fetchone()
        if not source:
            raise KeyError("prompt not found")
        version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM prompt_versions WHERE stage=?", (stage,)).fetchone()[0]
        cur = conn.execute("""INSERT INTO prompt_versions(stage,version,status,is_current,system_prompt,user_prompt,json_schema,parser_version,based_on_id,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)""", (stage, version, "draft", 0, source["system_prompt"], source["user_prompt"], source["json_schema"], source["parser_version"], source["id"], now()))
        prompt_id = int(cur.lastrowid)
    return next(item for item in list_prompts() if item["id"] == prompt_id)


def update_draft(prompt_id: int, values: dict[str, Any]) -> dict[str, Any]:
    allowed = {"system_prompt", "user_prompt", "json_schema", "parser_version"}
    with db() as conn:
        row = conn.execute("SELECT * FROM prompt_versions WHERE id=?", (prompt_id,)).fetchone()
        if not row:
            raise KeyError("prompt not found")
        if row["status"] != "draft":
            raise ValueError("历史版本和已发布版本不可修改，请先复制为草稿")
        updates = {k: v for k, v in values.items() if k in allowed}
        if "json_schema" in updates and not isinstance(updates["json_schema"], str):
            updates["json_schema"] = json.dumps(updates["json_schema"], ensure_ascii=False)
        if updates:
            conn.execute("UPDATE prompt_versions SET " + ",".join(f"{k}=?" for k in updates) + " WHERE id=?", (*updates.values(), prompt_id))
    return next(item for item in list_prompts() if item["id"] == prompt_id)


def publish(prompt_id: int) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM prompt_versions WHERE id=?", (prompt_id,)).fetchone()
        if not row:
            raise KeyError("prompt not found")
        if row["status"] != "draft":
            raise ValueError("只有草稿可以发布")
        conn.execute("UPDATE prompt_versions SET is_current=0 WHERE stage=? AND status='published'", (row["stage"],))
        conn.execute("UPDATE prompt_versions SET status='published',is_current=1,published_at=? WHERE id=?", (now(), prompt_id))
    return next(item for item in list_prompts() if item["id"] == prompt_id)


def prompt_snapshot() -> dict[str, Any]:
    return {stage: {"id": p["id"], "version": p["version"], "parser_version": p["parser_version"],
                    "system_prompt": p["system_prompt"], "user_prompt": p["user_prompt"], "json_schema": p["json_schema"]}
            for stage in DEFAULTS for p in [published_prompt(stage)]}
