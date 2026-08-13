"""Observable, evidence-first project audit pipeline.

LLMs extract and compare meaning. Python owns canonical document classification,
record de-duplication, arithmetic, dates and thresholds. Every model call, stage,
fact and finding is persisted so a reviewer can reconstruct the full run.
"""
import asyncio
import io
import json
import re
import traceback
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx

from .db import db, decode
from .document_parser import blocks_text, locate_quote, parse_document
from .providers import chat_with_trace, mineru_batch_result
from .model_config import resolve_route
from .prompt_store import published_prompt, render
from .trace_store import attach_input, finish_span, skipped_span, start_span
from .review_actions import applicable_rule


STAGES = {"queued": 0, "ocr": 5, "documents": 10, "document_classification": 16,
          "general_extraction": 28, "payment_extraction": 38, "change_extraction": 46,
          "contract_extraction": 54, "fact_persistence": 60, "deterministic_rules": 66, "semantic_consistency": 76,
          "policy_retrieval": 86, "text2sql": 89, "result_summary": 95, "complete": 100}

CANONICAL_PHASES = {
    "立项审批": ("立项", "会议纪要", "民主决策", "项目审批"),
    "预算控制价": ("预算", "控制价", "工程量清单", "boq"),
    "招标评标": ("招标", "评标", "中标", "采购结果"),
    "施工合同": ("合同", "协议"),
    "开工计量": ("开工", "进度", "计量", "监理令"),
    "变更签证": ("变更", "签证"),
    "竣工验收": ("竣工", "验收"),
    "结算付款": ("结算", "付款", "支付台账", "财务"),
}

RULES = [
    ("DOC-001", "全过程八阶段资料完整性", "确定性规则"),
    ("AMT-001", "中标价与合同价一致性", "确定性规则"),
    ("TERM-001", "招标与合同工期一致性", "确定性规则"),
    ("PAY-001", "累计付款与节点上限", "确定性规则"),
    ("CHG-001", "累计变更比例阈值", "确定性规则"),
    ("SEQ-001", "变更审批与实施先后顺序", "确定性规则"),
    ("ATT-001", "变更附件原件归档完整性", "确定性规则"),
    ("SEM-001", "范围实质变化与重复计价", "LLM语义规则"),
]

SEVERITIES={"high","moderate","minor","medium","low"}


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def event(run_id, stage, status, message, detail=None):
    with db() as conn:
        seq = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM audit_events WHERE run_id=?", (run_id,)).fetchone()[0]
        conn.execute("INSERT INTO audit_events(run_id,sequence,created_at,stage,status,message,detail_json) VALUES(?,?,?,?,?,?,?)",
                     (run_id, seq, now(), stage, status, message, json.dumps(detail or {}, ensure_ascii=False)))
        conn.execute("UPDATE audit_runs SET current_stage=?,progress=? WHERE id=?", (stage, STAGES.get(stage, 0), run_id))


def parse_json(text):
    cleaned = (text or "").strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        for left, right in (("{", "}"), ("[", "]")):
            a, b = cleaned.find(left), cleaned.rfind(right)
            if a >= 0 and b > a:
                try:
                    return json.loads(cleaned[a:b + 1])
                except Exception:
                    pass
    raise ValueError("模型输出不是有效 JSON")


def document_text(content):
    value = blocks_text(content)
    if value:
        return value
    parts = []
    for page in content.get("pages") or []:
        for line in page.get("lines") or []:
            text = (line.get("text") or "").strip()
            if text:
                parts.append(f"[P{page.get('page', 1)}-L{line.get('no', 1)}] {text}")
    return "\n".join(parts)


def plain_text(value):
    return re.sub(r"\[(?:P\d+-(?:B|L)\d+|P\d+-E\d+\|[^\]]+|P\d+-E\d+)\]\s*", "", value or "")


async def hydrate_mineru(document, run_id):
    content = document.get("content_json") or {}
    batch_id = (content.get("mineru") or {}).get("batch_id")
    if not batch_id:
        return document
    provider, model = resolve_route("ocr")
    span_id = start_span(run_id, "ocr", "ocr", f"MinerU OCR · {document['name']}",
                         provider_id=provider["id"], model=model,
                         metadata={"document_id": document["id"], "batch_id": batch_id})
    attach_input(span_id, {"batch_id": batch_id, "document_id": document["id"], "name": document["name"],
                           "provider": provider["id"], "model": model})
    for attempt in range(24):
        try:
            result = await mineru_batch_result(batch_id)
        except Exception as exc:
            finish_span(span_id, "failed", error={"type": type(exc).__name__, "message": str(exc), "attempt": attempt + 1})
            raise
        records = ((result.get("data") or {}).get("extract_result") or [])
        record = next((x for x in records if x.get("file_name") == document.get("name")), records[0] if records else {})
        if record.get("state") == "done":
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.get(record["full_zip_url"])
                response.raise_for_status()
            pages = {}
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                content_name = next((n for n in archive.namelist() if n.endswith("content_list.json")), None)
                entries = json.loads(archive.read(content_name).decode("utf-8", "replace")) if content_name else []
                for entry in entries:
                    text = (entry.get("text") or entry.get("content") or "").strip()
                    if not text:
                        continue
                    page_no = int(entry.get("page_idx", 0)) + 1
                    blocks = pages.setdefault(page_no, [])
                    blocks.append({"id": len(blocks) + 1, "type": entry.get("type", "text"),
                                   "text": text, "bbox": entry.get("bbox")})
            content.update({"pages": [{"page": p, "blocks": blocks} for p, blocks in sorted(pages.items())],
                            "extractor": "mineru-cloud-v4-vlm", "layout_model": "paragraph-blocks",
                            "mineru": {"batch_id": batch_id, "state": "done"}})
            with db() as conn:
                conn.execute("UPDATE documents SET content_json=?,pages=?,status=? WHERE id=?",
                             (json.dumps(content, ensure_ascii=False), len(pages), "已解析（MinerU）", document["id"]))
            document["content_json"] = content
            document["text"] = document_text(content)
            event(run_id, "documents", "running", f"MinerU 已回收并结构化：{document['name']}",
                  {"batch_id": batch_id, "pages": len(pages)})
            finish_span(span_id, output={"batch_result": result, "pages": content["pages"], "extractor": content["extractor"]})
            return document
        if record.get("state") == "failed":
            finish_span(span_id, "failed", error={"batch_result": result, "message": record.get("err_msg")})
            raise RuntimeError(f"MinerU 解析失败 {document['name']}：{record.get('err_msg', '未知原因')}")
        if attempt == 0:
            event(run_id, "documents", "running", f"等待 MinerU 完成扫描件：{document['name']}",
                  {"batch_id": batch_id, "state": record.get("state")})
        await asyncio.sleep(5)
    finish_span(span_id, "failed", error={"type": "Timeout", "batch_id": batch_id, "attempts": 24})
    raise RuntimeError(f"MinerU 解析超时：{document['name']}，可稍后重新发起审查")


