"""储能配置AGENT - WebUI 图表生成模块
基于 matplotlib 渲染图表，返回 PIL Image 供 Gradio 显示。

支持的图表：
- yearly_revenue       年度收益曲线（节省/运维/净收益/累计）
- monthly_revenue      月度收益柱状图
- sensitivity_heatmap  敏感性分析热力图
- cost_breakdown_pie   投资成本饱图
- cashflow_waterfall   现金流瀑布图
"""

from __future__ import annotations

import io
import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _setup_matplotlib():
    """配置 matplotlib 中文字体 + 非交互后端。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # 中文字体（按优先级尝试）
    font_candidates = [
        "Microsoft YaHei", "SimHei", "PingFang SC",
        "Hiragino Sans GB", "Heiti SC", "WenQuanYi Micro Hei",
        "Noto Sans CJK SC", "DejaVu Sans",
    ]
    for f in font_candidates:
        try:
            plt.rcParams["font.sans-serif"] = [f]
            plt.rcParams["axes.unicode_minus"] = False
            break
        except Exception:
            continue

    return plt


def _fig_to_image(fig):
    """matplotlib Figure → PIL Image。"""
    from PIL import Image
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="white")
    buf.seek(0)
    img = Image.open(buf).copy()
    buf.close()
    return img


# ----------------------------------------------------------------------
# 图表函数
# ----------------------------------------------------------------------
def yearly_revenue_chart(state) -> Optional["PIL.Image.Image"]:
    """年度收益曲线：节省/运维/净收益 + 累计现金流。"""
    cfg = state.optimal_config
    if cfg is None or not cfg.yearly_data:
        return None

    plt = _setup_matplotlib()
    df = pd.DataFrame(cfg.yearly_data)

    fig, ax1 = plt.subplots(figsize=(11, 5.5))

    years = df["年份"]
    ax1.bar(years, df["年节省(元)"], label="年节省电费", color="#4CAF50", alpha=0.7)
    ax1.bar(years, -df["年成本(元)"], label="年运维成本", color="#FF7043", alpha=0.7)
    ax1.plot(years, df["年净收益(元)"], "o-", label="年净收益", color="#1E88E5", linewidth=2)
    ax1.set_xlabel("年份")
    ax1.set_ylabel("金额（元）")
    ax1.axhline(0, color="black", linewidth=0.5)
    ax1.legend(loc="upper left")
    ax1.grid(axis="y", linestyle="--", alpha=0.4)

    # 右轴：累计现金流
    ax2 = ax1.twinx()
    cum = df["累计现金流(元)"]
    ax2.plot(years, cum, "s-", label="累计现金流", color="#FFC107",
             linewidth=2.5, markersize=7)
    ax2.set_ylabel("累计现金流（元）", color="#FF8F00")
    ax2.tick_params(axis="y", labelcolor="#FF8F00")
    # 标注首个回正年
    payback_year = None
    for i, v in enumerate(cum):
        if v >= 0:
            payback_year = i + 1
            break
    if payback_year:
        ax2.annotate(
            f"回收期: 第 {payback_year} 年",
            xy=(payback_year, 0),
            xytext=(payback_year + 1, max(cum) * 0.3),
            arrowprops=dict(arrowstyle="->", color="red"),
            fontsize=11, color="red",
        )

    plt.title(f"年度收益分析（投资 {cfg.total_investment:,.0f} 元 · IRR {cfg.irr*100:.2f}% · 回收期 {cfg.simple_payback_years:.2f} 年）",
              fontsize=13, pad=12)
    plt.tight_layout()
    img = _fig_to_image(fig)
    plt.close(fig)
    return img


def monthly_revenue_chart(state) -> Optional["PIL.Image.Image"]:
    """月度收益柱状图：用电量 vs 储能放电量 vs 月节省。"""
    rep = state.revenue_report
    if rep is None or rep.monthly_estimate.empty:
        return None

    plt = _setup_matplotlib()
    df = rep.monthly_estimate.copy()

    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                              gridspec_kw={"height_ratios": [2, 1]})

    # 上：用电 vs 放电
    months = df["月份"].astype(str)
    ax1 = axes[0]
    if "总用电量(kWh)" in df.columns and df["总用电量(kWh)"].dtype != object:
        ax1.bar(months, df["总用电量(kWh)"], label="总用电量", color="#90CAF9", alpha=0.7)
    if "峰段电量(kWh)" in df.columns:
        try:
            peak = pd.to_numeric(df["峰段电量(kWh)"], errors="coerce").fillna(0)
            ax1.bar(months, peak, label="峰段电量", color="#EF5350", alpha=0.85)
        except Exception:
            pass
    discharge = pd.to_numeric(df["储能放电量(kWh)"], errors="coerce").fillna(0)
    ax1.plot(months, discharge, "o-", label="储能放电量", color="#FFC107", linewidth=2)
    ax1.set_ylabel("电量（kWh）")
    ax1.legend(loc="upper right")
    ax1.grid(axis="y", linestyle="--", alpha=0.4)
    ax1.set_title("月度用电与储能放电对比", fontsize=12)

    # 下：月节省 + 净收益
    ax2 = axes[1]
    saving = pd.to_numeric(df["节省电费(元)"], errors="coerce").fillna(0)
    om = pd.to_numeric(df["运维成本(元)"], errors="coerce").fillna(0)
    net = pd.to_numeric(df["月净收益(元)"], errors="coerce").fillna(0)
    width = 0.35
    x = np.arange(len(months))
    ax2.bar(x - width/2, saving, width, label="月节省电费", color="#4CAF50", alpha=0.85)
    ax2.bar(x + width/2, net, width, label="月净收益", color="#1E88E5", alpha=0.85)
    ax2.set_xticks(x)
    ax2.set_xticklabels(months, rotation=45)
    ax2.set_ylabel("金额（元）")
    ax2.legend(loc="upper right")
    ax2.grid(axis="y", linestyle="--", alpha=0.4)

    plt.tight_layout()
    img = _fig_to_image(fig)
    plt.close(fig)
    return img


def sensitivity_heatmap(state) -> Optional["PIL.Image.Image"]:
    """敏感性分析热力图：因素 × 变化幅度 → 回收期。"""
    rep = state.revenue_report
    if rep is None or rep.sensitivity_analysis.empty:
        return None

    plt = _setup_matplotlib()
    df = rep.sensitivity_analysis.copy()

    # 转成 pivot
    try:
        pivot = df.pivot_table(
            index="敏感性因素", columns="变化幅度(%)",
            values="回收期(年)", aggfunc="mean",
        )
    except Exception as e:
        logger.warning("敏感性 pivot 失败: %s", e)
        return None

    # 列按数值排序
    def _key(c):
        s = str(c).replace("%", "").replace("+", "")
        try:
            return int(s)
        except Exception:
            return 0
    pivot = pivot[sorted(pivot.columns, key=_key)]

    fig, ax = plt.subplots(figsize=(9.5, max(3.5, len(pivot) * 0.9)))
    data = pivot.values

    im = ax.imshow(data, cmap="RdYlGn_r", aspect="auto")
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index)

    # 标注数值
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            v = data[i, j]
            if pd.isna(v) or np.isinf(v):
                txt = "∞"
            else:
                txt = f"{v:.2f}"
            ax.text(j, i, txt, ha="center", va="center",
                    color="black", fontsize=11, fontweight="bold")

    cbar = plt.colorbar(im, ax=ax, label="回收期（年，越短越好）")
    cbar.ax.tick_params(labelsize=10)
    ax.set_xlabel("变化幅度")
    ax.set_title("敏感性分析：参数变化对回收期的影响", fontsize=13, pad=12)

    plt.tight_layout()
    img = _fig_to_image(fig)
    plt.close(fig)
    return img


def cost_breakdown_pie(state) -> Optional["PIL.Image.Image"]:
    """投资成本构成饱图。"""
    cfg = state.optimal_config
    if cfg is None:
        return None

    plt = _setup_matplotlib()
    storage = state.config.storage_config

    battery = cfg.battery_capacity_kwh * storage.battery_cost_per_kwh
    inverter = cfg.inverter_power_kw * storage.inverter_cost_per_kw
    pcs = cfg.inverter_power_kw * storage.pcs_cost_per_kw
    equipment = battery + inverter + pcs
    install = equipment * storage.installation_rate
    other = equipment * storage.other_cost_rate

    labels = ["电池系统", "逆变器", "PCS", "安装费", "其他费用"]
    sizes = [battery, inverter, pcs, install, other]
    colors = ["#4CAF50", "#1E88E5", "#FFC107", "#FF7043", "#9C27B0"]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5.5))

    # 左：饱图
    wedges, texts, autotexts = ax1.pie(
        sizes, labels=labels, colors=colors, autopct="%1.1f%%",
        startangle=90, textprops={"fontsize": 11},
    )
    for at in autotexts:
        at.set_color("white")
        at.set_fontweight("bold")
    ax1.set_title(f"投资成本构成（总投资 {cfg.total_investment:,.0f} 元）", fontsize=13)

    # 右：横条
    ax2.barh(labels, sizes, color=colors)
    ax2.set_xlabel("金额（元）")
    ax2.set_title("成本明细金额", fontsize=13)
    for i, v in enumerate(sizes):
        ax2.text(v, i, f" {v:,.0f}", va="center", fontsize=10)
    ax2.invert_yaxis()
    ax2.grid(axis="x", linestyle="--", alpha=0.4)

    plt.tight_layout()
    img = _fig_to_image(fig)
    plt.close(fig)
    return img


def cashflow_waterfall(state) -> Optional["PIL.Image.Image"]:
    """现金流瀑布图：投资 → 每年净收益 → 期末累计。"""
    cfg = state.optimal_config
    if cfg is None or not cfg.yearly_data:
        return None

    plt = _setup_matplotlib()
    df = pd.DataFrame(cfg.yearly_data)

    # 构建瀑布数据
    labels = ["投资"] + [f"Y{int(y)}" for y in df["年份"]] + ["累计"]
    values = [-cfg.total_investment] + df["年净收益(元)"].tolist()
    final = sum(values)
    values_with_final = values + [final]

    cum = 0
    bottoms = []
    deltas = []
    colors = []
    for i, v in enumerate(values_with_final):
        if i == len(values_with_final) - 1:
            bottoms.append(0)
            deltas.append(final)
            colors.append("#1E88E5")
        else:
            if v >= 0:
                bottoms.append(cum)
                colors.append("#4CAF50")
            else:
                bottoms.append(cum + v)
                colors.append("#EF5350")
            deltas.append(abs(v))
            cum += v

    fig, ax = plt.subplots(figsize=(13, 5.5))
    x = np.arange(len(labels))
    bars = ax.bar(x, deltas, bottom=bottoms, color=colors, edgecolor="black", linewidth=0.5)

    # 连线（瀑布感）
    for i in range(len(values_with_final) - 1):
        if i == len(values_with_final) - 2:
            continue  # 最后一个是累计
        cur_top = bottoms[i] + deltas[i] if values_with_final[i] >= 0 else bottoms[i] + deltas[i]
        next_bottom = bottoms[i + 1] if values_with_final[i + 1] >= 0 else bottoms[i + 1] + deltas[i + 1]
        ax.plot([i + 0.4, i + 1 - 0.4], [cur_top, next_bottom],
                color="gray", linestyle="--", linewidth=0.8)

    # 数值标注
    for i, (b, d, v) in enumerate(zip(bottoms, deltas, values_with_final)):
        y = b + d / 2
        ax.text(i, y, f"{v:,.0f}", ha="center", va="center",
                fontsize=9, fontweight="bold",
                color="white" if i in (0, len(values_with_final) - 1) else "black")

    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_ylabel("金额（元）")
    ax.set_title(f"项目现金流瀑布图（{state.config.storage_config.project_life_years} 年周期）",
                 fontsize=13, pad=12)
    ax.axhline(0, color="black", linewidth=0.6)
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    plt.tight_layout()
    img = _fig_to_image(fig)
    plt.close(fig)
    return img


# ----------------------------------------------------------------------
# 总入口
# ----------------------------------------------------------------------
CHART_FUNCTIONS = {
    "yearly_revenue": ("年度收益曲线", yearly_revenue_chart),
    "monthly_revenue": ("月度收益对比", monthly_revenue_chart),
    "sensitivity_heatmap": ("敏感性热力图", sensitivity_heatmap),
    "cost_breakdown_pie": ("成本构成饱图", cost_breakdown_pie),
    "cashflow_waterfall": ("现金流瀑布图", cashflow_waterfall),
}


def render(state, chart_name: str):
    """根据名字渲染单张图。"""
    if chart_name not in CHART_FUNCTIONS:
        return None
    _, func = CHART_FUNCTIONS[chart_name]
    try:
        return func(state)
    except Exception as e:
        logger.exception("渲染 %s 失败", chart_name)
        return None


def render_all(state) -> dict:
    """渲染全部图，返回 {name: PIL.Image | None}。"""
    out = {}
    for name, (label, func) in CHART_FUNCTIONS.items():
        try:
            out[name] = func(state)
        except Exception as e:
            logger.exception("渲染 %s 失败", name)
            out[name] = None
    return out
