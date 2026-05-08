"""示例插件：根据当前最优配置给出电池选型建议。"""

from agent_tools import tool


@tool(
    name="battery_selection_advice",
    description="根据当前最优配置（容量、倍率），给出电池技术路线选型建议（磷酸铁锂/三元/钠电等）。",
    parameters={"type": "object", "properties": {}, "required": []},
)
def battery_selection_advice(state):
    cfg = state.optimal_config
    if cfg is None:
        return {"error": "请先调用 optimize_storage"}

    capacity = cfg.battery_capacity_kwh
    c_rate = cfg.charge_discharge_ratio
    duration = cfg.duration_hours

    # 简单决策逻辑
    if capacity < 500:
        scale = "小型"
    elif capacity < 5000:
        scale = "中型"
    else:
        scale = "大型"

    recommendations = []

    if c_rate > 1.0:
        recommendations.append({
            "技术路线": "三元锂",
            "理由": f"高倍率 {c_rate}C，需要功率型电池",
            "成本范围": "1500~2000 元/kWh",
            "循环寿命": "3000~5000 次",
        })
    elif duration <= 2:
        recommendations.append({
            "技术路线": "三元锂 / 高倍率磷酸铁锂",
            "理由": f"{duration}h 短时高功率应用",
            "成本范围": "1300~1800 元/kWh",
            "循环寿命": "4000~6000 次",
        })
    else:
        recommendations.append({
            "技术路线": "磷酸铁锂（LFP）",
            "理由": f"{duration}h 长时储能，安全性优先",
            "成本范围": "800~1300 元/kWh",
            "循环寿命": "6000~10000 次",
        })

    if capacity > 1000:
        recommendations.append({
            "技术路线": "钠离子电池（备选）",
            "理由": f"{scale}储能，钠电低成本+安全性突出",
            "成本范围": "600~900 元/kWh",
            "循环寿命": "3000~5000 次",
            "备注": "技术成熟度低于锂电，建议作为试点使用",
        })

    return {
        "msg": f"基于 {capacity:.0f} kWh / {duration:.1f}h / {c_rate:.2f}C 给出建议",
        "scale": scale,
        "recommendations": recommendations,
    }