def save_call(run_id, stage, trace=None, error=None):
    trace = trace or {}
    with db() as conn:
        conn.execute("""INSERT INTO ai_calls(run_id,stage,provider,model,started_at,duration_ms,success,input_tokens,output_tokens,request_hash,response_preview,error)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (run_id, stage, trace.get("provider", "unknown"), trace.get("model", "unknown"), trace.get("started_at", now()),
                      trace.get("duration_ms", 0), 0 if error else 1, trace.get("input_tokens"),
                      trace.get("output_tokens"), trace.get("request_hash", ""),
                      (trace.get("content") or "")[:1200], str(error)[:1000] if error else None))


def validate_structured_output(value, schema):
    errors = []
    expected = schema.get("type") if isinstance(schema, dict) else None
    if expected == "object" and not isinstance(value, dict):
        errors.append("根节点必须为 object")
    if expected == "array" and not isinstance(value, list):
        errors.append("根节点必须为 array")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"缺少必填字段 {key}")
        for key, definition in schema.get("properties", {}).items():
            if key not in value:
                continue
            kind = definition.get("type")
            if kind == "array" and not isinstance(value[key], list):
                errors.append(f"字段 {key} 必须为 array")
            if kind == "string" and not isinstance(value[key], str):
                errors.append(f"字段 {key} 必须为 string")
    return errors


async def model_json(run_id, stage, messages, purpose="fast", max_tokens=4000, route_overrides=None,
                     prompt_version=None, parent_span_id=None, prompt_variables=None):
    provider, model = resolve_route(stage, route_overrides)
    span_id = start_span(run_id, stage, "model_call", f"{provider['name']} · {model}", parent_span_id,
                         provider["id"], model, {"prompt_version": (prompt_version or {}).get("version")})
    request_view = {"messages": messages, "temperature": .1, "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                    "prompt_version": prompt_version or {}, "prompt_variables": prompt_variables or {},
                    "route": {"provider_id": provider["id"], "model": model}}
    attach_input(span_id, request_view, "完整模型请求")
    try:
        trace = await chat_with_trace(messages, purpose=purpose, json_mode=True, max_tokens=max_tokens,
                                      stage=stage, route_overrides=route_overrides)
        try:
            result = parse_json(trace["content"])
            schema = (prompt_version or {}).get("json_schema") or {}
            validation_errors = validate_structured_output(result, schema)
            if validation_errors:
                raise ValueError("；".join(validation_errors))
        except Exception as exc:
            save_call(run_id, stage, trace, error=exc)
            finish_span(span_id, "failed", output={"provider_response": trace.get("raw_response"),
                "assistant_message": trace.get("assistant_message"), "raw_text": trace.get("content")},
                error={"type": type(exc).__name__, "message": str(exc), "parser": "json-v1"})
            raise
        save_call(run_id, stage, trace)
        finish_span(span_id, output={"provider_response": trace.get("raw_response"),
            "response_headers": trace.get("response_headers"), "status_code": trace.get("status_code"),
            "finish_reason": trace.get("finish_reason"), "usage": trace.get("usage"),
            "assistant_message": trace.get("assistant_message"), "raw_text": trace.get("content"),
            "structured_output": result, "validation_errors": []},
            metadata={"request_hash": trace.get("request_hash"), "prompt_version": (prompt_version or {}).get("version")})
        if isinstance(result, dict):
            result["__trace"] = {"provider": provider["id"], "model": model, "span_id": span_id}
        return result
    except Exception as exc:
        if "trace" not in locals():
            save_call(run_id, stage, error=exc)
            finish_span(span_id, "failed", error={"type": type(exc).__name__, "message": str(exc)})
        raise


def num(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    value = str(value).replace(",", "").replace("，", "").replace("元", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(match.group()) if match else None


def ratio(value):
    result = num(value)
    if result is not None and ("%" in str(value) or result > 1):
        result /= 100
    return result


def clean_block(value):
    match = re.search(r"B?(\d+)$", str(value or ""))
    return int(match.group(1)) if match else None


def date_value(value):
    match = re.search(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", str(value or ""))
    if not match:
        return None
    return match.group().replace("/", "-").replace(".", "-")


def canonical_phase(document):
    text = f"{document.get('doc_type', '')} {document.get('name', '')}".lower()
    for phase, aliases in CANONICAL_PHASES.items():
        if phase in text or any(alias.lower() in text for alias in aliases):
            return phase
    return "其他资料"


def source_meta(document, quote, page=0):
    located = locate_quote(document.get("content_json") or {}, quote, page)
    return {"document_id": document["id"], "page": (located or {}).get("page") or page or 1,
            "block": (located or {}).get("block_id"), "quote": quote, "confidence": 0.99,
            "origin": "deterministic-parser", "parser_version_id":document.get("selected_parser_version_id"),
            "element_ids":[(located or {}).get("block_id")] if (located or {}).get("block_id") else []}


def scalar_fact(document, field, value, quote, page=0, confidence=.99):
    return {"field": field, "value": value, **source_meta(document, quote, page), "confidence": confidence}


def record_fact(document, field, value, quote, page=0, confidence=.99):
    return {"field": field, "value": value, **source_meta(document, quote, page), "confidence": confidence}


def label_amount(text, labels):
    for label in labels:
        pattern = rf"{re.escape(label)}(?:（元）|\(元\)|：|:|\s|\|)*\s*(?!(?:[\d,]+(?:\.\d+)?)\s*%)([\d,]+(?:\.\d+)?)"
        match = re.search(pattern, text, re.I)
        if match:
            return float(match.group(1).replace(",", "")), match.group(0)
    return None, None


def deterministic_table_facts(document):
    """Consume MinerU/PyMuPDF normalized table grids directly before LLM interpretation."""
    result=[]
    aliases={"payment_no":("付款编号","支付编号","编号"),"date":("付款日期","日期"),"stage":("付款节点","阶段","款项"),
      "base_amount":("计付基数","基数"),"ratio":("比例","支付比例"),"amount":("本次支付","本次付款","本期金额","付款金额","金额"),
      "cumulative":("累计付款","累计金额","累计"),"invoice_no":("发票号","发票编号"),
      "change_no":("变更编号","签证编号","编号"),"proposed_date":("提出日期","申请日期"),"approved_date":("批准日期","审批日期"),
      "implemented_date":("实施日期","施工日期"),"description":("变更内容","内容","事项"),"attachment_summary":("附件","附件说明")}
    for element in document.get("elements") or []:
        if element.get("element_type")!="table":continue
        grid=element.get("cell_grid_json") or element.get("cell_grid") or []
        if len(grid)<2:continue
        headers=[str(cell.get("text") or "").strip() for cell in grid[0]]
        def index(key):
            return next((i for i,h in enumerate(headers) if any(alias in h for alias in aliases[key])),None)
        payment_map={key:index(key) for key in ("payment_no","date","stage","base_amount","ratio","amount","cumulative","invoice_no")}
        change_map={key:index(key) for key in ("change_no","proposed_date","approved_date","implemented_date","description","amount","attachment_summary")}
        phase=canonical_phase(document)
        payment_ready=payment_map["payment_no"] is not None and payment_map["amount"] is not None
        change_ready=change_map["change_no"] is not None and change_map["amount"] is not None
        if phase=="变更签证":kind="change_record" if change_ready else None
        elif phase=="结算付款":kind="payment_record" if payment_ready else None
        else:
            payment_score=sum(payment_map[k] is not None for k in ("stage","ratio","cumulative","invoice_no"))
            change_score=sum(change_map[k] is not None for k in ("proposed_date","approved_date","implemented_date","description"))
            kind="payment_record" if payment_ready and payment_score>change_score else "change_record" if change_ready else None
        if not kind:continue
        mapping=payment_map if kind=="payment_record" else change_map
        for row in grid[1:]:
            values=[str(cell.get("text") or "").strip() for cell in row]
            def get(key):
                pos=mapping.get(key);return values[pos] if pos is not None and pos<len(values) else None
            record={key:get(key) for key in mapping if get(key) not in (None,"")}
            for key in ("base_amount","amount","cumulative"): 
                if key in record:record[key]=num(record[key])
            if "ratio" in record:record["ratio"]=ratio(record["ratio"])
            identity=record.get("payment_no") or record.get("change_no")
            if not identity:continue
            result.append({"field":kind,"value":record,"document_id":document["id"],"page":element.get("page",1),
                "block":element["element_id"],"element_ids":[element["element_id"]],"parser_version_id":document.get("selected_parser_version_id"),
                "quote":element.get("markdown") or element.get("text") or str(identity),"confidence":.995,"origin":"deterministic-table"})
    return result


def date_range_fact(document, text):
    """Parse raw start/end dates and compute calendar days inclusively in Python."""
    label_match=re.search("\\u8ba1\\u5212\\u5de5\\u671f|\\u5de5\\u671f\\u8981\\u6c42|\\u5408\\u540c\\u5de5\\u671f|\\u5de5\\u671f",text)
    if not label_match:return None
    window=text[label_match.start():label_match.start()+160]
    dates=re.findall(r"20\d{2}-\d{1,2}-\d{1,2}",window)
    if len(dates)<2:return None
    start,end=dates[0],dates[1]
    try:days=(datetime.fromisoformat(end)-datetime.fromisoformat(start)).days+1
    except ValueError:return None
    if days<=0:return None
    fact=record_fact(document,"construction_period",{"start_date":start,"end_date":end,"calendar_days":days},window[:window.find(end)+len(end)])
    fact.update({"computed_by":"python","formula":"(end_date - start_date).days + 1","inputs":{"start_date":start,"end_date":end}})
    return fact


def deterministic_extract(document):
    """Generic label/table parser. It supplements, never replaces, the LLM call."""
    text = plain_text(document.get("text", ""))
    phase = canonical_phase(document)
    facts = []
    labels = {
        "budget": ("估算投资", "预算送审价", "立项预算"),
        "max_price": ("核定招标控制价为人民币", "招标控制价"),
        "award_amount": ("中标价为", "中标金额"),
        "contract_amount": ("合同价", "签约合同价"),
        "settlement_submitted": ("承包人送审价", "送审结算价"),
        "settlement_audited": ("审定结算价", "审核结算价"),
        "settlement_reduction": ("核减金额",),
        "paid_cumulative": ("累计已支付",),
    }
    for field, aliases in labels.items():
        value, quote = label_amount(text, aliases)
        if value is not None:
            facts.append(scalar_fact(document, field, value, quote))

    if phase in ("施工合同", "招标评标"):
        period=date_range_fact(document,text)
        if period:
            facts.append(period)
    if phase == "施工合同":
        terms = {"advance_ratio": r"合同价\s*(\d+(?:\.\d+)?)%\s*作为预付款",
                 "progress_ratio": r"进度款[\s\S]{0,45}?(\d+(?:\.\d+)?)%",
                 "pre_acceptance_cap": r"竣工验收前[\s\S]{0,70}?(\d+(?:\.\d+)?)%",
                 "post_settlement_cap": r"结算审定后[\s\S]{0,70}?(\d+(?:\.\d+)?)%",
                 "retention_ratio": r"余\s*(\d+(?:\.\d+)?)%[\s\S]{0,30}?质量保证金",
                 "change_threshold": r"累计变更金额超过原合同价\s*\$?\s*(\d+(?:\.\d+)?)\s*\\?%\$?"}
        values = {}
        quotes = []
        for key, pattern in terms.items():
            match = re.search(pattern, text)
            if match:
                values[key] = float(match.group(1)) / 100
                quotes.append(match.group(0))
        warranty = re.search(r"质保期\s*(\d+)\s*个月", text)
        if warranty:
            values["warranty_months"] = int(warranty.group(1)); quotes.append(warranty.group(0))
        if values:
            facts.append(record_fact(document, "contract_terms", values, "；".join(quotes)))

    if phase == "招标评标":
        match = re.search(r"计划工期[^\d]{0,15}(\d+)\s*日", text)
        if match:
            fact=scalar_fact(document,"construction_period_days_stated",int(match.group(1)),match.group(0));fact["computed_by"]="source_document"
            facts.append(fact)

    if phase == "变更签证":
        pattern = re.compile(r"(BG[-_]?[A-Z0-9]+)\s+(20\d{2}-\d{2}-\d{2})\s+(20\d{2}-\d{2}-\d{2})\s+(.{8,180}?)\s+([\d,]+(?:\.\d+)?)\s+(20\d{2}-\d{2}-\d{2})\s+(.{3,80}?)(?=\n二、|\n三、|$)", re.S)
        for match in pattern.finditer(text):
            value = {"change_no": match.group(1).replace("_", "-"), "proposed_date": match.group(2),
                     "implemented_date": match.group(3), "description": re.sub(r"\s+", "", match.group(4)),
                     "amount": num(match.group(5)), "approved_date": match.group(6),
                     "attachment_summary": re.sub(r"\s+", "", match.group(7))[:180]}
            facts.append(record_fact(document, "change_record", value, match.group(0)[:500]))
        if not any(f["field"] == "change_record" for f in facts):
            no = re.search(r"BG[-_]?[A-Z0-9]+", text)
            dates = re.findall(r"20\d{2}-\d{2}-\d{2}", text)
            amount, _ = label_amount(text, ("本次资料累计变更", "本次变更金额"))
            if no and len(dates) >= 3 and amount is not None:
                facts.append(record_fact(document, "change_record", {"change_no": no.group(),
                    "proposed_date": dates[0], "implemented_date": dates[1], "approved_date": dates[2],
                    "amount": amount, "description": "", "attachment_summary": ""}, no.group()))

    if phase == "开工计量":
        pattern = re.compile(r"(JL[-_]?[A-Z0-9]+)\s+(20\d{2}-\d{2}-\d{2})\s+(.{4,100}?)\s+([\d,]+(?:\.\d+)?)\s+(.{2,40}?)(?=\nJL[-_]|\n二、|\n三、|$)", re.S)
        for match in pattern.finditer(text):
            facts.append(record_fact(document, "measurement_record", {"measurement_no": match.group(1),
                "date": match.group(2), "description": re.sub(r"\s+", "", match.group(3)),
                "confirmed_amount": num(match.group(4)), "opinion": re.sub(r"\s+", "", match.group(5))}, match.group(0)[:400]))

    if phase == "竣工验收":
        date_match = re.search(r"验收日期\s*(20\d{2}-\d{2}-\d{2})", text)
        result_match = re.search(r"验收结论\s*([^\n]{1,30})", text)
        if date_match:
            value = {"date": date_match.group(1), "result": result_match.group(1).strip() if result_match else ""}
            facts.append(record_fact(document, "acceptance_record", value, date_match.group(0)))

    if phase == "结算付款":
        row_pattern = re.compile(r"(FK[-_]?[A-Z0-9]+)\s+(20\d{2}-\d{2}-\d{2})\s+([^\n]{2,20})\s+([\d,]+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)%\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s+([^\s]+)", re.S)
        for match in row_pattern.finditer(text):
            value = {"payment_no": match.group(1), "date": match.group(2), "stage": match.group(3).strip(),
                     "base_amount": num(match.group(4)), "ratio": float(match.group(5)) / 100,
                     "amount": num(match.group(6)), "cumulative": num(match.group(7)),
                     "invoice_no": match.group(8).strip()}
            facts.append(record_fact(document, "payment_record", value, match.group(0)[:350]))
        submitted, _ = label_amount(text, ("承包人送审价", "送审结算价"))
        audited, audited_quote = label_amount(text, ("审定结算价", "审核结算价"))
        reduction, _ = label_amount(text, ("核减金额",))
        audit_date = re.search(r"审定日期\s*(20\d{2}-\d{2}-\d{2})", text)
        if audited is not None:
            facts.append(record_fact(document, "settlement_record", {"submitted": submitted, "audited": audited,
                "reduction": reduction, "audit_date": audit_date.group(1) if audit_date else None}, audited_quote))
    facts.extend(deterministic_table_facts(document))
    return facts


def fact_key(fact):
    value = fact.get("value")
    if isinstance(value, dict):
        identity = value.get("payment_no") or value.get("change_no") or value.get("measurement_no")
        if identity:
            return fact.get("field"), str(identity).upper()
    return fact.get("field"), fact.get("document_id"), json.dumps(value, ensure_ascii=False, sort_keys=True)


def merge_facts(*groups):
    merged = {}
    for fact in [x for group in groups for x in group]:
        if not fact.get("field") or not fact.get("document_id"):
            continue
        key = fact_key(fact)
        old = merged.get(key)
        if not old or float(fact.get("confidence", 0)) > float(old.get("confidence", 0)):
            if old and isinstance(fact.get("value"), dict) and isinstance(old.get("value"), dict):
                fact["value"] = {**old["value"], **{k: v for k, v in fact["value"].items() if v not in (None, "")}}
            merged[key] = fact
        elif isinstance(fact.get("value"), dict) and isinstance(old.get("value"), dict):
            richer = {**fact["value"], **{k: v for k, v in old["value"].items() if v not in (None, "")}}
            old["value"] = richer
    return list(merged.values())


FIELD_PHASES={
    "budget":{"立项审批","预算控制价"},"max_price":{"预算控制价","招标评标"},"award_amount":{"招标评标"},
    "contract_amount":{"施工合同"},"construction_period":{"招标评标","施工合同"},"construction_period_days_stated":{"招标评标","施工合同"},
    "scope_item":{"预算控制价","招标评标","施工合同"},"contract_terms":{"施工合同"},
    "payment_record":{"结算付款"},"paid_cumulative":{"结算付款"},"settlement_record":{"结算付款"},
    "settlement_submitted":{"结算付款"},"settlement_audited":{"结算付款"},"settlement_reduction":{"结算付款"},
    "change_record":{"变更签证"},"measurement_record":{"开工计量"},"acceptance_record":{"竣工验收"},
}
MONETARY_FIELDS={"budget","max_price","award_amount","contract_amount","settlement_submitted","settlement_audited","settlement_reduction","paid_cumulative"}
MODEL_FORBIDDEN_SCALAR_FIELDS=MONETARY_FIELDS|{"construction_period_days","construction_period_days_stated"}
MODEL_FORBIDDEN_RECORD_FIELDS={"payment_record","settlement_record","contract_terms","measurement_record"}
MODEL_FORBIDDEN_RECORD_KEYS={"amount","base_amount","cumulative","ratio","submitted","audited","reduction","confirmed_amount",
                             "advance_ratio","progress_ratio","pre_acceptance_cap","post_settlement_cap","retention_ratio","change_threshold","warranty_months"}


def fact_rejection_reason(fact, phase, peers):
    field=fact.get("field")
    if fact.get("origin")=="llm":
        if field in MODEL_FORBIDDEN_SCALAR_FIELDS or field in MODEL_FORBIDDEN_RECORD_FIELDS:
            return "model_computed_or_numeric_field_forbidden"
        value=fact.get("value")
        if isinstance(value,dict) and any(key in value for key in MODEL_FORBIDDEN_RECORD_KEYS):
            return "model_numeric_record_key_forbidden"
    allowed=FIELD_PHASES.get(field)
    if allowed and phase not in allowed:return f"field_not_allowed_in_phase:{phase}"
    if field in MONETARY_FIELDS:
        value=num(fact.get("value"))
        peer_values=[num(x.get("value")) for x in peers if x.get("field")==field and x.get("document_id")==fact.get("document_id")]
        peer_values=[x for x in peer_values if x is not None]
        if value is not None and peer_values and max(peer_values)>=10000 and value<max(peer_values)*.05:
            return "monetary_scale_outlier_or_percentage_context"
    return None


def canonicalize_fact_value(fact):
    """Normalize harmless OCR typography while preserving the source quote."""
    if fact.get("field")=="scope_item" and isinstance(fact.get("value"),str):
        value=re.sub(r"\s+"," ",fact["value"]).strip()
        value=re.sub(r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])"," ",value)
        value=re.sub(r"(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])"," ",value)
        fact["value"]=re.sub(r"\s+"," ",value).strip()
    return fact


def evidence_from_fact(fact, documents):
    doc = documents.get(int(fact.get("document_id") or 0), {})
    element_ids=[str(x) for x in (fact.get("element_ids") or [])]
    element=next((x for x in doc.get("elements") or [] if x.get("element_id") in element_ids),None)
    located={"page":element.get("page"),"block_id":element.get("element_id"),"bbox":element.get("bbox_json")} if element else locate_quote(doc.get("content_json") or {}, fact.get("quote"), fact.get("page") or 1)
    if not element_ids and (located or {}).get("block_id"):element_ids=[(located or {}).get("block_id")]
    return {"document_id": fact.get("document_id"), "document_name": doc.get("name", "未知资料"),
            "parser_version_id":fact.get("parser_version_id") or doc.get("selected_parser_version_id"),"element_ids":element_ids,
            "page": (located or {}).get("page") or fact.get("page") or 1,
            "block_id": (located or {}).get("block_id") or clean_block(fact.get("block")),
            "bbox": (located or {}).get("bbox"), "quote": fact.get("quote") or str(fact.get("value", ""))}


def rule_terms(rule):
    return [term.strip() for term in re.split(r"[、,，]", rule.get("fields") or "") if term.strip()]


def dated_element_matches(documents, term):
    """Find source dates nearest a configured event term. This is parsing, not LLM reasoning."""
    matches=[]
    date_pattern=re.compile(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?")
    for document in documents.values():
        for element in document.get("elements") or []:
            text=" ".join(str(element.get(key) or "") for key in ("text","markdown","html"))
            if term not in text:continue
            term_positions=[m.start() for m in re.finditer(re.escape(term),text)]
            dates=list(date_pattern.finditer(text))
            if not dates:continue
            chosen=min(dates,key=lambda match:min(abs(match.start()-pos) for pos in term_positions))
            value=date_value(chosen.group())
            if not value:continue
            meta=element.get("metadata_json") or {}
            matches.append({"term":term,"date":value,"document_id":document["id"],"document_name":document.get("name","未知资料"),
                "parser_version_id":document.get("selected_parser_version_id"),"element_ids":[element["element_id"]],
                "page":element.get("page") or 1,"block_id":element["element_id"],"bbox":element.get("bbox_json"),
                "page_width":meta.get("page_width"),"page_height":meta.get("page_height"),"quote":text[:1000]})
    unique={}
    for match in matches:unique.setdefault((match["term"],match["date"],match["document_id"],match["block_id"]),match)
    return list(unique.values())


def evaluate_user_date_rule(rule, documents):
    """Execute the supported user deterministic DSL: event date ordering."""
    terms=rule_terms(rule);description=rule.get("description") or ""
    if len(terms)<2:return {"status":"unsupported","reason":"need_at_least_two_event_terms"}
    relation=next((word for word in ("不得晚于","不晚于","早于","先于","晚于") if word in description),None)
    if not relation:return {"status":"unsupported","reason":"date_relation_not_declared"}
    right_term=terms[-1];right_matches=dated_element_matches(documents,right_term)
    left_matches=[match for term in terms[:-1] for match in dated_element_matches(documents,term)]
    if not left_matches or not right_matches:
        return {"status":"not_evaluable","reason":"event_date_evidence_missing","terms":terms,
                "matched":{"left":left_matches,"right":right_matches}}
    comparisons=[]
    for left in left_matches:
        for right in right_matches:
            if relation in ("早于","先于"):violated=left["date"]>=right["date"]
            elif relation in ("不晚于","不得晚于"):violated=left["date"]>right["date"]
            else:violated=left["date"]<=right["date"]
            comparisons.append({"left":left,"right":right,"relation":relation,"violated":violated})
    violations=[item for item in comparisons if item["violated"]]
    return {"status":"violated" if violations else "passed","formula":f"date({terms[0:-1]}) {relation} date({right_term})",
            "comparisons":comparisons,"violations":violations}


def semantic_rule_corpus(rule, documents, limit=30000):
    terms=rule_terms(rule)
    description=rule.get("description") or ""
    keywords=list(dict.fromkeys(terms+re.findall(r"[\u4e00-\u9fff]{2,8}",description)))
    rows=[]
    for document in documents.values():
        for element in document.get("elements") or []:
            text="\n".join(str(element.get(key) or "") for key in ("text","markdown") if element.get(key)).strip()
            if text and (not keywords or any(word in text for word in keywords)):
                rows.append(f"[document_id={document['id']} name={document.get('name')} page={element.get('page')} element_id={element.get('element_id')}]\n{text}")
    if not rows:
        for document in documents.values():
            rows.append(f"[document_id={document['id']} name={document.get('name')}]\n{document.get('text','')[:3000]}")
    return "\n\n".join(rows)[:limit]


def explicit_single_source_proof(evidences):
    text=" ".join(str(evidence.get("quote") or "") for evidence in evidences)
    return any(word in text for word in ("缺失","未确认","未附","未见","仅施工单位","没有建设单位","无建设单位"))


def insert_anomaly(conn, run_id, project_id, code, title, severity, summary, amount, confidence, evidences, rule_kind):
    conn.execute("""INSERT INTO anomalies(project_id,code,title,severity,summary,amount,confidence,status,evidence_json,rule_kind,created_at,run_id)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (project_id, code, title, severity, summary, round(amount) if amount is not None else None,
                  float(confidence), "待复核", json.dumps(evidences, ensure_ascii=False), rule_kind, now(), run_id))


