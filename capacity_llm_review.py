"""容量配置的 LLM 二次评审。

`capacity_gpt55.analyze_capacity_with_bill_method` 用数学公式给出候选清单和
"sweet spot" 最优解，本模块在数学结果之上再加一层 **大模型综合判断**：
把候选清单、用电特征、测算假设、用户偏好一起给到 LLM，让它输出推荐方案
和详细理由。

设计原则：
- 不修改 `capacity_gpt55.py` 的纯数学逻辑，本模块在外层做编排。
- LLM 不可用 / 返回格式异常 时优雅回退，调用方依然能拿到数值最优解。
- 输出结构稳定（JSON），供后端 API 和前端直接消费。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)


_REVIEW_SYSTEM_PROMPT = (
    "你是中国市场储能项目资深投资顾问，有 10 年以上工商业 / 工业储能投资经验。"
    "你擅长在多个容量方案中权衡投资回报、风险与现场可行性，给出可落地的推荐。"
    "你必须严格按要求的 JSON 格式作答，不要 Markdown 代码块、不要解释 JSON 之外的内容。"
)


def _safe_round(value: Any, digits: int = 2) -> Any:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return value
    if v != v or v in (float("inf"), float("-inf")):  # NaN / Inf
        return None
    return round(v, digits)


def _candidate_brief(record: dict[str, Any], idx: int) -> dict[str, Any]:
    """从完整候选记录里挑出 LLM 关心的关键字段，避免上下文过长。"""
    return {
        "idx": idx,
        "power_kw": _safe_round(record.get("inverter_power_kw"), 0),
        "capacity_kwh": _safe_round(record.get("battery_capacity_kwh"), 0),
        "duration_h": _safe_round(record.get("duration_hours"), 2),
        "investment_wan": _safe_round((record.get("investment_yuan") or 0) / 10000, 1),
        "annual_revenue_wan": _safe_round((record.get("annual_revenue_yuan") or 0) / 10000, 2),
        "revenue_per_kwh": _safe_round(record.get("annual_revenue_per_kwh"), 1),
        "marginal_per_kwh": _safe_round(record.get("marginal_revenue_per_kwh"), 1),
        "payback_years": _safe_round(record.get("payback_years"), 2),
        "irr_percent": _safe_round(record.get("irr_percent"), 2),
        "npv_wan": _safe_round((record.get("npv_yuan") or 0) / 10000, 1),
        "demand_revenue_wan": _safe_round((record.get("demand_revenue_yuan") or 0) / 10000, 2),
        "arbitrage_revenue_wan": _safe_round((record.get("arbitrage_revenue_yuan") or 0) / 10000, 2),
        "utilization": _safe_round(record.get("utilization_ratio"), 3),
        "is_numerical_best": bool(record.get("is_best")),
    }


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(text: str) -> Optional[dict]:
    """尽力从 LLM 输出里抠出 JSON 对象。"""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        # 干掉 markdown 代码围栏（```json ... ```）
        candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # 兜底：找第一个 { 到最后一个 } 的子串
    m = _JSON_BLOCK_RE.search(candidate)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def build_review_prompt(
    capacity_result: dict[str, Any],
    user_preference: str = "",
) -> str:
    """构造给 LLM 的容量评审 prompt。"""
    results = capacity_result.get("results") or []
    numerical_best = capacity_result.get("best") or {}
    load_profile = capacity_result.get("load_profile") or {}
    assumptions = capacity_result.get("assumptions") or {}

    candidates_brief = [_candidate_brief(r, i) for i, r in enumerate(results)]
    numerical_best_idx = next(
        (c["idx"] for c in candidates_brief if c.get("is_numerical_best")),
        None,
    )

    return f"""我已经按"逐月账单峰谷套利 + 需量兑现 + 边际收益拐点"测算了下面这些储能容量方案，请你帮我从中选出最优解并解释为什么。

## 项目用电画像
- 月均总电量: {_safe_round(load_profile.get('total_kwh'), 0)} kWh
- 日均尖峰+高峰电量: {_safe_round(load_profile.get('daily_peak_high_kwh'), 0)} kWh
- 日均谷段电量: {_safe_round(load_profile.get('daily_valley_kwh'), 0)} kWh
- 最大需量: {_safe_round(load_profile.get('max_demand_kw'), 0)} kW
- 需量电价: {_safe_round(load_profile.get('demand_price_yuan_per_kw_month'), 2)} 元/kW·月
- 分时电价来源: {load_profile.get('price_source') or '配置默认'}
- 账单覆盖月份数: {load_profile.get('month_count')}（折年系数 {_safe_round(load_profile.get('annualization_factor'), 2)}）

## 测算假设
- 系统效率: {assumptions.get('system_efficiency')}
- 投资单价: {assumptions.get('investment_unit_cost_yuan_per_kwh')} 元/kWh
- 需量兑现率: {assumptions.get('demand_realization_rate')}
- 需量功率上限比: {assumptions.get('demand_power_cap_ratio')}

