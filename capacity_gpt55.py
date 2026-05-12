"""GPT-5.5 style storage capacity sizing.

This module keeps the web page and tool-calling Agent on the same sizing
method: monthly bill data first, tariff spread second, then marginal benefit.
"""

from __future__ import annotations

import calendar
import math
import re
from typing import Any

import pandas as pd

from config import ElectricityRateConfig, StorageConfig
from storage_optimizer import OptimalConfig


PERIOD_PRICE_COLUMNS = {
    "peak": ("尖峰电价(元/kWh)", "尖峰电价", "尖电价", "尖段电价", "peak_price"),
    "high": ("高峰电价(元/kWh)", "高峰电价", "峰电价", "峰段电价", "high_price"),
    "flat": ("平段电价(元/kWh)", "平段电价", "平电价", "flat_price"),
    "valley": ("谷段电价(元/kWh)", "谷段电价", "谷电价", "低谷电价", "valley_price"),
}

PERIOD_KWH_COLUMNS = {
    "peak": ("尖峰电量(kWh)", "尖峰电量", "尖电量", "peak_kwh"),
    "high": ("高峰电量(kWh)", "高峰电量", "峰电量", "high_kwh"),
    "flat": ("平段电量(kWh)", "平段电量", "平电量", "flat_kwh"),
    "valley": ("谷段电量(kWh)", "谷段电量", "谷电量", "valley_kwh"),
}

DEMAND_PRICE_COLUMNS = ("需量电价(元/kW·月)", "需量电价", "需量单价", "demand_price")
DEFAULT_NA_CELL_COST_YUAN_PER_WH = 0.52
DEFAULT_STORAGE_SYSTEM_COST_YUAN_PER_KWH = 560.0
DEFAULT_STORAGE_COST_BASIS = (
    "按2025年国内市场均价口径估算：钠离子电芯约0.52元/Wh，"
    "2h储能系统按0.56元/Wh（560元/kWh）测算。"
)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        v = float(value)
    except Exception:
        return default
    return v if math.isfinite(v) else default


def _round(value: Any, digits: int = 2, default: float | None = 0.0) -> float | None:
    try:
        v = float(value)
    except Exception:
        return default
    if not math.isfinite(v):
        return default
    return round(v, digits)


def _row_float(row: pd.Series, columns: tuple[str, ...] | list[str], default: float = 0.0) -> float:
    for col in columns:
        if col in row.index:
            value = _to_float(row.get(col), default)
            if value:
                return value
    return default


def _normalize_price(value: Any, default: float) -> float:
    price = _to_float(value, 0.0)
    if price <= 0:
        return default
    # Some tariff files use fen/kWh or cent/kWh.
    if price > 5:
        price = price / 100
    return price


def _month_days(value: Any) -> int:
    text = str(value or "").strip()
    match = re.search(r"(20\d{2})\D{0,3}([01]?\d)", text)
    if not match:
        match = re.search(r"(20\d{2})([01]\d)", text)
    if not match:
        return 30
    year = int(match.group(1))
    month = max(1, min(12, int(match.group(2))))
    return calendar.monthrange(year, month)[1]