def scalar_facts(facts, field):
    return [f for f in facts if f.get("field") == field and num(f.get("value")) is not None]


def best_scalar(facts, field):
    values = scalar_facts(facts, field)
    if field in MONETARY_FIELDS and values:
        maximum=max(num(x.get("value")) or 0 for x in values)
        if maximum>=10000:values=[x for x in values if (num(x.get("value")) or 0)>=maximum*.05]
    return max(values, key=lambda x: float(x.get("confidence", 0))) if values else None


def records(facts, field):
    return [f for f in facts if f.get("field") == field and isinstance(f.get("value"), dict)]


def latest_value(records_, key):
    values = [(num(f["value"].get(key)), f) for f in records_]
    values = [(v, f) for v, f in values if v is not None]
    return max(values, key=lambda x: x[0]) if values else (None, None)


def payment_total(records_):
    """Compute paid total from raw payment amounts; never trust a model cumulative."""
    ordered=sorted(records_,key=lambda f:(date_value(f.get("value",{}).get("date")) or "",str(f.get("value",{}).get("payment_no") or "")))
    total=sum(num(f["value"].get("amount")) or 0 for f in ordered)
    return total,(ordered[-1] if ordered else None),{"formula":"sum(payment_record.amount)","record_count":len(ordered),
            "inputs":[{"payment_no":f["value"].get("payment_no"),"amount":num(f["value"].get("amount")) or 0} for f in ordered]}


