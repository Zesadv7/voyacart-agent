"""买家公开投影：先完整审核再持久化；执行输入始终由调用者另外保管。"""
from __future__ import annotations

import json
import math
from typing import Any

from app.infrastructure.security.output_guard import REDACTED, audit_output, is_secret_key, safe_url

# 协议/业务身份不受“正文内部工具名”替换影响，仍拒绝显式凭据模式。
_PROTOCOL = frozenset("id runId threadId messageId toolCallId toolCallName toolName tool role type name status kind source code contentHash content_hash snapshot_hash request_hash sha256 version product_id sku_id default_sku_id canonical_product_id buyer_id session_id operation_id confirmation_id order_id request_id interruptId".split())
_PRODUCT = frozenset("product_id title brand category price_major currency source_price_major source_currency skus landed_price material_tags ships_to highlights default_sku_id canonical_product_id weight_kg dimensions_cm origin_country source_platform image_url image_kind sku_id spec inventory stock available_stock score description updated_at image_alt data_provenance rating_summary rating_is_live source_locale source_language external_product_id".split())
_SKU = frozenset("sku_id spec price_major currency stock inventory available_stock weight_kg dimensions_cm".split())
_ARGUMENTS = {
    "product_search_tool": "normalized_query category ship_to top_k price_max_major target_currency excluded_material_tags required_material_tags product_id sku_id",
    "category_insight_tool": "question top_k",
    "web_search_tool": "query max_results top_k",
    "task_dispatch": "subagent_type description",
    "load_agent_skill_tool": "skill_id version",
    "lookup_strategy_memory_tool": "query scope",
    "remember_preference_tool": "kind statement",
    "forget_preference_tool": "statement memory_id expected_version",
    "update_preference_tool": "previous_statement kind statement memory_id expected_version",
    "create_order_tool": "items shipping_address product_id sku_id quantity",
    "query_order_tool": "order_id",
    "cancel_order_tool": "order_id reason",
    "show_shopping_form": "title questions context description",
}
_STATE = frozenset("products shoppingForm confirmations toolApprovals skillUsages searchCompleted status progress activities contextStatistics resumeDestination".split())
_ACTIVITY = frozenset("id runId kind label status sequence startedAt finishedAt source toolCallId toolName content inputSummary resultSummary truncated".split())
_ERROR = "本次执行未完成，请重试或查看已保存的结果。"


def secret_text(text: Any) -> str:
    """技术标签保留工具名，仅清除凭据与危险 URL。"""
    return audit_output(text if isinstance(text, str) else "", redact_tools=False)[1]


def safe_text(text: Any) -> str:
    return audit_output(text if isinstance(text, str) else "")[1]


def public_value(value: Any, *, _key: str = "", _depth: int = 0) -> Any:
    """复制 JSON 数据，去除结构化凭据；不将普通买家地址误当秘密。"""
    if _depth > 24:
        return None
    if isinstance(value, str):
        if _key.lower().endswith(("url", "uri")):
            return safe_url(value)
        return audit_output(value, redact_tools=_key not in _PROTOCOL)[1]
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [public_value(item, _key=_key, _depth=_depth + 1) for item in value]
    if isinstance(value, dict):
        return {safe_text(str(key)): REDACTED if is_secret_key(str(key)) else public_value(item, _key=str(key), _depth=_depth + 1)
                for key, item in value.items()}
    return None


def _select(value: Any, fields) -> dict:
    return public_value({key: value[key] for key in sorted(fields) if key in value}) if isinstance(value, dict) else {}


def _object(value: Any) -> dict | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError, RecursionError):
            return None
    return value if isinstance(value, dict) else None


def public_tool_arguments(name: str, value: Any) -> dict:
    data = _object(value)
    fields = _ARGUMENTS.get(name)
    if not data or fields is None:
        return {}
    result = _select(data, fields.split())
    if "items" in result:
        result["items"] = [_select(item, ("product_id", "sku_id", "quantity")) for item in result["items"]] if isinstance(result["items"], list) else []
    if "shipping_address" in result:
        result["shipping_address"] = _select(result["shipping_address"], "recipient_name country state city address_line postal_code phone".split())
    return result


