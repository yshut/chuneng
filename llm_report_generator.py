"""储能配置AGENT - LLM智能报告生成模块
将数字表格转化为可读的分析报告，生成执行摘要、投资建议、风险评估等。
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from llm_client import LLMClient
from storage_optimizer import OptimalConfig

logger = logging.getLogger(__name__)


class LLMReportGenerator:
    """LLM智能报告生成器"""

    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    # ------------------------------------------------------------------
    # 报告生成入口
    # ------------------------------------------------------------------
    def generate_full_report(self, config: OptimalConfig, report: dict,
                              investor_report: dict = None,
                              electricity_df=None) -> str:
        """生成完整的智能分析报告。

        Returns:
            Markdown格式的完整报告
        """
        sections = []

        # 1. 执行摘要
        sections.append(self.generate_executive_summary(config, report, investor_report))

        # 2. 储能配置分析
        sections.append(self._generate_config_analysis(config))

        # 3. 收益分析
        sections.append(self._generate_revenue_analysis(config, report))

        # 4. 资方/客户收益（如有）
        if investor_report:
            sections.append(self.generate_investor_report(investor_report))
            sections.append(self.generate_customer_report(investor_report))

        # 5. 风险分析
        sections.append(self.generate_risk_analysis(config, report))

        # 6. 投资建议
        sections.append(self._generate_investment_advice(config, report, investor_report))

        full_report = "\n\n".join(sections)

        # 添加报告头尾
        header = f"""# 储能项目投资分析报告

> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}
> 本报告由AI智能分析生成，仅供参考

---

"""
        return header + full_report

    # ------------------------------------------------------------------
    # 各章节生成
    # ------------------------------------------------------------------
    def generate_executive_summary(self, config: OptimalConfig, report: dict,
                                    investor_report: dict = None) -> str:
        """生成执行摘要。"""
        if not self.llm.available:
            return self._fallback_summary(config, report)

        data = {
            "储能容量_kWh": config.battery_capacity_kwh,
            "逆变器功率_kW": config.inverter_power_kw,
            "储能时长_h": config.duration_hours,
            "总投资_元": config.total_investment,
            "年节省电费_元": config.annual_savings,
            "年净收益_元": config.annual_revenue,
            "静态回收期_年": config.simple_payback_years,
            "净现值NPV_元": config.npv,
            "内部收益率IRR": config.irr,
            "度电成本_元每kWh": config.lcoe,
        }
        if investor_report:
            data["投资模式"] = investor_report.get("investment_mode", "自投")
            data["资方IRR"] = investor_report.get("investor_irr", "-")
            data["客户年节省"] = investor_report.get("customer_annual_savings", "-")

        prompt = f"""请根据以下储能项目数据，撰写一份简洁的执行摘要（200-300字），面向企业决策者。

数据：
{json.dumps(data, ensure_ascii=False, indent=2)}

要求：
1. 用通俗易懂的语言说明项目的核心价值
2. 突出关键经济指标（投资、回收期、收益率）
3. 如果有资方/客户分成，说明双方收益情况
4. 给出明确的投资建议（推荐/谨慎/不推荐）
5. 使用中文"""

        return "## 一、执行摘要\n\n" + self.llm.ask(prompt, system_prompt="你是一位资深的储能行业投资顾问，请用专业但易懂的语言撰写分析报告。")

    def generate_risk_analysis(self, config: OptimalConfig, report: dict) -> str:
        """生成风险分析。"""
        if not self.llm.available:
            return self._fallback_risk(config, report)

        data = {
            "回收期_年": config.simple_payback_years,
            "IRR": config.irr,
            "NPV_元": config.npv,
            "电池年衰减率": "2%",
            "项目寿命_年": 15,
            "电价年增长率": "3%",
        }

        prompt = f"""请根据以下储能项目数据，撰写风险分析（200-300字）。

数据：
{json.dumps(data, ensure_ascii=False, indent=2)}

要求：
1. 识别3-5个主要风险因素（技术、市场、政策、运营等）
2. 对每个风险给出发生概率和影响程度
3. 提出相应的风险应对措施
4. 给出整体风险评级（低/中/高）
5. 使用中文"""

        return "## 风险分析\n\n" + self.llm.ask(prompt, system_prompt="你是一位储能行业风险评估专家，请客观分析项目风险。")

    def generate_investor_report(self, investor_report: dict) -> str:
        """生成资方收益报告。"""
        if not self.llm.available:
            return self._fallback_investor_report(investor_report)

        prompt = f"""请根据以下储能项目资方（投资方）数据，撰写资方收益分析（200-300字）。

