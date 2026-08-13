import json
from datetime import datetime


REVIEW_MODES = {"gate", "incremental", "final"}

ACTION_TYPES = {
    "project_approval": {"label": "立项审批前审查", "required_phases": ["立项审批"]},
    "tender_release": {"label": "招标文件发布前审查", "required_phases": ["立项审批", "预算控制价", "招标评标"]},
    "contract_signing": {"label": "合同签订前审查", "required_phases": ["招标评标", "施工合同"]},
    "change_approval": {"label": "工程变更批准前审查", "required_phases": ["施工合同", "变更签证"]},
    "measurement_confirmation": {"label": "进度计量确认前审查", "required_phases": ["施工合同", "开工计量"]},
    "payment_approval": {"label": "付款审批前审查", "required_phases": ["施工合同", "开工计量", "结算付款"]},
    "acceptance_approval": {"label": "竣工验收前审查", "required_phases": ["施工合同", "开工计量", "竣工验收"]},
    "settlement_payment": {"label": "结算及尾款支付前审查", "required_phases": ["施工合同", "竣工验收", "结算付款"]},
    "document_increment": {"label": "新增资料增量审查", "required_phases": []},
    "final_review": {"label": "全过程全量终审", "required_phases": []},
}


def normalized_action(review_mode, action_type):
    mode = review_mode or "final"
    if mode not in REVIEW_MODES:
        raise ValueError("审查模式无效")
    action = action_type or ("final_review" if mode == "final" else "document_increment" if mode == "incremental" else "")
    if action not in ACTION_TYPES:
        raise ValueError("待审业务动作无效")
    if mode == "gate" and action in {"document_increment", "final_review"}:
        raise ValueError("阶段门禁必须选择具体业务动作")
    if mode == "incremental" and action != "document_increment":
        raise ValueError("资料增量模式的业务动作必须是 document_increment")
    if mode == "final" and action != "final_review":
        raise ValueError("全量终审的业务动作必须是 final_review")
    return mode, action


def parse_cutoff(value):
    if not value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    candidate = value.strip().replace("T", " ")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("资料截止时间格式无效") from exc
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def applicable_rule(rule, action_type):
    try:
        actions = json.loads(rule.get("applicable_actions_json") or '["*"]')
    except (TypeError, json.JSONDecodeError):
        actions = ["*"]
    return "*" in actions or action_type in actions


def action_definition(action_type):
    return {"type": action_type, **ACTION_TYPES[action_type]}