_PROVENANCE_SOURCES = frozenset({"esci", "synthetic", "catalog", "test-fixture", "constructed_fixture"})
_PROVENANCE_ARRAYS = {
    "synthetic": frozenset({"skus.price_major", "skus.stock", "ships_to", "price", "stock", "shipping"}),
    "derived": frozenset({"category", "material_tags", "origin_country"}),
}
_PROVENANCE_ENUMS = {
    "category_source": frozenset({"query-class-proxy-v1", "product-enrich-v1", "product-enrich-batch-v1"}),
    "category_method": frozenset({"train_exact_query_majority"}),
    "category_status": frozenset({"provisional_unreviewed"}),
    "material_status": frozenset({"unknown"}),
    "origin_country_method": frozenset({"source_locale_proxy"}),
    "source_text_processing": frozenset({"html_to_text; description_max_1000; bullet_max_200_and_5"}),
}


def public_product_provenance(value: Any) -> str | dict | None:
    """先选来源白名单，再经过 M2 审核；未知元数据及自称人工核验不公开。"""
    if isinstance(value, str):
        return public_value(value) if value in _PROVENANCE_SOURCES else None
    if (not isinstance(value, dict) or not isinstance(value.get("source"), str)
            or value["source"] not in _PROVENANCE_SOURCES
            or any(not isinstance(value.get(key), list) for key in _PROVENANCE_ARRAYS)):
        return None
    selected = {"source": value["source"]}
    for key, allowed in _PROVENANCE_ARRAYS.items():
        selected[key] = [entry for entry in value[key] if isinstance(entry, str) and entry in allowed]
    for key, allowed in _PROVENANCE_ENUMS.items():
        entry = value.get(key)
        if isinstance(entry, str) and entry in allowed:
            selected[key] = entry
    return public_value(selected)


def public_products(value: Any) -> list:
    if not isinstance(value, list):
        return []
    products = []
    for item in value:
        card = _select(item, _PRODUCT - {"data_provenance"})
        provenance = public_product_provenance(item.get("data_provenance")) if isinstance(item, dict) else None
        if provenance is not None:
            card["data_provenance"] = provenance
        if "skus" in card:
            card["skus"] = [_select(sku, _SKU) for sku in card["skus"]] if isinstance(card["skus"], list) else []
        products.append(card)
    return products


def public_confirmation(value: Any) -> dict:
    card = _select(value, "confirmation_id operation_id action buyer_id session_id snapshot_hash expires_at expired status payload result created_at updated_at resolved_at".split())
    if "payload" in card:
        payload = _select(card["payload"], "items shipping_address total_amount_minor currency amount_scope order_kind order_id reason order_status".split())
        if "items" in payload:
            payload["items"] = [_select(item, "product_id sku_id title unit_price_minor currency quantity".split()) for item in payload["items"]] if isinstance(payload["items"], list) else []
        if "shipping_address" in payload:
            payload["shipping_address"] = _select(payload["shipping_address"], "recipient_name country state city address_line postal_code phone".split())
        card["payload"] = payload
    if card.get("result") is not None:
        card["result"] = _public_order(card["result"])
    return card


def _public_order(value: Any) -> dict:
    order = _select(value, "order_id buyer_id session_id status total_amount_major total_amount_minor currency shipping_address lines created_at cancel_reason amount_scope order_kind".split())
    if isinstance(order.get("lines"), list):
        order["lines"] = [_select(item, "product_id sku_id title unit_price_major unit_price_minor currency quantity".split()) for item in order["lines"]]
    return order


