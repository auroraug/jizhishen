import json
import os
import re
import tempfile
import threading
import unittest
import asyncio
import io
import zipfile
import httpx
import fitz
from unittest.mock import AsyncMock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


TEST_DATA = tempfile.TemporaryDirectory(prefix="jizhishen-mvp-test-")
os.environ["DATA_DIR"] = TEST_DATA.name
os.environ["MODEL_CONFIG_PATH"] = str(Path(TEST_DATA.name) / "model-config.json")

from fastapi.testclient import TestClient
from backend.app.main import app, startup
from backend.app.db import db
from backend.app.trace_store import save_artifact, start_span
from backend.app.audit_pipeline import (best_scalar, deterministic_table_facts, fact_rejection_reason,
                                        label_amount, model_json, payment_limit, canonicalize_fact_value,
                                        deterministic_extract, payment_total, evaluate_user_date_rule,
                                        explicit_single_source_proof)
from backend.app.parser_service import normalize_mineru, build_chunks, table_grid, _document_zip, content_coverage_warnings
from backend.app.providers import mineru_local_submit_files
from backend.app.providers import test_provider_config as probe_provider_config
from backend.app.model_config import _normalize
from backend.app.prompt_store import published_prompt, seed_prompts


class MockOpenAIHandler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *_args):
        pass

    def _json(self, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": "mock-ok"}, {"id": "mock-invalid"}]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size) or b"{}")
        self.__class__.calls.append(body)
        model = body.get("model")
        messages = body.get("messages") or []
        system = messages[0].get("content", "") if messages else ""
        user = messages[-1].get("content", "") if messages else ""
        if model == "mock-invalid":
            content = "this is not json"
        elif "资料分类器" in system:
            pairs = re.findall(r'"document_id":\s*(\d+).*?"declared_type":\s*"([^"]+)"', user, re.S)
            content = json.dumps({"documents": [{"document_id": int(i), "phase": phase, "confidence": .99} for i, phase in pairs]}, ensure_ascii=False)
        elif "语义一致性" in system:
            content = '{"findings":[]}'
        elif "政策检索" in system:
            ids = [int(x) for x in re.findall(r'"id":\s*(\d+)', user)]
            content = json.dumps({"query": "工程审查政策", "ranked_ids": ids[:4]}, ensure_ascii=False)
        elif "结果摘要" in system:
            content = '{"summary":"Mock 全链路审查完成","focus":[]}'
        else:
            content = '{"facts":[]}'
        self._json(200, {"id": "mock-response", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}})


class ObservableMVPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        startup()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/v1"
        cls.client = TestClient(app)
        cfg = cls.client.get("/api/providers").json()
        cfg["providers"] = [
            {"id": "mock", "name": "Local Mock", "kind": "openai", "base_url": cls.base_url,
             "api_key": "secret-TRACE-KEY", "enabled": True, "models": ["mock-ok", "mock-invalid"]},
            {"id": "anonymous", "name": "Anonymous Local Mock", "kind": "openai", "base_url": cls.base_url,
             "api_key": "", "enabled": True, "models": ["mock-ok"]},
            {"id": "mineru_cloud", "name": "MinerU", "kind": "mineru", "base_url": "https://mineru.net/api/v4",
             "api_key": "mineru-SECRET", "enabled": True, "models": ["vlm"]},
        ]
        cfg["stage_routes"] = {stage: {"provider_id": "mock", "model": "mock-ok"} for stage in (
            "document_classification", "general_extraction", "payment_extraction", "change_extraction",
            "contract_extraction", "semantic_consistency", "policy_retrieval", "text2sql", "result_summary")}
        cfg["stage_routes"]["ocr"] = {"provider_id": "mineru_cloud", "model": "vlm"}
        response = cls.client.put("/api/providers", json={"schema_version": 1, "providers": cfg["providers"], "stage_routes": cfg["stage_routes"]})
        assert response.status_code == 200, response.text

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        TEST_DATA.cleanup()

    def test_legacy_extraction_prompt_is_migrated_by_schema_not_version_number(self):
        stage = "general_extraction"
        with db() as conn:
            before = conn.execute(
                "SELECT * FROM prompt_versions WHERE stage=? AND is_current=1", (stage,)
            ).fetchone()
            conn.execute("UPDATE prompt_versions SET is_current=0 WHERE stage=?", (stage,))
            legacy_version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM prompt_versions WHERE stage=?", (stage,)
            ).fetchone()[0]
            cursor = conn.execute(
                """INSERT INTO prompt_versions(stage,version,status,is_current,system_prompt,user_prompt,json_schema,parser_version,created_at,published_at)
                   VALUES(?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
                (stage, legacy_version, "published", 1, before["system_prompt"],
                 'Return {"facts":[{"quote":"x [P1-B1]","block":"B1"}]}',
                 before["json_schema"], "json-v2"),
            )
            legacy_id = cursor.lastrowid

        seed_prompts()
        current = published_prompt(stage)
        self.assertGreater(current["version"], legacy_version)
        self.assertEqual(current["parser_version"], "element-id-json-v2")
        self.assertIn("element_ids", current["user_prompt"])
        with db() as conn:
            legacy = conn.execute("SELECT * FROM prompt_versions WHERE id=?", (legacy_id,)).fetchone()
        self.assertEqual(legacy["status"], "published")
        self.assertEqual(legacy["is_current"], 0)
        self.assertIn("P1-B1", legacy["user_prompt"])

    def test_01_clean_start_has_no_seeded_demo_or_risk(self):
        self.assertEqual(self.client.get("/api/projects").json()["total"], 0)
        dashboard = self.client.get("/api/dashboard").json()
        self.assertEqual(dashboard["counts"]["anomalies"], 0)
        self.assertEqual(dashboard["counts"]["projects"], 0)

    def test_02_provider_secret_is_masked_and_models_are_discovered(self):
        payload = self.client.get("/api/providers").json()
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret-TRACE-KEY", text)
        self.assertIn("••••", text)
        self.assertTrue(next(p for p in payload["providers"] if p["id"] == "anonymous")["configured"])
        models = self.client.get("/api/providers/mock/models")
        self.assertEqual(models.status_code, 200)
        self.assertIn("mock-ok", models.json()["items"])
        anonymous = self.client.post("/api/providers/test", json={"provider_id": "anonymous"})
        self.assertEqual(anonymous.status_code, 200, anonymous.text)
        self.assertTrue(anonymous.json()["ok"], anonymous.text)

    def test_03_full_pipeline_trace_is_real_and_exportable(self):
        created = self.client.post("/api/projects", json={"id": "TEST-TRACE-001", "name": "隔离全链路测试工程",
            "community": "测试社区", "category": "市政工程", "budget": 1000000, "contract_amount": 0,
            "status": "施工中", "progress": 50, "with_demo_materials": False})
        self.assertEqual(created.status_code, 201, created.text)
        fixtures = [
            ("01_立项审批.txt", "立项审批", "项目概算：1,000,000元。\n建设范围：道路提升。"),
            ("02_预算.txt", "预算控制价", "最高投标限价：1,000,000元。"),
            ("03_招标.txt", "招标评标", "中标价格：900,000元。\n计划工期：120日历天。"),
            ("04_合同.txt", "施工合同", "签约合同价：人民币900,000元。\n合同工期总日历天数：120天。\n工程进度款按80%支付。"),
            ("05_计量.txt", "开工计量", "开工日期：2026-01-01。"),
            ("06_变更.txt", "变更签证", "本期无工程变更。"),
            ("07_验收.txt", "竣工验收", "验收日期 2026-06-01\n验收结论 合格"),
            ("08_付款.txt", "结算付款", "累计支付金额：700,000元。"),
        ]
        for name, doc_type, text in fixtures:
            response = self.client.post("/api/documents/upload", data={"project_id": "TEST-TRACE-001", "doc_type": doc_type,
                "source_system": "自动化测试夹具"}, files={"file": (name, text.encode("utf-8"), "text/plain")})
            self.assertEqual(response.status_code, 200, response.text)
        started = self.client.post("/api/projects/TEST-TRACE-001/audit", json={"route_overrides": {}})
        self.assertEqual(started.status_code, 202, started.text)
        run_id = started.json()["run_id"]
        run = self.client.get(f"/api/audit-runs/{run_id}").json()
        self.assertEqual(run["run"]["status"], "completed", run["run"].get("error"))
        self.assertGreater(len(run["spans"]), 10)
        trace = self.client.get(f"/api/trace/runs/{run_id}").json()
        stages = {span["stage"]: span["status"] for span in trace["spans"]}
        for stage in ("document_classification", "general_extraction", "payment_extraction", "change_extraction",
                      "contract_extraction", "semantic_consistency", "policy_retrieval", "result_summary"):
            self.assertIn(stage, stages)
        self.assertEqual(stages["text2sql"], "skipped")
        raw = json.dumps(trace, ensure_ascii=False)
        self.assertIn("messages", raw)
        self.assertIn("structured_output", raw)
        self.assertNotIn("secret-TRACE-KEY", raw)
        self.assertNotIn("mineru-SECRET", raw)
        exported = self.client.get(f"/api/trace/runs/{run_id}/export")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("attachment", exported.headers["content-disposition"])
        bundle = self.client.get(f"/api/trace/runs/{run_id}/bundle")
        self.assertEqual(bundle.status_code, 200)
        self.assertEqual(bundle.headers["content-type"], "application/zip")
        self.__class__.run_id = run_id

    def test_04_large_artifact_spills_to_gzip_and_round_trips(self):
        run_id = self.__class__.run_id
        span_id = start_span(run_id, "large_test", "test", "large artifact")
        artifact_id = save_artifact(run_id, span_id, "output", "large", {"text": "测" * 300000})
        with db() as conn:
            row = conn.execute("SELECT inline_json,file_path,size_bytes FROM trace_artifacts WHERE id=?", (artifact_id,)).fetchone()
        self.assertIsNone(row["inline_json"])
        self.assertTrue(Path(row["file_path"]).exists())
        self.assertGreater(row["size_bytes"], 256 * 1024)

    def test_05_model_failures_are_traced_without_hidden_fallback(self):
        run_id = self.__class__.run_id
        def trace(content, raw=None):
            return {"content": content, "provider": "Mock", "provider_id": "mock", "model": "mock-ok",
                "duration_ms": 1, "input_tokens": 1, "output_tokens": 1, "usage": {"total_tokens": 2},
                "request_hash": "abc", "started_at": "2026-01-01T00:00:00.000",
                "raw_response": raw or {"choices": [{"message": {"content": content}}]},
                "assistant_message": (raw or {"choices": [{"message": {"content": content}}]})["choices"][0]["message"],
                "response_headers": {}, "status_code": 200, "finish_reason": "stop"}
        prompt = {"version": 1, "json_schema": {"type": "object", "required": ["facts"]}}
        invalid = AsyncMock(return_value=trace("not-json"))
        with patch("backend.app.audit_pipeline.chat_with_trace", invalid):
            with self.assertRaises(ValueError):
                asyncio.run(model_json(run_id, "general_extraction", [{"role":"user","content":"x"}], prompt_version=prompt))
        self.assertEqual(invalid.await_count, 1, "MVP 不应悄悄调用第二个模型 fallback")
        timeout = AsyncMock(side_effect=httpx.ReadTimeout("mock timeout"))
        with patch("backend.app.audit_pipeline.chat_with_trace", timeout):
            with self.assertRaises(httpx.ReadTimeout):
                asyncio.run(model_json(run_id, "general_extraction", [{"role":"user","content":"x"}], prompt_version=prompt))
        tool_raw = {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{"id":"call_1","type":"function","function":{"name":"lookup","arguments":"{}"}}]}}]}
        with patch("backend.app.audit_pipeline.chat_with_trace", AsyncMock(return_value=trace("", tool_raw))):
            with self.assertRaises(ValueError):
                asyncio.run(model_json(run_id, "general_extraction", [{"role":"user","content":"x"}], prompt_version=prompt))
        with patch("backend.app.audit_pipeline.chat_with_trace", AsyncMock(return_value=trace("{}"))):
            with self.assertRaises(ValueError):
                asyncio.run(model_json(run_id, "general_extraction", [{"role":"user","content":"x"}], prompt_version=prompt))
        full = json.dumps(self.client.get(f"/api/trace/runs/{run_id}").json(), ensure_ascii=False)
        self.assertIn("mock timeout", full)
        self.assertIn("tool_calls", full)
        self.assertIn("缺少必填字段 facts", full)

    def test_06_derived_run_reuses_upstream_and_does_not_mutate_project(self):
        source = self.__class__.run_id
        response = self.client.post(f"/api/trace/runs/{source}/replay", json={"stage":"general_extraction","mode":"derived"})
        self.assertEqual(response.status_code, 201, response.text)
        derived_id = response.json()["run_id"]
        derived = self.client.get(f"/api/trace/runs/{derived_id}").json()
        self.assertEqual(derived["run"]["status"], "completed", derived["run"].get("error"))
        self.assertEqual(derived["run"]["parent_run_id"], source)
        self.assertFalse(derived["run"]["result"]["project_mutated"])
        self.assertTrue(any(x["status"] == "reused" for x in derived["spans"]))
        latest_business = self.client.get("/api/projects/TEST-TRACE-001/audit-runs/latest").json()["run"]
        self.assertEqual(latest_business["id"], source)

    def test_07_prompt_publish_is_immutable(self):
        prompts = self.client.get("/api/prompts").json()["items"]
        current = next(x for x in prompts if x["stage"] == "general_extraction" and x["is_current"])
        draft = self.client.post("/api/prompts/drafts", json={"stage": current["stage"], "based_on_id": current["id"]}).json()
        changed = self.client.patch(f"/api/prompts/{draft['id']}", json={"system_prompt": draft["system_prompt"] + "\n测试版本"})
        self.assertEqual(changed.status_code, 200)
        published = self.client.post(f"/api/prompts/{draft['id']}/publish")
        self.assertEqual(published.status_code, 200)
        immutable = self.client.patch(f"/api/prompts/{draft['id']}", json={"system_prompt": "should fail"})
        self.assertEqual(immutable.status_code, 409)
        tested = self.client.post(f"/api/trace/runs/{self.__class__.run_id}/replay",
            json={"stage":"general_extraction","mode":"probe","prompt_id":draft["id"]})
        self.assertEqual(tested.status_code, 201, tested.text)
        tested_trace = json.dumps(self.client.get(f"/api/trace/runs/{tested.json()['run_id']}").json(), ensure_ascii=False)
        self.assertIn("测试版本", tested_trace)

    def test_08_trace_delete_reverts_project_count(self):
        run_id = self.__class__.run_id
        response = self.client.delete(f"/api/trace/runs/{run_id}?confirm=run-{run_id}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(f"/api/trace/runs/{run_id}").status_code, 404)
        project = self.client.get("/api/projects/TEST-TRACE-001").json()["project"]
        self.assertEqual(project["risk_count"], 0)

    def test_09_demo_install_is_explicit_idempotent_and_excluded(self):
        before = self.client.get("/api/dashboard").json()["counts"]
        self.assertEqual(before["projects"], 1)
        self.assertEqual(before["demo_projects"], 0)
        installed = self.client.post("/api/demo/install")
        self.assertEqual(installed.status_code, 200, installed.text)
        self.assertEqual(installed.json()["installed"], 12)
        counts = self.client.get("/api/dashboard").json()["counts"]
        self.assertEqual(counts["projects"], 1)
        self.assertEqual(counts["demo_projects"], 12)
        projects = self.client.get("/api/projects").json()["items"]
        demos = [item for item in projects if item["is_demo"]]
        self.assertEqual(len(demos), 12)
        self.assertTrue(all(item["risk_count"] == 0 for item in demos))
        repeated = self.client.post("/api/demo/install")
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["installed"], 0)

    def test_10_rules_and_policies_are_manageable(self):
        initial_rules = self.client.get("/api/rules").json()["items"]
        self.assertGreaterEqual(len(initial_rules), 8)
        created = self.client.post("/api/rules", json={"code":"SCOPE-900","name":"测试范围规则",
            "kind":"LLM语义规则","fields":"招标范围、合同范围","description":"测试 CRUD",
            "severity":"medium","enabled":True})
        self.assertEqual(created.status_code, 201, created.text)
        updated = self.client.patch("/api/rules/SCOPE-900", json={"name":"测试范围规则（更新）",
            "kind":"LLM语义规则","fields":"范围","description":"更新成功","severity":"high","enabled":False})
        self.assertEqual(updated.status_code, 200, updated.text)
        rule = next(x for x in self.client.get("/api/rules").json()["items"] if x["code"] == "SCOPE-900")
        self.assertEqual(rule["severity"], "high")
        self.assertEqual(rule["enabled"], 0)
        self.assertEqual(self.client.delete("/api/rules/SCOPE-900").status_code, 200)

        created_policy = self.client.post("/api/policies", json={"title":"测试政策","clause":"工程变更",
            "text":"测试条款正文","source":"测试来源","effective_date":"2026-08-10",
            "is_template":False,"enabled":True})
        self.assertEqual(created_policy.status_code, 201, created_policy.text)
        policy_id = created_policy.json()["id"]
        updated_policy = self.client.patch(f"/api/policies/{policy_id}", json={"title":"测试政策（更新）",
            "clause":"工程变更","text":"更新后的正文","source":"测试来源","effective_date":None,
            "is_template":True,"enabled":False})
        self.assertEqual(updated_policy.status_code, 200, updated_policy.text)
        policy = next(x for x in self.client.get("/api/policies").json()["items"] if x["id"] == policy_id)
        self.assertEqual(policy["enabled"], 0)
        self.assertEqual(self.client.delete(f"/api/policies/{policy_id}").status_code, 200)

    def test_11_parser_versions_are_immutable_and_audit_snapshot_is_stable(self):
        project_id="TEST-PARSER-001"
        created=self.client.post("/api/projects",json={"id":project_id,"name":"解析版本测试工程","community":"新和村","category":"市政工程","budget":100000,"contract_amount":0,"status":"立项审批","progress":0})
        self.assertEqual(created.status_code,201,created.text)
        pdf=fitz.open();page=pdf.new_page();page.insert_text((72,90),"Contract amount 100000 yuan",fontsize=12);raw=pdf.tobytes();pdf.close()
        uploaded=self.client.post("/api/documents/upload",data={"project_id":project_id,"doc_type":"施工合同","parse_mode":"pymupdf"},files={"file":("contract.pdf",raw,"application/pdf")})
        self.assertEqual(uploaded.status_code,200,uploaded.text)
        document_id=uploaded.json()["document_id"];first=uploaded.json()["parser_version_id"]
        second_response=self.client.post(f"/api/documents/{document_id}/parser-versions",json={"parser_kind":"pymupdf","force":True})
        self.assertEqual(second_response.status_code,202,second_response.text);second=second_response.json()["parser_version_id"]
        self.assertNotEqual(first,second)
        versions=self.client.get(f"/api/documents/{document_id}/parser-versions").json()["items"]
        self.assertEqual(len(versions),2);self.assertTrue(all(v["source_sha256"]==versions[0]["source_sha256"] for v in versions))
        self.assertEqual(self.client.patch(f"/api/documents/{document_id}/active-parser-version",json={"parser_version_id":second}).status_code,200)
        run=self.client.post(f"/api/projects/{project_id}/audit",json={"parser_versions":{str(document_id):second}})
        self.assertEqual(run.status_code,202,run.text);run_id=run.json()["run_id"]
        third=self.client.post(f"/api/documents/{document_id}/parser-versions",json={"parser_kind":"pymupdf","force":True}).json()["parser_version_id"]
        self.client.patch(f"/api/documents/{document_id}/active-parser-version",json={"parser_version_id":third})
        with db() as conn:
            frozen=conn.execute("SELECT parser_version_id FROM audit_run_documents WHERE run_id=? AND document_id=?",(run_id,document_id)).fetchone()[0]
        self.assertEqual(frozen,second,"active 切换不能改写已运行审查的解析版本快照")
        self.assertEqual(self.client.delete(f"/api/parser-versions/{third}").status_code,409,"active 版本禁止删除")
        self.assertEqual(self.client.delete(f"/api/parser-versions/{second}").status_code,409,"审查快照引用版本禁止删除")
        deleted=self.client.delete(f"/api/parser-versions/{first}")
        self.assertEqual(deleted.status_code,200,deleted.text)
        self.assertTrue(deleted.json()["trace_preserved"])
        remaining=self.client.get(f"/api/documents/{document_id}/parser-versions").json()["items"]
        self.assertNotIn(first,[x["id"] for x in remaining])
        with db() as conn:
            tombstone=conn.execute("SELECT * FROM parser_version_deletions WHERE version_id=?",(first,)).fetchone()
        self.assertIsNotNone(tombstone)

    def test_12_mineru_normalization_preserves_tables_visuals_and_coordinates(self):
        html="<table><tr><th>付款编号</th><th>金额</th></tr><tr><td>FK-01</td><td>815000</td></tr></table>"
        entries=[{"page_idx":0,"type":"title","text":"付款资料","bbox":[10,10,200,40]},
                 {"page_idx":0,"type":"table","table_body":html,"bbox":[10,50,500,220]},
                 {"page_idx":0,"type":"image","img_path":"missing.png","bbox":[20,230,300,500],"image_caption":["施工现场"]},
                 {"page_idx":0,"type":"seal","bbox":[400,500,500,600]},
                 {"page_idx":0,"type":"list","list_items":["1. 现场管理", "工程计量"],"bbox":[10,610,500,700]}]
        elements=normalize_mineru(entries,Path(TEST_DATA.name),{1:(595,842)})
        self.assertEqual(len(elements),len(entries),"无文本视觉元素也不能在规范化时丢失")
        self.assertEqual([x["element_id"] for x in elements],["P1-E0001","P1-E0002","P1-E0003","P1-E0004","P1-E0005"])
        self.assertEqual(table_grid(html)[1][0]["text"],"FK-01")
        self.assertEqual(elements[1]["cell_grid"][1][1]["text"],"815000")
        self.assertEqual(elements[3]["element_type"],"seal");self.assertEqual(elements[3]["bbox"],[400.0,500.0,500.0,600.0])
        self.assertEqual(elements[4]["text"],"1. 现场管理\n工程计量")
        self.assertEqual(elements[4]["markdown"],"1. 现场管理\n- 工程计量")
        self.assertEqual(elements[4]["metadata"]["content_handler"],"list")
        self.assertEqual(elements[4]["metadata"]["consumed_content_fields"],["list_items"])
        chunks=build_chunks(elements,max_chars=100)
        self.assertTrue(any("P1-E0002" in c["element_ids"] for c in chunks))

    def test_12b_mineru_content_dispatch_is_explicit_and_audits_future_payloads(self):
        entries=[
            {"page_idx":0,"type":"text","content":[{"text":"正文 A"},{"text":"正文 B"}]},
            {"page_idx":0,"type":"equation","latex":"E=mc^2"},
            {"page_idx":0,"type":"code","code":"SELECT 1","language":"sql"},
            {"page_idx":0,"type":"reference","references":["制度第一条"]},
            {"page_idx":0,"type":"table_caption","table_caption":["付款明细"]},
            {"page_idx":0,"type":"future_widget","captions":["未来结构内容"]},
        ]
        elements=normalize_mineru(entries,Path(TEST_DATA.name),{1:(595,842)})
        self.assertEqual(elements[0]["text"],"正文 A\n正文 B")
        self.assertEqual(elements[1]["markdown"],"$$\nE=mc^2\n$$")
        self.assertEqual(elements[2]["metadata"]["content_handler"],"code")
        self.assertEqual(elements[3]["metadata"]["content_handler"],"reference")
        self.assertEqual(elements[4]["text"],"付款明细")
        future=elements[5]
        self.assertEqual(future["metadata"]["content_handler"],"unmapped")
        self.assertEqual(future["metadata"]["content_status"],"unmapped_payload")
        self.assertEqual(future["metadata"]["raw_content_fields"],["captions"])
        self.assertIn("P1-E0006:future_widget[captions]",content_coverage_warnings(elements)[0])

    def test_13_mineru_grid_coordinates_are_converted_to_pdf_points(self):
        entries=[{"page_idx":0,"type":"table","table_body":"<table><tr><td>x</td></tr></table>",
                  "bbox":[110,172,885,382]}]
        element=normalize_mineru(entries,Path(TEST_DATA.name),{1:(595.28,841.89)},"normalized_1000")[0]
        self.assertEqual(element["bbox"],[65.481,144.805,526.823,321.602])
        self.assertEqual(element["metadata"]["raw_bbox"],[110.0,172.0,885.0,382.0])
        self.assertEqual(element["metadata"]["raw_coordinate_space"],"normalized_1000")
        self.assertEqual(element["metadata"]["coordinate_space"],"pdf_points")

    def test_14_fact_validation_rejects_percentage_amounts_and_classifies_tables_by_phase(self):
        amount,quote=label_amount("| 合同价(元) | 2,936,800.00 |\n合同价 10% 作为预付款",("合同价",))
        self.assertEqual(amount,2936800.0);self.assertIn("2,936,800.00",quote)
        peers=[{"field":"contract_amount","value":2936800,"document_id":1,"confidence":.95},
               {"field":"contract_amount","value":10,"document_id":1,"confidence":.99}]
        self.assertIsNone(fact_rejection_reason(peers[0],"施工合同",peers))
        self.assertEqual(fact_rejection_reason(peers[1],"施工合同",peers),"monetary_scale_outlier_or_percentage_context")
        self.assertEqual(best_scalar(peers,"contract_amount")["value"],2936800)

        change_html="<table><tr><th>编号</th><th>提出日期</th><th>实施日期</th><th>变更/签证内容</th><th>金额(元)</th><th>批准日期</th><th>附件情况</th></tr><tr><td>BG-01</td><td>2025-09-02</td><td>2025-08-28</td><td>管线绕行</td><td>128640</td><td>2025-09-10</td><td>后补审批</td></tr></table>"
        payment_html="<table><tr><th>付款编号</th><th>付款日期</th><th>款项性质</th><th>计量/基数</th><th>支付比例</th><th>本次支付</th><th>累计支付</th><th>发票编号</th></tr><tr><td>FK-01</td><td>2025-08-02</td><td>预付款</td><td>2936800</td><td>10%</td><td>293680</td><td>293680</td><td>FP-1</td></tr></table>"
        def doc(doc_id,phase,html):
            return {"id":doc_id,"doc_type":phase,"name":phase,"selected_parser_version_id":9,
                    "elements":[{"element_id":"P1-E0001","page":1,"element_type":"table","cell_grid_json":table_grid(html),"markdown":"table"}]}
        changes=deterministic_table_facts(doc(1,"变更签证",change_html));payments=deterministic_table_facts(doc(2,"结算付款",payment_html))
        self.assertEqual([x["field"] for x in changes],["change_record"])
        self.assertEqual(changes[0]["value"]["change_no"],"BG-01")
        self.assertEqual([x["field"] for x in payments],["payment_record"])
        self.assertEqual(payments[0]["value"]["amount"],293680.0)
        base,cap,label=payment_limit(2936800,416960,3332000,{"pre_acceptance_cap":.9,"post_settlement_cap":.97},False)
        self.assertEqual((base,cap,label),(3353760,.9,"合同价（含已批准变更）"))
        self.assertEqual(round(base*cap),3018384)
        self.assertEqual(payment_limit(2936800,416960,3332000,{"post_settlement_cap":.97},True),(3332000,.97,"审定结算价"))

    def test_15_python_owns_all_calculation_outputs(self):
        document={"id":91,"doc_type":"施工合同","name":"施工合同","selected_parser_version_id":7,
                  "text":"| 计划工期 | 2025-04-25 至 2025-07-18 |","content_json":{}}
        facts=deterministic_extract(document)
        period=next(x for x in facts if x["field"]=="construction_period")
        self.assertEqual(period["value"],{"start_date":"2025-04-25","end_date":"2025-07-18","calendar_days":85})
        self.assertEqual(period["computed_by"],"python")
        self.assertEqual(fact_rejection_reason({"field":"construction_period_days","value":84,"document_id":91,
            "origin":"llm"},"施工合同",[]),"model_computed_or_numeric_field_forbidden")
        self.assertEqual(fact_rejection_reason({"field":"change_record","value":{"change_no":"BG-1","amount":10},
            "document_id":91,"origin":"llm"},"变更签证",[]),"model_numeric_record_key_forbidden")
        payments=[{"value":{"payment_no":"F2","date":"2025-02-01","amount":30,"cumulative":999}},
                  {"value":{"payment_no":"F1","date":"2025-01-01","amount":20,"cumulative":20}}]
        total,last,calculation=payment_total(payments)
        self.assertEqual(total,50);self.assertEqual(last["value"]["payment_no"],"F2")
        self.assertEqual(calculation["formula"],"sum(payment_record.amount)")

    def test_16_local_mineru_is_vlm_only_and_multi_document_zip_isolated(self):
        config={"providers":[
            {"id":"llm","name":"LLM","kind":"openai","base_url":"http://localhost/v1","enabled":True,"models":["x"]},
            {"id":"local_ocr","name":"Local MinerU","kind":"mineru-local","base_url":"http://localhost:8000","enabled":True,"models":["vlm-engine"]},
        ],"stage_routes":{stage:{"provider_id":"llm","model":"x"} for stage in (
            "document_classification","general_extraction","payment_extraction","change_extraction",
            "contract_extraction","semantic_consistency","policy_retrieval","text2sql","result_summary",
            "visual_general","visual_table_or_chart","visual_seal_signature","visual_form_checkbox")}}
        config["stage_routes"]["ocr"]={"provider_id":"local_ocr","model":"hybrid-engine"}
        with self.assertRaisesRegex(ValueError,"hybrid-engine"):_normalize(config)
        provider=config["providers"][1]
        with self.assertRaisesRegex(RuntimeError,"hybrid-engine"):
            asyncio.run(mineru_local_submit_files([],provider,"hybrid-engine"))
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,"w") as archive:
            archive.writestr("contract/vlm/contract_content_list.json","[]")
            archive.writestr("payment/vlm/payment_content_list.json","[]")
        selected=_document_zip(raw.getvalue(),"contract.pdf")
        with zipfile.ZipFile(io.BytesIO(selected)) as archive:
            self.assertEqual(archive.namelist(),["contract/vlm/contract_content_list.json"])
        scope=canonicalize_fact_value({"field":"scope_item","value":"新建及更换DN300-DN800雨污水管道"})
        self.assertEqual(scope["value"],"新建及更换 DN300-DN800 雨污水管道")
        contract={"id":1,"doc_type":"施工合同","name":"contract.pdf","selected_parser_version_id":1,
                  "content_json":{"pages":[]},"text":"累计变更金额超过原合同价 $10\\%$ 的"}
        terms=[x for x in deterministic_extract(contract) if x["field"]=="contract_terms"]
        self.assertEqual(terms[0]["value"]["change_threshold"],.10)

    def test_17_user_date_order_is_python_and_explicit_missing_allows_one_evidence(self):
        documents={1:{"id":1,"name":"验收资料.pdf","selected_parser_version_id":2,"elements":[
            {"element_id":"P1-E1","page":1,"text":"验收日期 2026-06-24","markdown":"","html":"","bbox_json":[1,1,2,2],"metadata_json":{}},
            {"element_id":"P1-E2","page":1,"text":"消防泵控制柜到货/调试确认单日期为 2026-06-26","markdown":"","html":"","bbox_json":[1,3,2,4],"metadata_json":{}},
        ]}}
        result=evaluate_user_date_rule({"fields":"到货、调试、验收","description":"设备到货和调试必须早于验收"},documents)
        self.assertEqual(result["status"],"violated")
        self.assertEqual(result["violations"][0]["left"]["date"],"2026-06-26")
        self.assertEqual(result["violations"][0]["right"]["date"],"2026-06-24")
        self.assertTrue(explicit_single_source_proof([{"quote":"品牌替换说明仅施工单位盖章，建设单位确认页缺失"}]))
        self.assertFalse(explicit_single_source_proof([{"quote":"建设单位已书面确认"}]))

    def test_16_unsaved_openai_provider_can_be_tested_before_save(self):
        provider={"id":"draft","name":"Draft model","kind":"openai","base_url":self.base_url,
                  "api_key":"","model_id":"mock-ok","models":["mock-ok"]}
        result=asyncio.run(probe_provider_config(provider))
        self.assertTrue(result["ok"],result)
        self.assertTrue(result["model_accepted"])
        self.assertIn("mock-ok",result["server_model_ids"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
