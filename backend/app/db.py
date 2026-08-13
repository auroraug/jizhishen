import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_LOCAL_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data" if os.name != "nt" else str(_LOCAL_DATA_DIR)))
DB_PATH = DATA_DIR / "audit.db"
FILES_DIR = DATA_DIR / "documents"
TRACES_DIR = DATA_DIR / "traces"
PARSER_ARTIFACTS_DIR = DATA_DIR / "parser-artifacts"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, community TEXT NOT NULL,
  budget INTEGER NOT NULL, contract_amount INTEGER NOT NULL, status TEXT NOT NULL,
  progress INTEGER NOT NULL, risk_count INTEGER NOT NULL DEFAULT 0,
  high_risk_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, facts_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL, doc_type TEXT NOT NULL, source_system TEXT NOT NULL,
  pages INTEGER NOT NULL, status TEXT NOT NULL, file_path TEXT, content_json TEXT NOT NULL,
  sha256 TEXT NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS document_parser_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL REFERENCES documents(id),
  document_version_no INTEGER,
  parser_kind TEXT NOT NULL, parser_name TEXT NOT NULL, parser_version TEXT NOT NULL,
  provider_id TEXT, model TEXT, status TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 0,
  source_sha256 TEXT NOT NULL, params_json TEXT NOT NULL DEFAULT '{}', artifact_dir TEXT,
  manifest_json TEXT NOT NULL DEFAULT '{}', content_json TEXT NOT NULL DEFAULT '{}',
  stats_json TEXT NOT NULL DEFAULT '{}', warnings_json TEXT NOT NULL DEFAULT '[]',
  parent_version_id INTEGER REFERENCES document_parser_versions(id), trace_run_id INTEGER REFERENCES audit_runs(id), created_at TEXT NOT NULL,
  completed_at TEXT, UNIQUE(document_id,parser_kind,parser_version,model,source_sha256)
);
CREATE TABLE IF NOT EXISTS document_parse_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, parser_version_id INTEGER NOT NULL REFERENCES document_parser_versions(id),
  attempt INTEGER NOT NULL DEFAULT 1, provider_id TEXT, batch_id TEXT, data_id TEXT,
  status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, request_json TEXT NOT NULL DEFAULT '{}',
  response_json TEXT NOT NULL DEFAULT '{}', error_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS document_elements (
  id INTEGER PRIMARY KEY AUTOINCREMENT, parser_version_id INTEGER NOT NULL REFERENCES document_parser_versions(id),
  element_id TEXT NOT NULL, page INTEGER NOT NULL, element_type TEXT NOT NULL, parent_element_id TEXT,
  reading_order INTEGER NOT NULL, bbox_json TEXT, text TEXT, html TEXT, markdown TEXT, asset_path TEXT,
  cell_grid_json TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', filtered_reason TEXT,
  UNIQUE(parser_version_id,element_id)
);
CREATE TABLE IF NOT EXISTS document_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, parser_version_id INTEGER NOT NULL REFERENCES document_parser_versions(id),
  chunk_id TEXT NOT NULL, title_path_json TEXT NOT NULL DEFAULT '[]', element_ids_json TEXT NOT NULL,
  page_from INTEGER, page_to INTEGER, content TEXT NOT NULL, token_estimate INTEGER NOT NULL DEFAULT 0,
  UNIQUE(parser_version_id,chunk_id)
);
CREATE TABLE IF NOT EXISTS visual_element_analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, parser_version_id INTEGER NOT NULL REFERENCES document_parser_versions(id),
  element_id TEXT NOT NULL, stage TEXT NOT NULL, provider_id TEXT, model TEXT, prompt_version_id INTEGER,
  status TEXT NOT NULL, input_json TEXT NOT NULL DEFAULT '{}', output_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS parser_version_deletions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, version_id INTEGER NOT NULL, document_id INTEGER NOT NULL,
  project_id TEXT NOT NULL, snapshot_json TEXT NOT NULL, artifact_archive TEXT, deleted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS anomalies (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id),
  code TEXT NOT NULL, title TEXT NOT NULL, severity TEXT NOT NULL, summary TEXT NOT NULL,
  amount INTEGER, confidence REAL NOT NULL, status TEXT NOT NULL, evidence_json TEXT NOT NULL,
  rule_kind TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timeline (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id),
  event_date TEXT NOT NULL, title TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id),
  project_run_no INTEGER,
  started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, rule_count INTEGER NOT NULL,
  fact_count INTEGER NOT NULL, anomaly_count INTEGER NOT NULL DEFAULT 0, provider TEXT NOT NULL,
  progress INTEGER NOT NULL DEFAULT 0, current_stage TEXT, error TEXT, result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS audit_run_documents (
  run_id INTEGER NOT NULL REFERENCES audit_runs(id), document_id INTEGER NOT NULL REFERENCES documents(id),
  parser_version_id INTEGER NOT NULL REFERENCES document_parser_versions(id), is_override INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(run_id,document_id)
);
CREATE TABLE IF NOT EXISTS extracted_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES audit_runs(id),
  project_id TEXT NOT NULL REFERENCES projects(id), document_id INTEGER REFERENCES documents(id),
  field_name TEXT NOT NULL, value_json TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0, document_phase TEXT,
  page INTEGER, line_start INTEGER, line_end INTEGER, block_id INTEGER, bbox_json TEXT, quote TEXT,
  provider TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES audit_runs(id),
  stage TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, started_at TEXT NOT NULL,
  duration_ms INTEGER NOT NULL, success INTEGER NOT NULL, input_tokens INTEGER,
  output_tokens INTEGER, request_hash TEXT NOT NULL, response_preview TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES audit_runs(id),
  sequence INTEGER NOT NULL, created_at TEXT NOT NULL, stage TEXT NOT NULL,
  status TEXT NOT NULL, message TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS policy_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, clause TEXT NOT NULL,
  text TEXT NOT NULL, source TEXT NOT NULL, effective_date TEXT, is_template INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rule_definitions (
  code TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
  fields TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL DEFAULT 'medium', enabled INTEGER NOT NULL DEFAULT 1,
  system_managed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prompt_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL, version INTEGER NOT NULL,
  status TEXT NOT NULL, is_current INTEGER NOT NULL DEFAULT 0,
  system_prompt TEXT NOT NULL, user_prompt TEXT NOT NULL, json_schema TEXT NOT NULL DEFAULT '{}',
  parser_version TEXT NOT NULL DEFAULT 'json-v1', based_on_id INTEGER REFERENCES prompt_versions(id),
  created_at TEXT NOT NULL, published_at TEXT, UNIQUE(stage,version)
);
CREATE TABLE IF NOT EXISTS trace_artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES audit_runs(id),
  span_id INTEGER, artifact_type TEXT NOT NULL, name TEXT NOT NULL,
  content_type TEXT NOT NULL DEFAULT 'application/json', inline_json TEXT, file_path TEXT,
  sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, redacted INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_spans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES audit_runs(id),
  parent_span_id INTEGER REFERENCES run_spans(id), sequence INTEGER NOT NULL,
  stage TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, duration_ms INTEGER,
  provider_id TEXT, model TEXT, input_artifact_id INTEGER REFERENCES trace_artifacts(id),
  output_artifact_id INTEGER REFERENCES trace_artifacts(id), error_artifact_id INTEGER REFERENCES trace_artifacts(id),
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_documents_project_type ON documents(project_id, doc_type);
CREATE INDEX IF NOT EXISTS idx_anomalies_project_severity ON anomalies(project_id, severity);
CREATE INDEX IF NOT EXISTS idx_timeline_project_date ON timeline(project_id, event_date);
CREATE INDEX IF NOT EXISTS idx_audit_runs_project_started ON audit_runs(project_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_facts_run ON extracted_facts(run_id);
CREATE INDEX IF NOT EXISTS idx_calls_run ON ai_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON audit_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_spans_run_seq ON run_spans(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON trace_artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_prompts_stage ON prompt_versions(stage,version DESC);
CREATE INDEX IF NOT EXISTS idx_parser_versions_document ON document_parser_versions(document_id,id DESC);
CREATE INDEX IF NOT EXISTS idx_parse_jobs_status ON document_parse_jobs(status,updated_at);
CREATE INDEX IF NOT EXISTS idx_elements_version_page ON document_elements(parser_version_id,page,reading_order);
CREATE INDEX IF NOT EXISTS idx_chunks_version ON document_chunks(parser_version_id,id);
CREATE INDEX IF NOT EXISTS idx_run_documents_run ON audit_run_documents(run_id);
CREATE INDEX IF NOT EXISTS idx_parser_version_deletions_document ON parser_version_deletions(document_id,deleted_at DESC);
"""

def prepare_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    PARSER_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

def connect():
    prepare_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        # Migrate databases created by the interactive-demo release.
        columns={r[1] for r in conn.execute("PRAGMA table_info(audit_runs)")}
        for name, ddl in {
            "progress":"INTEGER NOT NULL DEFAULT 0", "current_stage":"TEXT",
            "error":"TEXT", "result_json":"TEXT NOT NULL DEFAULT '{}'"
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE audit_runs ADD COLUMN {name} {ddl}")
        anomaly_columns={r[1] for r in conn.execute("PRAGMA table_info(anomalies)")}
        if "run_id" not in anomaly_columns:
            conn.execute("ALTER TABLE anomalies ADD COLUMN run_id INTEGER REFERENCES audit_runs(id)")
        fact_columns={r[1] for r in conn.execute("PRAGMA table_info(extracted_facts)")}
        if "block_id" not in fact_columns: conn.execute("ALTER TABLE extracted_facts ADD COLUMN block_id INTEGER")
        if "bbox_json" not in fact_columns: conn.execute("ALTER TABLE extracted_facts ADD COLUMN bbox_json TEXT")
        if "document_phase" not in fact_columns: conn.execute("ALTER TABLE extracted_facts ADD COLUMN document_phase TEXT")
        if "parser_version_id" not in fact_columns: conn.execute("ALTER TABLE extracted_facts ADD COLUMN parser_version_id INTEGER REFERENCES document_parser_versions(id)")
        if "element_ids_json" not in fact_columns: conn.execute("ALTER TABLE extracted_facts ADD COLUMN element_ids_json TEXT NOT NULL DEFAULT '[]'")
        document_columns={r[1] for r in conn.execute("PRAGMA table_info(documents)")}
        if "active_parser_version_id" not in document_columns:
            conn.execute("ALTER TABLE documents ADD COLUMN active_parser_version_id INTEGER REFERENCES document_parser_versions(id)")
        if "deleted_at" not in document_columns: conn.execute("ALTER TABLE documents ADD COLUMN deleted_at TEXT")
        project_columns={r[1] for r in conn.execute("PRAGMA table_info(projects)")}
        if "is_demo" not in project_columns:
            conn.execute("ALTER TABLE projects ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0")
        policy_columns={r[1] for r in conn.execute("PRAGMA table_info(policy_chunks)")}
        if "enabled" not in policy_columns:
            conn.execute("ALTER TABLE policy_chunks ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        run_columns={r[1] for r in conn.execute("PRAGMA table_info(audit_runs)")}
        for name, ddl in {
            "parent_run_id":"INTEGER REFERENCES audit_runs(id)",
            "derived_from_stage":"TEXT",
            "run_kind":"TEXT NOT NULL DEFAULT 'audit'",
            "config_snapshot_json":"TEXT NOT NULL DEFAULT '{}'",
            "prompt_versions_json":"TEXT NOT NULL DEFAULT '{}'",
            "route_overrides_json":"TEXT NOT NULL DEFAULT '{}'"
        }.items():
            if name not in run_columns:
                conn.execute(f"ALTER TABLE audit_runs ADD COLUMN {name} {ddl}")
        if "project_run_no" not in run_columns:
            conn.execute("ALTER TABLE audit_runs ADD COLUMN project_run_no INTEGER")
        parser_columns={r[1] for r in conn.execute("PRAGMA table_info(document_parser_versions)")}
        if "document_version_no" not in parser_columns:
            conn.execute("ALTER TABLE document_parser_versions ADD COLUMN document_version_no INTEGER")
        # Human-facing ordinals are immutable within their business scope. The
        # global id remains the only technical reference; deleting history leaves
        # a visible gap instead of silently renumbering later records.
        conn.execute("""UPDATE audit_runs SET project_run_no=(SELECT COUNT(*) FROM audit_runs prior
          WHERE prior.project_id=audit_runs.project_id AND prior.id<=audit_runs.id) WHERE project_run_no IS NULL""")
        conn.execute("""UPDATE document_parser_versions SET document_version_no=(SELECT COUNT(*) FROM document_parser_versions prior
          WHERE prior.document_id=document_parser_versions.document_id AND prior.id<=document_parser_versions.id) WHERE document_version_no IS NULL""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_runs_project_no ON audit_runs(project_id,project_run_no)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_parser_versions_document_no ON document_parser_versions(document_id,document_version_no)")
        conn.executescript("""
          CREATE TRIGGER IF NOT EXISTS trg_audit_run_project_no AFTER INSERT ON audit_runs
          WHEN NEW.project_run_no IS NULL BEGIN
            UPDATE audit_runs SET project_run_no=(SELECT COALESCE(MAX(project_run_no),0)+1 FROM audit_runs
              WHERE project_id=NEW.project_id AND id<>NEW.id) WHERE id=NEW.id;
          END;
          CREATE TRIGGER IF NOT EXISTS trg_parser_version_document_no AFTER INSERT ON document_parser_versions
          WHEN NEW.document_version_no IS NULL BEGIN
            UPDATE document_parser_versions SET document_version_no=(SELECT COALESCE(MAX(document_version_no),0)+1
              FROM document_parser_versions WHERE document_id=NEW.document_id AND id<>NEW.id) WHERE id=NEW.id;
          END;
        """)
        # One-time removal of seeded findings: after this marker every finding must
        # be produced by a recorded audit run.
        if not conn.execute("SELECT 1 FROM app_meta WHERE key='real_ai_mvp_v1'").fetchone():
            conn.execute("DELETE FROM anomalies")
            conn.execute("UPDATE projects SET risk_count=0, high_risk_count=0")
            conn.execute("INSERT INTO app_meta(key,value) VALUES('real_ai_mvp_v1','enabled')")
        if not conn.execute("SELECT 1 FROM policy_chunks").fetchone():
            policies=[
              ("MVP审查规则模板","付款控制","未达到竣工验收节点时，累计进度款原则上不得超过合同价乘以合同约定的进度付款比例。","系统内置审查模板（非正式制度依据）",None,1),
              ("MVP审查规则模板","工程变更","累计工程变更达到合同价10%时，应重点核验变更审批、民主程序及重新招标要求。","系统内置审查模板（非正式制度依据）",None,1),
              ("MVP审查规则模板","合同一致性","合同金额、工期、工程范围及主要付款条件应与招标文件和中标结果保持实质一致。","系统内置审查模板（非正式制度依据）",None,1),
              ("MVP审查规则模板","资料完整性","全过程审查至少需要立项、招标、中标、合同、工程量清单、变更、付款及验收资料。","系统内置审查模板（非正式制度依据）",None,1),
            ]
            conn.executemany("INSERT INTO policy_chunks(title,clause,text,source,effective_date,is_template) VALUES(?,?,?,?,?,?)",policies)
        if not conn.execute("SELECT 1 FROM app_meta WHERE key='rule_catalog_v1'").fetchone():
            created=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rules=[
              ("DOC-001","全过程八阶段资料完整性","确定性规则","标准阶段、文档类型","检查八个标准业务阶段是否都有可识别资料","high"),
              ("AMT-001","中标价与合同价一致性","确定性规则","中标金额、合同金额","比较中标金额与合同金额","high"),
              ("TERM-001","招标与合同工期一致性","确定性规则","招标工期、合同工期","比较不同资料中的施工工期","high"),
              ("PAY-001","累计付款与节点上限","确定性规则","付款累计值、验收/结算节点、合同上限","计算当前节点允许付款额与累计付款差额","high"),
              ("CHG-001","累计变更比例阈值","确定性规则","变更编号、变更金额、合同价","累计变更达到合同约定阈值时提示重点复核","high"),
              ("SEQ-001","变更审批与实施先后顺序","确定性规则","提出/批准/实施日期","检查是否存在先实施后审批","high"),
              ("ATT-001","变更附件原件归档完整性","确定性规则","附件说明、独立归档原件","提示核验汇总资料提及但未独立归档的附件","low"),
              ("SEM-001","范围实质变化与重复计价","LLM语义规则","招标范围、合同范围、BOQ、变更原因","由模型提出候选，经置信度和双证据门槛后保存","medium"),
            ]
            conn.executemany("""INSERT OR IGNORE INTO rule_definitions
              (code,name,kind,fields,description,severity,enabled,system_managed,created_at,updated_at)
              VALUES(?,?,?,?,?,?,1,1,?,?)""",[(*r,created,created) for r in rules])
            conn.execute("INSERT INTO app_meta(key,value) VALUES('rule_catalog_v1','seeded')")
        # Date ordering is deterministic business logic. Migrate legacy user
        # semantic rules that explicitly describe a date relationship so future
        # runs compare source dates in Python instead of asking an LLM to count.
        if not conn.execute("SELECT 1 FROM app_meta WHERE key='user_date_rule_kind_v1'").fetchone():
            conn.execute("""UPDATE rule_definitions SET kind='确定性规则',updated_at=?
              WHERE system_managed=0 AND kind='LLM语义规则'
              AND (description LIKE '%早于%' OR description LIKE '%晚于%' OR description LIKE '%先于%' OR description LIKE '%不晚于%')""",
              (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
            conn.execute("INSERT INTO app_meta(key,value) VALUES('user_date_rule_kind_v1','migrated')")
        # Mark the legacy GC2026 showcase records without changing user-created projects.
        conn.execute("UPDATE projects SET is_demo=1 WHERE id LIKE 'GC2026-%' AND is_demo=0")
        # Remove every legacy/static risk counter. Counts always derive from the
        # latest completed run and its current pending statuses.
        conn.execute("""UPDATE projects SET risk_count=COALESCE((SELECT COUNT(*) FROM anomalies a
          WHERE a.project_id=projects.id AND a.status='待复核' AND a.run_id=(SELECT r.id FROM audit_runs r
          WHERE r.project_id=projects.id AND r.status='completed' AND r.run_kind='audit' ORDER BY r.id DESC LIMIT 1)),0)""")
        conn.execute("""UPDATE projects SET high_risk_count=COALESCE((SELECT COUNT(*) FROM anomalies a
          WHERE a.project_id=projects.id AND a.status='待复核' AND a.severity='high' AND a.run_id=(SELECT r.id FROM audit_runs r
          WHERE r.project_id=projects.id AND r.status='completed' AND r.run_kind='audit' ORDER BY r.id DESC LIMIT 1)),0)""")
        conn.execute("""UPDATE projects SET facts_json='{}' WHERE is_demo=1 AND NOT EXISTS(
          SELECT 1 FROM audit_runs r WHERE r.project_id=projects.id AND r.status='completed' AND r.run_kind='audit')""")
        conn.execute("""DELETE FROM timeline WHERE project_id IN (SELECT p.id FROM projects p WHERE p.is_demo=1)
          AND NOT EXISTS(SELECT 1 FROM audit_runs r WHERE r.project_id=timeline.project_id AND r.status='completed' AND r.run_kind='audit')""")
        conn.execute("PRAGMA optimize")

def rows(rows_):
    return [dict(r) for r in rows_]

def decode(row, *fields):
    item = dict(row)
    for field in fields:
        item[field] = json.loads(item[field])
    return item
