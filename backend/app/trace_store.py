"""Lossless, redacted trace storage with large-artifact spillover."""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import TRACES_DIR, db, decode

INLINE_LIMIT = 256 * 1024
SENSITIVE_KEYS = {"authorization", "api_key", "apikey", "token", "access_token", "secret", "password"}
SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;\"']+"),
]


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def redact(value: Any, key: str = "") -> Any:
    if key.lower() in SENSITIVE_KEYS or key.lower().endswith(("_token", "_secret", "_password", "_api_key")):
        return "[REDACTED]" if value else value
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        result = value
        for pattern in SECRET_PATTERNS:
            result = pattern.sub(r"\1[REDACTED]", result)
        return result
    return value


def save_artifact(run_id: int, span_id: int | None, artifact_type: str, name: str,
                  value: Any, content_type: str = "application/json") -> int:
    safe = redact(value)
    if content_type == "application/json":
        raw = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    else:
        raw = str(safe).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    inline, file_path = None, None
    if len(raw) <= INLINE_LIMIT:
        inline = raw.decode("utf-8")
    else:
        directory = TRACES_DIR / f"run-{run_id}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest[:20]}-{artifact_type}.json.gz"
        if not path.exists():
            with gzip.open(path, "wb") as handle:
                handle.write(raw)
        file_path = str(path)
    with db() as conn:
        cur = conn.execute("""INSERT INTO trace_artifacts(run_id,span_id,artifact_type,name,content_type,inline_json,file_path,sha256,size_bytes,redacted,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
          (run_id, span_id, artifact_type, name, content_type, inline, file_path, digest, len(raw), 1, now()))
        return int(cur.lastrowid)


def load_artifact(row_or_id: Any) -> dict[str, Any]:
    if isinstance(row_or_id, int):
        with db() as conn:
            row = conn.execute("SELECT * FROM trace_artifacts WHERE id=?", (row_or_id,)).fetchone()
    else:
        row = row_or_id
    if not row:
        raise KeyError("trace artifact not found")
    item = dict(row)
    if item.get("inline_json") is not None:
        raw = item["inline_json"].encode("utf-8")
    else:
        with gzip.open(Path(item["file_path"]), "rb") as handle:
            raw = handle.read()
    text = raw.decode("utf-8", "replace")
    try:
        content = json.loads(text) if item.get("content_type") == "application/json" else text
    except json.JSONDecodeError:
        content = text
    return {**item, "content": content, "stored": "inline" if item.get("inline_json") is not None else "gzip"}


def start_span(run_id: int, stage: str, kind: str, name: str, parent_span_id: int | None = None,
               provider_id: str | None = None, model: str | None = None, metadata: Any = None) -> int:
    with db() as conn:
        sequence = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM run_spans WHERE run_id=?", (run_id,)).fetchone()[0]
        cur = conn.execute("""INSERT INTO run_spans(run_id,parent_span_id,sequence,stage,kind,name,status,started_at,provider_id,model,metadata_json)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
          (run_id, parent_span_id, sequence, stage, kind, name, "running", now(), provider_id, model,
           json.dumps(redact(metadata or {}), ensure_ascii=False)))
        return int(cur.lastrowid)


def finish_span(span_id: int, status: str = "completed", output: Any = None, error: Any = None,
                metadata: Any = None, output_name: str = "output") -> None:
    with db() as conn:
        span = conn.execute("SELECT * FROM run_spans WHERE id=?", (span_id,)).fetchone()
        if not span:
            return
        run_id = int(span["run_id"])
        started = datetime.strptime(span["started_at"], "%Y-%m-%d %H:%M:%S.%f")
    output_id = save_artifact(run_id, span_id, "output", output_name, output) if output is not None else None
    error_id = save_artifact(run_id, span_id, "error", "error", error) if error is not None else None
    finished = datetime.now()
    with db() as conn:
        conn.execute("""UPDATE run_spans SET status=?,finished_at=?,duration_ms=?,output_artifact_id=COALESCE(?,output_artifact_id),
          error_artifact_id=COALESCE(?,error_artifact_id),metadata_json=? WHERE id=?""",
          (status, finished.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], round((finished-started).total_seconds()*1000),
           output_id, error_id, json.dumps(redact(metadata or json.loads(span["metadata_json"])), ensure_ascii=False), span_id))


def attach_input(span_id: int, value: Any, name: str = "input") -> int:
    with db() as conn:
        span = conn.execute("SELECT run_id FROM run_spans WHERE id=?", (span_id,)).fetchone()
    artifact_id = save_artifact(int(span["run_id"]), span_id, "input", name, value)
    with db() as conn:
        conn.execute("UPDATE run_spans SET input_artifact_id=? WHERE id=?", (artifact_id, span_id))
    return artifact_id


def skipped_span(run_id: int, stage: str, reason: str) -> int:
    span_id = start_span(run_id, stage, "stage", stage)
    finish_span(span_id, "skipped", metadata={"reason": reason})
    return span_id


def run_trace(run_id: int, include_content: bool = True) -> dict[str, Any]:
    with db() as conn:
        run = conn.execute("SELECT r.*,p.name AS project_name FROM audit_runs r JOIN projects p ON p.id=r.project_id WHERE r.id=?", (run_id,)).fetchone()
        if not run:
            raise KeyError("run not found")
        spans = [decode(row, "metadata_json") for row in conn.execute("SELECT * FROM run_spans WHERE run_id=? ORDER BY sequence", (run_id,))]
        artifacts = [dict(row) for row in conn.execute("SELECT * FROM trace_artifacts WHERE run_id=? ORDER BY id", (run_id,))]
        events = [decode(row, "detail_json") for row in conn.execute("SELECT * FROM audit_events WHERE run_id=? ORDER BY sequence", (run_id,))]
    if include_content:
        artifacts = [load_artifact(row) for row in artifacts]
    else:
        artifacts = [{k: v for k, v in row.items() if k not in {"inline_json", "file_path"}} | {"stored": "inline" if row.get("inline_json") is not None else "gzip"} for row in artifacts]
    run_item = dict(run)
    for field in ("result_json", "config_snapshot_json", "prompt_versions_json", "route_overrides_json",
                  "action_snapshot_json", "decision_json"):
        try:
            run_item[field.removesuffix("_json")] = json.loads(run_item.pop(field) or "{}")
        except Exception:
            pass
    return {"schema_version": 1, "run": run_item, "spans": spans, "artifacts": artifacts, "events": events}