def payment_limit(contract_value,change_total,settlement,terms,has_acceptance):
    if settlement is not None and has_acceptance:
        return settlement,num(terms.get("post_settlement_cap")) or .97,"审定结算价"
    base=contract_value+change_total
    cap=num(terms.get("pre_acceptance_cap")) or num(terms.get("progress_ratio")) or .85
    return base,cap,"合同价（含已批准变更）"


async def execute(run_id, project_id):
    try:
        with db() as conn:
            run_row = dict(conn.execute("SELECT * FROM audit_runs WHERE id=?", (run_id,)).fetchone())
            action_snapshot=json.loads(run_row.get("action_snapshot_json") or "{}")
            action_type=action_snapshot.get("action_type") or "final_review"
            all_active_rules = {row["code"]: dict(row) for row in conn.execute(
                "SELECT * FROM rule_definitions WHERE enabled=1 ORDER BY code")}
            active_rules={code:rule for code,rule in all_active_rules.items() if applicable_rule(rule,action_type)}
        executable_rules = list(active_rules.values())
        def rule_enabled(code):
            return code in active_rules
        def rule_severity(code, fallback="medium"):
            value = active_rules.get(code, {}).get("severity", fallback)
            return value if value in SEVERITIES else fallback
        route_overrides = json.loads((run_row or {"route_overrides_json": "{}"})["route_overrides_json"] or "{}")
        event(run_id, "documents", "running", f"读取资料截止时点快照并建立坐标索引（{action_snapshot.get('action_label','全过程全量终审')}）")
        documents_span = start_span(run_id, "documents", "stage", "读取原件并建立版面索引")
        with db() as conn:
            project = dict(conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
            docs = []
            selected=list(conn.execute("""SELECT d.*,rd.document_role,v.id AS selected_parser_version_id,v.parser_kind,v.parser_name,v.parser_version,
              v.model AS parser_model,v.status AS parser_status,v.content_json AS parser_content_json,v.manifest_json AS parser_manifest_json,v.stats_json AS parser_stats_json
              FROM audit_run_documents rd JOIN documents d ON d.id=rd.document_id JOIN document_parser_versions v ON v.id=rd.parser_version_id
              WHERE rd.run_id=? ORDER BY d.id""",(run_id,)))
            if not selected:
                selected=list(conn.execute("""SELECT d.*,v.id AS selected_parser_version_id,v.parser_kind,v.parser_name,v.parser_version,
                  v.model AS parser_model,v.status AS parser_status,v.content_json AS parser_content_json,v.manifest_json AS parser_manifest_json,v.stats_json AS parser_stats_json
                  FROM documents d JOIN document_parser_versions v ON v.id=d.active_parser_version_id
                  WHERE d.project_id=? AND d.deleted_at IS NULL ORDER BY d.id""",(project_id,)))
            for row in selected:
                item=dict(row);item["content_json"]=json.loads(item.pop("parser_content_json") or "{}")
                item["parser_manifest"]=json.loads(item.pop("parser_manifest_json") or "{}");item["parser_stats"]=json.loads(item.pop("parser_stats_json") or "{}")
                item["elements"]=[decode(x,"bbox_json","cell_grid_json","metadata_json") for x in conn.execute(
                    "SELECT * FROM document_elements WHERE parser_version_id=? ORDER BY page,reading_order",(item["selected_parser_version_id"],))]
                item["chunks"]=[decode(x,"title_path_json","element_ids_json") for x in conn.execute(
                    "SELECT * FROM document_chunks WHERE parser_version_id=? ORDER BY id",(item["selected_parser_version_id"],))]
                item["text"]="\n\n".join(chunk["content"] for chunk in item["chunks"]) or document_text(item["content_json"])
                item["canonical_phase"] = canonical_phase(item)
                docs.append(item)
        usable = [d for d in docs if d.get("text")]
        reuse_baseline=bool(run_row.get("baseline_run_id") and run_row.get("review_mode") in {"gate","incremental"})
        processing_docs=[d for d in usable if d.get("document_role")=="current"] if reuse_baseline else usable
        event(run_id, "documents", "completed", f"共读取 {len(docs)} 份资料，其中 {len(usable)} 份可进入文本审查",
              {"document_count": len(docs), "usable_count": len(usable),
               "phases": {d["name"]: d["canonical_phase"] for d in docs}})
        attach_input(documents_span, {"project_id": project_id, "documents": [{"id": d["id"], "name": d["name"],
            "declared_type": d["doc_type"], "sha256": d["sha256"], "parser_version_id":d["selected_parser_version_id"],
            "parser_kind":d["parser_kind"],"parser_version":d["parser_version"],"parser_model":d["parser_model"],
            "manifest":d["parser_manifest"],"elements":d["elements"],"chunks":d["chunks"]} for d in docs]}, "项目解析版本与结构化元素索引")
        finish_span(documents_span, output={"documents": [{"id": d["id"], "name": d["name"],
            "text_length": len(d.get("text") or ""), "initial_phase": d["canonical_phase"],"parser_version_id":d["selected_parser_version_id"],
            "parser":d["parser_name"],"element_count":len(d["elements"]),"chunk_count":len(d["chunks"])} for d in docs]})
        if not usable:
            raise RuntimeError("没有可解析文本；扫描件 OCR 尚未完成")

        classification_prompt = published_prompt("document_classification")
        event(run_id, "document_classification", "running", "使用已发布 Prompt 对资料阶段进行语义分类")
        classification_span = start_span(run_id, "document_classification", "stage", "资料分类")
        classification_input = [{"document_id": d["id"], "name": d["name"], "declared_type": d["doc_type"],
                                 "excerpt": plain_text(d["text"])[:1800]} for d in processing_docs]
        try:
            classification_variables={"phases": list(CANONICAL_PHASES) + ["其他资料"], "documents": classification_input}
            classified = await model_json(run_id, "document_classification", [
                {"role": "system", "content": classification_prompt["system_prompt"]},
                {"role": "user", "content": render(classification_prompt["user_prompt"],
                    classification_variables)}],
                max_tokens=1800, route_overrides=route_overrides, prompt_version=classification_prompt,
                parent_span_id=classification_span,prompt_variables=classification_variables)
            allowed_phases = set(CANONICAL_PHASES) | {"其他资料"}
            classified_map = {int(x.get("document_id") or 0): x.get("phase") for x in classified.get("documents", [])
                              if x.get("phase") in allowed_phases}
            for document in processing_docs:
                document["canonical_phase"] = classified_map.get(document["id"], document["canonical_phase"])
            finish_span(classification_span, output={"classifications": [{"document_id": d["id"],
                "name": d["name"], "phase": d["canonical_phase"]} for d in processing_docs]})
            event(run_id, "document_classification", "completed", f"已分类本次需处理的 {len(processing_docs)} 份资料")
        except Exception as exc:
            finish_span(classification_span, "completed_with_warning", output={"classifications": [{"document_id": d["id"],
                "name": d["name"], "phase": d["canonical_phase"], "source": "deterministic"} for d in processing_docs]},
                error={"message": str(exc), "fallback": "deterministic document mapping"})
            event(run_id, "document_classification", "completed", "模型分类失败，已明确记录并使用确定性文件映射继续",
                  {"error": str(exc)[:180], "fallback": "deterministic"})

        llm_facts, parser_facts = [], []
        doc_ids = {d["id"] for d in docs}
        extraction_plan = [
            ("general_extraction", processing_docs),
            ("payment_extraction", [d for d in processing_docs if d["canonical_phase"] in {"结算付款", "竣工验收", "开工计量"}]),
            ("change_extraction", [d for d in processing_docs if d["canonical_phase"] == "变更签证"]),
            ("contract_extraction", [d for d in processing_docs if d["canonical_phase"] == "施工合同"]),
        ]
        for stage, stage_docs in extraction_plan:
            prompt = published_prompt(stage)
            event(run_id, stage, "running", f"{stage}：准备处理 {len(stage_docs)} 份匹配资料")
            stage_span = start_span(run_id, stage, "stage", stage, metadata={"document_ids": [d["id"] for d in stage_docs]})
            failures = []
            for document in stage_docs:
                source_chunks=document.get("chunks") or [{"chunk_id":"legacy","element_ids":[],"title_path":[],"page_from":1,"page_to":document.get("pages") or 1,"content":document["text"][:18000]}]
                for chunk in source_chunks:
                    document_payload=json.dumps({"document_id":document["id"],"name":document["name"],"phase":document["canonical_phase"],
                        "parser_version_id":document["selected_parser_version_id"],"parser":document["parser_name"],"chunk_id":chunk["chunk_id"],
                        "title_path":chunk["title_path_json"] if "title_path_json" in chunk else chunk.get("title_path",[]),
                        "pages":[chunk.get("page_from"),chunk.get("page_to")],"element_ids":chunk["element_ids_json"] if "element_ids_json" in chunk else chunk.get("element_ids",[]),
                        "structured_content":chunk["content"],"output_requirement":"每条事实必须返回 element_ids，引用上述真实元素 ID；不得仅依赖换行位置。"},ensure_ascii=False)
                    try:
                        prompt_variables={"document": document_payload}
                        extracted = await model_json(run_id, stage, [
                            {"role": "system", "content": prompt["system_prompt"]+" 每条事实必须提供 element_ids 数组，元素 ID 必须来自输入。"},
                            {"role": "user", "content": render(prompt["user_prompt"], prompt_variables)}],
                            max_tokens=3200, route_overrides=route_overrides, prompt_version=prompt,
                            parent_span_id=stage_span,prompt_variables=prompt_variables)
                        trace_meta = extracted.pop("__trace", {})
                        allowed_elements=set(chunk["element_ids_json"] if "element_ids_json" in chunk else chunk.get("element_ids",[]))
                        for fact in extracted.get("facts", []) if isinstance(extracted, dict) else []:
                            fact["document_id"] = document["id"]
                            fact["parser_version_id"] = document["selected_parser_version_id"]
                            fact["element_ids"]=[x for x in (fact.get("element_ids") or []) if x in allowed_elements]
                            fact["origin"] = "llm";fact["provider"] = trace_meta.get("provider", "unknown");fact["model"] = trace_meta.get("model", "unknown")
                            fact["source_stage"] = stage;fact["source_chunk"] = chunk["chunk_id"];llm_facts.append(fact)
                    except Exception as exc:
                        failures.append({"document_id": document["id"], "name": document["name"],"chunk_id":chunk["chunk_id"], "error": str(exc)})
            finish_span(stage_span, "completed_with_warning" if failures else "completed",
                        output={"document_count": len(stage_docs), "fact_count": len([f for f in llm_facts if f.get("source_stage") == stage]),
                                "failures": failures})
            event(run_id, stage, "completed", f"{stage} 完成，失败 {len(failures)} 份", {"failures": failures})

        parser_span = start_span(run_id, "deterministic_parser", "parser", "确定性字段解析与交叉校验")
        attach_input(parser_span, {"documents": [{"id":d["id"],"name":d["name"],"parser_version_id":d["selected_parser_version_id"],
            "elements":[{"element_id":x["element_id"],"page":x["page"],"type":x["element_type"],"bbox":x.get("bbox_json"),
                         "markdown":x.get("markdown"),"cell_grid":x.get("cell_grid_json")} for x in d["elements"]],
            "chunks":[{"chunk_id":x["chunk_id"],"element_ids":x.get("element_ids_json"),"pages":[x["page_from"],x["page_to"]],"content":x["content"]} for x in d["chunks"]]} for d in processing_docs]})
        for document in processing_docs:
            parser_facts.extend(deterministic_extract(document))
        finish_span(parser_span, output={"facts": parser_facts})
        facts = merge_facts(llm_facts, parser_facts)
        baseline_fact_rows=[]
        if reuse_baseline:
            with db() as conn:
                baseline_fact_rows=list(conn.execute("SELECT * FROM extracted_facts WHERE run_id=? ORDER BY id",(run_row["baseline_run_id"],)))
            current_doc_ids={d["id"] for d in processing_docs}
            for row in baseline_fact_rows:
                if row["document_id"] in current_doc_ids:continue
                facts.append({"field":row["field_name"],"value":json.loads(row["value_json"]),"confidence":row["confidence"],
                              "document_id":row["document_id"],"parser_version_id":row["parser_version_id"],
                              "element_ids":json.loads(row["element_ids_json"] or "[]"),"page":row["page"],
                              "block":row["block_id"],"quote":row["quote"],"origin":"baseline-reuse",
                              "provider":row["provider"],"model":row["model"],"source_stage":"baseline_reuse"})
            event(run_id,"fact_persistence","running",f"复用基线 Run #{run_row['baseline_run_id']} 的历史事实，仅重抽取本批 {len(processing_docs)} 份资料",
                  {"baseline_run_id":run_row["baseline_run_id"],"current_document_ids":sorted(current_doc_ids),"reused_candidates":len(baseline_fact_rows)})
        valid = [];rejected=[]
        validation_span=start_span(run_id,"fact_validation","validator","事实阶段约束、量纲校验与证据门槛")
        attach_input(validation_span,{"candidates":facts})
        with db() as conn:
            conn.execute("DELETE FROM extracted_facts WHERE run_id=?", (run_id,))
            for fact in facts:
                try:
                    canonicalize_fact_value(fact)
                    did = int(fact.get("document_id") or 0)
                    if did not in doc_ids or not fact.get("field"):
                        continue
                    selected_document=next(d for d in docs if d["id"]==did);source=selected_document["content_json"]
                    document_phase=selected_document["canonical_phase"]
                    rejection=fact_rejection_reason(fact,document_phase,facts)
                    if rejection:
                        rejected.append({"field":fact.get("field"),"value":fact.get("value"),"document_id":did,"phase":document_phase,
                                         "element_ids":fact.get("element_ids") or [],"reason":rejection,"origin":fact.get("origin")})
                        continue
                    located = locate_quote(source, fact.get("quote"), fact.get("page") or 1)
                    valid_ids={x["element_id"] for x in selected_document["elements"]}
                    element_ids=[x for x in (fact.get("element_ids") or []) if x in valid_ids]
                    if not element_ids and (located or {}).get("block_id") in valid_ids:element_ids=[(located or {}).get("block_id")]
                    if not element_ids:continue
                    fact["element_ids"]=element_ids;fact["parser_version_id"]=selected_document["selected_parser_version_id"];valid.append(fact)
                    deterministic_origin=fact.get("origin") in {"deterministic-parser","deterministic-table"}
                    provider = fact.get("origin") if deterministic_origin else fact.get("provider", "unknown")
                    model = "deterministic-table-v1" if provider=="deterministic-table" else "deterministic-v2" if provider == "deterministic-parser" else fact.get("model", "unknown")
                    conn.execute("""INSERT INTO extracted_facts(run_id,project_id,document_id,field_name,value_json,confidence,document_phase,parser_version_id,element_ids_json,page,line_start,line_end,block_id,bbox_json,quote,provider,model,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                 (run_id, project_id, did, fact["field"], json.dumps(fact.get("value"), ensure_ascii=False),
                                  float(fact.get("confidence", .7)), document_phase,selected_document["selected_parser_version_id"],json.dumps(element_ids), (located or {}).get("page") or fact.get("page"), None, None,
                                  (located or {}).get("block_id") or clean_block(fact.get("block")),
                                  json.dumps((located or {}).get("bbox")), fact.get("quote"), provider, model, now()))
                except Exception as exc:
                    rejected.append({"field":fact.get("field"),"value":fact.get("value"),"document_id":fact.get("document_id"),"reason":f"validation_error:{type(exc).__name__}"})
                    continue
            conn.execute("UPDATE audit_runs SET fact_count=? WHERE id=?", (len(valid), run_id))
        facts = valid
        finish_span(validation_span,"completed_with_warning" if rejected else "completed",output={"accepted":valid,"rejected":rejected,
                    "accepted_count":len(valid),"rejected_count":len(rejected)})
        event(run_id, "fact_persistence", "completed", f"已落库 {len(facts)} 条带证据事实",
              {"fact_count": len(facts), "llm_candidates": len(llm_facts), "parser_candidates": len(parser_facts),
               "rejected_count":len(rejected),"rejected_by_reason":dict(Counter(x["reason"] for x in rejected)),
               "record_counts": {kind: len(records(facts, kind)) for kind in ("payment_record", "change_record", "measurement_record")}})

        event(run_id, "deterministic_rules", "running", "执行金额、比例、日期顺序和八阶段完整性规则")
        rules_span = start_span(run_id, "deterministic_rules", "rules", "SQL/Python 确定性规则")
        attach_input(rules_span, {"facts": facts, "rules": executable_rules})
        documents = {d["id"]: d for d in docs}
        generated = 0
        with db() as conn:
            # Historical runs are immutable. Only clear a retry of this exact run.
            conn.execute("DELETE FROM anomalies WHERE run_id=?", (run_id,))
            phases = {d["canonical_phase"] for d in docs}
            expected_phases = list(CANONICAL_PHASES) if action_type=="final_review" else action_snapshot.get("required_phases",[])
            missing = [phase for phase in expected_phases if phase not in phases]
            if missing and rule_enabled("DOC-001"):
                title="全过程资料不完整" if action_type=="final_review" else "当前业务动作前置资料不完整"
                insert_anomaly(conn, run_id, project_id, "DOC-001", title, rule_severity("DOC-001", "high"),
                    f"{action_snapshot.get('action_label','当前审查')}缺少可识别的前置阶段：{'、'.join(missing)}。本结论只按资料截止时间 {run_row.get('cutoff_at') or '本次运行'} 判断，不要求尚未发生的后续阶段资料。",
                    None, 1, [], "deterministic")
                generated += 1

            award, contract = best_scalar(facts, "award_amount"), best_scalar(facts, "contract_amount")
            if rule_enabled("AMT-001") and award and contract and abs(num(award["value"]) - num(contract["value"])) > .5:
                difference = abs(num(award["value"]) - num(contract["value"]))
                insert_anomaly(conn, run_id, project_id, "AMT-001", "中标金额与合同金额不一致", rule_severity("AMT-001", "high"),
                    f"中标金额 {num(award['value']):,.0f} 元，合同金额 {num(contract['value']):,.0f} 元。",
                    difference, .99, [evidence_from_fact(award, documents), evidence_from_fact(contract, documents)], "deterministic")
                generated += 1

            periods = records(facts,"construction_period")
            unique_periods = {}
            for fact in periods:
                value=fact["value"];identity=(value.get("start_date"),value.get("end_date"))
                unique_periods.setdefault(identity,fact)
            if rule_enabled("TERM-001") and len(unique_periods) >= 2:
                ordered=list(unique_periods.values());lo,hi=ordered[0],ordered[1]
                insert_anomaly(conn, run_id, project_id, "TERM-001", "项目工期在资料间不一致", rule_severity("TERM-001", "high"),
                    f"资料记载的起止日期分别为 {lo['value'].get('start_date')} 至 {lo['value'].get('end_date')}、{hi['value'].get('start_date')} 至 {hi['value'].get('end_date')}。", None, .99,
                    [evidence_from_fact(lo, documents), evidence_from_fact(hi, documents)], "deterministic")
                generated += 1

            change_records = records(facts, "change_record")
            change_total = sum(num(f["value"].get("amount")) or 0 for f in change_records)
            payment_records = records(facts, "payment_record")
            paid,paid_fact,payment_calculation=payment_total(payment_records)
            settlement_records = records(facts, "settlement_record")
            settlement_fact = settlement_records[0] if settlement_records else None
            settlement = num(settlement_fact["value"].get("audited")) if settlement_fact else None
            terms_records = records(facts, "contract_terms")
            terms_fact = terms_records[0] if terms_records else None
            terms = terms_fact["value"] if terms_fact else {}
            contract_value = num(contract["value"]) if contract else float(project.get("contract_amount") or 0)
            acceptance = records(facts, "acceptance_record")
            if paid and paid_fact:
                base,cap,basis_name=payment_limit(contract_value,change_total,settlement,terms,bool(acceptance))
                basis_fact=settlement_fact if settlement is not None and acceptance else contract
                allowed = base * cap
                if rule_enabled("PAY-001") and paid > allowed + 1:
                    evidences = [evidence_from_fact(paid_fact, documents)]
                    if basis_fact:
                        evidences.append(evidence_from_fact(basis_fact, documents))
                    if terms_fact:
                        evidences.append(evidence_from_fact(terms_fact, documents))
                    if not (settlement is not None and acceptance):
                        evidences.extend(evidence_from_fact(f,documents) for f in change_records)
                    insert_anomaly(conn, run_id, project_id, "PAY-001", "累计付款超过当前节点允许额", rule_severity("PAY-001", "high"),
                        f"台账最新累计付款 {paid:,.0f} 元；按{basis_name} {base:,.0f} 元及 {cap:.0%} 节点上限，允许 {allowed:,.0f} 元，超出 {paid-allowed:,.0f} 元。",
                        paid - allowed, .99, evidences, "deterministic")
                    generated += 1

            threshold = num(terms.get("change_threshold")) or .10
            if rule_enabled("CHG-001") and change_total and contract_value and change_total / contract_value >= threshold:
                insert_anomaly(conn, run_id, project_id, "CHG-001", "累计工程变更达到重点复核阈值", rule_severity("CHG-001", "medium"),
                    f"按变更编号去重后累计 {change_total:,.0f} 元，占合同金额 {change_total/contract_value:.1%}，达到 {threshold:.0%} 阈值。",
                    change_total, .99, [evidence_from_fact(f, documents) for f in change_records], "deterministic")
                generated += 1

            for fact in change_records:
                value = fact["value"]
                approved, implemented = date_value(value.get("approved_date")), date_value(value.get("implemented_date"))
                change_no=str(value.get("change_no") or "").upper()
                if rule_enabled("SEQ-001") and change_no.startswith("BG") and approved and implemented and implemented < approved:
                    insert_anomaly(conn, run_id, project_id, "SEQ-001", "工程变更存在先实施后审批", rule_severity("SEQ-001", "high"),
                        f"变更 {value.get('change_no', '')} 于 {implemented} 实施，书面批准日期为 {approved}。",
                        num(value.get("amount")), .99, [evidence_from_fact(fact, documents)], "deterministic")
                    generated += 1

            user_deterministic_results=[]
            for rule in executable_rules:
                if rule["kind"]!="确定性规则" or rule.get("system_managed"):
                    continue
                result=evaluate_user_date_rule(rule,documents);user_deterministic_results.append({"rule":rule,"result":result})
                if result["status"]=="violated":
                    violation=result["violations"][0];evidences=[violation["left"],violation["right"]]
                    insert_anomaly(conn,run_id,project_id,rule["code"],rule["name"],rule_severity(rule["code"]),
                        f"确定性日期规则未通过：{violation['left']['term']} {violation['left']['date']}，"
                        f"要求{result['formula']}，对照事件日期为 {violation['right']['date']}。",None,.99,evidences,"deterministic-user-date-order")
                    generated+=1

            for rule in executable_rules:
                if rule["kind"]!="人工核验规则":continue
                insert_anomaly(conn,run_id,project_id,rule["code"],rule["name"],rule_severity(rule["code"]),
                    f"人工核验规则已启用：{rule.get('description') or '请按规则说明人工核验'}。核验字段：{rule.get('fields') or '未指定'}。",
                    None,1,[],"manual-review")
                generated+=1

            for fact in change_records:
                summary = str(fact["value"].get("attachment_summary") or "")
                explicitly_missing=(any(word in summary for word in ("缺","未附","未见")) and any(word in summary for word in ("附件","记录","资料")))
                if rule_enabled("ATT-001") and explicitly_missing:
                    insert_anomaly(conn, run_id, project_id, "ATT-001", "变更附件原件归档需补充核验", rule_severity("ATT-001", "low"),
                        f"变更 {fact['value'].get('change_no', '')} 明确记载附件或原始记录缺失：{summary}。请补充归档并人工核验；该提示不参与金额判断。",
                        None, .92, [evidence_from_fact(fact, documents)], "deterministic")
                    generated += 1
        with db() as conn:
            rule_outputs = [dict(row) for row in conn.execute("SELECT code,title,severity,summary,amount,confidence,status,rule_kind,evidence_json FROM anomalies WHERE run_id=?", (run_id,))]
        finish_span(rules_span, output={"finding_count":generated,"findings":rule_outputs,"calculation_policy":"python-only-v1",
            "calculations":{"payment_total":payment_calculation,"change_total":{"formula":"sum(unique change_record.amount)","value":change_total},
            "construction_periods":[{"document_id":f.get("document_id"),**f.get("value",{}),"formula":f.get("formula")} for f in periods],
            "user_deterministic_rules":user_deterministic_results}})
        event(run_id, "deterministic_rules", "completed", f"确定性规则生成 {generated} 项待复核事项", {"finding_count": generated})

        event(run_id, "semantic_consistency", "running", "复核招标、合同、清单与变更的实质语义")
        semantic_stage_span = start_span(run_id, "semantic_consistency", "stage", "语义一致性判断")
        semantic_prompt = published_prompt("semantic_consistency")
        semantic_payload = json.dumps({"project": project_id, "facts": facts}, ensure_ascii=False)
        semantic_corpus = "\n\n".join(f"=== id={d['id']} {d['name']} ({d['canonical_phase']}) ===\n{d['text'][:9000]}" for d in usable if d["canonical_phase"] in ("预算控制价", "招标评标", "施工合同", "变更签证"))
        custom_semantic_rules = [r for r in executable_rules if r["kind"] == "LLM语义规则" and r["code"] != "SEM-001"]
        try:
            semantic_variables={"facts": semantic_payload, "documents": semantic_corpus[:30000]}
            compared = await model_json(run_id, "semantic_consistency", [
                {"role": "system", "content": semantic_prompt["system_prompt"]+" 每条 evidence 必须包含 document_id、quote 和输入中真实的 element_ids；不得按换行号构造证据。"},
                {"role": "user", "content": render(semantic_prompt["user_prompt"],
                    semantic_variables)}],
                "reasoning", 3000, route_overrides, semantic_prompt, semantic_stage_span, semantic_variables)
            compared.pop("__trace", None)
            candidates = compared.get("findings", []) if isinstance(compared, dict) else []
        except Exception as exc:
            candidates = []
            event(run_id, "semantic_consistency", "running", "语义模型调用失败，确定性审查结果仍可交付", {"error": str(exc)[:180]})
        custom_rule_runs=[]
        for rule in custom_semantic_rules:
            rule_span=start_span(run_id,"semantic_consistency","custom_rule",f"用户语义规则 · {rule['code']} {rule['name']}",
                metadata={"rule_code":rule["code"],"rule_kind":rule["kind"]})
            corpus=semantic_rule_corpus(rule,documents)
            rule_system=("你是建设工程单规则核验器。只核验下面这一条规则，不处理任何其他规则。"
                "模型只做语义判断，不得计算金额、比例、合计、差额、日期差或日期先后；涉及这些内容必须返回 findings 为空。"
                "若原文直接明确记载缺失、未确认、未附或仅一方盖章，一条有效原文证据即可；否则至少提供两条相互印证的证据。"
                "每条证据必须逐字引用输入中的 document_id、element_id 和原文 quote。只返回 JSON。")
            rule_user=json.dumps({"rule":{"code":rule["code"],"name":rule["name"],"fields":rule["fields"],"criterion":rule["description"],
                "severity_is_assigned_by_backend":rule["severity"]},"documents":corpus,
                "output_schema":{"findings":[{"code":rule["code"],"title":"","summary":"","confidence":0.0,
                    "evidence":[{"document_id":0,"quote":"","element_ids":[""]}]}]}},ensure_ascii=False)
            try:
                result=await model_json(run_id,"semantic_consistency",[{"role":"system","content":rule_system},{"role":"user","content":rule_user}],
                    "reasoning",1800,route_overrides,None,rule_span,{"rule":rule,"documents":corpus})
                result.pop("__trace",None);rule_candidates=result.get("findings",[]) if isinstance(result,dict) else []
                candidates.extend(rule_candidates);finish_span(rule_span,output={"rule":rule,"findings":rule_candidates})
                custom_rule_runs.append({"rule_code":rule["code"],"candidate_count":len(rule_candidates),"status":"completed"})
            except Exception as exc:
                finish_span(rule_span,"failed",error={"message":str(exc)});custom_rule_runs.append({"rule_code":rule["code"],"candidate_count":0,"status":"failed","error":str(exc)})
        semantic_saved = 0
        candidate_decisions=[]
        with db() as conn:
            for index, finding in enumerate(candidates[:12], 1):
                finding_code = str(finding.get("code") or "").upper()
                builtin_candidate = finding.get("verdict") in ("substantial_scope_change", "unapproved_duplicate") and rule_enabled("SEM-001")
                custom_candidate = finding_code in active_rules and active_rules[finding_code]["kind"] == "LLM语义规则"
                if not (builtin_candidate or custom_candidate):
                    candidate_decisions.append({"code":finding_code,"decision":"rejected","reason":"rule_not_enabled_or_kind_mismatch"})
                    continue
                if float(finding.get("confidence", 0)) < .75:
                    candidate_decisions.append({"code":finding_code,"decision":"rejected","reason":"confidence_below_0.75"})
                    continue
                evidences = []
                for source in finding.get("evidence") or []:
                    did = int(source.get("document_id") or 0)
                    doc = documents.get(did, {})
                    requested_ids=[str(x) for x in (source.get("element_ids") or [])]
                    valid_ids={x["element_id"] for x in doc.get("elements") or []};element_ids=[x for x in requested_ids if x in valid_ids]
                    located=None
                    if element_ids:
                        element=next(x for x in doc["elements"] if x["element_id"]==element_ids[0]);meta=element.get("metadata_json") or {}
                        located={"page":element["page"],"block_id":element["element_id"],"bbox":element.get("bbox_json"),"page_width":meta.get("page_width"),"page_height":meta.get("page_height")}
                    if not located:located = locate_quote(doc.get("content_json") or {}, source.get("quote"), source.get("page") or 1)
                    if located and not element_ids and located.get("block_id") in valid_ids:element_ids=[located["block_id"]]
                    if did in documents and located:
                        evidences.append({"document_id": did, "document_name": doc.get("name", "未知资料"),
                            "page": located.get("page"), "block_id": located.get("block_id"), "bbox": located.get("bbox"),
                            "parser_version_id":doc.get("selected_parser_version_id"),"element_ids":element_ids,"quote": source.get("quote", "")})
                minimum_evidence=1 if custom_candidate and explicit_single_source_proof(evidences) else 2
                if len(evidences) < minimum_evidence:
                    candidate_decisions.append({"code":finding_code,"decision":"rejected","reason":f"evidence_below_{minimum_evidence}","valid_evidence_count":len(evidences)})
                    continue
                final_code = finding_code if custom_candidate else "SEM-001"
                insert_anomaly(conn, run_id, project_id, final_code,
                    finding.get("title") or "语义一致性待复核", rule_severity(final_code, "medium"),
                    str(finding.get("summary") or "")[:1000], None,
                    float(finding.get("confidence", .75)), evidences, "llm-semantic")
                generated += 1
                semantic_saved += 1
                candidate_decisions.append({"code":final_code,"decision":"saved","valid_evidence_count":len(evidences),"minimum_evidence":minimum_evidence})
        finish_span(semantic_stage_span, "completed", output={"candidate_count": len(candidates),
            "saved_count": semantic_saved, "findings": candidates,"candidate_decisions":candidate_decisions,"custom_rule_runs":custom_rule_runs})
        event(run_id, "semantic_consistency", "completed", f"语义复核返回 {len(candidates)} 项候选，{semantic_saved} 项通过证据门槛",
              {"candidate_count": len(candidates), "saved_count": semantic_saved})

        event(run_id, "policy_retrieval", "running", "生成政策查询并重排本次异常相关条款")
        policy_stage_span = start_span(run_id, "policy_retrieval", "stage", "政策 RAG 查询改写与重排")
        with db() as conn:
            policies = [dict(row) for row in conn.execute("SELECT * FROM policy_chunks WHERE enabled=1 ORDER BY id")]
            finding_rows = [dict(row) for row in conn.execute(
                "SELECT code,title,severity,summary FROM anomalies WHERE run_id=?", (run_id,))]
        finding_text = " ".join(row["title"] + " " + row["summary"] for row in finding_rows)
        groups = {"付款控制": ("付款", "进度款", "超付"), "工程变更": ("变更", "清单", "重复"),
                  "合同一致性": ("合同", "招标", "工期", "范围", "中标"),
                  "资料完整性": ("资料", "附件", "完整", "归档")}
        ranked = []
        for policy in policies:
            score = sum(finding_text.count(word) for word in groups.get(policy["clause"], ()))
            if score:
                ranked.append((score, policy))
        deterministic_hits = [p for _, p in sorted(ranked, key=lambda x: x[0], reverse=True)]
        policy_prompt = published_prompt("policy_retrieval")
        retrieval_error = None
        retrieval_output = {"query": finding_text[:500], "ranked_ids": [p["id"] for p in deterministic_hits]}
        try:
            policy_variables={"findings": finding_rows, "policies": [{"id": p["id"], "clause": p["clause"], "text": p["text"]} for p in policies]}
            rewritten = await model_json(run_id, "policy_retrieval", [
                {"role": "system", "content": policy_prompt["system_prompt"]},
                {"role": "user", "content": render(policy_prompt["user_prompt"],
                    policy_variables)}],
                max_tokens=1200, route_overrides=route_overrides, prompt_version=policy_prompt,
                parent_span_id=policy_stage_span,prompt_variables=policy_variables)
            rewritten.pop("__trace", None)
            retrieval_output = rewritten
        except Exception as exc:
            retrieval_error = str(exc)
        by_id = {p["id"]: p for p in policies}
        ranked_ids = [int(x) for x in retrieval_output.get("ranked_ids", []) if str(x).isdigit() and int(x) in by_id]
        policy_hits = [by_id[x] for x in ranked_ids] or deterministic_hits
        finish_span(policy_stage_span, "completed_with_warning" if retrieval_error else "completed",
                    output={"query": retrieval_output.get("query"), "candidates": policies,
                            "deterministic_scores": [{"id": p["id"], "score": score} for score, p in ranked],
                            "ranked_ids": [p["id"] for p in policy_hits]},
                    error={"message": retrieval_error, "fallback": "deterministic ranking"} if retrieval_error else None)
        event(run_id, "policy_retrieval", "completed", f"政策检索命中 {len(policy_hits)} 条相关条款",
              {"hit_count": len(policy_hits), "clauses": [p["clause"] for p in policy_hits], "error": retrieval_error})

        skipped_span(run_id, "text2sql", "本次审查没有自然语言数据库查询任务；金额和日期规则使用固定只读 SQL/Python")
        event(run_id, "text2sql", "skipped", "本次没有 Text2SQL 任务，未伪造模型调用")

        event(run_id, "result_summary", "running", "将既有事实、异常和政策命中整理为审查摘要")
        summary_stage_span = start_span(run_id, "result_summary", "stage", "结果摘要")
        summary_prompt = published_prompt("result_summary")
        ai_summary = {"summary": "", "focus": []}
        summary_error = None
        try:
            summary_variables={"facts": facts, "findings": finding_rows, "policies": policy_hits}
            ai_summary = await model_json(run_id, "result_summary", [
                {"role": "system", "content": summary_prompt["system_prompt"]},
                {"role": "user", "content": render(summary_prompt["user_prompt"],
                    summary_variables)}],
                max_tokens=1600, route_overrides=route_overrides, prompt_version=summary_prompt,
                parent_span_id=summary_stage_span,prompt_variables=summary_variables)
            ai_summary.pop("__trace", None)
        except Exception as exc:
            summary_error = str(exc)
        finish_span(summary_stage_span, "completed_with_warning" if summary_error else "completed",
                    output=ai_summary, error={"message": summary_error} if summary_error else None)
        event(run_id, "result_summary", "completed", "审查摘要已生成" if not summary_error else "摘要模型失败，结构化结果仍可交付",
              {"error": summary_error})

        with db() as conn:
            anomaly_count = conn.execute("SELECT COUNT(*) FROM anomalies WHERE run_id=?", (run_id,)).fetchone()[0]
            pending_count = conn.execute("SELECT COUNT(*) FROM anomalies WHERE run_id=? AND status='待复核'", (run_id,)).fetchone()[0]
            high = conn.execute("SELECT COUNT(*) FROM anomalies WHERE run_id=? AND severity='high' AND status='待复核'", (run_id,)).fetchone()[0]
            current_anomalies=[dict(row) for row in conn.execute("SELECT * FROM anomalies WHERE run_id=? ORDER BY id",(run_id,))]
            baseline_anomalies=[]
            if run_row.get("baseline_run_id"):
                baseline_anomalies=[dict(row) for row in conn.execute("SELECT * FROM anomalies WHERE run_id=? ORDER BY id",(run_row["baseline_run_id"],))]
            prior_by_key={str(item.get("risk_key") or item["code"]):item for item in baseline_anomalies}
            current_by_key={};transition_counts={"new":0,"ongoing":0,"resolved":0}
            for item in current_anomalies:
                key=str(item.get("risk_key") or item["code"]);prior=prior_by_key.get(key)
                transition="ongoing" if prior else "new";transition_counts[transition]+=1;current_by_key[key]=item
                conn.execute("UPDATE anomalies SET risk_key=?,lifecycle_status=?,prior_anomaly_id=? WHERE id=?",
                             (key,transition,prior["id"] if prior else None,item["id"]))
                conn.execute("INSERT INTO risk_transitions(run_id,project_id,risk_key,code,transition,prior_anomaly_id,current_anomaly_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                             (run_id,project_id,key,item["code"],transition,prior["id"] if prior else None,item["id"],now()))
            for key,prior in prior_by_key.items():
                if key in current_by_key:continue
                transition_counts["resolved"]+=1
                conn.execute("INSERT INTO risk_transitions(run_id,project_id,risk_key,code,transition,prior_anomaly_id,current_anomaly_id,created_at) VALUES(?,?,?,?,?,?,NULL,?)",
                             (run_id,project_id,key,prior["code"],"resolved",prior["id"],now()))
            blocking_codes={code for code,rule in active_rules.items() if int(rule.get("blocking") or 0)}
            blocking_findings=[item for item in current_anomalies if item["code"] in blocking_codes and item["status"]=="待复核"]
            if blocking_findings: recommendation,decision_status="暂缓审批","blocked"
            elif pending_count: recommendation,decision_status="附条件放行","conditional"
            else: recommendation,decision_status="建议放行","pass"
            decision={"status":decision_status,"recommendation":recommendation,"action_type":action_type,
                      "action_label":action_snapshot.get("action_label"),"blocking_count":len(blocking_findings),
                      "pending_count":pending_count,"risk_transitions":transition_counts,
                      "cutoff_at":run_row.get("cutoff_at"),"baseline_run_id":run_row.get("baseline_run_id")}
            conn.execute("UPDATE audit_runs SET decision_json=? WHERE id=?",(json.dumps(decision,ensure_ascii=False),run_id))
            if run_row.get("action_id"):
                conn.execute("UPDATE review_actions SET status=?,updated_at=? WHERE id=?",
                             ("blocked" if blocking_findings else "reviewed",now(),run_row["action_id"]))
            payment_records = records(facts, "payment_record")
            paid,_,_=payment_total(payment_records)
            change_total = sum(num(f["value"].get("amount")) or 0 for f in records(facts, "change_record"))
            scalar_fields = ("budget", "max_price", "award_amount", "contract_amount",
                             "settlement_submitted", "settlement_audited", "settlement_reduction")
            facts_summary = {}
            for field in scalar_fields:
                selected = best_scalar(facts, field)
                if selected:
                    facts_summary[field] = selected["value"]
            facts_summary.update({"project_id": project_id, "paid_amount": paid, "change_amount": change_total,
                                  "document_phases": sorted({d["canonical_phase"] for d in docs}),
                                  "payment_record_count": len(payment_records), "change_record_count": len(records(facts, "change_record"))})
            period=next(iter(records(facts,"construction_period")),None)
            if period:facts_summary["construction_period_days"]=period["value"].get("calendar_days")
            conn.execute("DELETE FROM timeline WHERE project_id=?", (project_id,))
            timeline = []
            for fact in facts:
                field, value = fact.get("field"), fact.get("value")
                if not isinstance(value, dict):
                    continue
                if field == "change_record": timeline.extend([(value.get("proposed_date"), f"{value.get('change_no', '变更')} 提出", "变更签证", "done"),
                    (value.get("approved_date"), f"{value.get('change_no', '变更')} 批准", "变更签证", "done"),
                    (value.get("implemented_date"), f"{value.get('change_no', '变更')} 实施", "变更签证", "done")])
                elif field == "payment_record": timeline.append((value.get("date"), f"{value.get('payment_no', '付款')} {value.get('stage', '')}", "付款台账", "done"))
                elif field == "measurement_record": timeline.append((value.get("date"), f"{value.get('measurement_no', '计量')} 计量确认", "进度计量", "done"))
                elif field == "acceptance_record": timeline.append((value.get("date"), f"竣工验收：{value.get('result', '')}", "竣工验收", "done"))
                elif field == "settlement_record": timeline.append((value.get("audit_date"), "竣工结算审定", "结算资料", "done"))
            seen = set()
            for date, title, source, state in sorted([x for x in timeline if x[0]], key=lambda x: x[0]):
                key = (date, title)
                if key in seen:
                    continue
                seen.add(key)
                conn.execute("INSERT INTO timeline(project_id,event_date,title,source,state) VALUES(?,?,?,?,?)",
                             (project_id, date, title, source, state))
            conn.execute("UPDATE projects SET risk_count=?,high_risk_count=?,updated_at=?,facts_json=? WHERE id=?",
                         (pending_count, high, now(), json.dumps(facts_summary, ensure_ascii=False), project_id))
            result = {"document_count": len(docs), "document_phases": sorted({d["canonical_phase"] for d in docs}),
                      "fact_count": len(facts), "anomaly_count": anomaly_count, "rules_executed": len(executable_rules),
                      "record_counts": {"payments": len(payment_records), "changes": len(records(facts, "change_record")),
                                        "measurements": len(records(facts, "measurement_record"))},
                      "policy_hits": [{"title": p["title"], "clause": p["clause"], "text": p["text"],
                                       "source": p["source"], "is_template": bool(p["is_template"])} for p in policy_hits],
                       "ai_summary": ai_summary,"review_mode":run_row.get("review_mode"),"action":action_snapshot,
                       "decision":decision}
            conn.execute("UPDATE audit_runs SET status='completed',finished_at=?,progress=100,current_stage='complete',anomaly_count=?,rule_count=?,result_json=? WHERE id=?",
                         (now(), anomaly_count, len(executable_rules), json.dumps(result, ensure_ascii=False), run_id))
        event(run_id, "complete", "completed", f"真实 AI 审查完成：{len(facts)} 条事实、{anomaly_count} 项待复核事项")
    except Exception as exc:
        with db() as conn:
            conn.execute("UPDATE audit_runs SET status='failed',finished_at=?,error=?,result_json=? WHERE id=?",
                         (now(), str(exc)[:1200], json.dumps({"trace": traceback.format_exc()[-3000:]}, ensure_ascii=False), run_id))
        event(run_id, "complete", "failed", f"审查失败：{str(exc)[:300]}")


def execute_audit(run_id, project_id):
    asyncio.run(execute(run_id, project_id))
