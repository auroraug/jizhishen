"""Versioned document parsing and lossless MinerU normalization."""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import io
import json
import mimetypes
import re
import shutil
import zipfile
from collections import Counter
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any

import fitz
import httpx

from .db import PARSER_ARTIFACTS_DIR, db, decode
from .document_parser import parse_document
from .model_config import resolve_route, snapshot
from .prompt_store import prompt_snapshot, published_prompt, render
from .providers import chat_with_trace, mineru_batch_result, mineru_local_result
from .trace_store import attach_input, finish_span, start_span


NORMALIZER_VERSION = "unified-elements-v3-explicit-content-dispatch"
PYMUPDF_VERSION = "pymupdf-layout-v1"
VISUAL_TYPES = {"image", "chart", "seal", "signature", "handwriting", "checkbox", "form"}


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_model_call(run_id: int, stage: str, trace: dict[str, Any] | None = None, error: Exception | None = None):
    trace=trace or {}
    with db() as conn:
        conn.execute("""INSERT INTO ai_calls(run_id,stage,provider,model,started_at,duration_ms,success,input_tokens,output_tokens,request_hash,response_preview,error)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(run_id,stage,trace.get("provider_id") or trace.get("provider") or "unknown",trace.get("model") or "unknown",
          trace.get("started_at") or now(),trace.get("duration_ms") or 0,0 if error else 1,trace.get("input_tokens"),trace.get("output_tokens"),
          trace.get("request_hash") or "",(trace.get("content") or "")[:1200],str(error)[:1000] if error else None))


def artifact_root(document_id: int, version_id: int) -> Path:
    path = PARSER_ARTIFACTS_DIR / str(document_id) / str(version_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows=[]; self.row=None; self.cell=None; self.attrs={}
    def handle_starttag(self, tag, attrs):
        if tag == "tr": self.row=[]
        if tag in {"td", "th"}:
            self.cell=[]; self.attrs=dict(attrs)
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.row is not None and self.cell is not None:
            self.row.append({"text":" ".join("".join(self.cell).split()), "rowspan":int(self.attrs.get("rowspan",1) or 1),
                             "colspan":int(self.attrs.get("colspan",1) or 1), "header":tag=="th"})
            self.cell=None; self.attrs={}
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row); self.row=None


def table_grid(html: str | None) -> list[list[dict[str, Any]]]:
    if not html: return []
    parser=_TableParser()
    try: parser.feed(html)
    except Exception: return []
    raw=parser.rows;grid:list[list[dict[str,Any]|None]]=[]
    for row_index,row in enumerate(raw):
        while len(grid)<=row_index:grid.append([])
        col=0
        for source in row:
            while col<len(grid[row_index]) and grid[row_index][col] is not None:col+=1
            rowspan=max(1,int(source.get("rowspan",1)));colspan=max(1,int(source.get("colspan",1)))
            for rr in range(row_index,row_index+rowspan):
                while len(grid)<=rr:grid.append([])
                while len(grid[rr])<col+colspan:grid[rr].append(None)
                for cc in range(col,col+colspan):
                    grid[rr][cc]={**source,"row":rr,"col":cc,"origin_row":row_index,"origin_col":col,"is_span_copy":rr!=row_index or cc!=col}
            col+=colspan
    width=max((len(row) for row in grid),default=0)
    return [[cell or {"text":"","rowspan":1,"colspan":1,"header":False,"row":r,"col":c,"origin_row":r,"origin_col":c,"is_span_copy":False}
             for c,cell in enumerate(row+[None]*(width-len(row)))] for r,row in enumerate(grid)]


def markdown_table_grid(markdown: str | None) -> list[list[dict[str, Any]]]:
    lines=[line.strip() for line in (markdown or "").splitlines() if line.strip().startswith("|") and line.strip().endswith("|")]
    rows=[]
    for line in lines:
        values=[cell.strip() for cell in line.strip("|").split("|")]
        if values and all(re.fullmatch(r":?-{3,}:?",cell.replace(" ","")) for cell in values):continue
        row_index=len(rows);rows.append([{"text":cell,"rowspan":1,"colspan":1,"header":not rows,"row":row_index,"col":col,
          "origin_row":row_index,"origin_col":col,"is_span_copy":False} for col,cell in enumerate(values)])
    return rows


def html_to_markdown(html: str | None) -> str:
    rows=table_grid(html)
    if not rows: return re.sub(r"<[^>]+>", " ", html or "").strip()
    width=max((sum(c.get("colspan",1) for c in row) for row in rows),default=0)
    expanded=[]
    for row in rows:
        values=[]
        for cell in row: values.extend([cell["text"]]+[""]*(cell.get("colspan",1)-1))
        expanded.append(values+[""]*(width-len(values)))
    lines=["| " + " | ".join(row) + " |" for row in expanded]
    if lines: lines.insert(1,"| " + " | ".join(["---"]*width) + " |")
    return "\n".join(lines)


def _bbox(value: Any) -> list[float] | None:
    if isinstance(value, dict): value=[value.get(k) for k in ("x0","y0","x1","y1")]
    if not isinstance(value, (list,tuple)) or len(value)<4: return None
    try: return [round(float(x),3) for x in value[:4]]
    except Exception: return None


def _entry_type(entry: dict[str, Any]) -> str:
    raw=str(entry.get("type") or entry.get("category_type") or entry.get("block_type") or "unknown").lower()
    mapping={"text":"paragraph","paragraph":"paragraph","interline_equation":"equation","display_formula":"equation",
             "inline_equation":"equation","equation":"equation","formula":"equation","table":"table",
             "table_caption":"table_caption","table_footnote":"table_footnote","image":"image","image_caption":"image_caption",
             "image_footnote":"image_footnote","list":"list","title":"title","header":"header","footer":"footer",
             "page_number":"page_number","code":"code","code_block":"code","reference":"reference","references":"reference",
             "footnote":"footnote","caption":"caption","chart":"chart","seal":"seal","stamp":"seal","signature":"signature",
             "handwriting":"handwriting","checkbox":"checkbox","form":"form"}
    if raw in mapping:return mapping[raw]
    for key in ("seal","stamp","signature","handwriting","checkbox","chart","table","image","formula","equation"):
        if key in raw: return "equation" if key=="formula" else key
    return raw or "unknown"


CONTENT_PAYLOAD_FIELDS={"text","content","markdown","html","table_body","list_items","caption","captions","image_caption",
    "table_caption","footnote","footnotes","table_footnote","image_footnote","references","reference","items","equation",
    "formula","latex","code"}


def _has_payload(value: Any) -> bool:
    if value is None:return False
    if isinstance(value,str):return bool(value.strip())
    if isinstance(value,(list,tuple,dict)):return bool(value)
    return True


def _lines(value: Any, item_fields: tuple[str,...]=( "text", "content")) -> list[str]:
    """Flatten one explicitly declared content field without inspecting arbitrary raw JSON."""
    if not _has_payload(value):return []
    values=value if isinstance(value,(list,tuple)) else [value];result=[]
    for item in values:
        if isinstance(item,str):text=item.strip()
        elif isinstance(item,dict):
            text=""
            for field in item_fields:
                if _has_payload(item.get(field)):
                    text=str(item[field]).strip();break
        else:text=str(item).strip()
        if text:result.append(text)
    return result


def _text_block(entry: dict[str,Any], handler: str) -> dict[str,Any]:
    consumed=[];text=""
    for field in ("text","content","markdown"):
        if _has_payload(entry.get(field)):
            text="\n".join(_lines(entry[field]));consumed.append(field);break
    return {"text":text,"html":"","markdown":text,"consumed_fields":consumed,"handler":handler,"allow_empty":False}


def _list_block(entry: dict[str,Any]) -> dict[str,Any]:
    consumed=[];items=[]
    if _has_payload(entry.get("list_items")):
        items=_lines(entry["list_items"],("text","content","value","label"));consumed.append("list_items")
    markdown=[]
    for item in items:
        # Preserve MinerU's explicit numbering; add a bullet only when no marker exists.
        markdown.append(item if re.match(r"^\s*(?:\d+[.\u3001)]|[-*+]\s+|[\uff08(]?[\u4e00-\u9fff\u2460-\u2473]+[\uff09)\u3001.])",item) else f"- {item}")
    return {"text":"\n".join(items),"html":"","markdown":"\n".join(markdown),"consumed_fields":consumed,
            "handler":"list","allow_empty":False,"item_count":len(items)}


def _table_block(entry: dict[str,Any]) -> dict[str,Any]:
    consumed=[];body=""
    for field in ("table_body","html","markdown"):
        if _has_payload(entry.get(field)):
            body=str(entry[field]).strip();consumed.append(field);break
    captions=[];footnotes=[]
    for field in ("table_caption","caption","captions"):
        if _has_payload(entry.get(field)):captions.extend(_lines(entry[field]));consumed.append(field)
    for field in ("table_footnote","footnote","footnotes"):
        if _has_payload(entry.get(field)):footnotes.extend(_lines(entry[field]));consumed.append(field)
    html=body if "<table" in body.lower() else "";table_md=html_to_markdown(html) if html else body
    sections=[*captions,table_md,*[f"> {line}" for line in footnotes]]
    return {"text":"\n".join([*captions,*footnotes]),"html":html,"markdown":"\n\n".join(x for x in sections if x),
            "consumed_fields":consumed,"handler":"table","allow_empty":False}


def _visual_block(entry: dict[str,Any], handler: str) -> dict[str,Any]:
    consumed=[];captions=[]
    for field in ("image_caption","caption","captions","text"):
        if _has_payload(entry.get(field)):captions.extend(_lines(entry[field]));consumed.append(field)
    text="\n".join(dict.fromkeys(captions))
    return {"text":text,"html":"","markdown":text,"consumed_fields":consumed,"handler":handler,"allow_empty":True}


def _equation_block(entry: dict[str,Any]) -> dict[str,Any]:
    consumed=[];formula=""
    for field in ("latex","equation","formula","text","content"):
        if _has_payload(entry.get(field)):formula=str(entry[field]).strip();consumed.append(field);break
    markdown=formula if formula.startswith(("$","\\[","\\(")) else (f"$$\n{formula}\n$$" if formula else "")
    return {"text":formula,"html":"","markdown":markdown,"consumed_fields":consumed,"handler":"equation","allow_empty":False}


def _code_block(entry: dict[str,Any]) -> dict[str,Any]:
    consumed=[];code=""
    for field in ("code","text","content"):
        if _has_payload(entry.get(field)):code=str(entry[field]).rstrip();consumed.append(field);break
    language=str(entry.get("language") or entry.get("lang") or "").strip()
    return {"text":code,"html":"","markdown":f"```{language}\n{code}\n```" if code else "","consumed_fields":consumed,
            "handler":"code","allow_empty":False}


def _annotation_block(entry: dict[str,Any], handler: str) -> dict[str,Any]:
    fields={"reference":("references","reference","items","text","content"),
            "caption":("captions","caption","text","content"),"footnote":("footnotes","footnote","text","content"),
            "table_caption":("table_caption","caption","text"),"table_footnote":("table_footnote","footnote","text"),
            "image_caption":("image_caption","caption","text"),"image_footnote":("image_footnote","footnote","text")}[handler]
    consumed=[];lines=[]
    for field in fields:
        if _has_payload(entry.get(field)):lines.extend(_lines(entry[field]));consumed.append(field)
    lines=list(dict.fromkeys(lines));prefix="> " if "footnote" in handler or handler=="reference" else ""
    return {"text":"\n".join(lines),"html":"","markdown":"\n".join(prefix+x for x in lines),"consumed_fields":consumed,
            "handler":handler,"allow_empty":False}


def _entry_content(entry: dict[str, Any], kind: str) -> tuple[str,str,str,dict[str,Any]]:
    """Explicit MinerU type dispatch with auditable field consumption."""
    if kind in {"paragraph","title","header","footer","page_number"}:result=_text_block(entry,kind)
    elif kind=="list":result=_list_block(entry)
    elif kind=="table":result=_table_block(entry)
    elif kind in {"image","chart","seal","signature","handwriting","checkbox","form"}:result=_visual_block(entry,kind)
    elif kind=="equation":result=_equation_block(entry)
    elif kind=="code":result=_code_block(entry)
    elif kind in {"reference","caption","footnote","table_caption","table_footnote","image_caption","image_footnote"}:
        result=_annotation_block(entry,kind)
    else:result={"text":"","html":"","markdown":"","consumed_fields":[],"handler":"unmapped","allow_empty":False}
    payload_fields=sorted(field for field in CONTENT_PAYLOAD_FIELDS if _has_payload(entry.get(field)))
    consumed=sorted(set(result.pop("consumed_fields",[])));unconsumed=sorted(set(payload_fields)-set(consumed))
    text,html,markdown=str(result.pop("text","")),str(result.pop("html","")),str(result.pop("markdown",""))
    allow_empty=bool(result.pop("allow_empty",False));has_canonical=bool(text or html or markdown)
    status="mapped" if has_canonical else "non_text_visual" if allow_empty and not payload_fields else "empty"
    if payload_fields and not has_canonical:status="unmapped_payload"
    audit={"content_handler":result.pop("handler"),"content_status":status,"consumed_content_fields":consumed,
           "unconsumed_content_fields":unconsumed,"raw_content_fields":payload_fields,**result}
    return text,html,markdown,audit


def normalize_pymupdf(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    elements=[]
    for page in pages:
        for order,block in enumerate(page.get("blocks") or [],1):
            elements.append({"element_id":f"P{page.get('page',1)}-E{order:04}","page":int(page.get("page",1)),
                "element_type":block.get("type") or "paragraph","parent_element_id":None,"reading_order":order,
                "bbox":block.get("bbox"),"text":block.get("text") or "","html":"","markdown":block.get("text") or "",
                "asset_path":None,"cell_grid":[],"metadata":{"source_block_id":block.get("id"),"page_width":page.get("width"),"page_height":page.get("height")}})
    return elements


def _mineru_bbox(value: Any, page_size: tuple[float,float] | None, coordinate_space: str) -> tuple[list[float] | None,dict[str,Any]]:
    raw=_bbox(value)
    if not raw:return None,{"raw_bbox":None,"raw_coordinate_space":coordinate_space,"coordinate_space":"pdf_points"}
    width,height=page_size or (None,None);mode=coordinate_space
    if mode=="auto":
        maximum=max(abs(x) for x in raw)
        if maximum<=1.5:mode="normalized_1"
        elif width and height and maximum<=1200 and (raw[2]>width*1.15 or raw[3]>height*1.15):mode="normalized_1000"
        else:mode="pdf_points"
    if width and height and mode=="normalized_1":bbox=[raw[0]*width,raw[1]*height,raw[2]*width,raw[3]*height]
    elif width and height and mode=="normalized_1000":bbox=[raw[0]*width/1000,raw[1]*height/1000,raw[2]*width/1000,raw[3]*height/1000]
    else:bbox=raw
    return [round(x,3) for x in bbox],{"raw_bbox":raw,"raw_coordinate_space":mode,"coordinate_space":"pdf_points"}


def normalize_mineru(entries: list[dict[str, Any]], root: Path, page_sizes: dict[int,tuple[float,float]], coordinate_space: str="auto") -> list[dict[str, Any]]:
    elements=[]; counters=Counter()
    for index,entry in enumerate(entries,1):
        page=int(entry.get("page_idx",0))+1; counters[page]+=1; kind=_entry_type(entry)
        text,html,markdown,content_audit=_entry_content(entry,kind)
        asset=entry.get("img_path") or entry.get("image_path") or entry.get("asset_path")
        asset_path=str((root/str(asset)).resolve()) if asset and (root/str(asset)).exists() else None
        page_size=page_sizes.get(page);bbox,bbox_meta=_mineru_bbox(entry.get("bbox") or entry.get("box"),page_size,coordinate_space)
        elements.append({"element_id":f"P{page}-E{counters[page]:04}","page":page,"element_type":kind,
            "parent_element_id":entry.get("parent_id"),"reading_order":counters[page],"bbox":bbox,
            "text":text,"html":html,"markdown":markdown,"asset_path":asset_path,"cell_grid":table_grid(html) or markdown_table_grid(markdown) if kind=="table" else [],
            "metadata":{"mineru_index":index-1,"raw_type":entry.get("type"),"raw":entry,**content_audit,**bbox_meta,
                        "page_width":page_sizes.get(page,(None,None))[0],"page_height":page_sizes.get(page,(None,None))[1]}})
    return elements


def build_chunks(elements: list[dict[str, Any]], max_chars: int=12000) -> list[dict[str, Any]]:
    chunks=[]; current=[]; chars=0; titles=[]
    def flush():
        nonlocal current,chars
        if not current:return
        pages=[x["page"] for x in current]; idx=len(chunks)+1
        content="\n\n".join(f"[{x['element_id']}|{x['element_type']}] {x['markdown'] or x['text'] or '[无文本视觉元素]'}" for x in current)
        chunks.append({"chunk_id":f"C{idx:04}","title_path":list(titles),"element_ids":[x["element_id"] for x in current],
                       "page_from":min(pages),"page_to":max(pages),"content":content,"token_estimate":max(1,len(content)//2)})
        current=[];chars=0
    for element in elements:
        body=element.get("markdown") or element.get("text") or ""
        if element["element_type"]=="title":
            flush(); titles=[body[:200]] if body else titles
        if current and chars+len(body)>max_chars: flush()
        current.append(element);chars+=len(body)
    flush();return chunks


def compatibility_content(elements: list[dict[str, Any]], page_sizes: dict[int,tuple[float,float]], extractor: str) -> dict[str, Any]:
    pages={}
    for element in elements:
        page=pages.setdefault(element["page"],{"page":element["page"],"width":page_sizes.get(element["page"],(None,None))[0],
                                              "height":page_sizes.get(element["page"],(None,None))[1],"blocks":[]})
        page["blocks"].append({"id":element["element_id"],"type":element["element_type"],
            "text":element.get("markdown") or element.get("text") or "","bbox":element.get("bbox"),"asset_path":element.get("asset_path")})
    return {"pages":[pages[p] for p in sorted(pages)],"extractor":extractor,"layout_model":NORMALIZER_VERSION,"language":"zh-CN"}


def persist_normalized(version_id: int, elements: list[dict[str, Any]], chunks: list[dict[str, Any]],
                       content: dict[str, Any], manifest: dict[str, Any], warnings: list[Any], status="ready"):
    root=artifact_root(manifest["document_id"],version_id); normalized=root/"normalized";normalized.mkdir(exist_ok=True)
    for name,value in (("elements.json.gz",elements),("chunks.json.gz",chunks)):
        with gzip.open(normalized/name,"wt",encoding="utf-8") as handle:json.dump(value,handle,ensure_ascii=False)
    unmapped=[x for x in elements if (x.get("metadata") or {}).get("content_status")=="unmapped_payload"]
    unconsumed=[x for x in elements if (x.get("metadata") or {}).get("unconsumed_content_fields")]
    stats={"pages":len({x['page'] for x in elements}),"elements":len(elements),"types":dict(Counter(x["element_type"] for x in elements)),
           "coordinate_elements":sum(1 for x in elements if x.get("bbox")),"tables":sum(1 for x in elements if x["element_type"]=="table"),
           "visual_elements":sum(1 for x in elements if x["element_type"] in VISUAL_TYPES),"chunks":len(chunks),
           "canonical_content_elements":sum(1 for x in elements if x.get("text") or x.get("html") or x.get("markdown")),
           "unmapped_content_elements":len(unmapped),"unconsumed_content_elements":len(unconsumed)}
    coverage={"unmapped_payload":[{"element_id":x["element_id"],"raw_type":x["metadata"].get("raw_type"),
                                  "raw_content_fields":x["metadata"].get("raw_content_fields",[])} for x in unmapped],
              "unconsumed_fields":[{"element_id":x["element_id"],"raw_type":x["metadata"].get("raw_type"),
                                     "fields":x["metadata"].get("unconsumed_content_fields",[])} for x in unconsumed]}
    manifest={**manifest,"normalizer_version":NORMALIZER_VERSION,"stats":stats,
              "content_coverage":coverage,
              "normalized":{"elements":"normalized/elements.json.gz","chunks":"normalized/chunks.json.gz"}}
    (root/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    with db() as conn:
        conn.execute("DELETE FROM document_elements WHERE parser_version_id=?",(version_id,));conn.execute("DELETE FROM document_chunks WHERE parser_version_id=?",(version_id,))
        conn.executemany("""INSERT INTO document_elements(parser_version_id,element_id,page,element_type,parent_element_id,reading_order,bbox_json,text,html,markdown,asset_path,cell_grid_json,metadata_json,filtered_reason)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",[(version_id,x["element_id"],x["page"],x["element_type"],x.get("parent_element_id"),x["reading_order"],json.dumps(x.get("bbox")),x.get("text"),x.get("html"),x.get("markdown"),x.get("asset_path"),json.dumps(x.get("cell_grid") or [],ensure_ascii=False),json.dumps(x.get("metadata") or {},ensure_ascii=False),x.get("filtered_reason")) for x in elements])
        conn.executemany("""INSERT INTO document_chunks(parser_version_id,chunk_id,title_path_json,element_ids_json,page_from,page_to,content,token_estimate)
          VALUES(?,?,?,?,?,?,?,?)""",[(version_id,x["chunk_id"],json.dumps(x["title_path"],ensure_ascii=False),json.dumps(x["element_ids"]),x["page_from"],x["page_to"],x["content"],x["token_estimate"]) for x in chunks])
        conn.execute("""UPDATE document_parser_versions SET status=?,artifact_dir=?,manifest_json=?,content_json=?,stats_json=?,warnings_json=?,completed_at=? WHERE id=?""",
                     (status,str(root),json.dumps(manifest,ensure_ascii=False),json.dumps(content,ensure_ascii=False),json.dumps(stats,ensure_ascii=False),json.dumps(warnings,ensure_ascii=False),now(),version_id))
    return stats


def _page_sizes(path: Path) -> dict[int,tuple[float,float]]:
    if path.suffix.lower()!=".pdf": return {}
    with fitz.open(path) as pdf:return {i+1:(round(p.rect.width,2),round(p.rect.height,2)) for i,p in enumerate(pdf)}


def create_trace_run(project_id: str, provider: str, stage: str) -> int:
    with db() as conn:
        cur=conn.execute("""INSERT INTO audit_runs(project_id,started_at,status,rule_count,fact_count,anomaly_count,provider,progress,current_stage,result_json,run_kind,config_snapshot_json,prompt_versions_json,route_overrides_json)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(project_id,now(),"running",0,0,0,provider,0,stage,"{}","parse",json.dumps(snapshot(),ensure_ascii=False),json.dumps(prompt_snapshot(),ensure_ascii=False),"{}"))
        return int(cur.lastrowid)


def create_version(document_id: int, parser_kind: str, parser_version: str, provider_id: str|None, model: str|None,
                   params: dict[str,Any]|None=None, force=False) -> dict[str,Any]:
    with db() as conn:
        doc=conn.execute("SELECT * FROM documents WHERE id=?",(document_id,)).fetchone()
        if not doc:raise KeyError("document not found")
        if not force:
            old=conn.execute("""SELECT * FROM document_parser_versions WHERE document_id=? AND parser_kind=? AND parser_version=? AND COALESCE(model,'')=COALESCE(?,'') AND source_sha256=? ORDER BY id DESC LIMIT 1""",
                             (document_id,parser_kind,parser_version,model,doc["sha256"])).fetchone()
            if old:return dict(old)
        actual_version=parser_version if not force else f"{parser_version}-r{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        trace_run_id=create_trace_run(doc["project_id"],provider_id or parser_kind,"document_parse")
        if parser_kind=="mineru":
            parser_name="MinerU 本地视觉解析（VLM）" if str(provider_id or "").lower().endswith("local") else "MinerU 云端视觉解析"
        else:parser_name="PyMuPDF 文字版面解析"
        cur=conn.execute("""INSERT INTO document_parser_versions(document_id,parser_kind,parser_name,parser_version,provider_id,model,status,is_active,source_sha256,params_json,created_at,trace_run_id)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(document_id,parser_kind,parser_name,actual_version,provider_id,model,"queued",0,doc["sha256"],json.dumps(params or {},ensure_ascii=False),now(),trace_run_id))
        return dict(conn.execute("SELECT * FROM document_parser_versions WHERE id=?",(cur.lastrowid,)).fetchone())


def activate_version(document_id: int, version_id: int):
    with db() as conn:
        version=conn.execute("SELECT * FROM document_parser_versions WHERE id=? AND document_id=?",(version_id,document_id)).fetchone()
        if not version:raise KeyError("parser version not found")
        if version["status"] not in {"ready","ready_with_warnings"}:raise ValueError("只有完成的解析版本可以设为 active")
        conn.execute("UPDATE document_parser_versions SET is_active=0 WHERE document_id=?",(document_id,))
        conn.execute("UPDATE document_parser_versions SET is_active=1 WHERE id=?",(version_id,))
        conn.execute("UPDATE documents SET active_parser_version_id=?,content_json=?,pages=?,status=? WHERE id=?",
                     (version_id,version["content_json"],json.loads(version["stats_json"] or "{}").get("pages",0),f"已解析（{version['parser_name']}）",document_id))


def run_pymupdf_version(version_id: int, make_active=True) -> dict[str,Any]:
    with db() as conn:
        version=conn.execute("SELECT v.*,d.file_path,d.project_id FROM document_parser_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=?",(version_id,)).fetchone()
    path=Path(version["file_path"]);run_id=int(version["trace_run_id"]);span=start_span(run_id,"document_parse","parser","PyMuPDF 版面解析",provider_id="pymupdf",model=PYMUPDF_VERSION)
    attach_input(span,{"document_id":version["document_id"],"source_sha256":version["source_sha256"],"parser":PYMUPDF_VERSION})
    try:
        pages,extractor=parse_document(path);elements=normalize_pymupdf(pages);sizes=_page_sizes(path);chunks=build_chunks(elements)
        content=compatibility_content(elements,sizes,extractor);manifest={"document_id":version["document_id"],"parser_version_id":version_id,"parser":"pymupdf","source_sha256":version["source_sha256"],"raw_files":[]}
        stats=persist_normalized(version_id,elements,chunks,content,manifest,[])
        if make_active:activate_version(version["document_id"],version_id)
        finish_span(span,output={"manifest":manifest,"stats":stats,"elements":elements,"chunks":chunks})
        with db() as conn:conn.execute("UPDATE audit_runs SET status='completed',progress=100,current_stage='complete',finished_at=?,result_json=? WHERE id=?",(now(),json.dumps({"parser_version_id":version_id,"stats":stats},ensure_ascii=False),run_id))
        return {"version_id":version_id,"stats":stats}
    except Exception as exc:
        finish_span(span,"failed",error={"type":type(exc).__name__,"message":str(exc)})
        with db() as conn:
            conn.execute("UPDATE document_parser_versions SET status='failed',warnings_json=? WHERE id=?",(json.dumps([str(exc)],ensure_ascii=False),version_id));conn.execute("UPDATE audit_runs SET status='failed',finished_at=?,error=? WHERE id=?",(now(),str(exc),run_id))
        raise


def ensure_legacy_version(document_id: int) -> int|None:
    with db() as conn:
        doc=conn.execute("SELECT * FROM documents WHERE id=?",(document_id,)).fetchone()
        if not doc:return None
        if doc["active_parser_version_id"]:return int(doc["active_parser_version_id"])
        existing=conn.execute("SELECT id FROM document_parser_versions WHERE document_id=? AND status IN ('ready','ready_with_warnings') ORDER BY id DESC LIMIT 1",(document_id,)).fetchone()
    if existing:
        activate_version(document_id,int(existing["id"]));return int(existing["id"])
    version=create_version(document_id,"pymupdf",PYMUPDF_VERSION,"pymupdf",PYMUPDF_VERSION)
    run_pymupdf_version(version["id"],True);return int(version["id"])


def _safe_extract(raw: bytes, destination: Path) -> list[dict[str,Any]]:
    destination.mkdir(parents=True,exist_ok=True);manifest=[]
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for info in archive.infolist():
            pure=PurePosixPath(info.filename)
            if pure.is_absolute() or ".." in pure.parts:continue
            target=destination.joinpath(*pure.parts).resolve()
            if destination.resolve() not in target.parents and target!=destination.resolve():continue
            if info.is_dir():target.mkdir(parents=True,exist_ok=True);continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(info) as source,target.open("wb") as output:shutil.copyfileobj(source,output)
            manifest.append({"path":str(target.relative_to(destination)),"size":target.stat().st_size,"sha256":hashlib.sha256(target.read_bytes()).hexdigest()})
    return manifest


def _json_file(root: Path, suffix: str) -> tuple[Path|None,Any]:
    path=next((p for p in root.rglob("*.json") if p.name.endswith(suffix)),None)
    if not path:return None,[]
    try:return path,json.loads(path.read_text("utf-8",errors="replace"))
    except Exception:return path,[]


async def analyze_visual_elements(version_id: int, elements: list[dict[str,Any]], run_id: int):
    results=[];seen=set()
    for element in elements:
        table_fallback=element["element_type"]=="table" and not element.get("cell_grid")
        if (element["element_type"] not in VISUAL_TYPES and not table_fallback) or not element.get("asset_path"):continue
        path=Path(element["asset_path"])
        if not path.exists() or path.stat().st_size<5000:continue
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen:continue
        seen.add(digest)
        stage="visual_seal_signature" if element["element_type"] in {"seal","signature"} else "visual_form_checkbox" if element["element_type"] in {"form","checkbox","handwriting"} else "visual_table_or_chart" if element["element_type"] in {"chart","table"} else "visual_general"
        prompt=published_prompt(stage);provider,model=resolve_route(stage);mime=mimetypes.guess_type(path.name)[0] or "image/png"
        variables={"element":{"element_id":element["element_id"],"type":element["element_type"],"page":element["page"],"nearby_text":element.get("text") or ""}}
        user=render(prompt["user_prompt"],variables);messages=[{"role":"system","content":prompt["system_prompt"]},{"role":"user","content":[{"type":"text","text":user},{"type":"image_url","image_url":{"url":f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"}}]}]
        span=start_span(run_id,stage,"llm",f"视觉元素分析 {element['element_id']}",provider_id=provider["id"],model=model,metadata={"parser_version_id":version_id,"element_id":element["element_id"]})
        attach_input(span,{"system_prompt":prompt["system_prompt"],"user_prompt":user,"prompt_version":{"id":prompt["id"],"version":prompt["version"]},"image_artifact":{"path":str(path),"sha256":digest,"size":path.stat().st_size,"mime":mime}})
        created=now()
        trace=None
        try:
            trace=await chat_with_trace(messages,json_mode=True,max_tokens=1000,stage=stage);raw=trace.get("content") or "{}";output=json.loads(raw.strip().replace("```json","").replace("```","") or "{}")
            save_model_call(run_id,stage,trace)
            finish_span(span,output={"structured_output":output,"provider":trace["provider_id"],"model":trace["model"],"raw_response":trace["raw_response"]})
            status="completed";error={}
        except Exception as exc:
            save_model_call(run_id,stage,trace,exc)
            output={};error={"type":type(exc).__name__,"message":str(exc)};status="failed";finish_span(span,"failed",error=error)
        with db() as conn:conn.execute("""INSERT INTO visual_element_analyses(parser_version_id,element_id,stage,provider_id,model,prompt_version_id,status,input_json,output_json,error_json,created_at,completed_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(version_id,element["element_id"],stage,provider["id"],model,prompt["id"],status,json.dumps({"image_sha256":digest,"prompt":user},ensure_ascii=False),json.dumps(output,ensure_ascii=False),json.dumps(error,ensure_ascii=False),created,now()))
        results.append({"element_id":element["element_id"],"status":status,"output":output})
    return results


def reuse_visual_analyses(source_version_id: int, target_version_id: int, run_id: int) -> list[dict[str,Any]]:
    """Clone immutable visual outputs when only the canonical normalizer changes."""
    span=start_span(run_id,"visual_analysis_reuse","parser","Reuse visual analyses from immutable parent",
                    metadata={"source_parser_version_id":source_version_id,"target_parser_version_id":target_version_id})
    attach_input(span,{"source_parser_version_id":source_version_id,"reason":"normalizer-only derived version"})
    with db() as conn:
        source_rows=conn.execute("SELECT * FROM visual_element_analyses WHERE parser_version_id=? ORDER BY id",(source_version_id,)).fetchall()
        for row in source_rows:
            conn.execute("""INSERT INTO visual_element_analyses(parser_version_id,element_id,stage,provider_id,model,prompt_version_id,status,input_json,output_json,error_json,created_at,completed_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(target_version_id,row["element_id"],row["stage"],row["provider_id"],row["model"],row["prompt_version_id"],
              row["status"],row["input_json"],row["output_json"],row["error_json"],row["created_at"],row["completed_at"]))
    results=[{"element_id":row["element_id"],"status":row["status"],"output":json.loads(row["output_json"] or "{}")}
             for row in source_rows]
    finish_span(span,output={"reused_count":len(results),"items":results})
    return results


def content_coverage_warnings(elements: list[dict[str,Any]]) -> list[str]:
    warnings=[]
    unmapped=[x for x in elements if (x.get("metadata") or {}).get("content_status")=="unmapped_payload"]
    unconsumed=[x for x in elements if (x.get("metadata") or {}).get("unconsumed_content_fields")]
    if unmapped:
        details=", ".join(f"{x['element_id']}:{x['metadata'].get('raw_type')}[{','.join(x['metadata'].get('raw_content_fields',[]))}]" for x in unmapped[:20])
        warnings.append(f"Canonical content unmapped for {len(unmapped)} element(s): {details}")
    if unconsumed:
        details=", ".join(f"{x['element_id']}[{','.join(x['metadata'].get('unconsumed_content_fields',[]))}]" for x in unconsumed[:20])
        warnings.append(f"Canonical content has unconsumed fields in {len(unconsumed)} element(s): {details}")
    return warnings


def _document_zip(raw: bytes, document_name: str) -> bytes:
    """Return a one-document ZIP from a local multi-file MinerU result."""
    source=io.BytesIO(raw);target=io.BytesIO();stem=Path(document_name).stem
    with zipfile.ZipFile(source) as archive:
        files=[item for item in archive.infolist() if not item.is_dir()]
        roots={PurePosixPath(item.filename).parts[0] for item in files if PurePosixPath(item.filename).parts}
        matched=[root for root in roots if Path(root).stem==stem or root==stem]
        if not matched and len(roots)==1:matched=list(roots)
        selected=[item for item in files if matched and PurePosixPath(item.filename).parts[0] in matched]
        if not selected:
            raise RuntimeError(f"本地 MinerU ZIP 中找不到 {document_name} 的解析产物")
        with zipfile.ZipFile(target,"w",zipfile.ZIP_DEFLATED) as output:
            for item in selected:output.writestr(item.filename,archive.read(item))
    return target.getvalue()


async def finalize_mineru_version(version_id: int, record: dict[str,Any], raw_zip: bytes|None=None,
                                  reuse_visual_from: int|None=None):
    with db() as conn:version=conn.execute("SELECT v.*,d.file_path FROM document_parser_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=?",(version_id,)).fetchone()
    run_id=int(version["trace_run_id"]);root=artifact_root(version["document_id"],version_id);raw_dir=root/"raw";span=start_span(run_id,"mineru_download_normalize","parser","MinerU 完整产物下载与规范化",provider_id=version["provider_id"],model=version["model"])
    attach_input(span,{"record":record,"parser_version_id":version_id})
    if raw_zip is None:
        async with httpx.AsyncClient(timeout=180) as client:
            response=await client.get(record["full_zip_url"]);response.raise_for_status();raw=response.content
    else:raw=raw_zip
    raw_dir.mkdir(parents=True,exist_ok=True);zip_path=raw_dir/"mineru-result.zip";zip_path.write_bytes(raw);files=_safe_extract(raw,raw_dir/"extracted")
    content_path,entries=_json_file(raw_dir/"extracted","content_list.json")
    if not isinstance(entries,list):entries=[]
    sizes=_page_sizes(Path(version["file_path"]));elements=normalize_mineru(entries,raw_dir/"extracted",sizes,"normalized_1000");chunks=build_chunks(elements);content=compatibility_content(elements,sizes,f"mineru-{version['model']}-{NORMALIZER_VERSION}")
    warnings=[]
    if not entries:warnings.append("MinerU ZIP 中未找到有效 content_list.json")
    if entries and len(elements)!=len(entries):warnings.append(f"原始条目 {len(entries)}，规范化元素 {len(elements)}")
    warnings.extend(content_coverage_warnings(elements))
    manifest={"document_id":version["document_id"],"parser_version_id":version_id,"parser":"mineru","provider_id":version["provider_id"],"model":version["model"],"source_sha256":version["source_sha256"],
              "zip":{"path":"raw/mineru-result.zip","size":len(raw),"sha256":hashlib.sha256(raw).hexdigest()},"raw_files":files,"content_list":str(content_path.relative_to(root)) if content_path else None,"raw_entry_count":len(entries),"mapped_entry_count":len(elements)}
    stats=persist_normalized(version_id,elements,chunks,content,manifest,warnings,"visual_analyzing")
    visuals=reuse_visual_analyses(reuse_visual_from,version_id,run_id) if reuse_visual_from else await analyze_visual_elements(version_id,elements,run_id)
    visual_by_id={x["element_id"]:x["output"] for x in visuals if x["status"]=="completed" and x.get("output")}
    if visual_by_id:
        for element in elements:
            if element["element_id"] in visual_by_id:
                element["metadata"]={**(element.get("metadata") or {}),"visual_analysis":visual_by_id[element["element_id"]],
                                     "visual_analysis_role":"multimodal_candidate_not_source_truth"}
                if not (element.get("markdown") or element.get("text")):
                    element["markdown"]="[多模态视觉分析，仅作候选] "+json.dumps(visual_by_id[element["element_id"]],ensure_ascii=False)
        chunks=build_chunks(elements);content=compatibility_content(elements,sizes,f"mineru-{version['model']}-{NORMALIZER_VERSION}")
    stats=persist_normalized(version_id,elements,chunks,content,manifest,warnings,"ready_with_warnings" if warnings else "ready")
    if bool(json.loads(version["params_json"] or "{}").get("auto_activate",False)):
        activate_version(int(version["document_id"]),version_id)
    finish_span(span,"completed_with_warning" if warnings else "completed",output={"manifest":manifest,"stats":stats,"warnings":warnings,"elements":elements,"chunks":chunks,"visual_analyses":visuals})
    with db() as conn:
        conn.execute("UPDATE audit_runs SET status='completed',progress=100,current_stage='complete',finished_at=?,result_json=? WHERE id=?",(now(),json.dumps({"parser_version_id":version_id,"stats":stats,"warnings":warnings,"visual_analyses":len(visuals)},ensure_ascii=False),run_id))
        conn.execute("UPDATE document_parse_jobs SET status=?,progress=100,response_json=?,updated_at=?,finished_at=? WHERE parser_version_id=?",("ready_with_warnings" if warnings else "ready",json.dumps(record,ensure_ascii=False),now(),now(),version_id))
    return {"version_id":version_id,"stats":stats,"warnings":warnings}


def create_renormalized_version(source_version_id: int) -> dict[str,Any]:
    """Create, but never mutate, a child version bound to a prior MinerU raw ZIP."""
    with db() as conn:
        source=conn.execute("SELECT * FROM document_parser_versions WHERE id=?",(source_version_id,)).fetchone()
    if not source:raise KeyError("parser version not found")
    if source["parser_kind"]!="mineru" or source["status"] not in {"ready","ready_with_warnings"}:
        raise ValueError("only completed MinerU versions can be renormalized")
    raw_path=Path(source["artifact_dir"] or "")/"raw"/"mineru-result.zip"
    if not raw_path.exists():raise FileNotFoundError("source MinerU raw ZIP not found")
    params={**json.loads(source["params_json"] or "{}"),"operation":"renormalize_raw_zip",
            "derived_from_parser_version_id":source_version_id,"normalizer_version":NORMALIZER_VERSION,
            "source_raw_zip_sha256":hashlib.sha256(raw_path.read_bytes()).hexdigest()}
    child=create_version(int(source["document_id"]),"mineru",NORMALIZER_VERSION,source["provider_id"],source["model"],params,True)
    with db() as conn:
        conn.execute("UPDATE document_parser_versions SET parent_version_id=?,status='queued' WHERE id=?",(source_version_id,child["id"]))
        conn.execute("""INSERT INTO document_parse_jobs(parser_version_id,attempt,provider_id,status,progress,request_json,response_json,error_json,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)""",(child["id"],1,source["provider_id"],"normalizing",10,
          json.dumps({"operation":"renormalize_raw_zip","source_parser_version_id":source_version_id,"normalizer_version":NORMALIZER_VERSION},ensure_ascii=False),"{}","{}",now(),now()))
    return {**child,"parent_version_id":source_version_id,"source_raw_zip":str(raw_path)}


async def run_renormalized_version(source_version_id: int, target_version_id: int):
    with db() as conn:
        source=conn.execute("SELECT * FROM document_parser_versions WHERE id=?",(source_version_id,)).fetchone()
        target=conn.execute("SELECT * FROM document_parser_versions WHERE id=?",(target_version_id,)).fetchone()
    try:
        raw_path=Path(source["artifact_dir"])/"raw"/"mineru-result.zip";raw=raw_path.read_bytes()
        span=start_span(int(target["trace_run_id"]),"mineru_raw_reuse","parser","Reuse immutable MinerU raw ZIP",
                        provider_id=source["provider_id"],model=source["model"],metadata={"source_parser_version_id":source_version_id})
        attach_input(span,{"source_parser_version_id":source_version_id,"raw_zip":{"path":str(raw_path),"size":len(raw),
                     "sha256":hashlib.sha256(raw).hexdigest()},"normalizer_version":NORMALIZER_VERSION})
        finish_span(span,output={"target_parser_version_id":target_version_id,"bytes_copied":len(raw)})
        await finalize_mineru_version(target_version_id,{"state":"done","operation":"renormalize_raw_zip",
              "source_parser_version_id":source_version_id,"normalizer_version":NORMALIZER_VERSION},raw,reuse_visual_from=source_version_id)
    except Exception as exc:
        with db() as conn:
            conn.execute("UPDATE document_parser_versions SET status='failed',warnings_json=? WHERE id=?",
                         (json.dumps([str(exc)],ensure_ascii=False),target_version_id))
            conn.execute("UPDATE document_parse_jobs SET status='normalize_failed',progress=100,error_json=?,updated_at=?,finished_at=? WHERE parser_version_id=?",
                         (json.dumps({"type":type(exc).__name__,"message":str(exc)},ensure_ascii=False),now(),now(),target_version_id))
            conn.execute("UPDATE audit_runs SET status='failed',finished_at=?,error=? WHERE id=?",(now(),str(exc),target["trace_run_id"]))


async def poll_mineru_batch(batch_id: str, versions_by_name: dict[str,int], provider_id: str|None=None, max_attempts=120):
    pending=set(versions_by_name)
    for attempt in range(max_attempts):
        try:
            result=await mineru_batch_result(batch_id,provider_id)
        except Exception as exc:
            if attempt>=max_attempts-1:raise
            await asyncio.sleep(min(30,2+attempt//3))
            continue
        records=((result.get("data") or {}).get("extract_result") or [])
        for record in records:
            name=record.get("file_name")
            if name not in pending:continue
            version_id=versions_by_name[name];state=record.get("state") or "pending"
            with db() as conn:conn.execute("UPDATE document_parse_jobs SET status=?,progress=?,response_json=?,updated_at=? WHERE parser_version_id=?",(state,70 if state=="running" else 40,json.dumps(record,ensure_ascii=False),now(),version_id))
            if state=="done":
                try:await finalize_mineru_version(version_id,record)
                except Exception as exc:
                    with db() as conn:
                        conn.execute("UPDATE document_parser_versions SET status='failed',warnings_json=? WHERE id=?",(json.dumps([str(exc)],ensure_ascii=False),version_id));conn.execute("UPDATE document_parse_jobs SET status='normalize_failed',error_json=?,updated_at=?,finished_at=? WHERE parser_version_id=?",(json.dumps({"type":type(exc).__name__,"message":str(exc)},ensure_ascii=False),now(),now(),version_id));row=conn.execute("SELECT trace_run_id FROM document_parser_versions WHERE id=?",(version_id,)).fetchone();conn.execute("UPDATE audit_runs SET status='failed',finished_at=?,error=? WHERE id=?",(now(),str(exc),row["trace_run_id"]))
                pending.remove(name)
            elif state=="failed":
                with db() as conn:
                    conn.execute("UPDATE document_parser_versions SET status='failed',warnings_json=? WHERE id=?",(json.dumps([record.get("err_msg") or "MinerU解析失败"],ensure_ascii=False),version_id));conn.execute("UPDATE document_parse_jobs SET status='provider_failed',error_json=?,updated_at=?,finished_at=? WHERE parser_version_id=?",(json.dumps(record,ensure_ascii=False),now(),now(),version_id))
                pending.remove(name)
        if not pending:return
        await asyncio.sleep(5)
    with db() as conn:
        for name in pending:
            version_id=versions_by_name[name];conn.execute("UPDATE document_parser_versions SET status='failed',warnings_json=? WHERE id=?",(json.dumps(["MinerU轮询超时"],ensure_ascii=False),version_id));conn.execute("UPDATE document_parse_jobs SET status='provider_failed',error_json=?,updated_at=?,finished_at=? WHERE parser_version_id=?",(json.dumps({"message":"MinerU轮询超时"},ensure_ascii=False),now(),now(),version_id))


async def poll_mineru_local_task(task_id: str, versions_by_name: dict[str,int], provider_id: str,
                                 max_attempts: int=240):
    """Poll the local async API and normalize the full VLM ZIP per document."""
    pending=set(versions_by_name)
    for attempt in range(max_attempts):
        try:status=await mineru_batch_result(task_id,provider_id)
        except Exception as exc:
            if attempt>=max_attempts-1:
                status={"status":"failed","error":str(exc)}
            else:
                await asyncio.sleep(min(15,2+attempt//5));continue
        state=str(status.get("status") or "pending").lower()
        progress={"pending":25,"queued":25,"processing":65,"running":65,"completed":90,"failed":100}.get(state,40)
        with db() as conn:
            for name in pending:
                conn.execute("UPDATE document_parse_jobs SET status=?,progress=?,response_json=?,updated_at=? WHERE parser_version_id=?",
                    (state,progress,json.dumps(status,ensure_ascii=False),now(),versions_by_name[name]))
                conn.execute("UPDATE document_parser_versions SET status=? WHERE id=?",
                    ("running" if state in {"processing","running"} else "submitted",versions_by_name[name]))
        if state=="completed":
            try:raw,result_meta=await mineru_local_result(task_id,provider_id)
            except Exception as exc:
                status={**status,"status":"failed","error":str(exc)};state="failed"
            else:
                for name in list(pending):
                    version_id=versions_by_name[name]
                    try:
                        selected=_document_zip(raw,name)
                        await finalize_mineru_version(version_id,{**status,"state":"done","file_name":name,
                            "local_result":result_meta,"task_id":task_id,"engine":"vlm-engine"},selected)
                    except Exception as exc:
                        with db() as conn:
                            conn.execute("UPDATE document_parser_versions SET status='failed',warnings_json=? WHERE id=?",
                                (json.dumps([str(exc)],ensure_ascii=False),version_id))
                            conn.execute("UPDATE document_parse_jobs SET status='normalize_failed',error_json=?,updated_at=?,finished_at=? WHERE parser_version_id=?",
                                (json.dumps({"type":type(exc).__name__,"message":str(exc)},ensure_ascii=False),now(),now(),version_id))
                            row=conn.execute("SELECT trace_run_id FROM document_parser_versions WHERE id=?",(version_id,)).fetchone()
                            conn.execute("UPDATE audit_runs SET status='failed',finished_at=?,error=? WHERE id=?",(now(),str(exc),row["trace_run_id"]))
                    pending.remove(name)
                return
        if state in {"failed","error","cancelled"}:
            message=status.get("error") or status.get("message") or "本地 MinerU 解析失败"
            with db() as conn:
                for name in pending:
                    version_id=versions_by_name[name]
                    conn.execute("UPDATE document_parser_versions SET status='failed',warnings_json=? WHERE id=?",
                        (json.dumps([message],ensure_ascii=False),version_id))
                    conn.execute("UPDATE document_parse_jobs SET status='provider_failed',error_json=?,updated_at=?,finished_at=? WHERE parser_version_id=?",
                        (json.dumps(status,ensure_ascii=False),now(),now(),version_id))
                    row=conn.execute("SELECT trace_run_id FROM document_parser_versions WHERE id=?",(version_id,)).fetchone()
                    conn.execute("UPDATE audit_runs SET status='failed',finished_at=?,error=? WHERE id=?",(now(),str(message),row["trace_run_id"]))
            return
        await asyncio.sleep(3)
    with db() as conn:
        for name in pending:
            version_id=versions_by_name[name]
            conn.execute("UPDATE document_parser_versions SET status='failed',warnings_json=? WHERE id=?",
                (json.dumps(["本地 MinerU 轮询超时"],ensure_ascii=False),version_id))


def version_payload(version_id: int, include_elements=False) -> dict[str,Any]:
    with db() as conn:
        row=conn.execute("SELECT * FROM document_parser_versions WHERE id=?",(version_id,)).fetchone()
        if not row:raise KeyError("parser version not found")
        item=decode(row,"params_json","manifest_json","content_json","stats_json","warnings_json")
        jobs=[decode(x,"request_json","response_json","error_json") for x in conn.execute("SELECT * FROM document_parse_jobs WHERE parser_version_id=? ORDER BY attempt",(version_id,))]
        item["jobs"]=jobs
        if include_elements:
            item["elements"]=[decode(x,"bbox_json","cell_grid_json","metadata_json") for x in conn.execute("SELECT * FROM document_elements WHERE parser_version_id=? ORDER BY page,reading_order",(version_id,))]
            item["chunks"]=[decode(x,"title_path_json","element_ids_json") for x in conn.execute("SELECT * FROM document_chunks WHERE parser_version_id=? ORDER BY id",(version_id,))]
        item["visual_analyses"]=[decode(x,"input_json","output_json","error_json") for x in conn.execute("SELECT * FROM visual_element_analyses WHERE parser_version_id=? ORDER BY id",(version_id,))]
    return item