def _month_key(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"(20\d{2})\D{0,3}([01]?\d)", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"
    match = re.search(r"(20\d{2})([01]\d)", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"
    return text


def _period_prices(row: pd.Series, rate: ElectricityRateConfig) -> dict[str, float]:
    return {
        "peak": _normalize_price(_row_float(row, PERIOD_PRICE_COLUMNS["peak"]), rate.peak_price),
        "high": _normalize_price(_row_float(row, PERIOD_PRICE_COLUMNS["high"]), rate.high_price),
        "flat": _normalize_price(_row_float(row, PERIOD_PRICE_COLUMNS["flat"]), rate.flat_price),
        "valley": _normalize_price(_row_float(row, PERIOD_PRICE_COLUMNS["valley"]), rate.valley_price),
    }


def _has_tou_price(df: pd.DataFrame) -> bool:
    for cols in PERIOD_PRICE_COLUMNS.values():
        for col in cols:
            if col in df.columns and pd.to_numeric(df[col], errors="coerce").fillna(0).gt(0).any():
                return True
    return False


def _estimate_demand_price(df: pd.DataFrame, rate: ElectricityRateConfig) -> float:
    explicit = []
    for col in DEMAND_PRICE_COLUMNS:
        if col in df.columns:
            values = pd.to_numeric(df[col], errors="coerce").dropna()
            explicit.extend(float(v) for v in values if 0 < float(v) <= 200)
    if explicit:
        explicit.sort()
        return round(explicit[len(explicit) // 2], 4)

    ratios: list[float] = []
    if "需量电费(元)" in df.columns and "最大需量(kW)" in df.columns:
        for _, row in df.iterrows():
            charge = _to_float(row.get("需量电费(元)"))
            demand = _to_float(row.get("最大需量(kW)"))
            if charge > 0 and demand > 0:
                ratio = charge / demand
                if 1 <= ratio <= 200:
                    ratios.append(ratio)
    if ratios:
        ratios.sort()
        return round(ratios[len(ratios) // 2], 4)
    return float(rate.demand_charge or 0)


def _irr(cash_flows: list[float], max_iter: int = 200, tol: float = 1e-7) -> float | None:
    if not cash_flows or not any(v < 0 for v in cash_flows) or not any(v > 0 for v in cash_flows):
        return None
    rate = 0.1
    for _ in range(max_iter):
        if rate <= -0.99:
            rate = -0.9
        npv = sum(cf / (1 + rate) ** i for i, cf in enumerate(cash_flows))
        dnpv = sum(-i * cf / (1 + rate) ** (i + 1) for i, cf in enumerate(cash_flows))
        if abs(dnpv) < 1e-12:
            break
        nxt = rate - npv / dnpv
        if not math.isfinite(nxt):
            break
        if abs(nxt - rate) < tol:
            return nxt
        rate = nxt
    return rate if math.isfinite(rate) else None


def _finance_metrics(investment: float, annual_revenue: float,
                     storage: StorageConfig) -> tuple[float | None, float | None]:
    if investment <= 0 or annual_revenue <= 0:
        return None, None
    years = max(1, int(getattr(storage, "project_life_years", 15) or 15))
    discount = float(getattr(storage, "discount_rate", 0.06) or 0.06)
    degradation = float(getattr(storage, "annual_degradation", 0.0) or 0.0)
    inflation = float(getattr(storage, "electricity_inflation", 0.0) or 0.0)
    cash_flows = [-investment]
    npv = -investment
    for year in range(1, years + 1):
        factor = (1 - degradation) ** (year - 1) * (1 + inflation) ** (year - 1)
        cash = annual_revenue * factor
        cash_flows.append(cash)
        npv += cash / (1 + discount) ** year
    return npv, _irr(cash_flows)


def _candidate_schemes(options: dict[str, Any], storage: StorageConfig) -> list[tuple[float, float]]:
    raw_schemes = options.get("candidate_schemes") or options.get("schemes") or []
    schemes: list[tuple[float, float]] = []
    if isinstance(raw_schemes, dict):
        raw_schemes = [raw_schemes]
    for item in raw_schemes:
        power = capacity = None
        if isinstance(item, dict):
            power = item.get("power_kw") or item.get("inverter_power_kw") or item.get("power")
            capacity = item.get("capacity_kwh") or item.get("battery_capacity_kwh") or item.get("capacity")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            power, capacity = item[0], item[1]
        power = _to_float(power)
        capacity = _to_float(capacity)
        if power > 0 and capacity > 0:
            schemes.append((power, capacity))

    raw_capacities = options.get("capacities_kwh") or options.get("capacities") or []
    if isinstance(raw_capacities, (int, float, str)):
        raw_capacities = [raw_capacities]
    raw_durations = options.get("durations_hours") or options.get("durations") or [2]
    if isinstance(raw_durations, (int, float, str)):
        raw_durations = [raw_durations]
    durations = [_to_float(v) for v in raw_durations]
    durations = [v for v in durations if v > 0]
    for capacity in raw_capacities:
        capacity = _to_float(capacity)
        if capacity <= 0:
            continue
        for duration in durations or [2]:
            schemes.append((capacity / duration, capacity))

    if not schemes:
        schemes = [
            (500, 1000), (1000, 2000), (1250, 2500), (1500, 3000),
            (2000, 4000), (2500, 5000), (3000, 6000), (3500, 7000),
        ]

    out: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    min_capacity = float(getattr(storage, "min_capacity_kwh", 0) or 0)
    max_capacity = float(getattr(storage, "max_capacity_kwh", 10**12) or 10**12)
    min_power = float(getattr(storage, "min_power_kw", 0) or 0)
    max_power = float(getattr(storage, "max_power_kw", 10**12) or 10**12)
    for power, capacity in schemes:
        capacity = max(min_capacity, min(max_capacity, float(capacity)))
        power = max(min_power, min(max_power, float(power)))
        key = (round(power, 4), round(capacity, 4))
        if capacity <= 0 or power <= 0 or key in seen:
            continue
        seen.add(key)
        out.append((power, capacity))
    return sorted(out, key=lambda item: (item[1], item[0]))


def _calculate_candidate(power_kw: float, capacity_kwh: float, df: pd.DataFrame,
                         rate: ElectricityRateConfig, storage: StorageConfig,
                         options: dict[str, Any], annualization_factor: float,
                         demand_price: float, max_demand_kw: float) -> dict[str, Any]:
    efficiency = _to_float(
        options.get("system_efficiency")
        or options.get("round_trip_efficiency")
        or options.get("efficiency"),
        0.86,
    )
    efficiency = min(1.0, max(0.01, efficiency))
    unit_cost = _to_float(
        options.get("investment_unit_cost_yuan_per_kwh")
        or options.get("unit_cost_yuan_per_kwh")
        or options.get("capex_yuan_per_kwh"),
        DEFAULT_STORAGE_SYSTEM_COST_YUAN_PER_KWH,
    )
    demand_realization = _to_float(options.get("demand_realization_rate"), 0.5)
    demand_realization = min(1.0, max(0.0, demand_realization))
    demand_power_cap_ratio = _to_float(options.get("demand_power_cap_ratio"), 0.75)
    demand_power_cap_ratio = min(1.0, max(0.0, demand_power_cap_ratio))

    discharge_kwh = 0.0
    charge_kwh = 0.0
    arbitrage = 0.0
    monthly_rows: list[dict[str, Any]] = []

    for _, row in df.iterrows():
        month = _month_key(row.get("月份", ""))
        days = _month_days(row.get("月份", ""))
        peak_kwh = _row_float(row, PERIOD_KWH_COLUMNS["peak"])
        high_kwh = _row_float(row, PERIOD_KWH_COLUMNS["high"])
        total_kwh = _to_float(row.get("总电量(kWh)"))
        high_load_kwh = peak_kwh + high_kwh
        if high_load_kwh <= 0 and total_kwh > 0:
            high_load_kwh = total_kwh * 0.4

        prices = _period_prices(row, rate)
        possible_discharge = capacity_kwh * days
        month_discharge = min(possible_discharge, max(0.0, high_load_kwh))
        remaining = month_discharge
        discharge_value = 0.0
        for period_kwh, price in sorted(
            [(peak_kwh, prices["peak"]), (high_kwh, prices["high"])],
            key=lambda item: item[1],
            reverse=True,
        ):
            use = min(max(0.0, period_kwh), remaining)
            if use <= 0:
                continue
            discharge_value += use * price
            remaining -= use
            if remaining <= 0:
                break

        month_charge = month_discharge / efficiency if month_discharge > 0 else 0.0
        charge_cost = month_charge * prices["valley"]
        month_arbitrage = discharge_value - charge_cost

        discharge_kwh += month_discharge
        charge_kwh += month_charge
        arbitrage += month_arbitrage
        monthly_rows.append({
            "month": month,
            "days": days,
            "high_load_kwh": _round(high_load_kwh, 2),
            "discharge_kwh": _round(month_discharge, 2),
            "charge_kwh": _round(month_charge, 2),
            "discharge_value_yuan": _round(discharge_value, 2),
            "charge_cost_yuan": _round(charge_cost, 2),
            "arbitrage_revenue_yuan": _round(month_arbitrage, 2),
            "peak_price_yuan_per_kwh": _round(prices["peak"], 4),
            "high_price_yuan_per_kwh": _round(prices["high"], 4),
            "valley_price_yuan_per_kwh": _round(prices["valley"], 4),
        })

    annual_arbitrage = arbitrage * annualization_factor
    annual_discharge = discharge_kwh * annualization_factor
    annual_charge = charge_kwh * annualization_factor
    demand_reduction_kw = min(power_kw, max_demand_kw * demand_power_cap_ratio) if max_demand_kw > 0 else 0.0
    demand_revenue = demand_reduction_kw * demand_price * 12 * demand_realization
    annual_revenue = annual_arbitrage + demand_revenue
    investment = capacity_kwh * unit_cost
    payback = investment / annual_revenue if annual_revenue > 0 else None
    npv, irr = _finance_metrics(investment, annual_revenue, storage)
    possible_annual_discharge = capacity_kwh * 365
    utilization_ratio = annual_discharge / possible_annual_discharge if possible_annual_discharge > 0 else 0.0

    duration = capacity_kwh / power_kw if power_kw > 0 else 0.0
    return {
        "name": f"{power_kw / 1000:g}MW / {capacity_kwh / 1000:g}MWh",
        "battery_capacity_kwh": _round(capacity_kwh, 2),
        "inverter_power_kw": _round(power_kw, 2),
        "duration_hours": _round(duration, 2),
        "daily_charge_kwh": _round(capacity_kwh / efficiency, 2),
        "daily_discharge_kwh": _round(capacity_kwh, 2),
        "total_investment_yuan": _round(investment, 2),
        "annual_savings_yuan": _round(annual_revenue, 2),
        "annual_revenue_yuan": _round(annual_revenue, 2),
        "arbitrage_revenue_yuan": _round(annual_arbitrage, 2),
        "demand_revenue_yuan": _round(demand_revenue, 2),
        "demand_reduction_kw": _round(demand_reduction_kw, 2),
        "demand_realization_rate": _round(demand_realization, 4),
        "demand_price_yuan_per_kw_month": _round(demand_price, 4),
        "annual_discharge_kwh": _round(annual_discharge, 2),
        "annual_charge_kwh": _round(annual_charge, 2),
        "utilization_ratio": _round(utilization_ratio, 4),
        "annual_revenue_per_kwh": _round(annual_revenue / capacity_kwh if capacity_kwh > 0 else 0, 2),
        "unit_investment_yuan_per_kwh": _round(unit_cost, 2),
        "cell_cost_yuan_per_wh": _round(_to_float(options.get("cell_cost_yuan_per_wh"), DEFAULT_NA_CELL_COST_YUAN_PER_WH), 4),
        "cost_basis": options.get("cost_basis") or DEFAULT_STORAGE_COST_BASIS,
        "system_efficiency": _round(efficiency, 4),
        "payback_years": _round(payback, 2, None) if payback is not None else None,
        "npv_yuan": _round(npv, 2, None),
        "irr": _round(irr, 4, None),
        "irr_percent": _round(irr * 100, 2, None) if irr is not None else None,
        "lcoe_yuan_per_kwh": _round(investment / (annual_discharge * max(1, int(getattr(storage, "project_life_years", 15) or 15))) if annual_discharge > 0 else 0, 4),
        "charge_window": "0:00-8:00",
        "discharge_window": "尖峰/高峰时段",
        "monthly_estimate": monthly_rows,
    }


def _attach_marginal_metrics(records: list[dict[str, Any]]) -> None:
    previous: dict[str, Any] | None = None
    for record in sorted(records, key=lambda item: item["battery_capacity_kwh"]):
        if previous is None:
            record["marginal_revenue_yuan"] = None
            record["marginal_revenue_per_kwh"] = None
        else:
            delta_capacity = record["battery_capacity_kwh"] - previous["battery_capacity_kwh"]
            delta_revenue = record["annual_revenue_yuan"] - previous["annual_revenue_yuan"]
            record["marginal_revenue_yuan"] = _round(delta_revenue, 2)
            record["marginal_revenue_per_kwh"] = _round(delta_revenue / delta_capacity, 2, None) if delta_capacity > 0 else None
        previous = record


def _select_sweet_spot(records: list[dict[str, Any]], options: dict[str, Any]) -> dict[str, Any]:
    positive = [r for r in sorted(records, key=lambda item: item["battery_capacity_kwh"]) if r["annual_revenue_yuan"] > 0]
    if not positive:
        return max(records, key=lambda item: item["annual_revenue_yuan"])

    payback_values = [r["payback_years"] for r in positive if r.get("payback_years") is not None]
    min_payback = min(payback_values) if payback_values else math.inf
    best_efficiency = max(float(r.get("annual_revenue_per_kwh") or 0) for r in positive)
    payback_tolerance = _to_float(options.get("payback_tolerance_years"), 0.65)
    marginal_drop_threshold = _to_float(options.get("marginal_drop_threshold"), 0.35)
    min_marginal_ratio = _to_float(options.get("min_marginal_efficiency_ratio"), 0.25)

    selected = positive[0]
    previous_marginal: float | None = None
    stop_reason = "首个正收益方案"
    for idx, record in enumerate(positive):
        if idx == 0:
            selected = record
            stop_reason = "首个正收益方案"
            continue

        payback = record.get("payback_years")
        marginal = record.get("marginal_revenue_per_kwh")
        if payback is None or payback > min_payback + payback_tolerance:
            stop_reason = f"下一档回收期超过最短回收期 {payback_tolerance:g} 年以上"
            break
        if marginal is not None and previous_marginal is not None and marginal < previous_marginal * marginal_drop_threshold:
            stop_reason = "下一档边际收益出现明显下降"
            break
        if marginal is not None and best_efficiency > 0 and marginal < best_efficiency * min_marginal_ratio:
            stop_reason = "下一档边际收益低于单位容量收益阈值"
            break
        selected = record
        stop_reason = "边际收益仍在甜点区"
        if marginal is not None:
            previous_marginal = marginal

    selected["selection_reason"] = stop_reason
    return selected


def analyze_capacity_with_bill_method(df: pd.DataFrame, rate: ElectricityRateConfig,
                                      storage: StorageConfig,
                                      options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Analyze capacity options using the bill-first marginal-benefit method."""
    options = dict(options or {})
    if df is None or df.empty:
        raise ValueError("电费数据为空")

    work = df.copy()
    work["月份"] = work.get("月份", "").map(_month_key) if "月份" in work.columns else ""
    month_count = max(1, int(work["月份"].nunique()) if "月份" in work.columns else len(work))
    annualize_partial = bool(options.get("annualize_partial_year", True))
    annualization_factor = 1.0
    warnings: list[str] = []
    if annualize_partial and month_count < 12:
        annualization_factor = 12 / month_count
        warnings.append(f"当前只有 {month_count} 个月账单，收益按 {annualization_factor:.2f} 倍折年；要完全复现全年测算需解析 12 个月账单。")

    max_demand_kw = float(pd.to_numeric(work.get("最大需量(kW)", 0), errors="coerce").fillna(0).max())
    demand_price = _estimate_demand_price(work, rate)
    schemes = _candidate_schemes(options, storage)
    records = [
        _calculate_candidate(power, capacity, work, rate, storage, options,
                             annualization_factor, demand_price, max_demand_kw)
        for power, capacity in schemes
    ]
    _attach_marginal_metrics(records)
    best = _select_sweet_spot(records, options)

    for record in records:
        payback = record.get("payback_years") or 999
        revenue_per_kwh = float(record.get("annual_revenue_per_kwh") or 0)
        marginal = float(record.get("marginal_revenue_per_kwh") or revenue_per_kwh or 0)
        record["score"] = _round(revenue_per_kwh * 0.6 + marginal * 0.4 - payback * 8, 4)
        record["is_best"] = record is best

    ordered = [best] + [
        r for r in sorted(records, key=lambda item: item["battery_capacity_kwh"])
        if r is not best
    ]
    for idx, record in enumerate(ordered, start=1):
        record["rank"] = idx
        record["is_best"] = record is best

    total_kwh = float(pd.to_numeric(work.get("总电量(kWh)", 0), errors="coerce").fillna(0).sum())
    peak_high_kwh = (
        pd.to_numeric(work.get("尖峰电量(kWh)", 0), errors="coerce").fillna(0)
        + pd.to_numeric(work.get("高峰电量(kWh)", 0), errors="coerce").fillna(0)
    ).sum()
    valley_kwh = float(pd.to_numeric(work.get("谷段电量(kWh)", 0), errors="coerce").fillna(0).sum())
    total_days = sum(_month_days(v) for v in work["月份"]) if "月份" in work.columns else month_count * 30

    result = {
        "ok": True,
        "method": "gpt55_bill_marginal_v1",
        "msg": f"已按逐月账单峰谷套利口径分析 {len(ordered)} 个组合，推荐 {best['inverter_power_kw']:g}kW / {best['battery_capacity_kwh']:g}kWh",
        "candidate_count": len(ordered),
        "positive_count": sum(1 for r in ordered if (r.get("annual_revenue_yuan") or 0) > 0),
        "scoring_basis": (
            "参考 GPT5.5 思路：逐月放电量=min(储能容量×当月天数, 尖峰+高峰实际电量)；"
            "充电量=放电量/系统效率；峰谷套利=放电电价收入-谷电充电成本；"
            "需量收益=可削减功率×需量电价×12×兑现率；推荐按边际收益拐点，而不是单纯最大容量或最高IRR。"
        ),
        "assumptions": {
            "system_efficiency": _round(_to_float(options.get("system_efficiency") or options.get("round_trip_efficiency") or options.get("efficiency"), 0.86), 4),
            "investment_unit_cost_yuan_per_kwh": _round(_to_float(options.get("investment_unit_cost_yuan_per_kwh") or options.get("unit_cost_yuan_per_kwh") or options.get("capex_yuan_per_kwh"), DEFAULT_STORAGE_SYSTEM_COST_YUAN_PER_KWH), 2),
            "cell_cost_yuan_per_wh": _round(_to_float(options.get("cell_cost_yuan_per_wh"), DEFAULT_NA_CELL_COST_YUAN_PER_WH), 4),
            "cost_basis": options.get("cost_basis") or DEFAULT_STORAGE_COST_BASIS,
            "demand_realization_rate": _round(_to_float(options.get("demand_realization_rate"), 0.5), 4),
            "demand_power_cap_ratio": _round(_to_float(options.get("demand_power_cap_ratio"), 0.75), 4),
            "annualization_factor": _round(annualization_factor, 4),
            "price_source": "账单分时电价" if _has_tou_price(work) else "配置默认电价/需量电费反推",
        },
        "warnings": warnings,
        "load_profile": {
            "month_count": month_count,
            "total_kwh": _round(total_kwh * annualization_factor, 2),
            "daily_kwh": _round(total_kwh / total_days if total_days > 0 else 0, 2),
            "daily_peak_high_kwh": _round(peak_high_kwh / total_days if total_days > 0 else 0, 2),
            "daily_valley_kwh": _round(valley_kwh / total_days if total_days > 0 else 0, 2),
            "max_demand_kw": _round(max_demand_kw, 2),
            "demand_price_yuan_per_kw_month": _round(demand_price, 4),
            "annualization_factor": _round(annualization_factor, 4),
            "has_tou_price": _has_tou_price(work),
        },
        "best": best,
        "results": ordered,
    }
    return result


def build_optimal_config_from_record(record: dict[str, Any], storage: StorageConfig) -> OptimalConfig:
    """Build an OptimalConfig facade so existing revenue/report code can reuse the result."""
    annual_revenue = float(record.get("annual_revenue_yuan") or 0)
    investment = float(record.get("total_investment_yuan") or 0)
    annual_discharge = float(record.get("annual_discharge_kwh") or 0)
    years = max(1, int(getattr(storage, "project_life_years", 15) or 15))
    degradation = float(getattr(storage, "annual_degradation", 0.0) or 0.0)
    inflation = float(getattr(storage, "electricity_inflation", 0.0) or 0.0)
    cumulative = -investment
    yearly_data = []
    for year in range(1, years + 1):
        capacity_factor = (1 - degradation) ** (year - 1)
        price_factor = (1 + inflation) ** (year - 1)
        yearly_revenue = annual_revenue * capacity_factor * price_factor
        yearly_discharge = annual_discharge * capacity_factor
        cumulative += yearly_revenue
        yearly_data.append({
            "年份": year,
            "年节省(元)": round(yearly_revenue, 2),
            "年成本(元)": 0.0,
            "年净收益(元)": round(yearly_revenue, 2),
            "累计现金流(元)": round(cumulative, 2),
            "年放电量(kWh)": round(yearly_discharge, 2),
            "容量保持率(%)": round(capacity_factor * 100, 2),
            "电价系数": round(price_factor, 4),
        })

    return OptimalConfig(
        battery_capacity_kwh=float(record.get("battery_capacity_kwh") or 0),
        inverter_power_kw=float(record.get("inverter_power_kw") or 0),
        charge_discharge_ratio=(
            float(record.get("inverter_power_kw") or 0) / float(record.get("battery_capacity_kwh") or 1)
        ),
        duration_hours=float(record.get("duration_hours") or 0),
        charge_start_hour=0,
        charge_end_hour=8,
        discharge_start_hour=8,
        discharge_end_hour=22,
        daily_charge_kwh=float(record.get("daily_charge_kwh") or 0),
        daily_discharge_kwh=float(record.get("daily_discharge_kwh") or 0),
        total_investment=investment,
        annual_savings=annual_revenue,
        annual_revenue=annual_revenue,
        simple_payback_years=float(record.get("payback_years") or 0),
        npv=float(record.get("npv_yuan") or 0),
        irr=float(record.get("irr") or 0),
        lcoe=float(record.get("lcoe_yuan_per_kwh") or 0),
        yearly_data=yearly_data,
    )


def result_brief(result: dict[str, Any], limit: int = 8) -> dict[str, Any]:
    """Small payload for LLM tool results."""
    keys = [
        "rank", "is_best", "name", "battery_capacity_kwh", "inverter_power_kw",
        "duration_hours", "annual_revenue_yuan", "arbitrage_revenue_yuan",
        "demand_revenue_yuan", "annual_revenue_per_kwh",
        "marginal_revenue_per_kwh", "payback_years",
    ]
    rows = []
    for row in (result.get("results") or [])[:limit]:
        rows.append({key: row.get(key) for key in keys})
    return {
        "method": result.get("method"),
        "msg": result.get("msg"),
        "warnings": result.get("warnings") or [],
        "assumptions": result.get("assumptions") or {},
        "load_profile": result.get("load_profile") or {},
        "best": {key: (result.get("best") or {}).get(key) for key in keys},
        "top_results": rows,
    }
