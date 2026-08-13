import hashlib
import json
from datetime import datetime
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from .db import FILES_DIR, db

NOW = "2026-08-08 09:42"

PROJECTS = [
 ("GC2026-018","村文化广场改造工程","公共设施","东湾社区",2860000,2716000,"施工中",72,3,1),
 ("GC2026-021","工业园区雨污分流工程","市政工程","南栅社区",5180000,4968000,"施工中",46,5,2),
 ("GC2026-015","综合市场升级改造项目","改造工程","北面社区",3640000,3485000,"待验收",94,2,0),
 ("GC2026-012","社区停车场建设项目","公共设施","白沙社区",2260000,2118000,"施工中",68,2,1),
 ("GC2026-025","旧厂房消防改造工程","改造工程","新联社区",1920000,1856000,"招标中",12,1,0),
 ("GC2026-009","环村道路提升工程","市政工程","怀德社区",7350000,6980000,"施工中",81,4,1),
 ("GC2026-027","党群服务中心装修工程","装修工程","博涌社区",1560000,1492000,"合同签订",8,1,0),
 ("GC2026-006","农贸市场屋面修缮工程","修缮工程","赤岗社区",980000,936000,"已完工",100,0,0),
 ("GC2026-030","智慧安防设备采购项目","设备采购","龙眼社区",1240000,1178000,"招标中",5,2,0),
 ("GC2026-003","河涌沿岸绿化提升工程","绿化工程","镇口社区",3080000,2936000,"待结算",100,3,1),
 ("GC2026-033","社区物业电梯更新项目","设备采购","大宁社区",2080000,1988000,"立项审批",2,0,0),
 ("GC2026-024","老年活动中心加固工程","修缮工程","树田社区",2680000,2560000,"施工中",37,2,0),
]

DOC_TYPES = [
 ("立项申请及预算批复.pdf","立项材料","OA审批系统"),
 ("民主决策会议纪要.pdf","民主程序","三资监管系统"),
 ("招标文件.pdf","招标文件","招投标系统"),
 ("招标工程量清单.pdf","工程量清单","招投标系统"),
 ("中标结果通知书.pdf","中标结果","招投标系统"),
 ("建设工程施工合同.pdf","施工合同","工程项目系统"),
 ("工程变更联系单-02.pdf","工程变更","工程项目系统"),
 ("第三期付款凭证.pdf","付款凭证","财务系统"),
 ("阶段验收记录.pdf","验收资料","档案系统"),
]

def lines_for(project, doc_type):
    pid,name,_,community,budget,contract,*_ = project
    base = [f"项目编号：{pid}", f"项目名称：{name}", f"建设单位：{community}股份经济联合社"]
    variants = {
      "立项材料": [f"项目概算：{budget:,}元。", "建设范围：广场地面铺装、排水设施、照明设施及配套绿化。", "资金来源：集体自有资金。"],
      "民主程序": ["会议应到成员45人，实到41人。", "经表决，赞成39票，反对1票，弃权1票。", "同意按规定开展公开招投标。"],
      "招标文件": [f"最高投标限价：{budget:,}元。", "计划工期：120日历天。", "进度款按已完成合格工程量的80%支付。", "工程竣工验收合格后支付至合同价款的97%，余款作为质量保证金。"],
      "工程量清单": ["分部分项工程：广场地面及基层。", "排水沟、雨水口及管道改造。", "钢结构遮雨设施，含基础、钢构件及面板安装。", "庭院灯、线缆及控制箱。"],
      "中标结果": [f"中标单位：东莞市莞建工程有限公司。", f"中标价格：{contract:,}元。", "项目负责人：梁建国。"],
      "施工合同": [f"签约合同价：人民币{contract:,}元。", "合同工期总日历天数：150天。" if pid=="GC2026-018" else "合同工期总日历天数：120天。", "工程进度款按已完成合格工程量的80%支付。", "质量保证金为结算价款的3%。", "累计工程变更超过合同价10%时须重新履行集体审议和镇级审批程序。"],
      "工程变更": ["变更原因：现场使用需求调整。", "新增钢结构雨棚一项，暂定金额186,000元。", "排水管线局部改造，暂定金额92,000元。", "本次累计变更金额278,000元。", "审批附件：未检出镇级审批单。"],
      "付款凭证": ["第一期付款：815,000元。", "第二期付款：1,086,000元。", "本次第三期支付：900,000元。", "累计支付金额：2,801,000元。", "对应工程节点：阶段验收，完成比例72%。"],
      "验收资料": ["本次验收性质：阶段性验收。", "现场形象进度：72%。", "质量结论：已完成部分符合设计要求。", "尚未完成竣工验收。"],
    }
    return base + variants[doc_type] + ["本文件由业务系统同步归档，原始记录可追溯。"]

def make_pdf(path: Path, title: str, lines: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    try: pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light")); font="STSong-Light"
    except Exception: font="Helvetica"
    c=canvas.Canvas(str(path), pagesize=A4); width,height=A4
    c.setTitle(title); c.setFont(font,16); c.drawCentredString(width/2,height-55,title)
    c.setFont(font,10); y=height-95
    for i,line in enumerate(lines,1):
        c.setFillColorRGB(.55,.58,.61); c.drawRightString(55,y,str(i))
        c.setFillColorRGB(.12,.15,.18); c.drawString(67,y,line)
        y-=28
        if y<70: c.showPage(); c.setFont(font,10); y=height-70
    c.setFont(font,8); c.setFillColorRGB(.55,.58,.61); c.drawCentredString(width/2,30,"智能审查模拟资料 · 仅供演示")
    c.save()

def seed():
    with db() as conn:
        for p in PROJECTS:
            pid,name,cat,community,budget,contract,status,progress,risks,high=p
            if conn.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
                continue
            facts={}
            conn.execute("""INSERT INTO projects(id,name,category,community,budget,contract_amount,status,progress,risk_count,high_risk_count,updated_at,facts_json,is_demo)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)""",(pid,name,cat,community,budget,contract,status,progress,0,0,NOW,json.dumps(facts,ensure_ascii=False)))
            for ix,(filename,doc_type,source) in enumerate(DOC_TYPES):
                lines=lines_for(p,doc_type); path=FILES_DIR/pid/filename; make_pdf(path,filename[:-4],lines)
                sha=hashlib.sha256(path.read_bytes()).hexdigest()
                content={"pages":[{"page":1,"width":595,"height":842,"lines":[{"no":i+1,"text":line,"bbox":[67,90+i*28,520,110+i*28]} for i,line in enumerate(lines)]}],"extractor":"simulated-layout-v1","language":"zh-CN"}
                conn.execute("INSERT INTO documents(project_id,name,doc_type,source_system,pages,status,file_path,content_json,sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(pid,filename,doc_type,source,1,"已解析",str(path),json.dumps(content,ensure_ascii=False),sha,NOW))
        # Findings are intentionally not seeded. They are created only by a
        # recorded real audit run in audit_pipeline.py.
