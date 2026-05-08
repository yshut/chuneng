"""储能配置AGENT - 收益分析模块
根据最优储能配置计算收益，生成详细的收益报告和表格。
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from config import ElectricityRateConfig, StorageConfig, InvestorConfig
from storage_optimizer import OptimalConfig

logger = logging.getLogger(__name__)


@dataclass
class RevenueReport:
    """收益分析报告"""
    summary: dict = field(default_factory=dict)
    yearly_details: pd.DataFrame = field(default_factory=pd.DataFrame)
    monthly_estimate: pd.DataFrame = field(default_factory=pd.DataFrame)
    sensitivity_analysis: pd.DataFrame = field(default_factory=pd.DataFrame)
    cost_breakdown: pd.DataFrame = field(default_factory=pd.DataFrame)
    risk_assessment: dict = field(default_factory=dict)


@dataclass
class InvestorCustomerReport:
    """资方/客户收益分析报告"""
    investment_mode: str = "self"                   # 投资模式
    investor_yearly: pd.DataFrame = field(default_factory=pd.DataFrame)   # 资方年度收益
    customer_yearly: pd.DataFrame = field(default_factory=pd.DataFrame)   # 客户年度收益
    loan_schedule: pd.DataFrame = field(default_factory=pd.DataFrame)     # 贷款还款明细
    investor_summary: dict = field(default_factory=dict)   # 资方汇总指标
    customer_summary: dict = field(default_factory=dict)   # 客户汇总指标


class RevenueAnalyzer:
    """收益分析器"""

    def __init__(self, rate_config: ElectricityRateConfig = None, storage_config: StorageConfig = None):
        self.rate = rate_config or ElectricityRateConfig()
        self.storage = storage_config or StorageConfig()

    def analyze(self, config: OptimalConfig, electricity_df: pd.DataFrame) -> RevenueReport:
        """执行完整的收益分析。

        Args:
            config: StorageOptimizer.optimize() 的输出
            electricity_df: DataExtractor.to_dataframe() 的输出

        Returns:
            收益分析报告
        """
        report = RevenueReport()

        # 1. 汇总指标
        report.summary = self._build_summary(config, electricity_df)

        # 2. 年度详细数据
        report.yearly_details = self._build_yearly_details(config)

        # 3. 月度收益估算
        report.monthly_estimate = self._build_monthly_estimate(config, electricity_df)

        # 4. 敏感性分析
        report.sensitivity_analysis = self._build_sensitivity_analysis(config, electricity_df)

        # 5. 成本分解
        report.cost_breakdown = self._build_cost_breakdown(config)

        # 6. 风险评估
        report.risk_assessment = self._assess_risk(config, electricity_df)

        return report

    # ------------------------------------------------------------------
    # 汇总指标
    # ------------------------------------------------------------------
    def _build_summary(self, config: OptimalConfig, df: pd.DataFrame) -> dict:
        """构建收益汇总指标。"""
        total_investment = config.total_investment
        annual_revenue = config.annual_revenue
        payback = config.simple_payback_years

        # 计算项目全周期收益
        project_years = self.storage.project_life_years
        total_revenue = sum(y["年净收益(元)"] for y in config.yearly_data) if config.yearly_data else annual_revenue * project_years
        roi = (total_revenue / total_investment * 100) if total_investment > 0 else 0

        return {
            "总投资(元)": total_investment,
            "年节省电费(元)": config.annual_savings,
            "年运维成本(元)": round(total_investment * (self.storage.annual_om_cost_rate + self.storage.insurance_rate), 2),
            "年净收益(元)": annual_revenue,
            "静态回收期(年)": payback,
            "项目周期(年)": project_years,
            "项目全周期净收益(元)": round(total_revenue, 2),
            "投资回报率ROI(%)": round(roi, 2),
            "净现值NPV(元)": config.npv,
            "内部收益率IRR": config.irr,
            "度电成本LCOE(元/kWh)": config.lcoe,
            "电池容量(kWh)": config.battery_capacity_kwh,
            "逆变器功率(kW)": config.inverter_power_kw,
            "储能时长(h)": config.duration_hours,
            "日均放电量(kWh)": config.daily_discharge_kwh,
            "年放电量(kWh)": round(config.daily_discharge_kwh * 365, 2),
        }

    # ------------------------------------------------------------------
    # 年度详细数据
    # ------------------------------------------------------------------
    def _build_yearly_details(self, config: OptimalConfig) -> pd.DataFrame:
        """构建年度详细收益表。"""
        if config.yearly_data:
            return pd.DataFrame(config.yearly_data)

        # 如果没有预计算数据，生成基础表
        records = []
        years = self.storage.project_life_years
        degradation = self.storage.annual_degradation
        inflation = self.storage.electricity_inflation

        cumulative = -config.total_investment
        for year in range(1, years + 1):
            cap_factor = (1 - degradation) ** year
            price_factor = (1 + inflation) ** year
            savings = config.annual_savings * cap_factor * price_factor
            om_cost = config.total_investment * (self.storage.annual_om_cost_rate + self.storage.insurance_rate)
            net = savings - om_cost
            cumulative += net

            records.append({
                "年份": year,
                "容量保持率(%)": round(cap_factor * 100, 2),
                "电价增长系数": round(price_factor, 4),
                "年节省电费(元)": round(savings, 2),
                "年运维成本(元)": round(om_cost, 2),
                "年净收益(元)": round(net, 2),
                "累计净收益(元)": round(cumulative, 2),
            })

        return pd.DataFrame(records)

    # ------------------------------------------------------------------
    # 月度收益估算
    # ------------------------------------------------------------------
    def _build_monthly_estimate(self, config: OptimalConfig, df: pd.DataFrame) -> pd.DataFrame:
        """构建月度收益估算表。"""
        records = []

        if not df.empty:
            for _, row in df.iterrows():
                month = row.get("月份", "")
                total_kwh = row.get("总电量(kWh)", 0)
                peak_kwh = row.get("尖峰电量(kWh)", 0)
                high_kwh = row.get("高峰电量(kWh)", 0)
                valley_kwh = row.get("谷段电量(kWh)", 0)

                # 估算该月储能收益
                # 日放电量比例按该月用电占比分配
                monthly_ratio = total_kwh / df["总电量(kWh)"].sum() if df["总电量(kWh)"].sum() > 0 else 1 / len(df)
                monthly_discharge = config.daily_discharge_kwh * 30 * monthly_ratio
                monthly_saving = monthly_discharge * (self.rate.peak_price - self.rate.valley_price)
                monthly_om = config.total_investment * (self.storage.annual_om_cost_rate + self.storage.insurance_rate) / 12

                records.append({
                    "月份": month,
                    "总用电量(kWh)": total_kwh,
                    "峰段电量(kWh)": peak_kwh + high_kwh,
                    "谷段电量(kWh)": valley_kwh,
                    "储能放电量(kWh)": round(monthly_discharge, 2),
                    "节省电费(元)": round(monthly_saving, 2),
                    "运维成本(元)": round(monthly_om, 2),
                    "月净收益(元)": round(monthly_saving - monthly_om, 2),
                })
        else:
            # 无原始数据时按均匀分布估算
            for month in range(1, 13):
                monthly_discharge = config.daily_discharge_kwh * 30
                monthly_saving = monthly_discharge * (self.rate.peak_price - self.rate.valley_price)
                monthly_om = config.total_investment * (self.storage.annual_om_cost_rate + self.storage.insurance_rate) / 12

                records.append({
                    "月份": f"第{month}月",
                    "总用电量(kWh)": "-",
                    "峰段电量(kWh)": "-",
                    "谷段电量(kWh)": "-",
                    "储能放电量(kWh)": round(monthly_discharge, 2),
                    "节省电费(元)": round(monthly_saving, 2),
                    "运维成本(元)": round(monthly_om, 2),
                    "月净收益(元)": round(monthly_saving - monthly_om, 2),
                })

        return pd.DataFrame(records)

    # ------------------------------------------------------------------
    # 敏感性分析
    # ------------------------------------------------------------------
    def _build_sensitivity_analysis(self, config: OptimalConfig, df: pd.DataFrame) -> pd.DataFrame:
        """构建敏感性分析表。"""
        records = []
        base_payback = config.simple_payback_years
        base_npv = config.npv

        # 1. 电价变化敏感性
        for delta in [-20, -10, 0, 10, 20]:
            factor = 1 + delta / 100
            adj_savings = config.annual_savings * factor
            adj_net = adj_savings - config.total_investment * (self.storage.annual_om_cost_rate + self.storage.insurance_rate)
            adj_payback = config.total_investment / adj_net if adj_net > 0 else float('inf')

            records.append({
                "敏感性因素": "电价变化",
                "变化幅度(%)": f"{delta:+d}%",
                "年净收益(元)": round(adj_net, 2),
                "回收期(年)": round(adj_payback, 2),
                "回收期变化(年)": round(adj_payback - base_payback, 2),
            })

        # 2. 电池成本变化敏感性
        for delta in [-20, -10, 0, 10, 20]:
            factor = 1 + delta / 100
            adj_investment = config.total_investment * factor
            adj_payback = adj_investment / config.annual_revenue if config.annual_revenue > 0 else float('inf')

            records.append({
                "敏感性因素": "电池成本变化",
                "变化幅度(%)": f"{delta:+d}%",
                "年净收益(元)": round(config.annual_revenue, 2),
                "回收期(年)": round(adj_payback, 2),
                "回收期变化(年)": round(adj_payback - base_payback, 2),
            })

        # 3. 充放电效率变化敏感性
        for delta in [-5, -2, 0, 2, 5]:
            eff = (self.storage.charge_efficiency * self.storage.discharge_efficiency) + delta / 100
            if eff <= 0:
                continue
            adj_savings = config.annual_savings * (eff / (self.storage.charge_efficiency * self.storage.discharge_efficiency))
            adj_net = adj_savings - config.total_investment * (self.storage.annual_om_cost_rate + self.storage.insurance_rate)
            adj_payback = config.total_investment / adj_net if adj_net > 0 else float('inf')

            records.append({
                "敏感性因素": "充放电效率变化",
                "变化幅度(%)": f"{delta:+d}%",
                "年净收益(元)": round(adj_net, 2),
                "回收期(年)": round(adj_payback, 2),
                "回收期变化(年)": round(adj_payback - base_payback, 2),
            })

        return pd.DataFrame(records)

    # ------------------------------------------------------------------
    # 成本分解
    # ------------------------------------------------------------------
    def _build_cost_breakdown(self, config: OptimalConfig) -> pd.DataFrame:
        """构建成本分解表。"""
        battery_cost = config.battery_capacity_kwh * self.storage.battery_cost_per_kwh
        inverter_cost = config.inverter_power_kw * self.storage.inverter_cost_per_kw
        pcs_cost = config.inverter_power_kw * self.storage.pcs_cost_per_kw
        equipment_subtotal = battery_cost + inverter_cost + pcs_cost
        install_cost = equipment_subtotal * self.storage.installation_rate
        other_cost = equipment_subtotal * self.storage.other_cost_rate

        # 年运维成本
        annual_om = config.total_investment * self.storage.annual_om_cost_rate
        annual_insurance = config.total_investment * self.storage.insurance_rate
        total_annual_om = annual_om + annual_insurance
        lifetime_om = total_annual_om * self.storage.project_life_years

        records = [
            {"成本项目": "电池系统", "单价": f"{self.storage.battery_cost_per_kwh}元/kWh",
             "数量": f"{config.battery_capacity_kwh}kWh", "金额(元)": round(battery_cost, 2), "类别": "初始投资"},
            {"成本项目": "逆变器", "单价": f"{self.storage.inverter_cost_per_kw}元/kW",
             "数量": f"{config.inverter_power_kw}kW", "金额(元)": round(inverter_cost, 2), "类别": "初始投资"},
            {"成本项目": "PCS", "单价": f"{self.storage.pcs_cost_per_kw}元/kW",
             "数量": f"{config.inverter_power_kw}kW", "金额(元)": round(pcs_cost, 2), "类别": "初始投资"},
            {"成本项目": "安装费", "单价": f"{self.storage.installation_rate*100}%",
             "数量": "-", "金额(元)": round(install_cost, 2), "类别": "初始投资"},
            {"成本项目": "其他费用", "单价": f"{self.storage.other_cost_rate*100}%",
             "数量": "-", "金额(元)": round(other_cost, 2), "类别": "初始投资"},
            {"成本项目": "初始投资合计", "单价": "-", "数量": "-",
             "金额(元)": round(config.total_investment, 2), "类别": "初始投资"},
            {"成本项目": "年运维费", "单价": f"{self.storage.annual_om_cost_rate*100}%",
             "数量": "-", "金额(元)": round(annual_om, 2), "类别": "运营成本"},
            {"成本项目": "年保险费", "单价": f"{self.storage.insurance_rate*100}%",
             "数量": "-", "金额(元)": round(annual_insurance, 2), "类别": "运营成本"},
            {"成本项目": "年运维合计", "单价": "-", "数量": "-",
             "金额(元)": round(total_annual_om, 2), "类别": "运营成本"},
            {"成本项目": "全周期运维合计", "单价": "-", "数量": f"{self.storage.project_life_years}年",
             "金额(元)": round(lifetime_om, 2), "类别": "运营成本"},
            {"成本项目": "全周期总成本", "单价": "-", "数量": "-",
             "金额(元)": round(config.total_investment + lifetime_om, 2), "类别": "合计"},
        ]

        return pd.DataFrame(records)

    # ------------------------------------------------------------------
    # 风险评估
    # ------------------------------------------------------------------
    def _assess_risk(self, config: OptimalConfig, df: pd.DataFrame) -> dict:
        """评估项目风险。"""
        risks = []

        # 回收期风险
        if config.simple_payback_years > 8:
            risks.append({"风险项": "回收期过长", "风险等级": "高",
                          "说明": f"静态回收期{config.simple_payback_years}年，超过8年"})
        elif config.simple_payback_years > 6:
            risks.append({"风险项": "回收期较长", "风险等级": "中",
                          "说明": f"静态回收期{config.simple_payback_years}年"})
        else:
            risks.append({"风险项": "回收期", "风险等级": "低",
                          "说明": f"静态回收期{config.simple_payback_years}年，投资回报良好"})

        # IRR风险
        if config.irr < 0.05:
            risks.append({"风险项": "IRR偏低", "风险等级": "高",
                          "说明": f"内部收益率{config.irr*100:.1f}%，低于5%"})
        elif config.irr < 0.08:
            risks.append({"风险项": "IRR一般", "风险等级": "中",
                          "说明": f"内部收益率{config.irr*100:.1f}%"})
        else:
            risks.append({"风险项": "IRR良好", "风险等级": "低",
                          "说明": f"内部收益率{config.irr*100:.1f}%，超过8%"})

        # 电价政策风险
        risks.append({"风险项": "电价政策变化", "风险等级": "中",
                       "说明": "电价政策调整可能影响峰谷价差，建议关注政策动态"})

        # 电池衰减风险
        risks.append({"风险项": "电池衰减", "风险等级": "低",
                       "说明": f"年衰减率{self.storage.annual_degradation*100:.1f}%，{self.storage.calendar_life_years}年日历寿命"})

        return {
            "风险列表": pd.DataFrame(risks),
            "整体风险等级": "中" if any(r["风险等级"] == "中" for r in risks) else ("高" if any(r["风险等级"] == "高" for r in risks) else "低"),
        }

    # ------------------------------------------------------------------
    # 资方/客户收益分析
    # ------------------------------------------------------------------
    def analyze_investor_customer(self, config: OptimalConfig, df: pd.DataFrame,
                                   investor_cfg: InvestorConfig) -> InvestorCustomerReport:
        """资方/客户收益分离分析。

        支持三种模式：
        - self: 自投模式，全部收益归己，可含贷款
        - loan: 贷款模式，计算自有资金/贷款资金比例后的净收益
        - emc: 合同能源管理，资方投资，按比例分成
        """
        mode = investor_cfg.investment_mode
        report = InvestorCustomerReport(investment_mode=mode)

        if mode == "self":
            report = self._analyze_self_investment(config, df, investor_cfg)
        elif mode == "loan":
            report = self._analyze_loan_mode(config, df, investor_cfg)
        elif mode == "emc":
            report = self._analyze_emc_mode(config, df, investor_cfg)
        else:
            report = self._analyze_self_investment(config, df, investor_cfg)

        return report

    def _analyze_self_investment(self, config: OptimalConfig, df: pd.DataFrame,
                                  investor_cfg: InvestorConfig) -> InvestorCustomerReport:
        """自投模式分析。"""
        years = self.storage.project_life_years
        degradation = self.storage.annual_degradation
        inflation = self.storage.electricity_inflation
        om_rate = self.storage.annual_om_cost_rate + self.storage.insurance_rate

        investor_records = []
        customer_records = []
        cumulative_investor = -config.total_investment

        for year in range(1, years + 1):
            cap_factor = (1 - degradation) ** year
            price_factor = (1 + inflation) ** year
            annual_savings = config.annual_savings * cap_factor * price_factor
            annual_om = config.total_investment * om_rate
            net_revenue = annual_savings - annual_om

            cumulative_investor += net_revenue

            investor_records.append({
                "年份": year,
                "年节省电费(元)": round(annual_savings, 2),
                "年运维成本(元)": round(annual_om, 2),
                "年净收益(元)": round(net_revenue, 2),
                "累计收益(元)": round(cumulative_investor, 2),
                "容量保持率(%)": round(cap_factor * 100, 2),
            })
            customer_records.append({
                "年份": year,
                "年节省电费(元)": round(annual_savings, 2),
                "年分成收益(元)": round(net_revenue, 2),
                "累计收益(元)": round(cumulative_investor, 2),
            })

        investor_df = pd.DataFrame(investor_records)
        customer_df = pd.DataFrame(customer_records)

        total_investor_net = sum(r["年净收益(元)"] for r in investor_records)
        investor_irr = config.irr

        return InvestorCustomerReport(
            investment_mode="自投",
            investor_yearly=investor_df,
            customer_yearly=customer_df,
            loan_schedule=pd.DataFrame(),
            investor_summary={
                "总投资(元)": config.total_investment,
                "年净收益(元)": config.annual_revenue,
                "全周期净收益(元)": round(total_investor_net, 2),
                "回收期(年)": config.simple_payback_years,
                "IRR": investor_irr,
                "NPV(元)": config.npv,
            },
            customer_summary={
                "年节省电费(元)": config.annual_savings,
                "全周期节省(元)": round(total_investor_net + config.total_investment, 2),
                "无需额外投资": "是",
            },
        )

    def _analyze_loan_mode(self, config: OptimalConfig, df: pd.DataFrame,
                            investor_cfg: InvestorConfig) -> InvestorCustomerReport:
        """贷款模式分析。"""
        total_investment = config.total_investment
        loan_ratio = investor_cfg.loan_ratio
        interest_rate = investor_cfg.loan_interest_rate
        loan_years = investor_cfg.loan_years

        loan_amount = total_investment * loan_ratio
        equity_amount = total_investment * (1 - loan_ratio)

        # 等额本息还款
        monthly_rate = interest_rate / 12
        total_months = loan_years * 12
        if monthly_rate > 0:
            monthly_payment = loan_amount * monthly_rate * (1 + monthly_rate) ** total_months / \
                              ((1 + monthly_rate) ** total_months - 1)
        else:
            monthly_payment = loan_amount / total_months if total_months > 0 else 0

        annual_payment = monthly_payment * 12

        years = self.storage.project_life_years
        degradation = self.storage.annual_degradation
        inflation = self.storage.electricity_inflation
        om_rate = self.storage.annual_om_cost_rate + self.storage.insurance_rate

        investor_records = []
        loan_records = []
        remaining_loan = loan_amount
        cumulative_net = -equity_amount

        for year in range(1, years + 1):
            cap_factor = (1 - degradation) ** year
            price_factor = (1 + inflation) ** year
            annual_savings = config.annual_savings * cap_factor * price_factor
            annual_om = total_investment * om_rate

            # 贷款还款（前N年还款，之后无贷款）
            if year <= loan_years:
                year_interest = remaining_loan * interest_rate
                year_principal = annual_payment - year_interest
                remaining_loan = max(0, remaining_loan - year_principal)
                loan_cost = annual_payment
            else:
                year_interest = 0
                year_principal = 0
                loan_cost = 0

            net_revenue = annual_savings - annual_om - loan_cost
            cumulative_net += net_revenue

            investor_records.append({
                "年份": year,
                "年节省电费(元)": round(annual_savings, 2),
                "年运维成本(元)": round(annual_om, 2),
                "年还款(元)": round(loan_cost, 2),
                "年净收益(元)": round(net_revenue, 2),
                "累计净收益(元)": round(cumulative_net, 2),
            })

            if year <= loan_years:
                loan_records.append({
                    "年份": year,
                    "年初余额(元)": round(remaining_loan + year_principal, 2),
                    "年还款(元)": round(annual_payment, 2),
                    "年利息(元)": round(year_interest, 2),
                    "年本金(元)": round(year_principal, 2),
                    "年末余额(元)": round(remaining_loan, 2),
                })

        investor_df = pd.DataFrame(investor_records)
        loan_df = pd.DataFrame(loan_records)
        customer_df = investor_df.copy()

        # 计算自有资金IRR
        cash_flows = [-equity_amount]
        for r in investor_records:
            cash_flows.append(r["年净收益(元)"])
        investor_irr = self._compute_irr_simple(cash_flows)

        total_net = sum(r["年净收益(元)"] for r in investor_records)

        return InvestorCustomerReport(
            investment_mode="贷款",
            investor_yearly=investor_df,
            customer_yearly=customer_df,
            loan_schedule=loan_df,
            investor_summary={
                "总投资(元)": total_investment,
                "自有资金(元)": round(equity_amount, 2),
                "贷款金额(元)": round(loan_amount, 2),
                "贷款利率": interest_rate,
                "贷款期限(年)": loan_years,
                "年还款额(元)": round(annual_payment, 2),
                "全周期净收益(元)": round(total_net, 2),
                "自有资金IRR": round(investor_irr, 4),
                "全周期节省(元)": round(total_net + equity_amount, 2),
            },
            customer_summary={
                "年节省电费(元)": config.annual_savings,
                "全周期净收益(元)": round(total_net, 2),
            },
        )

    def _analyze_emc_mode(self, config: OptimalConfig, df: pd.DataFrame,
                           investor_cfg: InvestorConfig) -> InvestorCustomerReport:
        """EMC（合同能源管理）模式分析。"""
        investor_ratio = investor_cfg.investor_share_ratio
        customer_ratio = investor_cfg.customer_share_ratio

        years = self.storage.project_life_years
        degradation = self.storage.annual_degradation
        inflation = self.storage.electricity_inflation
        om_rate = self.storage.annual_om_cost_rate + self.storage.insurance_rate

        investor_records = []
        customer_records = []
        cumulative_investor = -config.total_investment
        cumulative_customer = 0

        for year in range(1, years + 1):
            cap_factor = (1 - degradation) ** year
            price_factor = (1 + inflation) ** year
            annual_savings = config.annual_savings * cap_factor * price_factor
            annual_om = config.total_investment * om_rate
            net_revenue = annual_savings - annual_om

            # 分成
            investor_share = net_revenue * investor_ratio
            customer_share = net_revenue * customer_ratio

            cumulative_investor += investor_share
            cumulative_customer += customer_share

            investor_records.append({
                "年份": year,
                "总节省(元)": round(annual_savings, 2),
                "运维成本(元)": round(annual_om, 2),
                "净收益(元)": round(net_revenue, 2),
                f"资方分成({investor_ratio*100:.0f}%)(元)": round(investor_share, 2),
                "资方累计收益(元)": round(cumulative_investor, 2),
            })
            customer_records.append({
                "年份": year,
                "总节省(元)": round(annual_savings, 2),
                f"客户分成({customer_ratio*100:.0f}%)(元)": round(customer_share, 2),
                "客户累计收益(元)": round(cumulative_customer, 2),
                "客户零投资": "是",
            })

        investor_df = pd.DataFrame(investor_records)
        customer_df = pd.DataFrame(customer_records)

        # 资方IRR
        cash_flows = [-config.total_investment]
        for r in investor_records:
            cash_flows.append(r[f"资方分成({investor_ratio*100:.0f}%)(元)"])
        investor_irr = self._compute_irr_simple(cash_flows)

        # 资方回收期
        for i, r in enumerate(investor_records):
            if sum(rec[f"资方分成({investor_ratio*100:.0f}%)(元)"] for rec in investor_records[:i+1]) >= config.total_investment:
                investor_payback = i + 1
                break
        else:
            investor_payback = float('inf')

        return InvestorCustomerReport(
            investment_mode="合同能源管理(EMC)",
            investor_yearly=investor_df,
            customer_yearly=customer_df,
            loan_schedule=pd.DataFrame(),
            investor_summary={
                "总投资(元)": config.total_investment,
                "资方分成比例": f"{investor_ratio*100:.0f}%",
                "年均净收益(元)": round(cumulative_investor / years, 2),
                "全周期净收益(元)": round(cumulative_investor, 2),
                "资方回收期(年)": investor_payback,
                "资方IRR": round(investor_irr, 4),
            },
            customer_summary={
                "客户投资": "0元（零投资）",
                "客户分成比例": f"{customer_ratio*100:.0f}%",
                "年均分成(元)": round(cumulative_customer / years, 2),
                "全周期分成(元)": round(cumulative_customer, 2),
            },
        )

    @staticmethod
    def _compute_irr_simple(cash_flows: list, max_iter: int = 1000, tol: float = 1e-8) -> float:
        """简单IRR计算（牛顿法）。"""
        rate = 0.1
        for _ in range(max_iter):
            npv = sum(cf / (1 + rate) ** i for i, cf in enumerate(cash_flows))
            dnpv = sum(-i * cf / (1 + rate) ** (i + 1) for i, cf in enumerate(cash_flows))
            if abs(dnpv) < 1e-12:
                break
            new_rate = rate - npv / dnpv
            if abs(new_rate - rate) < tol:
                return new_rate
            rate = new_rate
        return rate

    def export_investor_report(self, investor_report: InvestorCustomerReport, output_path: str) -> Path:
        """导出资方/客户收益分析到Excel。"""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with pd.ExcelWriter(str(path), engine="openpyxl") as writer:
            # 资方收益
            if not investor_report.investor_yearly.empty:
                investor_report.investor_yearly.to_excel(writer, sheet_name="资方收益", index=False)

            # 客户收益
            if not investor_report.customer_yearly.empty:
                investor_report.customer_yearly.to_excel(writer, sheet_name="客户收益", index=False)

            # 贷款还款计划
            if not investor_report.loan_schedule.empty:
                investor_report.loan_schedule.to_excel(writer, sheet_name="贷款还款计划", index=False)

            # 汇总指标
            summary_data = []
            for k, v in investor_report.investor_summary.items():
                summary_data.append({"指标": f"资方-{k}", "数值": v})
            for k, v in investor_report.customer_summary.items():
                summary_data.append({"指标": f"客户-{k}", "数值": v})
            pd.DataFrame(summary_data).to_excel(writer, sheet_name="收益汇总", index=False)

        logger.info("资方/客户收益报告已导出: %s", path)
        return path

    # ------------------------------------------------------------------
    # 导出报告
    # ------------------------------------------------------------------
    def export_report(self, report: RevenueReport, output_path: str) -> Path:
        """导出收益分析报告到Excel。"""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with pd.ExcelWriter(str(path), engine="openpyxl") as writer:
            # 1. 收益汇总
            summary_df = pd.DataFrame([
                {"指标": k, "数值": v} for k, v in report.summary.items()
            ])
            summary_df.to_excel(writer, sheet_name="收益汇总", index=False)

            # 2. 年度收益明细
            if not report.yearly_details.empty:
                report.yearly_details.to_excel(writer, sheet_name="年度收益明细", index=False)

            # 3. 月度收益估算
            if not report.monthly_estimate.empty:
                report.monthly_estimate.to_excel(writer, sheet_name="月度收益估算", index=False)

            # 4. 敏感性分析
            if not report.sensitivity_analysis.empty:
                report.sensitivity_analysis.to_excel(writer, sheet_name="敏感性分析", index=False)

            # 5. 成本分解
            if not report.cost_breakdown.empty:
                report.cost_breakdown.to_excel(writer, sheet_name="成本分解", index=False)

            # 6. 风险评估
            if report.risk_assessment:
                risk_df = report.risk_assessment.get("风险列表", pd.DataFrame())
                if not risk_df.empty:
                    risk_df.to_excel(writer, sheet_name="风险评估", index=False)
                    # 在下方写入整体风险等级
                    overall = report.risk_assessment.get("整体风险等级", "未知")
                    ws = writer.sheets["风险评估"]
                    ws.cell(row=len(risk_df) + 3, column=1, value="整体风险等级")
                    ws.cell(row=len(risk_df) + 3, column=2, value=overall)

        logger.info("收益分析报告已导出: %s", path)
        return path
