"""储能配置AGENT - LLM编排Agent
实现自然语言交互、多方案对比决策、智能问答。
"""

import json
import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

from config import AgentConfig, InvestorConfig
from data_extractor import DataExtractor, ElectricityBillData
from llm_client import LLMClient
from llm_document_parser import LLMDocumentParser
from llm_report_generator import LLMReportGenerator
from revenue_analyzer import RevenueAnalyzer, InvestorCustomerReport, RevenueReport
from storage_optimizer import StorageOptimizer, OptimalConfig

logger = logging.getLogger(__name__)


class LLMAgent:
    """LLM编排Agent - 自然语言交互与多方案对比"""

    SYSTEM_PROMPT = """你是一个专业的储能项目分析助手。你可以：
1. 解析用户的用电数据描述，提取关键参数
2. 运行储能配置优化分析
3. 生成投资收益报告
4. 对比多种配置方案
5. 回答关于储能技术和投资的问题

请用中文回复，语气专业但易懂。"""

    def __init__(self, config: AgentConfig = None):
        self.config = config or AgentConfig()
        self.llm = LLMClient(self.config.llm_config)
        self.parser = LLMDocumentParser(self.llm)
        self.report_gen = LLMReportGenerator(self.llm)
        self.optimizer = StorageOptimizer(
            rate_config=self.config.rate_config,
            storage_config=self.config.storage_config,
        )
        self.analyzer = RevenueAnalyzer(
            rate_config=self.config.rate_config,
            storage_config=self.config.storage_config,
        )
        # 缓存上次分析结果
        self._last_config: Optional[OptimalConfig] = None
        self._last_report: Optional[RevenueReport] = None
        self._last_investor_report: Optional[InvestorCustomerReport] = None
        self._last_df: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # 自然语言输入处理
    # ------------------------------------------------------------------
    def run_with_nl_input(self, description: str) -> dict:
        """根据自然语言描述运行完整分析。

        示例输入：
        - "月用电约50万度，最大需量2000kW，峰谷电价差0.8元"
        - "我们工厂在广东，月电费大概35万，想配储能"
        """
        if not self.llm.available:
            logger.warning("LLM不可用，无法处理自然语言输入")
            return {}

        # 1. 用LLM提取结构化参数
        extract_prompt = f"""请从以下描述中提取用电数据，返回JSON格式：

用户描述：{description}

提取以下字段（找不到的用合理默认值）：
{{
    "monthly_kwh": 月用电量(kWh),
    "max_demand_kw": 最大需量(kW),
    "peak_kwh": 尖峰电量(kWh, 估算),
    "high_kwh": 高峰电量(kWh, 估算),
    "flat_kwh": 平段电量(kWh, 估算),
    "valley_kwh": 谷段电量(kWh, 估算),
    "peak_price": 尖峰电价(元/kWh),
    "high_price": 高峰电价(元/kWh),
    "flat_price": 平段电价(元/kWh),
    "valley_price": 谷段电价(元/kWh),
    "demand_charge": 需量电费(元/kW/月),
    "months": 月份数(默认12),
    "location": 地区
}}

注意：如果没有提到分时电量，按典型工商业比例估算：尖峰10%，高峰30%，平段35%，谷段25%。
直接返回JSON，不要markdown标记。"""

        result = self.llm.ask_json(extract_prompt, system_prompt=self.SYSTEM_PROMPT)
        if "error" in result:
            return {"error": "无法从描述中提取数据", "raw": result.get("raw", "")}

        # 2. 生成模拟电费数据
        df = self._generate_df_from_params(result)

        # 3. 更新电价配置
        if result.get("peak_price"):
            self.config.rate_config.peak_price = result["peak_price"]
        if result.get("high_price"):
            self.config.rate_config.high_price = result["high_price"]
        if result.get("flat_price"):
            self.config.rate_config.flat_price = result["flat_price"]
        if result.get("valley_price"):
            self.config.rate_config.valley_price = result["valley_price"]
        if result.get("demand_charge"):
            self.config.rate_config.demand_charge = result["demand_charge"]

        # 4. 运行分析
        return self._run_analysis(df)

    # ------------------------------------------------------------------
    # 文件处理
    # ------------------------------------------------------------------
    def run_with_files(self, file_paths: list[str]) -> dict:
        """处理文件并运行分析。"""
        # 使用LLM文档解析器
        bills = self.parser.parse_batch(file_paths)
        if not bills:
            return {"error": "未能从文件中提取到数据"}

        df = self._bills_to_dataframe(bills)
        return self._run_analysis(df)

    # ------------------------------------------------------------------
    # 多方案对比
    # ------------------------------------------------------------------
    def compare_scenarios(self, electricity_df: pd.DataFrame = None,
                           variations: list[dict] = None) -> dict:
        """运行多方案对比分析。

        Args:
            electricity_df: 电费数据（为None时使用上次的数据）
            variations: 方案变体列表，每个变体是一个参数覆盖字典

        示例：
            variations = [
                {"name": "2小时储能", "duration_hours": 2},
                {"name": "4小时储能", "duration_hours": 4},
            ]
        """
        df = electricity_df or self._last_df
        if df is None or df.empty:
            return {"error": "无可用的电费数据"}

        if not variations:
            variations = [
                {"name": "2小时方案", "battery_cost_per_kwh": 1000},
                {"name": "4小时方案", "battery_cost_per_kwh": 1200},
                {"name": "经济型方案", "battery_cost_per_kwh": 800},
            ]

        results = []
        for variant in variations:
            name = variant.pop("name", f"方案{len(results)+1}")

            # 临时修改配置
            orig_config = self._clone_storage_config()
            for k, v in variant.items():
                if hasattr(self.config.storage_config, k):
                    setattr(self.config.storage_config, k, v)

            # 运行优化
            self.optimizer = StorageOptimizer(
                rate_config=self.config.rate_config,
                storage_config=self.config.storage_config,
            )
            config = self.optimizer.optimize(df)
            report = self.analyzer.analyze(config, df)

            results.append({
                "name": name,
                "config": config,
                "report": report,
                "summary": report.summary,
            })

            # 恢复配置
            self.config.storage_config = orig_config
            self.optimizer = StorageOptimizer(
                rate_config=self.config.rate_config,
                storage_config=self.config.storage_config,
            )

        # 生成对比报告
        comparison = self._generate_comparison(results)

        return {
            "scenarios": results,
            "comparison": comparison,
            "recommendation": self._generate_recommendation(results),
        }

    # ------------------------------------------------------------------
    # 对话交互
    # ------------------------------------------------------------------
    def chat(self, user_input: str) -> str:
        """处理用户对话输入。"""
        if not self.llm.available:
            return "LLM服务不可用，请配置API Key后重试。"

        # 检测意图
        intent = self._detect_intent(user_input)

        if intent == "analyze":
            return self._handle_analyze(user_input)
        elif intent == "compare":
            return self._handle_compare(user_input)
        elif intent == "query":
            return self._handle_query(user_input)
        elif intent == "file":
            return self._handle_file(user_input)
        else:
            return self._handle_general(user_input)

    def interactive_loop(self):
        """交互式对话循环。"""
        print("=" * 60)
        print("       储能配置AGENT - 智能分析助手")
        print("=" * 60)
        print("输入用电数据描述开始分析，或输入问题获取建议。")
        print("输入 'quit' 或 'exit' 退出。")
        print("输入 'file <路径>' 处理文件。")
        print("=" * 60)

        while True:
            try:
                user_input = input("\n> ").strip()
                if not user_input:
                    continue
                if user_input.lower() in ("quit", "exit", "q"):
                    print("再见！")
                    break
                if user_input.lower().startswith("file "):
                    file_path = user_input[5:].strip()
                    result = self.run_with_files([file_path])
                    if "error" in result:
                        print(f"错误: {result['error']}")
                    else:
                        print(f"分析完成！结果已保存到 output/ 目录。")
                    continue

                response = self.chat(user_input)
                print(f"\n{response}")

            except KeyboardInterrupt:
                print("\n\n再见！")
                break
            except Exception as e:
                logger.error("处理出错: %s", e)
                print(f"处理出错: {e}")

    # ------------------------------------------------------------------
    # 意图识别
    # ------------------------------------------------------------------
    def _detect_intent(self, user_input: str) -> str:
        """识别用户意图。"""
        classify_prompt = f"""请判断用户输入的意图类型，只返回类型名称：

用户输入：{user_input}

可选类型：
- analyze: 用户提供了用电数据，想进行分析（包含电量、需量等数字）
- compare: 用户想对比多个方案（包含"对比"、"比较"、"哪个好"等）
- query: 用户在查询已有结果（包含"刚才"、"上次"、"回收期"等）
- file: 用户想处理文件（包含文件路径或"文件"、"PDF"等）
- general: 一般性问题或闲聊

只返回类型名称，不要其他内容。"""

        try:
            intent = self.llm.ask(classify_prompt, max_tokens=20).strip().lower()
            if intent in ("analyze", "compare", "query", "file", "general"):
                return intent
        except Exception:
            pass

        # 简单规则兜底
        lower = user_input.lower()
        if any(kw in lower for kw in ["万度", "kwh", "电量", "需量", "用电"]):
            return "analyze"
        if any(kw in lower for kw in ["对比", "比较", "方案", "哪个"]):
            return "compare"
        if any(kw in lower for kw in [".pdf", ".xlsx", ".doc", "文件"]):
            return "file"
        if any(kw in lower for kw in ["结果", "报告", "回收期", "收益"]):
            return "query"
        return "general"

    # ------------------------------------------------------------------
    # 各意图处理
    # ------------------------------------------------------------------
    def _handle_analyze(self, user_input: str) -> str:
        """处理分析请求。"""
        result = self.run_with_nl_input(user_input)
        if "error" in result:
            return f"分析失败：{result['error']}"

        config = result.get("config")
        if not config:
            return "分析未产生结果，请检查输入数据。"

        response = f"""分析完成！

**储能配置**：
- 电池容量：{config.battery_capacity_kwh:,.0f} kWh
- 逆变器功率：{config.inverter_power_kw:,.0f} kW
- 储能时长：{config.duration_hours:.1f} 小时

**经济指标**：
- 总投资：{config.total_investment:,.0f} 元
- 年节省电费：{config.annual_savings:,.0f} 元
- 年净收益：{config.annual_revenue:,.0f} 元
- 静态回收期：{config.simple_payback_years:.2f} 年
- 内部收益率：{config.irr*100:.2f}%

详细报告已保存到 output/ 目录。"""
        return response

    def _handle_compare(self, user_input: str) -> str:
        """处理对比请求。"""
        if self._last_df is None:
            return "请先提供用电数据进行分析，然后再进行方案对比。"

        # 让LLM生成对比方案
        scenario_prompt = f"""用户想对比储能方案。用户说：{user_input}

请根据用户需求生成3个对比方案，返回JSON格式：
{{
    "scenarios": [
        {{"name": "方案名称", "battery_cost_per_kwh": 1000, "description": "方案描述"}},
        ...
    ]
}}

方案应该有明确的差异（如不同时长、不同成本、不同技术路线）。
直接返回JSON。"""

        result = self.llm.ask_json(scenario_prompt)
        variations = result.get("scenarios", [
            {"name": "经济型", "battery_cost_per_kwh": 800},
            {"name": "标准型", "battery_cost_per_kwh": 1200},
            {"name": "高端型", "battery_cost_per_kwh": 1500},
        ])

        compare_result = self.compare_scenarios(self._last_df, variations)

        if "error" in compare_result:
            return f"对比失败：{compare_result['error']}"

        # 格式化对比结果
        response = "方案对比结果：\n\n"
        for i, scenario in enumerate(compare_result["scenarios"], 1):
            s = scenario["summary"]
            response += f"**{scenario['name']}**：\n"
            response += f"  - 投资：{s.get('总投资(元)', 0):,.0f} 元\n"
            response += f"  - 回收期：{s.get('静态回收期(年)', 0):.2f} 年\n"
            response += f"  - IRR：{s.get('内部收益率IRR', 0)*100:.2f}%\n\n"

        recommendation = compare_result.get("recommendation", "")
        if recommendation:
            response += f"**建议**：{recommendation}\n"

        return response

    def _handle_query(self, user_input: str) -> str:
        """处理查询请求。"""
        if self._last_config is None:
            return "暂无分析结果，请先进行分析。"

        config = self._last_config
        data = {
            "电池容量_kWh": config.battery_capacity_kwh,
            "功率_kW": config.inverter_power_kw,
            "投资_元": config.total_investment,
            "年收益_元": config.annual_revenue,
            "回收期_年": config.simple_payback_years,
            "IRR": config.irr,
            "NPV_元": config.npv,
        }

        prompt = f"""用户查询：{user_input}

当前分析结果数据：
{json.dumps(data, ensure_ascii=False, indent=2)}

请回答用户的问题，用通俗易懂的语言。"""
        return self.llm.ask(prompt, system_prompt=self.SYSTEM_PROMPT)

    def _handle_file(self, user_input: str) -> str:
        """处理文件请求。"""
        # 提取文件路径
        import re
        path_match = re.search(r'[^\s]+\.(pdf|docx?|xlsx?|csv|png|jpe?g)', user_input, re.IGNORECASE)
        if path_match:
            file_path = path_match.group(0)
            result = self.run_with_files([file_path])
            if "error" in result:
                return f"处理失败：{result['error']}"
            return f"文件处理完成！请查看 output/ 目录中的分析报告。"
        return "请提供文件路径，例如：分析 input/电费.xlsx"

    def _handle_general(self, user_input: str) -> str:
        """处理一般性问题。"""
        context = ""
        if self._last_config:
            c = self._last_config
            context = f"\n当前项目背景：容量{c.battery_capacity_kwh:.0f}kWh，投资{c.total_investment:.0f}元，回收期{c.simple_payback_years:.2f}年。"

        prompt = f"{user_input}{context}"
        return self.llm.ask(prompt, system_prompt=self.SYSTEM_PROMPT)

    # ------------------------------------------------------------------
    # 核心分析流程
    # ------------------------------------------------------------------
    def _run_analysis(self, df: pd.DataFrame) -> dict:
        """运行完整的储能分析流程。"""
        self._last_df = df
        results = {}

        # 1. 优化储能配置
        config = self.optimizer.optimize(df)
        self._last_config = config
        results["config"] = config

        # 2. 收益分析
        report = self.analyzer.analyze(config, df)
        self._last_report = report
        results["report"] = report

        # 3. 资方/客户分析
        investor_report = self.analyzer.analyze_investor_customer(
            config, df, self.config.investor_config
        )
        self._last_investor_report = investor_report
        results["investor_report"] = investor_report

        # 4. 导出Excel
        self.analyzer.export_report(report, str(self.config.output_dir / "收益分析报告.xlsx"))
        self.optimizer.export_config_to_excel(config, str(self.config.output_dir / "储能配置参数.xlsx"))
        self.analyzer.export_investor_report(investor_report, str(self.config.output_dir / "资方客户收益.xlsx"))
        df.to_excel(str(self.config.output_dir / "电费数据.xlsx"), index=False)

        # 5. 生成智能报告（如有LLM）
        if self.llm.available:
            ic_dict = {
                "investment_mode": investor_report.investment_mode,
                "investor_irr": investor_report.investor_summary.get("资方IRR", investor_report.investor_summary.get("IRR", "-")),
                "customer_annual_savings": investor_report.customer_summary.get("年均分成(元)", investor_report.customer_summary.get("年节省电费(元)", "-")),
            }
            md_report = self.report_gen.generate_full_report(
                config, report.summary, ic_dict, df
            )
            self.report_gen.export_report(md_report, str(self.config.output_dir / "智能分析报告.md"))
            results["md_report"] = md_report

        return results

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _generate_df_from_params(self, params: dict) -> pd.DataFrame:
        """从参数字典生成模拟电费DataFrame。"""
        import numpy as np
        np.random.seed(42)

        monthly_kwh = params.get("monthly_kwh", 500000)
        max_demand = params.get("max_demand_kw", monthly_kwh / 30 / 16 * 1.3)
        months = params.get("months", 12)

        records = []
        for m in range(1, months + 1):
            factor = 1 + np.random.uniform(-0.05, 0.05)
            total = monthly_kwh * factor

            peak = params.get("peak_kwh", total * 0.10)
            high = params.get("high_kwh", total * 0.30)
            flat = params.get("flat_kwh", total * 0.35)
            valley = params.get("valley_kwh", total * 0.25)

            peak_p = params.get("peak_price", self.config.rate_config.peak_price)
            high_p = params.get("high_price", self.config.rate_config.high_price)
            flat_p = params.get("flat_price", self.config.rate_config.flat_price)
            valley_p = params.get("valley_price", self.config.rate_config.valley_price)
            demand_c = params.get("demand_charge", self.config.rate_config.demand_charge)

            energy_charge = peak * peak_p + high * high_p + flat * flat_p + valley * valley_p
            demand_charge = max_demand * demand_c

            records.append({
                "月份": f"2025-{m:02d}",
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

        return pd.DataFrame(records)

    def _bills_to_dataframe(self, bills: list[ElectricityBillData]) -> pd.DataFrame:
        """将ElectricityBillData列表转为DataFrame。"""
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

    def _clone_storage_config(self):
        """克隆当前储能配置。"""
        from copy import deepcopy
        return deepcopy(self.config.storage_config)

    def _generate_comparison(self, results: list[dict]) -> str:
        """生成方案对比文本。"""
        if not self.llm.available:
            lines = []
            for r in results:
                s = r["summary"]
                lines.append(f"{r['name']}: 投资{s.get('总投资(元)',0):,.0f}元, "
                           f"回收期{s.get('静态回收期(年)',0):.2f}年, "
                           f"IRR={s.get('内部收益率IRR',0)*100:.2f}%")
            return "\n".join(lines)

        data = []
        for r in results:
            s = r["summary"]
            data.append({
                "方案名": r["name"],
                "投资_元": s.get("总投资(元)", 0),
                "年收益_元": s.get("年净收益(元)", 0),
                "回收期_年": s.get("静态回收期(年)", 0),
                "IRR": s.get("内部收益率IRR", 0),
                "NPV_元": s.get("净现值NPV(元)", 0),
            })

        prompt = f"""请对比以下储能方案，分析各方案优劣：

{json.dumps(data, ensure_ascii=False, indent=2)}

要求：
1. 列出各方案的关键指标对比
2. 分析各方案的适用场景
3. 给出推荐意见
4. 用中文，200字左右"""

        return self.llm.ask(prompt, system_prompt=self.SYSTEM_PROMPT)

    def _generate_recommendation(self, results: list[dict]) -> str:
        """生成推荐意见。"""
        if not results:
            return ""

        # 找IRR最高和回收期最短的方案
        best_irr = max(results, key=lambda r: r["config"].irr)
        best_payback = min(results, key=lambda r: r["config"].simple_payback_years)

        if best_irr["name"] == best_payback["name"]:
            return f"推荐 {best_irr['name']}：IRR最高({best_irr['config'].irr*100:.2f}%)且回收期最短({best_payback['config'].simple_payback_years:.2f}年)。"

        return (f"综合推荐 {best_payback['name']}：回收期最短({best_payback['config'].simple_payback_years:.2f}年)，"
                f"IRR为{best_payback['config'].irr*100:.2f}%。"
                f"如追求长期收益最大化，可选 {best_irr['name']}（IRR={best_irr['config'].irr*100:.2f}%）。")
