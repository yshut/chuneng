"""Shared demo electricity bill data for CLI and Agent tools."""

from __future__ import annotations

import numpy as np
import pandas as pd


def create_demo_df(seed: int = 42) -> pd.DataFrame:
    """Create a deterministic 12-month industrial/commercial bill sample."""
    rng = np.random.default_rng(seed)
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    base_total = 500000

    records = []
    for i, month in enumerate(months):
        season_factor = 1.0 + 0.2 * np.sin((i - 1) * np.pi / 6)
        total = base_total * season_factor * (1 + rng.uniform(-0.05, 0.05))

        peak = total * rng.uniform(0.08, 0.12)
        high = total * rng.uniform(0.25, 0.35)
        flat = total * rng.uniform(0.30, 0.40)
        valley = total - peak - high - flat

        max_demand = total / 30 / 16 * rng.uniform(1.2, 1.5)
        contract_cap = max_demand * 1.2

        energy_charge = peak * 1.2 + high * 1.0 + flat * 0.65 + valley * 0.35
        demand_charge = max_demand * 38
        total_amount = energy_charge + demand_charge

        records.append({
            "月份": month,
            "总电量(kWh)": round(total, 2),
            "尖峰电量(kWh)": round(peak, 2),
            "高峰电量(kWh)": round(high, 2),
            "平段电量(kWh)": round(flat, 2),
            "谷段电量(kWh)": round(valley, 2),
            "最大需量(kW)": round(max_demand, 2),
            "合同容量(kVA)": round(contract_cap, 2),
            "总电费(元)": round(total_amount, 2),
            "电量电费(元)": round(energy_charge, 2),
            "需量电费(元)": round(demand_charge, 2),
            "容量电费(元)": 0,
            "功率因数": round(rng.uniform(0.88, 0.95), 2),
        })

    return pd.DataFrame(records)