## 候选方案（金额单位：万元，按容量从小到大排列）
{json.dumps(candidates_brief, ensure_ascii=False, indent=2)}

数值模型当前推荐的方案索引为 **{numerical_best_idx}**（标记为 `is_numerical_best: true`），评分公式为 `年化每kWh收益×0.6 + 边际每kWh收益×0.4 - 回收期×8`。

## 用户偏好 / 现场约束
{user_preference.strip() if user_preference else '无特别偏好。请按"投资回报最稳健 + 现场可落地"综合判断。'}

## 你的任务
1. 综合 IRR、回收期、NPV、边际收益拐点、投资规模、利用率，选出 1 个最优方案。
2. 如果你的选择与数值模型一致，请说明你认同的理由；如果不一致，请清楚说明你为什么覆盖。
3. 给出 1–2 个备选方案，注明适用场景（例如"客户现金流紧张" / "想要更激进的削峰"等）。
4. 提示 1–2 条需要现场尽调或下一步确认的事项。

## 输出格式（必须严格遵守，只输出一个 JSON 对象，不要任何额外文字）
{{
  "chosen_index": <候选方案的 idx 整数>,
  "agrees_with_numerical": <true / false>,
  "reasoning": "<200 字以内说明为什么选这个方案>",
  "key_metrics": ["<亮点1>", "<亮点2>"],
  "risks": ["<风险1>", "<风险2>"],
  "comparison": "<150 字以内对比其他候选方案的优劣>",
  "backup_recommendations": [
    {{"index": <int>, "scenario": "<什么情况下选它，50 字以内>"}}
  ],
  "next_steps": ["<现场尽调 / 进一步确认事项>"]
}}
"""


def llm_review_capacity(
    capacity_result: dict[str, Any],
    llm_client: Any,
    user_preference: str = "",
    *,
    temperature: float = 0.3,
    max_tokens: int = 1500,
) -> Optional[dict[str, Any]]:
    """对 `analyze_capacity_with_bill_method` 的结果做 LLM 评审。

    Parameters
    ----------
    capacity_result : dict
        `analyze_capacity_with_bill_method` 返回的完整字典。
    llm_client : Any
        实现了 `.chat(messages, temperature, max_tokens)` 的 LLM 客户端。
    user_preference : str, optional
        用户额外约束或偏好（例如"预算 ≤ 300 万"、"优先回收期"）。

    Returns
    -------
    dict
        成功时返回带 `chosen_config` 的字典；如果 LLM 不可用、调用失败或返回格式
        异常，返回带 `error` 字段的字典，调用方应当回退到数值最优解。
    None
        capacity_result 没有候选方案时直接返回 None。
    """
    if not capacity_result:
        return None
    results = capacity_result.get("results") or []
    if not results:
        return None
    if llm_client is None or not getattr(llm_client, "available", False):
        return {"ok": False, "error": "LLM 客户端不可用，已回退到数值模型推荐", "fallback": True}

    prompt = build_review_prompt(capacity_result, user_preference=user_preference)

    try:
        reply = llm_client.chat(
            messages=[
                {"role": "system", "content": _REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        logger.exception("LLM 容量评审调用失败")
        return {"ok": False, "error": f"LLM 调用失败：{exc}", "fallback": True}

    parsed = _extract_json(reply or "")
    if not parsed or "chosen_index" not in parsed:
        return {
            "ok": False,
            "error": "LLM 返回格式不符合预期",
            "raw_reply": (reply or "")[:600],
            "fallback": True,
        }

    try:
        idx = int(parsed["chosen_index"])
    except (TypeError, ValueError):
        return {"ok": False, "error": "chosen_index 不是整数", "raw_reply": reply[:600], "fallback": True}
    if idx < 0 or idx >= len(results):
        return {
            "ok": False,
            "error": f"LLM 选择的索引 {idx} 越界（共 {len(results)} 个候选）",
            "raw_reply": reply[:600],
            "fallback": True,
        }

    chosen_config = results[idx]
    numerical_best_idx = next(
        (i for i, r in enumerate(results) if r.get("is_best")), None
    )

    return {
        "ok": True,
        "chosen_index": idx,
        "chosen_config": chosen_config,
        "agrees_with_numerical": idx == numerical_best_idx,
        "numerical_best_index": numerical_best_idx,
        "reasoning": str(parsed.get("reasoning") or ""),
        "key_metrics": list(parsed.get("key_metrics") or []),
        "risks": list(parsed.get("risks") or []),
        "comparison": str(parsed.get("comparison") or ""),
        "backup_recommendations": [
            {
                "index": int(item.get("index", -1)) if str(item.get("index", "")).isdigit() else -1,
                "scenario": str(item.get("scenario") or ""),
            }
            for item in (parsed.get("backup_recommendations") or [])
            if isinstance(item, dict)
        ],
        "next_steps": [str(s) for s in (parsed.get("next_steps") or [])],
        "user_preference": user_preference,
        "model": getattr(getattr(llm_client, "config", None), "model", None),
    }