def public_tool_result(name: str, content: Any) -> str:
    data = _object(content)
    if name in {"product_search_tool", "create_order_tool", "cancel_order_tool", "query_order_tool"} and data is None:
        return json.dumps({"status": "unavailable"}, ensure_ascii=False)
    if name == "product_search_tool" and data is not None:
        # 分页和失败标记必须保留，否则不完整结果会被错误声称完整。
        result = _select(data, "success ok status total total_candidates offset next_offset incomplete next_field_offset fragment_product_id result_ref recall_strategy rerank_applied retrieval_variant vector_available".split())
        if "hits" in data:
            result["hits"] = public_products(data["hits"]) if isinstance(data["hits"], list) else None
        if data.get("error"):
            result["error"] = _ERROR
        if "fragment" in data:
            result["incomplete"] = True
        if data.get("reranker_error"):
            result["reranker_error"] = "RERANK_UNAVAILABLE"
        diagnostics = data.get("retrieval_diagnostics")
        if isinstance(diagnostics, dict):
            result["retrieval_diagnostics"] = _select(diagnostics, "filter_mode eligible_products lexical_weight vector_weight candidate_limit vector_source".split())
            if diagnostics.get("reranker_error"):
                result["retrieval_diagnostics"]["reranker_error"] = "RERANK_UNAVAILABLE"
        return json.dumps(result, ensure_ascii=False)
    if name in {"create_order_tool", "cancel_order_tool", "query_order_tool"} and data is not None:
        result = _select(data, "confirmation_required success ok status".split())
        if "confirmation" in data:
            result["confirmation"] = public_confirmation(data["confirmation"])
        if "order" in data:
            result["order"] = _public_order(data["order"])
        elif "order_id" in data:
            result.update(_public_order(data))
        if data.get("error"):
            result["error"] = _ERROR
        return json.dumps(result, ensure_ascii=False)
    # Skill、专家回答、异常、未知工具与 DATA 块不公开原文或任意媒体元数据。
    return json.dumps({"status": "unavailable", "notice": "工具内容不在公开展示范围内。"}, ensure_ascii=False)


def public_activity(value: Any) -> dict:
    kind = value.get("kind") if isinstance(value, dict) else None
    common = _ACTIVITY - {"content", "truncated", "inputSummary", "resultSummary", "toolCallId", "toolName"}
    fields = common | {"inputSummary", "resultSummary", "toolCallId", "toolName"} if kind == "tool" else common
    result = _select(value, fields)
    if kind == "thinking":
        result["content"] = ""
    elif kind != "tool":
        result.update(kind="unknown", label="执行步骤")
    return result


def public_state(value: Any) -> dict:
    state = _select(value, _STATE)
    if "products" in state:
        state["products"] = public_products(state["products"])
    if "confirmations" in state:
        state["confirmations"] = [public_confirmation(item) for item in state["confirmations"]] if isinstance(state["confirmations"], list) else []
    if "skillUsages" in state:
        state["skillUsages"] = [_select(item, "id version title contentHash status toolCallId source error".split()) for item in state["skillUsages"]] if isinstance(state["skillUsages"], list) else []
    if "activities" in state:
        state["activities"] = [public_activity(item) for item in state["activities"] if isinstance(item, dict)] if isinstance(state["activities"], list) else []
    if "toolApprovals" in state:
        approvals = []
        for item in state["toolApprovals"] if isinstance(state["toolApprovals"], list) else []:
            card = _select(item, "id tool label arguments toolCallId".split())
            card["arguments"] = public_tool_arguments(card.get("tool", ""), item.get("arguments"))
            approvals.append(card)
        state["toolApprovals"] = approvals
    return state


def public_messages(messages: Any) -> list:
    result = []
    for item in messages if isinstance(messages, list) else []:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant", "system", "tool"}:
            continue
        message = _select(item, "id role name content runId toolCallId".split())
        if message.get("role") in {"system", "tool"}:
            message["content"] = ""
        result.append(message)
    # 老版本多个 TEXT_BLOCK 可能把同一凭据拆成相邻消息。按连续角色联合审核，
    # 一旦联合命中就隐藏整组，不把脱敏后字符重新切片造出新的泄露边界。
    group = []
    for message in [*result, {"role": None}]:
        if group and message.get("role") != group[-1].get("role"):
            joined = "".join(item.get("content", "") for item in group if isinstance(item.get("content"), str))
            if audit_output(joined)[0] is False:
                for item in group:
                    item["content"] = REDACTED
            group = []
        group.append(message)
    return result


