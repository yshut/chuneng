"""示例插件：估算储能项目的年 CO2 减排量。

根据已有的 OptimalConfig，按全国电网平均碳排因子估算储能放电
所替代的火电带来的 CO2 减排（粗算，实际应根据当地电网因子和调度策略修正）。
"""

from agent_tools import tool


# 国家发改委 2024 年公布的全国电网平均排放因子（kg CO2 / kWh），仅作示例
DEFAULT_GRID_EMISSION_FACTOR = 0.5703


@tool(
    name="estimate_co2_reduction",
    description="估算储能项目的年/全周期 CO2 减排量（吨）。需要先完成 optimize_storage。可指定 grid_factor 覆盖默认 0.5703 kgCO2/kWh。",
    parameters={
        "type": "object",
        "properties": {
            "grid_factor": {
                "type": "number",
                "description": "电网碳排因子 kgCO2/kWh，默认 0.5703",
            },
        },
        "required": [],
    },
)
def estimate_co2_reduction(state, grid_factor: float = DEFAULT_GRID_EMISSION_FACTOR):
    cfg = state.optimal_config
    if cfg is None:
        return {"error": "请先调用 optimize_storage 计算储能配置"}

    daily_discharge = cfg.daily_discharge_kwh or 0
    annual_discharge = daily_discharge * 365
    project_years = state.config.storage_config.project_life_years

    # 储能并不直接减排（其充电时本身用电），但实际上：
    # - 谷段充电时电网负荷低，多为火电基荷；峰段放电替代尖峰煤电/气电
    # - 简化：按"放电量 × 因子 × 0.3 调度系数"估算等效减排
    annual_co2_kg = annual_discharge * grid_factor * 0.3
    lifetime_co2_kg = annual_co2_kg * project_years

    return {
        "msg": "已估算 CO2 减排量",
        "grid_factor_kg_per_kwh": grid_factor,
        "annual_discharge_kwh": round(annual_discharge, 2),
        "annual_co2_reduction_ton": round(annual_co2_kg / 1000, 2),
        "lifetime_co2_reduction_ton": round(lifetime_co2_kg / 1000, 2),
        "note": "估算基于0.3调度系数（峰段火电替代比例），实际数值需结合当地电网调度数据。",
    }