数据：
{json.dumps(investor_report, ensure_ascii=False, indent=2)}

要求：
1. 说明资方的投资总额、年收益、回收期
2. 分析投资回报率和现金流情况
3. 如果有贷款，说明贷款还款压力
4. 如果是EMC模式，说明分成收益
5. 给出资方投资建议
6. 使用中文"""

        return "## 资方收益分析\n\n" + self.llm.ask(prompt, system_prompt="你是一位储能项目投资分析师，请为资方提供专业的收益分析。")

    def generate_customer_report(self, investor_report: dict) -> str:
        """生成客户收益报告。"""
        if not self.llm.available:
            return self._fallback_customer_report(investor_report)

        prompt = f"""请根据以下储能项目客户（用电方）数据，撰写客户收益分析（200-300字）。

数据：
{json.dumps(investor_report, ensure_ascii=False, indent=2)}

要求：
1. 说明客户能节省多少电费
2. 分析对客户用电成本的影响
3. 如果是EMC模式，说明客户无需投资即可享受的收益
4. 分析储能对客户用电稳定性的影响
5. 给出客户合作建议
6. 使用中文"""

        return "## 客户收益分析\n\n" + self.llm.ask(prompt, system_prompt="你是一位储能行业顾问，请为客户分析储能项目的收益。")

    # ------------------------------------------------------------------
    # 内部生成方法
    # ------------------------------------------------------------------
    def _generate_config_analysis(self, config: OptimalConfig) -> str:
        """生成储能配置分析。"""
        if not self.llm.available:
            return self._fallback_config_analysis(config)

        data = {
            "电池容量_kWh": config.battery_capacity_kwh,
            "逆变器功率_kW": config.inverter_power_kw,
            "充放电倍率_C": config.charge_discharge_ratio,
            "储能时长_h": config.duration_hours,
            "充电时段": f"{config.charge_start_hour}:00-{config.charge_end_hour}:00",
            "放电时段": f"{config.discharge_start_hour}:00-{config.discharge_end_hour}:00",
            "日充电量_kWh": config.daily_charge_kwh,
            "日放电量_kWh": config.daily_discharge_kwh,
        }

        prompt = f"""请根据以下储能配置参数，撰写配置方案分析（200-300字）。

数据：
{json.dumps(data, ensure_ascii=False, indent=2)}

要求：
1. 解释储能配置的核心参数含义
2. 分析充放电策略的合理性
3. 说明配置是否适合典型的工商业用电场景
4. 如有优化空间，给出建议
5. 使用中文"""

        return "## 二、储能配置方案\n\n" + self.llm.ask(prompt, system_prompt="你是一位储能系统工程师，请分析储能配置方案。")

    def _generate_revenue_analysis(self, config: OptimalConfig, report: dict) -> str:
        """生成收益分析。"""
        if not self.llm.available:
            return self._fallback_revenue_analysis(config, report)

        summary = report.get("summary", {})
        data = {
            "总投资_元": config.total_investment,
            "年节省电费_元": config.annual_savings,
            "年净收益_元": config.annual_revenue,
            "回收期_年": config.simple_payback_years,
            "NPV_元": config.npv,
            "IRR": config.irr,
            "LCOE_元每kWh": config.lcoe,
            "项目周期收益_元": summary.get("项目全周期净收益(元)", 0),
            "ROI_%": summary.get("投资回报率ROI(%)", 0),
        }

        prompt = f"""请根据以下储能项目收益数据，撰写收益分析（200-300字）。

数据：
{json.dumps(data, ensure_ascii=False, indent=2)}

要求：
1. 解释各项经济指标的含义和优劣
2. 与行业平均水平对比（IRR 8-12%，回收期5-8年为行业正常水平）
3. 分析项目的经济可行性
4. 使用中文"""

        return "## 三、收益分析\n\n" + self.llm.ask(prompt, system_prompt="你是一位储能项目财务分析师，请用专业但易懂的语言分析项目收益。")

    def _generate_investment_advice(self, config: OptimalConfig, report: dict,
                                      investor_report: dict = None) -> str:
        """生成投资建议。"""
        if not self.llm.available:
            return "## 投资建议\n\n建议根据项目具体情况进行综合评估后决策。"

        data = {
            "回收期_年": config.simple_payback_years,
            "IRR": config.irr,
            "NPV_元": config.npv,
            "总投资_元": config.total_investment,
        }
        if investor_report:
            data["投资模式"] = investor_report.get("investment_mode", "自投")
            data["资方IRR"] = investor_report.get("investor_irr", "-")

        prompt = f"""请根据以下数据，给出储能项目的投资建议（200-300字）。