def public_custom(name: str, value: Any) -> Any:
    if name == "activity.upsert":
        return public_activity(value)
    if name == "error":
        return {"message": _ERROR}
    fields = {
        "agent.dispatch": "agent task_id subagent_type status",
        "runtime.degraded": "component fallback",
        "cache.hit": "similarity score source",
        "model.fallback": "from to reason code",
        "context.compressed": "strategy before_tokens after_tokens tokens_before tokens_after summary_length context_messages checkpoint_id messages_before messages_after saved_tokens compression_count estimated_tokens budget_limit active_tokens token_limit before after truncated source policy_version reason archived_results summary_changed elapsed_ms status",
        "usage.summary": "run_id total input_tokens output_tokens cached_tokens total_tokens cost_usd requests calls models budget status elapsed_ms model_calls observed_input_tokens observed_output_tokens unknown_usage_calls active_attempts_at_finish usage_complete ttft_p95_ms model_elapsed_ms tool_calls tool_errors fallbacks unknown_cost_calls",
        "skill.preload": "id version title contentHash status toolCallId source error",
    }.get(name)
    if fields:
        result = _select(value, fields.split())
        if name == "model.fallback" and "reason" in result:
            result["reason"] = "已切换可用模型。"
        return result
    if name == "a2ui":
        return _select(value, "version createSurface updateComponents updateDataModel deleteSurface surfaceUpdate beginRendering dataModelUpdate".split())
    return {}


def public_agui_event(event: dict, tool_names: dict | None = None) -> dict:
    """保留事件类型、ID 和序号；任何未完成增量都不得成为公开日志正文。"""
    kind = event.get("type")
    result = _select(event, "type timestamp threadId runId messageId toolCallId toolCallName parentMessageId role code name outcome".split())
    if kind == "TEXT_MESSAGE_CONTENT":
        result["delta"] = ""
    elif kind == "TOOL_CALL_ARGS":
        result["delta"] = json.dumps(public_tool_arguments((tool_names or {}).get(event.get("toolCallId"), ""), event.get("delta")), ensure_ascii=False)
    elif kind == "TOOL_CALL_RESULT":
        result["content"] = public_tool_result((tool_names or {}).get(event.get("toolCallId"), ""), event.get("content"))
    elif kind == "MESSAGES_SNAPSHOT":
        result["messages"] = public_messages(event.get("messages"))
    elif kind == "STATE_SNAPSHOT":
        result["snapshot"] = public_state(event.get("snapshot"))
    elif kind == "CUSTOM":
        result["value"] = public_custom(str(event.get("name", "")), event.get("value"))
    elif kind == "RUN_ERROR":
        result["message"] = _ERROR if not isinstance(event.get("message"), str) else safe_text(event["message"])
    return result


def public_trade_event(event: Any) -> dict | None:
    if hasattr(event, "to_dict"):
        event = event.to_dict()
    if not isinstance(event, dict) or event.get("type") == "token.delta":
        return None
    kind, payload = event.get("type"), _object(event.get("payload")) or {}
    result = _select(event, "shopping_session_id type occurred_at correlation".split())
    if kind == "final.result":
        output = _select(payload, ("text", "budget_fallback"))
    elif kind == "tool.invoke":
        output = {"tool": public_value(payload.get("tool"), _key="tool"), "args": public_tool_arguments(payload.get("tool", ""), payload.get("args"))}
    elif kind == "tool.result":
        output = {"tool": public_value(payload.get("tool"), _key="tool"), **json.loads(public_tool_result(payload.get("tool", ""), payload))}
    elif kind in {"confirmation.required", "confirmation.resolved"}:
        output = {"confirmation": public_confirmation(payload.get("confirmation"))}
    elif kind == "plan.update":
        output = {"tasks": [_select(task, ("id", "subject", "state")) for task in payload.get("tasks", []) if isinstance(task, dict)]}
    elif kind == "ui.surface":
        output = _select(payload, ("form",))
    elif kind in {"task.queued", "task.started"}:
        output = _select(payload, ("task_id", "request_id", "status"))
    else:
        output = public_custom(str(kind), payload)
    result["payload"] = output
    return result
