"""储能配置AGENT - 工具层
将项目中所有功能封装为 LLM 可调用的工具（Function Calling）。

每个工具包含：
- name: 工具名（LLM 用来调用）
- description: 自然语言描述
- parameters: JSON Schema 描述参数
- func: 实际执行函数

Agent 在运行时持有一个 AgentState 对象，所有工具通过它读写共享状态
（电费数据、当前最优配置、收益报告等）。

支持 **插件式扩展**：
1. 用 @tool(...) 装饰器声明工具
2. 把文件放到 plugins/ 目录，启动时自动发现并注册
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from config import AgentConfig, ElectricityRateConfig, StorageConfig, InvestorConfig
from data_extractor import DataExtractor, ElectricityBillData
from document_parser import DocumentParser
from revenue_analyzer import (
    InvestorCustomerReport,
    RevenueAnalyzer,
    RevenueReport,
)
from storage_optimizer import OptimalConfig, StorageOptimizer

logger = logging.getLogger(__name__)


# ======================================================================
# Agent 状态
# ======================================================================
@dataclass
class AgentState:
    """Agent 运行时共享状态。所有工具读写此对象。"""

    config: AgentConfig = field(default_factory=AgentConfig)

    # 业务对象（懒初始化）
    parser: Optional[DocumentParser] = None
    extractor: Optional[DataExtractor] = None
    optimizer: Optional[StorageOptimizer] = None
    analyzer: Optional[RevenueAnalyzer] = None

    # 分析结果缓存
    electricity_df: Optional[pd.DataFrame] = None
    optimal_config: Optional[OptimalConfig] = None
    revenue_report: Optional[RevenueReport] = None
    investor_report: Optional[InvestorCustomerReport] = None
    md_report: Optional[str] = None

    # 可选：LLM 文档解析器、报告生成器、长期记忆
    llm_parser: Any = None
    llm_report_gen: Any = None
    memory: Any = None         # HierarchicalMemory 实例（working+摘要+事实+工具日志）
    vector_memory: Any = None  # VectorMemory 实例（语义检索）
    kb: Any = None             # KnowledgeBase 实例（RAG 知识库）
    user_id: str = "main"      # 当前用户标识

    # 由 StorageAgent 注入的 LLM 客户端（仅供 sub-agent 工具使用）
    llm_client: Any = None
    # 由 StorageAgent 注入的 ToolRegistry 引用（用于 sub-agent 调度）
    tool_registry: Any = None

    def __post_init__(self):
        if self.parser is None:
            self.parser = DocumentParser(
                ocr_language=self.config.ocr_language,
                tesseract_lang=self.config.tesseract_lang,
            )
        if self.extractor is None:
            self.extractor = DataExtractor()
        self._rebuild_workers()

    def _rebuild_workers(self):
        """电价/储能配置变化后重建优化器和分析器。"""
        self.optimizer = StorageOptimizer(
            rate_config=self.config.rate_config,
            storage_config=self.config.storage_config,
        )
        self.analyzer = RevenueAnalyzer(
            rate_config=self.config.rate_config,
            storage_config=self.config.storage_config,
        )


# ======================================================================
# 工具基类与注册器
# ======================================================================
@dataclass
class Tool:
    """单个工具定义。"""

    name: str
    description: str
    parameters: dict           # OpenAI/Qwen function calling 格式的 JSON Schema
    func: Callable[..., Any]   # 实际执行函数 (state, **kwargs) -> Any

    def to_openai_schema(self) -> dict:
        """转成 OpenAI/Qwen tool 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """工具注册器，统一管理所有工具。"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        if tool.name in self._tools:
            logger.warning("工具名重复，覆盖旧定义: %s", tool.name)
        self._tools[tool.name] = tool

    def unregister(self, name: str):
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def call(self, name: str, args: dict, state: AgentState,
             on_progress: Optional[Callable[[dict], None]] = None) -> str:
        """调用指定工具，返回字符串结果（供 LLM 阅读）。

        on_progress: 可选回调。如果工具函数声明了 `on_progress` 或 `progress` 参数，
        会自动注入。工具调用 on_progress(dict) 即可发布中间进度，
        Agent 主循环负责把进度透传到 CLI/WebUI。
        """
        tool = self.get(name)
        if not tool:
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
        try:
            kwargs = dict(args or {})
            # 自动注入 on_progress 参数（如果工具签名有）
            if on_progress is not None:
                try:
                    sig = inspect.signature(tool.func)
                    if "on_progress" in sig.parameters:
                        kwargs["on_progress"] = on_progress
                    elif "progress" in sig.parameters:
                        kwargs["progress"] = on_progress
                except (TypeError, ValueError):
                    pass
            result = tool.func(state, **kwargs)
            return _to_text(result)
        except TypeError as e:
            return json.dumps({"error": f"参数错误: {e}"}, ensure_ascii=False)
        except Exception as e:
            logger.exception("工具 %s 执行失败", name)
            return json.dumps({"error": f"执行失败: {e}"}, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 插件自动发现
    # ------------------------------------------------------------------
    def load_plugins(self, plugins_dir: str | Path) -> int:
        """扫描 plugins_dir 下所有 .py 文件，导入并自动注册其中
        被 @tool 装饰的函数。返回成功加载的工具数量。
        """
        plugins_path = Path(plugins_dir)
        if not plugins_path.exists():
            logger.info("插件目录不存在，跳过: %s", plugins_path)
            return 0

        loaded = 0
        for py_file in sorted(plugins_path.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"plugins.{py_file.stem}", py_file
                )
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                # 扫描模块所有属性，找带 _tool_meta 标记的函数
                for attr_name in dir(module):
                    obj = getattr(module, attr_name)
                    meta = getattr(obj, "_tool_meta", None)
                    if meta is not None:
                        tool = Tool(
                            name=meta["name"],
                            description=meta["description"],
                            parameters=meta["parameters"],
                            func=obj,
                        )
                        self.register(tool)
                        loaded += 1
                        logger.info("已加载插件工具: %s (来自 %s)", tool.name, py_file.name)
            except Exception as e:
                logger.warning("加载插件 %s 失败: %s", py_file.name, e)

        return loaded


# ----------------------------------------------------------------------
# @tool 装饰器
# ----------------------------------------------------------------------
def tool(name: str, description: str, parameters: dict = None):
    """声明一个工具函数。被装饰的函数会带上 _tool_meta 属性，
    供 ToolRegistry.load_plugins 自动发现。

    用法：
        @tool(
            name="my_tool",
            description="干啥的",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "number"}},
                "required": ["x"]
            }
        )
        def my_tool(state, x):
            return {"result": x * 2}
    """
    def decorator(func: Callable):
        func._tool_meta = {
            "name": name,
            "description": description,
            "parameters": parameters or {"type": "object", "properties": {}, "required": []},
        }
        return func
    return decorator


def _to_text(result: Any) -> str:
    """把工具返回值转为字符串，方便回填到 LLM 对话上下文。"""
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, default=str)
    if isinstance(result, pd.DataFrame):
        return result.to_string(index=False)
    return str(result)


# ======================================================================
# 工具实现
# ======================================================================

# ---------- 数据获取类 ----------

def _tool_list_input_files(state: AgentState, directory: str = "input") -> dict:
    """列出指定目录下所有支持的文件。"""
    path = Path(directory)
    if not path.exists():
        return {"directory": str(path), "files": [], "msg": "目录不存在"}
    files = []
    for ext in DocumentParser.SUPPORTED_EXTENSIONS:
        files.extend([str(p) for p in path.glob(f"*{ext}")])
        files.extend([str(p) for p in path.glob(f"*{ext.upper()}")])
    files = sorted(set(files))
    return {"directory": str(path), "files": files, "count": len(files)}


def _tool_parse_files(state: AgentState, file_paths: list[str]) -> dict:
    """解析文件并提取电费数据，存入 state.electricity_df。"""
    if not file_paths:
        return {"error": "file_paths 不能为空"}

    paths = [Path(p) for p in file_paths]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        return {"error": "以下文件不存在", "missing": missing}

    # 优先用 LLM 解析器（如果已注入）
    if state.llm_parser is not None:
        bills = state.llm_parser.parse_batch(paths)
        if bills:
            df = _bills_to_df(bills)
            state.electricity_df = df
            return {
                "msg": f"使用 LLM 成功解析 {len(file_paths)} 个文件，提取到 {len(df)} 条电费记录",
                "preview": df.head(3).to_dict(orient="records"),
                "months": df["月份"].tolist() if "月份" in df.columns else [],
            }

    # 回退到传统解析
    parsed = state.parser.parse_batch(paths)
    df = state.extractor.extract_from_parsed(parsed)
    if df.empty:
        return {"error": "未能从文件中提取到有效电费数据，请检查文件内容"}

    state.electricity_df = df
    return {
        "msg": f"成功解析 {len(file_paths)} 个文件，提取到 {len(df)} 条电费记录",
        "preview": df.head(3).to_dict(orient="records"),
        "months": df["月份"].tolist() if "月份" in df.columns else [],
    }


def _tool_use_demo_data(state: AgentState) -> dict:
    """加载内置 12 个月示例电费数据。"""
    df = _create_demo_df()
    state.electricity_df = df
    return {
        "msg": "已加载 12 个月示例数据",
        "rows": len(df),
        "total_kwh": float(df["总电量(kWh)"].sum()),
        "avg_monthly_kwh": float(df["总电量(kWh)"].mean()),
        "preview": df.head(3).to_dict(orient="records"),
    }


def _tool_parse_natural_language(state: AgentState, description: str) -> dict:
    """从用户口述文本中提取用电参数（月用电量、需量、电价等），生成模拟12个月数据。"""
    import re
    import numpy as np

    # 简单规则提取，LLM 在外层已经整理过参数也能直接传过来
    monthly_kwh = None
    max_demand = None

    m = re.search(r"(月用电|月电量|月用电量)[^\d]*([\d.]+)\s*(万)?", description)
    if m:
        val = float(m.group(2))
        if m.group(3) == "万":
            val *= 10000
        monthly_kwh = val

    m = re.search(r"(最大需量|需量)[^\d]*([\d.]+)\s*kW", description)
    if m:
        max_demand = float(m.group(2))

    if not monthly_kwh:
        return {
            "error": "未能从描述中提取月用电量。请明确说明，例如：'月用电50万度' 或 'monthly_kwh=500000'。"
        }

    if not max_demand:
        max_demand = monthly_kwh / 30 / 16 * 1.3

    # 生成12个月模拟数据
    np.random.seed(42)
    rate = state.config.rate_config
    records = []
    for mo in range(1, 13):
        factor = 1 + np.random.uniform(-0.05, 0.05)
        total = monthly_kwh * factor
        peak = total * 0.10
        high = total * 0.30
        flat = total * 0.35
        valley = total * 0.25
        energy_charge = peak * rate.peak_price + high * rate.high_price + flat * rate.flat_price + valley * rate.valley_price
        demand_charge = max_demand * rate.demand_charge
        records.append({
            "月份": f"2025-{mo:02d}",
            "总电量(kWh)": round(total, 2),
            "尖峰电量(kWh)": round(peak, 2),
            "高峰电量(kWh)": round(high, 2),
            "平段电量(kWh)": round(flat, 2),
            "谷段电量(kWh)": round(valley, 2),
            "最大需量(kW)": round(max_demand, 2),
            "合同容量(kVA)": round(max_demand * 1.2, 2),
            "总电费(元)": round(energy_charge + demand_charge, 2),
            "电量电费(元)": round(energy_charge, 2),
            "需量电费(元)": round(demand_charge, 2),
            "容量电费(元)": 0,
            "功率因数": 0.92,
        })
    df = pd.DataFrame(records)
    state.electricity_df = df
    return {
        "msg": "已根据描述生成12个月模拟数据",
        "monthly_kwh": monthly_kwh,
        "max_demand_kw": max_demand,
        "rows": len(df),
    }


# ---------- 配置修改类 ----------

def _tool_update_rate_config(
    state: AgentState,
    peak_price: Optional[float] = None,
    high_price: Optional[float] = None,
    flat_price: Optional[float] = None,
    valley_price: Optional[float] = None,
    demand_charge: Optional[float] = None,
) -> dict:
    """更新电价配置。只传需要修改的字段即可。"""
    rate = state.config.rate_config
    changed = {}
    for k, v in [
        ("peak_price", peak_price),
        ("high_price", high_price),
        ("flat_price", flat_price),
        ("valley_price", valley_price),
        ("demand_charge", demand_charge),
    ]:
        if v is not None:
            setattr(rate, k, v)
            changed[k] = v
    state._rebuild_workers()
    return {"msg": "电价配置已更新", "changed": changed, "current": rate.__dict__.copy()}


def _tool_update_storage_config(
    state: AgentState,
    battery_cost_per_kwh: Optional[float] = None,
    inverter_cost_per_kw: Optional[float] = None,
    pcs_cost_per_kw: Optional[float] = None,
    charge_efficiency: Optional[float] = None,
    discharge_efficiency: Optional[float] = None,
    depth_of_discharge: Optional[float] = None,
    annual_degradation: Optional[float] = None,
    project_life_years: Optional[int] = None,
    discount_rate: Optional[float] = None,
) -> dict:
    """更新储能系统配置（电池成本、效率、寿命等）。只传需要修改的字段即可。"""
    storage = state.config.storage_config
    changed = {}
    for k, v in [
        ("battery_cost_per_kwh", battery_cost_per_kwh),
        ("inverter_cost_per_kw", inverter_cost_per_kw),
        ("pcs_cost_per_kw", pcs_cost_per_kw),
        ("charge_efficiency", charge_efficiency),
        ("discharge_efficiency", discharge_efficiency),
        ("depth_of_discharge", depth_of_discharge),
        ("annual_degradation", annual_degradation),
        ("project_life_years", project_life_years),
        ("discount_rate", discount_rate),
    ]:
        if v is not None:
            setattr(storage, k, v)
            changed[k] = v
    state._rebuild_workers()
    return {"msg": "储能配置已更新", "changed": changed}


def _tool_set_investment_mode(
    state: AgentState,
    mode: str,
    loan_ratio: Optional[float] = None,
    loan_interest_rate: Optional[float] = None,
    loan_years: Optional[int] = None,
    investor_share_ratio: Optional[float] = None,
    customer_share_ratio: Optional[float] = None,
) -> dict:
    """设置投资模式：self(自投) / loan(贷款) / emc(合同能源管理)。"""
    if mode not in ("self", "loan", "emc"):
        return {"error": f"mode 必须是 self/loan/emc 之一，收到: {mode}"}
    inv = state.config.investor_config
    inv.investment_mode = mode
    changed = {"investment_mode": mode}
    for k, v in [
        ("loan_ratio", loan_ratio),
        ("loan_interest_rate", loan_interest_rate),
        ("loan_years", loan_years),
        ("investor_share_ratio", investor_share_ratio),
        ("customer_share_ratio", customer_share_ratio),
    ]:
        if v is not None:
            setattr(inv, k, v)
            changed[k] = v
    return {"msg": f"投资模式设置为 {mode}", "changed": changed}


# ---------- 分析类 ----------

def _tool_optimize_storage(state: AgentState) -> dict:
    """根据当前电费数据计算最优储能配置。"""
    if state.electricity_df is None or state.electricity_df.empty:
        return {"error": "请先用 parse_files / use_demo_data / parse_natural_language 加载电费数据"}

    config = state.optimizer.optimize(state.electricity_df)
    state.optimal_config = config
    return {
        "msg": "储能配置优化完成",
        "battery_capacity_kwh": config.battery_capacity_kwh,
        "inverter_power_kw": config.inverter_power_kw,
        "duration_hours": config.duration_hours,
        "total_investment_yuan": config.total_investment,
        "annual_savings_yuan": config.annual_savings,
        "annual_revenue_yuan": config.annual_revenue,
        "simple_payback_years": config.simple_payback_years,
        "npv_yuan": config.npv,
        "irr": config.irr,
        "lcoe_yuan_per_kwh": config.lcoe,
    }


def _tool_analyze_revenue(state: AgentState) -> dict:
    """生成详细收益分析（年度、月度、敏感性、风险）。需先调用 optimize_storage。"""
    if state.optimal_config is None:
        return {"error": "请先调用 optimize_storage"}
    if state.electricity_df is None:
        return {"error": "请先加载电费数据"}

    report = state.analyzer.analyze(state.optimal_config, state.electricity_df)
    state.revenue_report = report
    return {
        "msg": "收益分析完成",
        "summary": report.summary,
        "yearly_rows": len(report.yearly_details),
        "monthly_rows": len(report.monthly_estimate),
        "risk_level": report.risk_assessment.get("整体风险等级", "-"),
    }


def _tool_analyze_investor_customer(state: AgentState) -> dict:
    """运行资方/客户收益分配分析。模式由 set_investment_mode 设定。"""
    if state.optimal_config is None:
        return {"error": "请先调用 optimize_storage"}
    if state.electricity_df is None:
        return {"error": "请先加载电费数据"}

    inv_report = state.analyzer.analyze_investor_customer(
        state.optimal_config, state.electricity_df, state.config.investor_config
    )
    state.investor_report = inv_report
    return {
        "msg": "资方/客户收益分析完成",
        "investment_mode": inv_report.investment_mode,
        "investor_summary": inv_report.investor_summary,
        "customer_summary": inv_report.customer_summary,
    }


def _tool_compare_scenarios(state: AgentState, scenarios: list[dict],
                             on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """对比多个储能方案。每个 scenario 是一个参数字典，会临时覆盖 storage_config。

    示例：
        scenarios = [
            {"name": "经济型", "battery_cost_per_kwh": 800},
            {"name": "标准型", "battery_cost_per_kwh": 1200},
            {"name": "高端型", "battery_cost_per_kwh": 1500}
        ]

    支持流式进度：每完成一个方案 yield 一次 progress 事件。
    """
    if state.electricity_df is None:
        return {"error": "请先加载电费数据"}
    if not scenarios:
        return {"error": "scenarios 不能为空"}

    from copy import deepcopy

    results = []
    base_storage = deepcopy(state.config.storage_config)
    base_rate = deepcopy(state.config.rate_config)
    total = len(scenarios)

    for i, sc in enumerate(scenarios):
        name = sc.pop("name", f"方案{i+1}") if isinstance(sc, dict) else f"方案{i+1}"
        if on_progress:
            on_progress({"step": i + 1, "total": total, "phase": "computing", "name": name})

        # 临时覆盖
        for k, v in sc.items():
            if hasattr(state.config.storage_config, k):
                setattr(state.config.storage_config, k, v)
            elif hasattr(state.config.rate_config, k):
                setattr(state.config.rate_config, k, v)
        state._rebuild_workers()
        cfg = state.optimizer.optimize(state.electricity_df)
        result = {
            "方案名": name,
            "电池容量_kWh": cfg.battery_capacity_kwh,
            "总投资_元": cfg.total_investment,
            "年净收益_元": cfg.annual_revenue,
            "回收期_年": cfg.simple_payback_years,
            "IRR": cfg.irr,
            "NPV_元": cfg.npv,
        }
        results.append(result)

        if on_progress:
            on_progress({"step": i + 1, "total": total, "phase": "done", "result": result})

        # 恢复
        state.config.storage_config = deepcopy(base_storage)
        state.config.rate_config = deepcopy(base_rate)

    state._rebuild_workers()

    # 推荐：选 IRR 最高
    best = max(results, key=lambda r: r["IRR"]) if results else None

    return {
        "msg": "方案对比完成",
        "scenarios": results,
        "recommendation": f"推荐 {best['方案名']}（IRR={best['IRR']*100:.2f}%, 回收期={best['回收期_年']:.2f}年）" if best else "",
    }


def _tool_ab_experiment(state: AgentState, name: str,
                          variant_a: dict, variant_b: dict,
                          metrics: list[str] = None,
                          on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """A/B 实验：在同一份电费数据上跑两个变体（任意 storage/rate/investor 字段），输出差异表。

    示例：
        ab_experiment(
            name="电价 A:0.35 vs B:0.30",
            variant_a={"valley_price": 0.35},
            variant_b={"valley_price": 0.30},
            metrics=["irr", "payback", "npv", "investment"]
        )

    variant 字典中字段会按先后匹配到 storage_config / rate_config / investor_config 上。
    """
    if state.electricity_df is None:
        return {"error": "请先加载电费数据"}

    from copy import deepcopy

    metrics = metrics or ["investment", "annual_revenue", "irr", "payback", "npv", "lcoe"]

    base_storage = deepcopy(state.config.storage_config)
    base_rate = deepcopy(state.config.rate_config)
    base_investor = deepcopy(state.config.investor_config)

    def _apply(variant: dict):
        for k, v in (variant or {}).items():
            if hasattr(state.config.storage_config, k):
                setattr(state.config.storage_config, k, v)
            elif hasattr(state.config.rate_config, k):
                setattr(state.config.rate_config, k, v)
            elif hasattr(state.config.investor_config, k):
                setattr(state.config.investor_config, k, v)

    def _restore():
        state.config.storage_config = deepcopy(base_storage)
        state.config.rate_config = deepcopy(base_rate)
        state.config.investor_config = deepcopy(base_investor)
        state._rebuild_workers()

    def _run(label: str, variant: dict) -> dict:
        if on_progress:
            on_progress({"phase": "running", "variant": label, "params": variant})
        _restore()
        _apply(variant)
        state._rebuild_workers()
        cfg = state.optimizer.optimize(state.electricity_df)
        rep = state.analyzer.analyze(cfg, state.electricity_df)
        m = {
            "investment": cfg.total_investment,
            "annual_savings": cfg.annual_savings,
            "annual_revenue": cfg.annual_revenue,
            "irr": cfg.irr,
            "payback": cfg.simple_payback_years,
            "npv": cfg.npv,
            "lcoe": cfg.lcoe,
            "battery_kwh": cfg.battery_capacity_kwh,
            "power_kw": cfg.inverter_power_kw,
            "risk_level": rep.risk_assessment.get("整体风险等级", "-"),
        }
        return {k: m.get(k, "-") for k in metrics}

    a_metrics = _run("A", variant_a)
    b_metrics = _run("B", variant_b)
    _restore()

    # 差异
    diff = {}
    for k in metrics:
        a, b = a_metrics.get(k), b_metrics.get(k)
        try:
            d = float(b) - float(a)
            pct = (d / float(a) * 100) if float(a) != 0 else None
            diff[k] = {"abs": round(d, 4), "pct": round(pct, 2) if pct is not None else None}
        except (TypeError, ValueError):
            diff[k] = {"abs": "-", "pct": "-"}

    # 推荐
    score_a = (a_metrics.get("irr", 0) or 0) - 0.05 * (a_metrics.get("payback", 99) or 99)
    score_b = (b_metrics.get("irr", 0) or 0) - 0.05 * (b_metrics.get("payback", 99) or 99)
    winner = "A" if score_a > score_b else ("B" if score_b > score_a else "tie")

    return {
        "msg": f"A/B 实验完成: {name}",
        "name": name,
        "variant_a": {"params": variant_a, "metrics": a_metrics},
        "variant_b": {"params": variant_b, "metrics": b_metrics},
        "diff_b_minus_a": diff,
        "winner": winner,
        "explanation": f"按 IRR 主导评分，{winner} 方案更优" if winner != "tie" else "两方案差异不大",
    }


# ---------- 输出类 ----------

def _tool_get_current_state(state: AgentState) -> dict:
    """查看 Agent 当前内存中的分析状态。"""
    return {
        "已加载电费数据": state.electricity_df is not None and not state.electricity_df.empty,
        "电费数据行数": len(state.electricity_df) if state.electricity_df is not None else 0,
        "已计算最优配置": state.optimal_config is not None,
        "已生成收益报告": state.revenue_report is not None,
        "已生成资方分析": state.investor_report is not None,
        "已生成智能报告": state.md_report is not None,
        "当前电价配置": state.config.rate_config.__dict__,
        "当前投资模式": state.config.investor_config.investment_mode,
        "电池成本_元每kWh": state.config.storage_config.battery_cost_per_kwh,
    }


def _tool_export_excel(state: AgentState, output_dir: Optional[str] = None) -> dict:
    """将当前所有结果导出到 Excel 文件。"""
    out_dir = Path(output_dir) if output_dir else state.config.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    files = {}
    if state.electricity_df is not None:
        p = out_dir / "电费数据.xlsx"
        state.electricity_df.to_excel(p, index=False)
        files["电费数据"] = str(p)

    if state.optimal_config is not None:
        p = out_dir / "储能配置参数.xlsx"
        state.optimizer.export_config_to_excel(state.optimal_config, p)
        files["储能配置"] = str(p)

    if state.revenue_report is not None:
        p = out_dir / "收益分析报告.xlsx"
        state.analyzer.export_report(state.revenue_report, p)
        files["收益分析"] = str(p)

    if state.investor_report is not None:
        p = out_dir / "资方客户收益.xlsx"
        state.analyzer.export_investor_report(state.investor_report, p)
        files["资方客户收益"] = str(p)

    if state.md_report:
        p = out_dir / "智能分析报告.md"
        p.write_text(state.md_report, encoding="utf-8")
        files["智能分析报告"] = str(p)

    if not files:
        return {"error": "当前无结果可导出，请先完成分析"}
    return {"msg": "导出成功", "files": files, "output_dir": str(out_dir)}


def _tool_generate_report(state: AgentState) -> dict:
    """调用 LLM 生成 Markdown 智能分析报告。"""
    if state.optimal_config is None or state.revenue_report is None:
        return {"error": "请先完成 optimize_storage 和 analyze_revenue"}
    if state.llm_report_gen is None:
        return {"error": "LLM 报告生成器未初始化（可能 API Key 未配置）"}

    ic_dict = None
    if state.investor_report:
        ic_dict = {
            "investment_mode": state.investor_report.investment_mode,
            "investor_irr": state.investor_report.investor_summary.get(
                "资方IRR", state.investor_report.investor_summary.get("IRR", "-")
            ),
            "customer_annual_savings": state.investor_report.customer_summary.get(
                "年均分成(元)", state.investor_report.customer_summary.get("年节省电费(元)", "-")
            ),
        }

    md = state.llm_report_gen.generate_full_report(
        state.optimal_config, state.revenue_report.summary, ic_dict, state.electricity_df
    )
    state.md_report = md
    return {"msg": "智能报告已生成", "length": len(md), "preview": md[:500]}


# ======================================================================
# 注册表构建
# ======================================================================
def build_default_registry() -> ToolRegistry:
    """构建默认工具注册表。"""
    reg = ToolRegistry()

    reg.register(Tool(
        name="list_input_files",
        description="列出指定目录中所有支持的电费文件（PDF/Word/Excel/图片等）。默认查看 input/ 目录。",
        parameters={
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "要扫描的目录路径，默认 'input'"},
            },
            "required": [],
        },
        func=_tool_list_input_files,
    ))

    reg.register(Tool(
        name="parse_files",
        description="解析一个或多个电费账单文件（PDF/Word/Excel/图片），提取电费数据并加载到当前会话。这是开始分析前的第一步。",
        parameters={
            "type": "object",
            "properties": {
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "文件路径列表，例如 ['input/jan.pdf', 'input/feb.xlsx']",
                },
            },
            "required": ["file_paths"],
        },
        func=_tool_parse_files,
    ))

    reg.register(Tool(
        name="use_demo_data",
        description="加载内置的 12 个月示例电费数据（无需文件）。当用户没有真实数据但想看演示时使用。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=_tool_use_demo_data,
    ))

    reg.register(Tool(
        name="parse_natural_language",
        description="从用户的口述文字中提取用电信息并生成12个月模拟数据。当用户说'月用电50万度'之类的描述但没有具体文件时使用。",
        parameters={
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "用户的自然语言描述"},
            },
            "required": ["description"],
        },
        func=_tool_parse_natural_language,
    ))

    reg.register(Tool(
        name="update_rate_config",
        description="更新电价配置（尖峰/高峰/平段/谷段电价、需量电费）。只需传要修改的字段。",
        parameters={
            "type": "object",
            "properties": {
                "peak_price": {"type": "number", "description": "尖峰电价 元/kWh"},
                "high_price": {"type": "number", "description": "高峰电价 元/kWh"},
                "flat_price": {"type": "number", "description": "平段电价 元/kWh"},
                "valley_price": {"type": "number", "description": "谷段电价 元/kWh"},
                "demand_charge": {"type": "number", "description": "需量电费 元/kW/月"},
            },
            "required": [],
        },
        func=_tool_update_rate_config,
    ))

    reg.register(Tool(
        name="update_storage_config",
        description="更新储能系统配置（电池成本、效率、寿命、折现率等）。只需传要修改的字段。",
        parameters={
            "type": "object",
            "properties": {
                "battery_cost_per_kwh": {"type": "number", "description": "电池成本 元/kWh"},
                "inverter_cost_per_kw": {"type": "number", "description": "逆变器成本 元/kW"},
                "pcs_cost_per_kw": {"type": "number", "description": "PCS成本 元/kW"},
                "charge_efficiency": {"type": "number", "description": "充电效率 0~1"},
                "discharge_efficiency": {"type": "number", "description": "放电效率 0~1"},
                "depth_of_discharge": {"type": "number", "description": "放电深度 0~1"},
                "annual_degradation": {"type": "number", "description": "年衰减率 0~1"},
                "project_life_years": {"type": "integer", "description": "项目寿命（年）"},
                "discount_rate": {"type": "number", "description": "折现率 0~1"},
            },
            "required": [],
        },
        func=_tool_update_storage_config,
    ))

    reg.register(Tool(
        name="set_investment_mode",
        description="设置投资模式：self(自投) / loan(贷款) / emc(合同能源管理)。在调用 analyze_investor_customer 之前使用。",
        parameters={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["self", "loan", "emc"], "description": "投资模式"},
                "loan_ratio": {"type": "number", "description": "贷款比例（loan模式），0~1"},
                "loan_interest_rate": {"type": "number", "description": "贷款年利率（loan模式）"},
                "loan_years": {"type": "integer", "description": "贷款期限/年（loan模式）"},
                "investor_share_ratio": {"type": "number", "description": "资方分成比例（emc模式）"},
                "customer_share_ratio": {"type": "number", "description": "客户分成比例（emc模式）"},
            },
            "required": ["mode"],
        },
        func=_tool_set_investment_mode,
    ))

    reg.register(Tool(
        name="optimize_storage",
        description="根据已加载的电费数据计算最优储能配置（电池容量、PCS功率、充放电策略、回收期等）。需先加载电费数据。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=_tool_optimize_storage,
    ))

    reg.register(Tool(
        name="analyze_revenue",
        description="生成详细收益分析报告（年度收益、月度收益、敏感性分析、成本分解、风险评估）。需先调用 optimize_storage。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=_tool_analyze_revenue,
    ))

    reg.register(Tool(
        name="analyze_investor_customer",
        description="按当前 set_investment_mode 设定的模式，分析资方与客户的收益分配。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=_tool_analyze_investor_customer,
    ))

    reg.register(Tool(
        name="compare_scenarios",
        description="对比多个储能方案。每个方案是一个参数字典，可包含 name 和任意 storage_config / rate_config 字段。",
        parameters={
            "type": "object",
            "properties": {
                "scenarios": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "方案列表，例如 [{'name':'经济型','battery_cost_per_kwh':800},{'name':'标准型','battery_cost_per_kwh':1200}]",
                },
            },
            "required": ["scenarios"],
        },
        func=_tool_compare_scenarios,
    ))

    reg.register(Tool(
        name="ab_experiment",
        description="A/B 实验：在同一份电费数据上跑两个变体（两套电价/两种电池技术/两种投资模式），输出关键指标对比与赢家推荐。variant_a/variant_b 字段名会自动匹配到 storage_config / rate_config / investor_config。",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "实验名称，例如 '电价 0.35 vs 0.30'"},
                "variant_a": {
                    "type": "object",
                    "description": "变体 A 的参数覆盖，例如 {'valley_price': 0.35} 或 {'battery_cost_per_kwh': 800, 'cycle_life': 6000}",
                },
                "variant_b": {
                    "type": "object",
                    "description": "变体 B 的参数覆盖",
                },
                "metrics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "对比指标，可选 investment/annual_revenue/irr/payback/npv/lcoe/battery_kwh/power_kw/risk_level，默认全选",
                },
            },
            "required": ["name", "variant_a", "variant_b"],
        },
        func=_tool_ab_experiment,
    ))

    reg.register(Tool(
        name="get_current_state",
        description="查看当前 Agent 的内存状态：是否已加载数据、是否已分析、当前配置等。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=_tool_get_current_state,
    ))

    reg.register(Tool(
        name="export_excel",
        description="将当前所有分析结果导出为 Excel 文件（电费数据、储能配置、收益分析、资方分析、智能报告）。",
        parameters={
            "type": "object",
            "properties": {
                "output_dir": {"type": "string", "description": "输出目录，默认使用 config 中的 output_dir"},
            },
            "required": [],
        },
        func=_tool_export_excel,
    ))

    reg.register(Tool(
        name="generate_report",
        description="使用 LLM 生成完整的 Markdown 格式智能分析报告（包含执行摘要、配置分析、收益分析、风险分析、投资建议）。需先完成 optimize_storage 和 analyze_revenue。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=_tool_generate_report,
    ))

    reg.register(Tool(
        name="recall_memory",
        description="跨【近期对话/历史摘要/事实档案】关键词检索分层记忆。用户提到'之前/上次/还记得'时优先用此工具。",
        parameters={
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关键词列表（最多5个），所有关键词都需命中才返回。例如 ['EMC', '7:3']",
                },
                "limit": {"type": "integer", "description": "返回结果数，默认5"},
            },
            "required": ["keywords"],
        },
        func=_tool_recall_memory,
    ))

    reg.register(Tool(
        name="memory_stats",
        description="查看分层记忆 + 向量库状态（working / summaries / facts / tool_log 各层大小，用户名）。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=_tool_memory_stats,
    ))

    reg.register(Tool(
        name="list_facts",
        description="列出当前用户档案里的所有长期事实（KV，例如 月用电量/投资模式/电池成本）。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=_tool_list_facts,
    ))

    reg.register(Tool(
        name="set_fact",
        description="手动登记一条长期事实，写入用户档案。用于用户明确给的关键参数或偏好（例：月用电量=50万kWh, 投资模式=EMC 7:3）。",
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "事实名（中文 key 也可）"},
                "value": {"type": "string", "description": "事实值"},
            },
            "required": ["key", "value"],
        },
        func=_tool_set_fact,
    ))

    reg.register(Tool(
        name="remove_fact",
        description="从用户档案删除一条事实。",
        parameters={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
        func=_tool_remove_fact,
    ))

    reg.register(Tool(
        name="force_compress_memory",
        description="强制压缩 working 区到摘要 + 事实（一般无需手动调；记忆条数过多想立刻整理时使用）。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=_tool_force_compress_memory,
    ))

    reg.register(Tool(
        name="install_python_package",
        description=(
            "当其他工具因为缺少 Python 可选依赖而失败时（错误信息含 "
            "'Missing optional dependency'、'No module named X'、"
            "'pip install X' 等关键字），调用本工具自动安装。"
            "仅允许白名单中的包：xlrd / pdfplumber / python-docx / openpyxl / "
            "pillow / tabulate / matplotlib / chromadb / sentence-transformers / "
            "pytesseract / easyocr / pypdf / pymupdf / openai。"
            "安装成功后请立即重试之前失败的工具。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "package": {
                    "type": "string",
                    "description": "要安装的 Python 包名（可带版本约束，如 xlrd>=2.0.1）",
                },
                "reason": {
                    "type": "string",
                    "description": "为什么需要安装该包（如：解析 .xls 时缺 xlrd）",
                },
            },
            "required": ["package"],
        },
        func=_tool_install_python_package,
    ))

    reg.register(Tool(
        name="semantic_search_memory",
        description="基于嵌入向量的语义搜索：对用户的整个历史对话做相似度检索，返回最相关的 K 条。比 recall_memory 更智能（无需精确关键词），适合大量历史的复杂回忆。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用自然语言描述要回忆的内容"},
                "k": {"type": "integer", "description": "返回结果数，默认5"},
                "role": {
                    "type": "string",
                    "enum": ["user", "assistant", "any"],
                    "description": "限定角色：仅用户消息/仅助手回复/全部",
                },
            },
            "required": ["query"],
        },
        func=_tool_semantic_search_memory,
    ))

    # -------- 知识库 (RAG) --------
    reg.register(Tool(
        name="search_knowledge_base",
        description="从离线知识库（行业政策、电价文件、白皮书等）中做语义检索，返回带来源的相关段落。回答涉及政策/标准/电价文件等问题时优先使用。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询问题，自然语言"},
                "k": {"type": "integer", "description": "返回片段数，默认5"},
                "source_filter": {"type": "string", "description": "可选：仅在指定来源文件名中检索"},
            },
            "required": ["query"],
        },
        func=_tool_search_knowledge_base,
    ))

    reg.register(Tool(
        name="index_document_to_kb",
        description="把一个本地文件（.txt/.md/.pdf/.docx/.xlsx）切块并加入知识库，方便之后语义检索。",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件绝对/相对路径"},
                "metadata": {"type": "object", "description": "可选：自定义元数据（如 {'类别':'电价政策','地区':'江苏'}）"},
            },
            "required": ["file_path"],
        },
        func=_tool_index_document_to_kb,
    ))

    reg.register(Tool(
        name="list_kb_documents",
        description="列出知识库中所有已索引的文档及其 chunk 数。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=_tool_list_kb_documents,
    ))

    reg.register(Tool(
        name="remove_kb_document",
        description="从知识库删除指定 source 的所有 chunk。",
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "要删除的 source 文件名"},
            },
            "required": ["source"],
        },
        func=_tool_remove_kb_document,
    ))

    # -------- 多 Agent 协作 --------
    reg.register(Tool(
        name="delegate_to_data_agent",
        description="把【数据相关】子任务（解析文件、自然语言转电费、加载示例数据、用电画像）委托给【数据 Agent】。task 用一两句话描述要做什么。",
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "对子 Agent 的任务描述，例如 '解析 input/ 下所有文件并总结用电特征'"},
            },
            "required": ["task"],
        },
        func=_tool_delegate_to_data_agent,
    ))

    reg.register(Tool(
        name="delegate_to_config_agent",
        description="把【储能配置/收益/对比/A-B 实验】等子任务委托给【配置 Agent】。前提是数据已加载。",
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "例如 '优化储能配置并做一次 EMC vs 自投 的 A/B 对比'"},
            },
            "required": ["task"],
        },
        func=_tool_delegate_to_config_agent,
    ))

    reg.register(Tool(
        name="delegate_to_writer_agent",
        description="把【报告撰写/Excel 导出/政策引用】子任务委托给【报告 Agent】。前提是 optimal_config 与 revenue_report 已生成。",
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "例如 '写一份完整 Markdown 报告，引用电价政策'"},
            },
            "required": ["task"],
        },
        func=_tool_delegate_to_writer_agent,
    ))

    reg.register(Tool(
        name="parallel_subagents",
        description=(
            "asyncio 并行执行多个子 Agent。适合互不冲突的任务一起跑（例如 data_agent 解析 + writer_agent 准备模板，"
            "或 data_agent 解析 + KB 检索）。注意：同时跑两个会写同一份 state 的角色（如两个 config_agent）会冲突，"
            "只跑一类写状态的 agent。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "agents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": ["data", "config", "writer"]},
                            "task": {"type": "string"},
                        },
                        "required": ["role", "task"],
                    },
                    "description": (
                        "并行任务列表，例如 "
                        "[{'role':'data','task':'解析 input/'},{'role':'writer','task':'查知识库准备引用'}]"
                    ),
                },
            },
            "required": ["agents"],
        },
        func=_tool_parallel_subagents,
    ))

    return reg


# ----------------------------------------------------------------------
# 记忆相关工具实现
# ----------------------------------------------------------------------
def _tool_recall_memory(state: AgentState, keywords: list[str], limit: int = 5) -> dict:
    """从分层长期记忆中检索关键词（覆盖 working/summaries/facts）。"""
    if state.memory is None:
        return {"error": "未启用长期记忆"}
    if not keywords:
        return {"error": "keywords 不能为空"}
    results = state.memory.search_keywords(keywords[:5], limit=limit)
    if not results:
        return {"msg": f"未找到同时包含 {keywords} 的记忆", "matches": 0}
    return {
        "msg": f"找到 {len(results)} 条匹配",
        "results": [
            {
                "source": r.get("source"),
                "role": r.get("role"),
                "score": r.get("score"),
                "matched_keywords": r.get("matched"),
                "ts": r.get("ts"),
                "text": r.get("text"),
            }
            for r in results
        ],
    }


def _tool_memory_stats(state: AgentState) -> dict:
    info = {
        "user_id": getattr(state, "user_id", "main"),
        "hier_memory": state.memory.stats() if state.memory else None,
        "vector_memory": state.vector_memory.stats() if state.vector_memory else None,
    }
    return info


def _tool_list_facts(state: AgentState) -> dict:
    if state.memory is None:
        return {"error": "未启用长期记忆"}
    facts = state.memory.list_facts()
    return {"msg": f"共 {len(facts)} 条事实", "facts": facts}


def _tool_set_fact(state: AgentState, key: str, value: str) -> dict:
    """手动登记一条长期事实（如用户偏好、关键参数）。"""
    if state.memory is None:
        return {"error": "未启用长期记忆"}
    if not key or not value:
        return {"error": "key 和 value 都不能为空"}
    state.memory.set_fact(key, value, source="agent_tool")
    return {"msg": f"✓ 已记录: {key} = {value}"}


def _tool_remove_fact(state: AgentState, key: str) -> dict:
    if state.memory is None:
        return {"error": "未启用长期记忆"}
    ok = state.memory.remove_fact(key)
    return {"msg": "✓ 已删除" if ok else f"事实不存在: {key}"}


def _tool_force_compress_memory(state: AgentState) -> dict:
    """强制触发一次记忆压缩（提取摘要+事实）。"""
    if state.memory is None:
        return {"error": "未启用长期记忆"}
    state.memory.force_compress()
    return {"msg": "✓ 已强制压缩 working 区，生成新摘要并抽取事实",
             "stats": state.memory.stats()}


# 允许 Agent 自助安装的 Python 包白名单。
# 都是项目可选依赖；不在白名单的包会被拒绝，避免任意命令执行风险。
_PIP_INSTALL_WHITELIST = {
    "xlrd",                       # 旧 .xls 文件读取
    "openpyxl",                   # .xlsx 读取
    "pdfplumber",                 # PDF 文本解析
    "python-docx",                # .docx 解析
    "pillow",                     # 图片处理
    "tabulate",                   # DataFrame.to_markdown()
    "matplotlib",                 # 图表
    "chromadb",                   # 向量记忆
    "sentence-transformers",      # 本地嵌入 / 重排器
    "pytesseract",                # OCR
    "easyocr",                    # OCR
    "pypdf",                      # PDF 兜底解析
    "pymupdf",                    # PDF 兜底解析
    "openai",                     # LLM SDK
}


def _tool_install_python_package(state: AgentState, package: str,
                                  reason: str = "") -> dict:
    """通过 pip 安装一个白名单内的 Python 包，用于补齐缺失的可选依赖。

    安全约束：
    - 只允许安装预定义白名单内的包名（小写比较）
    - 包名只能是字母/数字/点/下划线/中划线（防止参数注入）
    - 不接受额外参数（如 --index-url），避免被诱导从恶意源安装
    - 设置 5 分钟超时
    """
    name_norm = (package or "").strip().lower()
    if not name_norm:
        return {"error": "package 不能为空"}

    # 防注入：只允许 PEP 503 包名 + 可选版本约束
    import re as _re
    if not _re.fullmatch(r"[a-z0-9._-]+([<>=!~]=?[a-z0-9._-]+)?", name_norm):
        return {"error": f"包名格式非法: {package}（仅允许字母/数字/点/中划线/下划线）"}

    base = _re.split(r"[<>=!~]", name_norm)[0]
    if base not in _PIP_INSTALL_WHITELIST:
        return {
            "error": f"包 '{base}' 不在白名单内，拒绝安装",
            "whitelist": sorted(_PIP_INSTALL_WHITELIST),
            "hint": "如需扩展白名单，请人工修改 agent_tools.py 中的 _PIP_INSTALL_WHITELIST",
        }

    logger.info("Agent 请求安装 Python 包: %s（理由: %s）", name_norm, reason or "未说明")

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "--no-input", name_norm],
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"error": "pip install 超时（>5min）"}
    except Exception as e:
        return {"error": f"pip install 启动失败: {e}"}

    stdout_tail = (proc.stdout or "").strip().splitlines()[-15:]
    stderr_tail = (proc.stderr or "").strip().splitlines()[-15:]

    if proc.returncode != 0:
        return {
            "error": f"pip install 失败 (exit={proc.returncode})",
            "package": name_norm,
            "stderr_tail": "\n".join(stderr_tail),
            "stdout_tail": "\n".join(stdout_tail),
        }

    # 尝试 import 一次验证
    import_ok = None
    try:
        importlib.import_module(base.replace("-", "_"))
        import_ok = True
    except Exception:
        import_ok = False

    return {
        "msg": f"✓ 已安装 {name_norm}",
        "import_ok": import_ok,
        "stdout_tail": "\n".join(stdout_tail),
        "hint": "已就绪，可重新调用之前失败的工具" if import_ok
                 else "安装成功但 import 失败，可能需要重启进程才生效",
    }


def _tool_semantic_search_memory(state: AgentState, query: str, k: int = 5,
                                  role: str = "any") -> dict:
    """对历史对话做语义检索（向量记忆）。"""
    if state.vector_memory is None or not state.vector_memory.available:
        return {"error": "向量记忆未启用，请安装 chromadb 并配置嵌入模型"}
    where = None
    if role and role != "any":
        where = {"role": role}
    hits = state.vector_memory.search(query, k=k, where=where)
    if not hits:
        return {"msg": f"未找到与 '{query}' 语义相关的历史记忆", "matches": 0}
    return {
        "msg": f"找到 {len(hits)} 条语义相关历史",
        "results": [
            {
                "role": h["metadata"].get("role", "?"),
                "score": h["score"],
                "timestamp": h["metadata"].get("timestamp"),
                "text": h["text"][:500],
            }
            for h in hits
        ],
    }


# ----------------------------------------------------------------------
# 知识库 (RAG) 工具实现
# ----------------------------------------------------------------------
def _tool_search_knowledge_base(state: AgentState, query: str, k: int = 5,
                                 source_filter: Optional[str] = None) -> dict:
    """在知识库中做语义检索。"""
    kb = getattr(state, "kb", None)
    if kb is None or not kb.is_ready:
        return {"error": "知识库未启用，请先调用 index_document_to_kb 或在启动时启用 KB"}
    hits = kb.search(query, k=k, source_filter=source_filter)
    if not hits:
        return {"msg": f"未找到与 '{query}' 相关的知识", "matches": 0}
    return {
        "msg": f"找到 {len(hits)} 条相关知识",
        "results": [
            {
                "source": h["source"],
                "score": h["score"],
                "chunk_idx": h["metadata"].get("chunk_idx"),
                "content": h["content"][:600],
            }
            for h in hits
        ],
    }


def _tool_index_document_to_kb(state: AgentState, file_path: str,
                                metadata: Optional[dict] = None,
                                on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """把文件入知识库。"""
    kb = getattr(state, "kb", None)
    if kb is None:
        return {"error": "知识库未启用（请在启动时打开 KB）"}
    try:
        n = kb.index_file(file_path, metadata=metadata, on_progress=on_progress)
    except FileNotFoundError:
        return {"error": f"文件不存在: {file_path}"}
    except Exception as e:
        return {"error": f"入库失败: {e}"}
    return {"msg": f"已入库 {n} 个 chunk", "source": Path(file_path).name, "chunks": n}


def _tool_list_kb_documents(state: AgentState) -> dict:
    kb = getattr(state, "kb", None)
    if kb is None or not kb.is_ready:
        return {"error": "知识库未启用"}
    docs = kb.list_documents()
    return {"msg": f"共 {len(docs)} 个文档，{kb.count()} 个 chunk", "documents": docs}


def _tool_remove_kb_document(state: AgentState, source: str) -> dict:
    kb = getattr(state, "kb", None)
    if kb is None or not kb.is_ready:
        return {"error": "知识库未启用"}
    n = kb.remove_document(source)
    return {"msg": f"已删除 {n} 个 chunk（source={source}）", "removed_chunks": n}


# ----------------------------------------------------------------------
# 子 Agent 委托工具实现（多 Agent 协作）
# ----------------------------------------------------------------------
def _delegate_to_subagent(state: AgentState, role: str, task: str,
                            on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """主 Agent 把子任务委托给指定角色的子 Agent。"""
    if state.llm_client is None or state.tool_registry is None:
        return {"error": "子 Agent 不可用：缺少 llm_client 或 tool_registry"}
    try:
        from subagents import make_subagent
    except ImportError as e:
        return {"error": f"导入子 Agent 失败: {e}"}
    try:
        sub = make_subagent(role, state, state.tool_registry, state.llm_client,
                             on_progress=on_progress)
    except ValueError as e:
        return {"error": str(e)}
    res = sub.run(task)
    return res


def _tool_delegate_to_data_agent(state: AgentState, task: str,
                                   on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """委托给数据 Agent：解析文件 / 自然语言 / 加载示例数据。"""
    return _delegate_to_subagent(state, "data", task, on_progress=on_progress)


def _tool_delegate_to_config_agent(state: AgentState, task: str,
                                     on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """委托给配置 Agent：储能优化、收益分析、A/B 实验、方案对比。"""
    return _delegate_to_subagent(state, "config", task, on_progress=on_progress)


def _tool_delegate_to_writer_agent(state: AgentState, task: str,
                                     on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """委托给报告 Agent：撰写 Markdown 报告 / Excel 导出 / 引用知识库。"""
    return _delegate_to_subagent(state, "writer", task, on_progress=on_progress)


def _tool_parallel_subagents(state: AgentState, agents: list[dict],
                               on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """asyncio 并行执行多个子 Agent，返回所有结果。

    适合：
    - 解析数据 + RAG 检索 同时启动
    - 配置优化 + 报告大纲撰写 同时启动
    - 不同方案的 sub-agent 同时跑

    注意：所有子 Agent 共享同一份 AgentState，存在写竞争（电费数据/最优配置）。
    所以并行时同类子 Agent 不要同时写同一份状态（如不要同时跑两个 ConfigAgent）。

    参数：
        agents = [
            {"role": "data", "task": "解析 input/"},
            {"role": "writer", "task": "起草报告大纲（基于知识库政策摘录）"}
        ]
    """
    if not agents:
        return {"error": "agents 不能为空"}
    if state.llm_client is None or state.tool_registry is None:
        return {"error": "缺少 llm_client 或 tool_registry"}

    try:
        from subagents import make_subagent
    except ImportError as e:
        return {"error": f"导入子 Agent 失败: {e}"}

    import asyncio
    import time as _time

    async def _run_one(idx: int, spec: dict):
        role = spec.get("role")
        task = spec.get("task", "")
        if on_progress:
            on_progress({"phase": "parallel_start", "idx": idx, "role": role, "task": task[:80]})
        try:
            sub = make_subagent(role, state, state.tool_registry, state.llm_client,
                                 on_progress=lambda p: on_progress(
                                     {**(p or {}), "parallel_idx": idx}
                                 ) if on_progress else None)
        except ValueError as e:
            return {"idx": idx, "role": role, "error": str(e)}

        t0 = _time.time()
        try:
            res = await asyncio.to_thread(sub.run, task)
        except Exception as e:
            return {"idx": idx, "role": role, "error": str(e),
                     "elapsed_sec": round(_time.time() - t0, 2)}
        if on_progress:
            on_progress({"phase": "parallel_finish", "idx": idx, "role": role,
                          "elapsed_sec": round(_time.time() - t0, 2)})
        return {"idx": idx, **res, "elapsed_sec": round(_time.time() - t0, 2)}

    async def _run_all():
        return await asyncio.gather(*[_run_one(i, s) for i, s in enumerate(agents)],
                                     return_exceptions=False)

    try:
        # 在已有事件循环中（如 mcp server）需要 nest 处理
        try:
            loop = asyncio.get_running_loop()
            # 如果当前已经有 loop，则用线程跑一个新的
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(asyncio.run, _run_all())
                results = fut.result()
        except RuntimeError:
            results = asyncio.run(_run_all())
    except Exception as e:
        return {"error": f"并行调度失败: {e}"}

    return {
        "msg": f"并行执行了 {len(results)} 个子 Agent",
        "results": results,
    }


# ======================================================================
# 辅助
# ======================================================================
def _bills_to_df(bills: list[ElectricityBillData]) -> pd.DataFrame:
    records = []
    for b in bills:
        records.append({
            "月份": b.month,
            "总电量(kWh)": b.total_kwh,
            "尖峰电量(kWh)": b.peak_kwh,
            "高峰电量(kWh)": b.high_kwh,
            "平段电量(kWh)": b.flat_kwh,
            "谷段电量(kWh)": b.valley_kwh,
            "最大需量(kW)": b.max_demand_kw,
            "合同容量(kVA)": b.contract_capacity_kva,
            "总电费(元)": b.total_amount,
            "电量电费(元)": b.energy_charge,
            "需量电费(元)": b.demand_charge,
            "容量电费(元)": b.capacity_charge,
            "功率因数": b.power_factor,
        })
    return pd.DataFrame(records)


def _create_demo_df() -> pd.DataFrame:
    import numpy as np
    np.random.seed(42)
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    base_total = 500000
    records = []
    for i, month in enumerate(months):
        season_factor = 1.0 + 0.2 * np.sin((i - 1) * np.pi / 6)
        total = base_total * season_factor * (1 + np.random.uniform(-0.05, 0.05))
        peak = total * np.random.uniform(0.08, 0.12)
        high = total * np.random.uniform(0.25, 0.35)
        flat = total * np.random.uniform(0.30, 0.40)
        valley = total - peak - high - flat
        max_demand = total / 30 / 16 * np.random.uniform(1.2, 1.5)
        contract_cap = max_demand * 1.2
        energy_charge = (peak * 1.2 + high * 1.0 + flat * 0.65 + valley * 0.35)
        demand_charge = max_demand * 38
        records.append({
            "月份": month,
            "总电量(kWh)": round(total, 2),
            "尖峰电量(kWh)": round(peak, 2),
            "高峰电量(kWh)": round(high, 2),
            "平段电量(kWh)": round(flat, 2),
            "谷段电量(kWh)": round(valley, 2),
            "最大需量(kW)": round(max_demand, 2),
            "合同容量(kVA)": round(contract_cap, 2),
            "总电费(元)": round(energy_charge + demand_charge, 2),
            "电量电费(元)": round(energy_charge, 2),
            "需量电费(元)": round(demand_charge, 2),
            "容量电费(元)": 0,
            "功率因数": round(np.random.uniform(0.88, 0.95), 2),
        })
    return pd.DataFrame(records)
