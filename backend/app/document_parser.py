import re
from pathlib import Path

import fitz
from docx import Document


def pdf_layout(path: Path):
    pages=[]
    with fitz.open(path) as pdf:
        for page_index,page in enumerate(pdf):
            blocks=[]
            for block_index,raw in enumerate(page.get_text("blocks",sort=True),1):
                x0,y0,x1,y1,text,*_=raw
                text=re.sub(r"[ \t]+"," ",text).strip()
                if not text: continue
                blocks.append({"id":block_index,"type":"text","text":text,"bbox":[round(x0,2),round(y0,2),round(x1,2),round(y1,2)]})
            pages.append({"page":page_index+1,"width":round(page.rect.width,2),"height":round(page.rect.height,2),"blocks":blocks})
    return pages


def parse_document(path: Path):
    suffix=path.suffix.lower()
    if suffix==".pdf": return pdf_layout(path),"pymupdf-layout-v1"
    if suffix==".docx":
        blocks=[]
        for paragraph in Document(str(path)).paragraphs:
            text=paragraph.text.strip()
            if text: blocks.append({"id":len(blocks)+1,"type":"paragraph","text":text,"bbox":None})
        return [{"page":1,"width":None,"height":None,"blocks":blocks}],"python-docx-paragraph-v1"
    if suffix==".txt":
        text=path.read_text("utf-8",errors="replace")
        paragraphs=[x.strip() for x in re.split(r"\n\s*\n",text) if x.strip()]
        return [{"page":1,"width":None,"height":None,"blocks":[{"id":i+1,"type":"paragraph","text":x,"bbox":None} for i,x in enumerate(paragraphs)]}],"text-paragraph-v1"
    return [],"mineru-pending"


def blocks_text(content):
    parts=[]
    for page in content.get("pages") or []:
        for block in page.get("blocks") or []:
            text=(block.get("text") or "").strip()
            if text: parts.append(f"[P{page.get('page',1)}-B{block.get('id',1)}] {text}")
    return "\n".join(parts)


def locate_quote(content, quote, page_number=1):
    def norm(value): return re.sub(r"\s+","",str(value or "")).replace(",","").replace("，","")
    needle=norm(quote); candidates=[]
    pages=content.get("pages") or []
    selected=[p for p in pages if int(p.get("page",1))==int(page_number)] or pages
    for page in selected:
        for block in page.get("blocks") or []:
            hay=norm(block.get("text")); score=0
            if needle and (needle in hay or hay in needle): score=min(len(needle),len(hay))
            elif needle and hay:
                tokens=set(re.findall(r"[\u4e00-\u9fff]|\d+(?:\.\d+)?",needle))
                score=sum(len(t) for t in tokens if t in hay)
            if score: candidates.append((score,page,block))
    candidates.sort(key=lambda x:x[0],reverse=True)
    if not candidates: return None
    _,page,block=candidates[0]
    return {"page":page.get("page",1),"page_width":page.get("width"),"page_height":page.get("height"),"block_id":block.get("id"),"bbox":block.get("bbox"),"block_text":block.get("text")}
