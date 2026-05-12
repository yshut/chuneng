"""通用金融计算工具。

集中放 NPV/IRR/折旧/年金等通用函数，避免在 storage_optimizer.py
和 webserver.py 里维护两套相同实现。
"""
from __future__ import annotations

import math
from typing import Iterable, Optional


def npv(rate: float, cash_flows: Iterable[float]) -> float:
    """折现值。cash_flows[0] 是第 0 年（通常是初始投资，负数）。"""
    return sum(cf / (1 + rate) ** i for i, cf in enumerate(cash_flows))


def irr(
    cash_flows: list[float],
    *,
    initial_guess: float = 0.1,
    max_iter: int = 1000,
    tol: float = 1e-8,
) -> Optional[float]:
    """牛顿法求 IRR。

    Returns
    -------
    float
        收敛到的内部收益率（小数形式，0.10 = 10%）。
    None
        现金流不存在符号变化（无解）、求导为零（无法收敛）、
        或迭代发散（数值不稳定）。

    上层调用者负责把 None 翻译成业务上想要的兜底值（0.0、"N/A" 等）。
    """
    if not cash_flows:
        return None
    has_neg = any(cf < 0 for cf in cash_flows)
    has_pos = any(cf > 0 for cf in cash_flows)
    if not (has_neg and has_pos):
        return None

    rate = initial_guess
    for _ in range(max_iter):
        if rate <= -0.99:
            rate = -0.9
        # 累计 NPV 和 dNPV/drate
        cur_npv = 0.0
        cur_dnpv = 0.0
        for i, cf in enumerate(cash_flows):
            denom = (1 + rate) ** i
            cur_npv += cf / denom
            cur_dnpv += -i * cf / ((1 + rate) ** (i + 1))
        if abs(cur_dnpv) < 1e-12:
            return None
        new_rate = rate - cur_npv / cur_dnpv
        if not math.isfinite(new_rate):
            return None
        if new_rate <= -0.99:
            new_rate = -0.9
        if abs(new_rate - rate) < tol:
            return new_rate
        rate = new_rate
    return rate if math.isfinite(rate) else None


def irr_or_default(cash_flows: list[float], default: float = 0.0, **kwargs) -> float:
    """irr 的便捷封装：求不出 IRR 时返回 default 而非 None。"""
    value = irr(cash_flows, **kwargs)
    return value if value is not None else default
