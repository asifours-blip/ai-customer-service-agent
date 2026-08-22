"""受控 Agent 工作流（LangGraph 编排层）。

分层铁律（D-001）：Graph=编排，Service=业务逻辑，Tool=对外操作。
节点不写业务规则：意图→router、资格→EligibilityService、权限→service 层。
db 会话经 state 注入（图不自行开事务）；recursion_limit=MAX_AGENT_STEPS+2 防死循环。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app.agent.entity import SessionEntities, resolve_entities
from app.agent.router import (
    Confirmation,
    IntentClassifier,
    RuleBasedIntentClassifier,
    detect_confirmation,
)
from app.agent.state import AgentState, Route, make_pending, pending_expired
from app.llm.client import LLMClient
from app.models.base import utcnow
from app.rag.answerer import RagService
from app.security.guardrails import GUARDRAIL_REPLY, detect_injection
from app.services.eligibility import evaluate_after_sales
from app.tools import ToolRegistry
from app.tools.executor import ToolExecutor

MAX_STEPS = 8

_POLICY_QUERY = {"REFUND": "退货退款政策 时限", "EXCHANGE": "换货政策 时限", "REPAIR": "保修维修政策 期限"}
_TYPE_WORDS = (
    ("退款", "REFUND"), ("退货", "REFUND"), ("换", "EXCHANGE"),
    ("维修", "REPAIR"), ("报修", "REPAIR"), ("坏", "REPAIR"),
)


def guess_after_sales_type(text: str) -> str:
    for kw, t in _TYPE_WORDS:
        if kw in text:
            return t
    return "REPAIR"


def build_agent_graph(
    llm: LLMClient,
    rag: RagService,
    tools: ToolRegistry,
    classifier: IntentClassifier | None = None,
    executor: ToolExecutor | None = None,
) -> Any:
    """构建编译后的受控 Agent 图。依赖全注入，Fake 可离线全流程测试。"""
    clf = classifier or RuleBasedIntentClassifier()
    ex = executor or ToolExecutor()

    def run_tool(name: str, db: Session, user_id: str, args: dict[str, Any]) -> Any:
        return ex.execute(db, tools.require(name), user_id, args)

    # ---------- 节点 ----------

    def node_guardrail(state: AgentState) -> dict[str, Any]:
        """注入检测（软防线）：标记则保守回复；硬防线始终在 Tool 权限层。"""
        verdict = detect_injection(state["user_query"])
        if verdict.flagged:
            return {
                "route": Route.HUMAN,
                "final_answer": GUARDRAIL_REPLY,
                "error_type": "INJECTION_FLAGGED:" + ",".join(verdict.patterns),
            }
        return {}

    def check_pending(state: AgentState) -> dict[str, Any]:
        pending = state.get("pending_action")
        if pending and not pending_expired(pending):
            conf = detect_confirmation(state["user_query"])
            if conf is Confirmation.YES:
                return {"confirmation": "YES"}
            if conf is Confirmation.NO:
                return {
                    "confirmation": "NO",
                    "pending_action": None,
                    "final_answer": "好的，已取消本次申请。还有其他可以帮您的吗？",
                }
            return {"confirmation": "TOPIC_SWITCH", "pending_action": None}
        if pending:
            return {"pending_action": None, "confirmation": "EXPIRED"}
        return {"confirmation": "NONE"}

    def classify(state: AgentState) -> dict[str, Any]:
        r = clf.classify(state["user_query"])
        return {"intent": r.intent.value}

    def resolve_entity(state: AgentState) -> dict[str, Any]:
        session = SessionEntities(
            active_order_id=state.get("active_order_id"),
            active_ticket_id=state.get("active_ticket_id"),
        )
        res = resolve_entities(state["user_query"], session, state.get("user_order_ids", []))
        return {"entity_order_id": res.order_id, "entity_ticket_id": res.ticket_id, "ambiguous": res.ambiguous}

    def route_after_entity(state: AgentState) -> str:
        if state.get("confirmation") == "YES":
            return "execute_confirmed"
        if state.get("ambiguous"):
            return "clarify"
        need_order_no_entity = state.get("intent") in ("ORDER_QUERY", "LOGISTICS_QUERY") and not state.get(
            "entity_order_id"
        )
        if need_order_no_entity:
            return "clarify"
        if state.get("intent") == "TICKET_QUERY" and not state.get("entity_ticket_id"):
            return "clarify"
        mapping = {
            "PRODUCT_QA": "rag",
            "POLICY_QA": "rag",
            "ORDER_QUERY": "order_tool",
            "LOGISTICS_QUERY": "logistics_tool",
            "TICKET_QUERY": "ticket_tool",
            "AFTER_SALES": "after_sales",
            "CHITCHAT": "direct_llm",
        }
        return mapping.get(state.get("intent", "UNKNOWN"), "human")

    def node_rag(state: AgentState) -> dict[str, Any]:
        db: Session = state["db"]
        result = rag.answer(db, state["user_query"])
        return {
            "route": Route.RAG,
            "rag_answer": result.answer,
            "rag_sources": result.sources,
            "rag_abstained": result.abstained,
            "final_answer": result.answer,
            "prompt_tokens": state.get("prompt_tokens", 0) + result.usage_prompt_tokens,
            "completion_tokens": state.get("completion_tokens", 0) + result.usage_completion_tokens,
        }

    def node_order_tool(state: AgentState) -> dict[str, Any]:
        db: Session = state["db"]
        r = run_tool("query_order", db, state["user_id"], {"order_id": state["entity_order_id"]})
        call = {"tool": "query_order", "args": {"order_id": state["entity_order_id"]}, "ok": r.success}
        if r.success:
            d = r.data or {}
            answer = (
                f"订单 {d['order_id']}：状态 {d['status']}，商品 {d['product_id']}，"
                f"金额 {d['amount']} 元，下单时间 {d['created_at'][:16].replace('T', ' ')}。"
            )
            return {
                "route": Route.ORDER_TOOL,
                "tool_calls": [call],
                "tool_results": [r.data or {}],
                "final_answer": answer,
                "active_order_id": d["order_id"],
            }
        err_msg = r.error["message"] if r.error else "查询失败"
        return {"route": Route.ORDER_TOOL, "tool_calls": [call], "tool_results": [r.error or {}],
                "final_answer": f"查询订单失败：{err_msg}"}

    def node_logistics_tool(state: AgentState) -> dict[str, Any]:
        db: Session = state["db"]
        order_id = state.get("entity_order_id") or state.get("active_order_id")
        if not order_id:
            return {"route": Route.LOGISTICS_TOOL, "tool_calls": [], "final_answer": "请问要查询哪个订单的物流？"}
        r = run_tool("query_logistics", db, state["user_id"], {"order_id": order_id})
        call = {"tool": "query_logistics", "args": {"order_id": order_id}, "ok": r.success}
        if r.success:
            d = r.data or {}
            answer = (
                f"订单 {d['order_id']} 的物流：{d['carrier']} {d['tracking_number']}，"
                f"当前状态「{d['status']}」，最新位置 {d['current_location']}。"
            )
            return {"route": Route.LOGISTICS_TOOL, "tool_calls": [call], "tool_results": [d],
                    "final_answer": answer, "active_order_id": d["order_id"]}
        if r.error and r.error["type"] == "NOT_FOUND":
            return {"route": Route.LOGISTICS_TOOL, "tool_calls": [call], "tool_results": [r.error],
                    "final_answer": f"订单 {order_id} 暂无物流记录（可能尚未发货）。", "active_order_id": order_id}
        err_msg = r.error["message"] if r.error else "查询失败"
        return {"route": Route.LOGISTICS_TOOL, "tool_calls": [call], "tool_results": [r.error or {}],
                "final_answer": f"物流查询失败：{err_msg}"}

    def node_ticket_tool(state: AgentState) -> dict[str, Any]:
        db: Session = state["db"]
        r = run_tool("query_ticket", db, state["user_id"], {"ticket_id": state["entity_ticket_id"]})
        call = {"tool": "query_ticket", "args": {"ticket_id": state["entity_ticket_id"]}, "ok": r.success}
        if r.success:
            d = r.data or {}
            answer = (
                f"工单 {d['ticket_id']}：{d['title']}（{d['category']}），"
                f"状态 {d['status']}，优先级 {d['priority']}。"
            )
            return {"route": Route.TICKET_TOOL, "tool_calls": [call], "tool_results": [d], "final_answer": answer,
                    "active_ticket_id": d["ticket_id"]}
        err_msg = r.error["message"] if r.error else "查询失败"
        return {"route": Route.TICKET_TOOL, "tool_calls": [call], "tool_results": [r.error or {}],
                "final_answer": f"查询工单失败：{err_msg}"}

    def node_after_sales(state: AgentState) -> dict[str, Any]:
        db: Session = state["db"]
        order_id = state.get("entity_order_id") or state.get("active_order_id")
        if not order_id:
            return {"route": Route.AFTER_SALES, "tool_calls": [],
                    "final_answer": "请问是哪个订单需要售后？您可以提供订单号（如 A10001）。"}
        req_type = guess_after_sales_type(state["user_query"])
        r = tools.require("query_order").execute(db, state["user_id"], {"order_id": order_id})
        call_q = {"tool": "query_order", "args": {"order_id": order_id}, "ok": r.success}
        if not r.success:
            err_msg = r.error["message"] if r.error else "处理失败"
            return {"route": Route.AFTER_SALES, "tool_calls": [call_q], "tool_results": [r.error or {}],
                    "final_answer": f"无法处理售后：{err_msg}"}
        # 权限已由工具层校验；重新取 ORM 对象供确定性资格判定
        from app.services.orders import get_order as svc_get_order

        order = svc_get_order(db, order_id, state["user_id"])
        elig = evaluate_after_sales(order, req_type, utcnow())
        policy = rag.answer(db, _POLICY_QUERY[req_type])
        policy_text = "（详见售后政策）" if policy.abstained else ""
        det = elig.details
        if elig.eligible:
            pending = make_pending(
                "CREATE_AFTER_SALES_TICKET",
                {
                    "order_id": order_id,
                    "category": req_type,
                    "title": f"{order.product_id} 售后申请（{req_type}）",
                    "description": state["user_query"][:500],
                    "eligibility_reason": elig.reason_code,
                },
                pending_id=f"pa-{uuid4().hex[:16]}",
            )
            window = det.get("allowed_days")
            answer = (
                f"已核对订单 {order_id}（签收 {det.get('delivered_days')} 天，"
                f"{req_type} 申请期为 {window} 天内）{policy_text}，符合申请条件。"
                "需要我为您创建售后工单吗？（回复【确认】即可）"
            )
            return {"route": Route.AFTER_SALES, "tool_calls": [call_q], "tool_results": [r.data or {}],
                    "eligibility": {"eligible": True, "reason_code": elig.reason_code, "policy_rule": elig.policy_rule},
                    "rag_sources": policy.sources, "rag_abstained": policy.abstained,
                    "pending_action": pending, "final_answer": answer, "active_order_id": order_id}
        dd, ad = det.get("delivered_days"), det.get("allowed_days")
        reason_map = {
            "BEYOND_RETURN_WINDOW": f"很抱歉，订单 {order_id} 签收已 {dd} 天，超出 {ad} 天退货期",
            "BEYOND_EXCHANGE_WINDOW": f"很抱歉，订单 {order_id} 签收已 {dd} 天，超出 {ad} 天换货期",
            "BEYOND_WARRANTY": f"很抱歉，订单 {order_id} 已超出保修期（{ad} 天）",
            "ORDER_NOT_DELIVERED": f"订单 {order_id} 当前状态为 {det.get('order_status')}，需签收后才能申请售后",
        }
        default_reason = f"很抱歉，订单 {order_id} 不符合申请条件（{elig.reason_code}）"
        answer = reason_map.get(elig.reason_code, default_reason) + "。"
        return {"route": Route.AFTER_SALES, "tool_calls": [call_q], "tool_results": [r.data or {}],
                "eligibility": {"eligible": False, "reason_code": elig.reason_code, "policy_rule": elig.policy_rule},
                "rag_sources": policy.sources, "rag_abstained": policy.abstained,
                "final_answer": answer, "active_order_id": order_id}

    def node_execute_confirmed(state: AgentState) -> dict[str, Any]:
        """用户确认后执行：REVALIDATE（权限+资格重新校验）→ 幂等建单。"""
        db: Session = state["db"]
        pending = state.get("pending_action") or {}
        payload: dict[str, Any] = dict(pending.get("payload", {}))
        order_id = payload.get("order_id")
        category = payload.get("category", "REPAIR")
        if not order_id:
            return {"route": Route.EXECUTE_CONFIRMED, "final_answer": "申请信息已失效，请重新发起售后申请。"}
        # REVALIDATE 1：权限
        rq = run_tool("query_order", db, state["user_id"], {"order_id": order_id})
        if not rq.success:
            err_msg = rq.error["message"] if rq.error else "权限校验失败"
            return {"route": Route.EXECUTE_CONFIRMED, "final_answer": f"重新核验未通过：{err_msg}"}
        # REVALIDATE 2：资格（不用缓存判断）
        from app.services.orders import get_order as svc_get_order

        order = svc_get_order(db, order_id, state["user_id"])
        elig = evaluate_after_sales(order, category, utcnow())
        if not elig.eligible:
            return {"route": Route.EXECUTE_CONFIRMED, "pending_action": None,
                    "final_answer": f"重新核验后订单 {order_id} 已不满足申请条件（{elig.reason_code}），未创建工单。"}
        # 幂等建单：idempotency_key = pending_action_id
        rc = run_tool(
            "create_ticket",
            db,
            state["user_id"],
            {
                "order_id": order_id,
                "category": category,
                "title": payload.get("title", f"{order_id} 售后申请"),
                "description": payload.get("description", ""),
                "idempotency_key": str(pending.get("id") or f"pa-{uuid4().hex[:16]}"),
            },
        )
        call_c = {"tool": "create_ticket", "args": {"order_id": order_id, "category": category}, "ok": rc.success}
        if rc.success:
            d = rc.data or {}
            if rc.idempotent_replay:
                answer = f"工单 {d['ticket_id']} 已存在（本次为重复确认，未重复创建）。"
            else:
                answer = (
                    f"已为您创建售后工单 {d['ticket_id']}（{category}），客服将尽快处理，"
                    "可在【我的工单】查看进度。"
                )
            return {"route": Route.EXECUTE_CONFIRMED, "pending_action": None, "tool_calls": [call_c],
                    "tool_results": [d], "final_answer": answer, "active_order_id": order_id,
                    "active_ticket_id": d.get("ticket_id")}
        if rc.error and rc.error["type"] == "DUPLICATE":
            return {"route": Route.EXECUTE_CONFIRMED, "pending_action": None, "tool_calls": [call_c],
                    "tool_results": [rc.error], "final_answer": f"{rc.error['message']}，无需重复申请。"}
        return {"route": Route.EXECUTE_CONFIRMED, "tool_calls": [call_c], "tool_results": [rc.error or {}],
                "final_answer": f"创建工单失败：{rc.error['message']}" if rc.error else "创建失败"}

    def node_direct_llm(state: AgentState) -> dict[str, Any]:
        resp = llm.complete(
            "你是智能客服助手，简短友好地寒暄或回答一般问题，不承诺业务操作。",
            state["user_query"],
            max_tokens=300,
        )
        return {"route": Route.DIRECT_LLM, "final_answer": resp.content,
                "prompt_tokens": state.get("prompt_tokens", 0) + resp.usage.prompt_tokens,
                "completion_tokens": state.get("completion_tokens", 0) + resp.usage.completion_tokens}

    def node_clarify(state: AgentState) -> dict[str, Any]:
        orders: list[str] = state.get("user_order_ids", [])
        if state.get("ambiguous") and len(orders) > 1:
            answer = f"您有多个订单，请问指哪一个？{'、'.join(orders[:4])}"
        elif state.get("intent") == "TICKET_QUERY":
            answer = "请问要查询哪个工单？可提供工单号（如 T10001）。"
        else:
            answer = "请问是哪个订单？您可以提供订单号（如 A10001），或在【我的订单】查看。"
        return {"route": Route.CLARIFY, "final_answer": answer}

    def node_human(state: AgentState) -> dict[str, Any]:
        return {
            "route": Route.HUMAN,
            "final_answer": (
                "这个问题我需要转接人工客服为您处理（工作时间 9:00-21:00）。"
                "您也可以先描述具体问题，我会创建工单记录。"
            ),
        }

    def node_finalize(state: AgentState) -> dict[str, Any]:
        answer = state.get("final_answer") or "抱歉，暂时无法处理您的请求，请稍后再试或转人工。"
        return {"final_answer": answer, "steps_used": state.get("steps_used", 0) + 1}

    # ---------- 装配 ----------

    b = StateGraph(AgentState)
    b.add_node("guardrail", node_guardrail)
    b.add_node("check_pending", check_pending)
    b.add_node("classify", classify)
    b.add_node("resolve_entity", resolve_entity)
    b.add_node("rag", node_rag)
    b.add_node("order_tool", node_order_tool)
    b.add_node("logistics_tool", node_logistics_tool)
    b.add_node("ticket_tool", node_ticket_tool)
    b.add_node("after_sales", node_after_sales)
    b.add_node("execute_confirmed", node_execute_confirmed)
    b.add_node("direct_llm", node_direct_llm)
    b.add_node("clarify", node_clarify)
    b.add_node("human", node_human)
    b.add_node("finalize", node_finalize)

    b.add_edge(START, "guardrail")
    b.add_conditional_edges(
        "guardrail",
        lambda s_: "finalize" if s_.get("error_type", "").startswith("INJECTION_FLAGGED") else "check_pending",
        {"finalize": "finalize", "check_pending": "check_pending"},
    )
    b.add_conditional_edges(
        "check_pending",
        lambda s: "finalize" if s.get("confirmation") == "NO" else "classify",
        {"finalize": "finalize", "classify": "classify"},
    )
    b.add_edge("classify", "resolve_entity")
    b.add_conditional_edges("resolve_entity", route_after_entity, {
        "rag": "rag", "order_tool": "order_tool", "logistics_tool": "logistics_tool",
        "ticket_tool": "ticket_tool", "after_sales": "after_sales", "execute_confirmed": "execute_confirmed",
        "direct_llm": "direct_llm", "clarify": "clarify", "human": "human",
    })
    for leaf in ("rag", "order_tool", "logistics_tool", "ticket_tool", "after_sales",
                 "execute_confirmed", "direct_llm", "clarify", "human"):
        b.add_edge(leaf, "finalize")
    b.add_edge("finalize", END)

    return b.compile()