数据：
{json.dumps(data, ensure_ascii=False, indent=2)}

要求：
1. 给出明确的投资评级（强烈推荐/推荐/谨慎/不推荐）
2. 列出3条核心投资理由
3. 列出2-3条需要关注的风险点
4. 给出具体的实施建议（时机、规模、合作模式等）
5. 使用中文"""

        return "## 投资建议\n\n" + self.llm.ask(prompt, system_prompt="你是一位资深的储能投资顾问，请给出专业的投资建议。")

    # ------------------------------------------------------------------
    # 无LLM时的回退报告
    # ------------------------------------------------------------------
    def _fallback_summary(self, config: OptimalConfig, report: dict) -> str:
        return f"""## 一、执行摘要

本储能项目配置容量 {config.battery_capacity_kwh:,.0f} kWh，功率 {config.inverter_power_kw:,.0f} kW，储能时长 {config.duration_hours:.1f} 小时。

项目总投资 {config.total_investment:,.0f} 元，预计年节省电费 {config.annual_savings:,.0f} 元，年净收益 {config.annual_revenue:,.0f} 元。

静态回收期 {config.simple_payback_years:.2f} 年，内部收益率 {config.irr*100:.2f}%，净现值 {config.npv:,.0f} 元。

{"项目经济性良好，建议投资。" if config.simple_payback_years < 7 else "项目回收期较长，建议谨慎评估。" if config.simple_payback_years < 10 else "项目回收期过长，不建议投资。"}
"""

    def _fallback_risk(self, config: OptimalConfig, report: dict) -> str:
        risk_level = "低" if config.simple_payback_years < 6 else ("中" if config.simple_payback_years < 8 else "高")
        return f"""## 风险分析

**整体风险等级：{risk_level}**

主要风险因素：
1. **电价政策风险**：峰谷电价政策调整可能影响收益
2. **电池衰减风险**：电池年衰减率约2%，影响后期收益
3. **技术风险**：储能系统故障可能影响正常运行
4. **市场风险**：电价波动可能影响实际收益

风险应对措施：建议签订长期用电协议，选择优质电池供应商，定期维护保养。
"""

    def _fallback_config_analysis(self, config: OptimalConfig) -> str:
        return f"""## 二、储能配置方案

- 电池容量：{config.battery_capacity_kwh:,.0f} kWh
- 逆变器功率：{config.inverter_power_kw:,.0f} kW
- 储能时长：{config.duration_hours:.1f} 小时
- 充放电倍率：{config.charge_discharge_ratio:.4f} C
- 充电时段：{config.charge_start_hour}:00 - {config.charge_end_hour}:00（谷段）
- 放电时段：{config.discharge_start_hour}:00 - {config.discharge_end_hour}:00（峰段）
- 日充电量：{config.daily_charge_kwh:,.0f} kWh
- 日放电量：{config.daily_discharge_kwh:,.0f} kWh
"""

    def _fallback_revenue_analysis(self, config: OptimalConfig, report: dict) -> str:
        return f"""## 三、收益分析

- 总投资：{config.total_investment:,.0f} 元
- 年节省电费：{config.annual_savings:,.0f} 元
- 年净收益：{config.annual_revenue:,.0f} 元
- 静态回收期：{config.simple_payback_years:.2f} 年
- 净现值NPV：{config.npv:,.0f} 元
- 内部收益率IRR：{config.irr*100:.2f}%
- 度电成本LCOE：{config.lcoe:.4f} 元/kWh
"""

    def _fallback_investor_report(self, investor_report: dict) -> str:
        return "## 资方收益分析\n\n（LLM不可用，无法生成智能分析报告，请查看Excel中的详细数据）"

    def _fallback_customer_report(self, investor_report: dict) -> str:
        return "## 客户收益分析\n\n（LLM不可用，无法生成智能分析报告，请查看Excel中的详细数据）"

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export_report(self, report_content: str, output_path: str) -> Path:
        """导出报告为Markdown文件。"""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report_content, encoding="utf-8")
        logger.info("智能分析报告已导出: %s", path)
        return path
