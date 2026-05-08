"""储能配置AGENT - 储能优化配置模块
根据电费数据生成最优储能配置参数。
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from config import ElectricityRateConfig, StorageConfig

logger = logging.getLogger(__name__)


@dataclass
class OptimalConfig:
    """最优储能配置结果"""
    # 基本参数
    battery_capacity_kwh: float = 0.0      # 电池容量 (kWh)
    inverter_power_kw: float = 0.0         # 逆变器/PCS功率 (kW)
    charge_discharge_ratio: float = 0.0    # 充放电倍率 (C-rate)
    duration_hours: float = 0.0            # 储能时长 (小时)

    # 充放电策略
    charge_start_hour: int = 0             # 充电开始时间
    charge_end_hour: int = 0               # 充电结束时间
    discharge_start_hour: int = 0          # 放电开始时间
    discharge_end_hour: int = 0            # 放电结束时间
    daily_charge_kwh: float = 0.0          # 日充电量
    daily_discharge_kwh: float = 0.0       # 日放电量

    # 经济参数
    total_investment: float = 0.0          # 总投资 (元)
    annual_savings: float = 0.0            # 年节省电费 (元)
    annual_revenue: float = 0.0            # 年净收益 (元，扣除运维)
    simple_payback_years: float = 0.0      # 静态回收期 (年)
    npv: float = 0.0                       # 净现值 (元)
    irr: float = 0.0                       # 内部收益率
    lcoe: float = 0.0                      # 度电成本 (元/kWh)

    # 年度数据
    yearly_data: list = field(default_factory=list)


class StorageOptimizer:
    """储能配置优化器"""

    def __init__(self, rate_config: ElectricityRateConfig = None, storage_config: StorageConfig = None):
        self.rate = rate_config or ElectricityRateConfig()
        self.storage = storage_config or StorageConfig()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def optimize(self, electricity_df: pd.DataFrame) -> OptimalConfig:
        """根据电费数据优化储能配置。

        Args:
            electricity_df: DataExtractor.to_dataframe() 的输出

        Returns:
            最优储能配置
        """
        if electricity_df.empty:
            raise ValueError("电费数据为空，无法优化")

        # 1. 分析用电特征
        load_profile = self._analyze_load_profile(electricity_df)

        # 2. 计算最优容量和功率
        capacity_kwh, power_kw = self._optimize_capacity_power(load_profile, electricity_df)

        # 3. 确定最优充放电策略
        charge_strategy = self._optimize_charge_strategy(load_profile, capacity_kwh, power_kw)

        # 4. 计算经济指标
        config = self._calculate_economics(
            capacity_kwh, power_kw, charge_strategy, electricity_df, load_profile
        )

        return config

    # ------------------------------------------------------------------
    # 负荷分析
    # ------------------------------------------------------------------
    def _analyze_load_profile(self, df: pd.DataFrame) -> dict:
        """分析用电负荷特征。"""
        total_kwh = df["总电量(kWh)"].mean()
        peak_kwh = df["尖峰电量(kWh)"].mean()
        high_kwh = df["高峰电量(kWh)"].mean()
        flat_kwh = df["平段电量(kWh)"].mean()
        valley_kwh = df["谷段电量(kWh)"].mean()
        max_demand = df["最大需量(kW)"].max() if "最大需量(kW)" in df.columns else 0

        # 估算日均用电
        days_per_month = 30
        daily_kwh = total_kwh / days_per_month

        # 估算各时段日均用电
        daily_peak = peak_kwh / days_per_month
        daily_high = high_kwh / days_per_month
        daily_flat = flat_kwh / days_per_month
        daily_valley = valley_kwh / days_per_month

        # 估算平均负荷 (kW)
        avg_load = daily_kwh / 24
        peak_load = max_demand if max_demand > 0 else avg_load * 2.5

        return {
            "total_monthly_kwh": total_kwh,
            "peak_monthly_kwh": peak_kwh,
            "high_monthly_kwh": high_kwh,
            "flat_monthly_kwh": flat_kwh,
            "valley_monthly_kwh": valley_kwh,
            "daily_kwh": daily_kwh,
            "daily_peak": daily_peak,
            "daily_high": daily_high,
            "daily_flat": daily_flat,
            "daily_valley": daily_valley,
            "avg_load_kw": avg_load,
            "peak_load_kw": peak_load,
            "max_demand_kw": max_demand,
        }

    # ------------------------------------------------------------------
    # 容量功率优化
    # ------------------------------------------------------------------
    def _optimize_capacity_power(self, load_profile: dict, df: pd.DataFrame) -> tuple:
        """优化储能容量和功率。"""
        daily_kwh = load_profile["daily_kwh"]
        daily_valley = load_profile["daily_valley"]
        daily_peak = load_profile["daily_peak"]
        daily_high = load_profile["daily_high"]
        peak_load = load_profile["peak_load_kw"]

        # 谷段可充电量 = 谷段用电量 * 充电比例系数
        # 保守取谷段用电量的80%作为可充空间（考虑变压器容量限制）
        chargeable_kwh = daily_valley * 0.8

        # 峰段需放电量 = (尖峰+高峰)用电量 * 削峰比例
        # 目标：削峰填谷，将谷段充电的电量在峰段放出
        dischargeable_kwh = (daily_peak + daily_high) * 0.6

        # 储能容量取两者较小值（受限于可充电量）
        optimal_kwh = min(chargeable_kwh, dischargeable_kwh)

        # 考虑充放电效率
        effective_kwh = optimal_kwh / (self.storage.charge_efficiency * self.storage.discharge_efficiency)

        # 考虑放电深度
        nominal_kwh = effective_kwh / self.storage.depth_of_discharge

        # 储能时长：通常2-4小时
        # 根据负荷曲线选择最优时长
        duration = self._optimize_duration(nominal_kwh, load_profile)

        # 功率 = 容量 / 时长
        power_kw = nominal_kwh / duration

        # 约束条件
        nominal_kwh = max(self.storage.min_capacity_kwh, min(nominal_kwh, self.storage.max_capacity_kwh))
        power_kw = max(self.storage.min_power_kw, min(power_kw, self.storage.max_power_kw))

        # 功率不超过最大需量的50%（避免过度配置）
        if peak_load > 0:
            power_kw = min(power_kw, peak_load * 0.5)

        logger.info("优化结果: 容量=%.0f kWh, 功率=%.0f kW, 时长=%.1f h",
                     nominal_kwh, power_kw, duration)
        return nominal_kwh, power_kw

    def _optimize_duration(self, capacity_kwh: float, load_profile: dict) -> float:
        """优化储能时长。"""
        daily_peak = load_profile["daily_peak"]
        daily_high = load_profile["daily_high"]
        peak_load = load_profile["peak_load_kw"]

        # 峰段持续时间（小时）
        peak_duration = 0
        for start, end in zip(self.rate.peak_hours[::2], self.rate.peak_hours[1::2]):
            peak_duration += end - start
        for start, end in zip(self.rate.high_hours[::2], self.rate.high_hours[1::2]):
            peak_duration += end - start

        # 默认2小时
        if peak_duration <= 0:
            peak_duration = 4

        # 时长约束
        duration = min(4.0, max(1.0, peak_duration / 2))
        return round(duration, 1)

    # ------------------------------------------------------------------
    # 充放电策略优化
    # ------------------------------------------------------------------
    def _optimize_charge_strategy(self, load_profile: dict, capacity_kwh: float, power_kw: float) -> dict:
        """优化充放电时间策略。"""
        valley_start, valley_end = self.rate.valley_hours[0], self.rate.valley_hours[1]

        # 充电策略：在谷段充电
        charge_start = valley_start
        charge_end = valley_end

        # 放电策略：在尖峰+高峰段放电
        peak_periods = list(self.rate.peak_hours) + list(self.rate.high_hours)
        discharge_start = min(peak_periods[0::2]) if peak_periods else 10
        discharge_end = max(peak_periods[1::2]) if peak_periods else 22

        # 计算日充放电量
        charge_hours = charge_end - charge_start
        discharge_hours = discharge_end - discharge_start

        daily_charge = min(power_kw * charge_hours, capacity_kwh)
        daily_discharge = daily_charge * self.storage.charge_efficiency * self.storage.discharge_efficiency

        return {
            "charge_start": charge_start,
            "charge_end": charge_end,
            "discharge_start": discharge_start,
            "discharge_end": discharge_end,
            "daily_charge_kwh": daily_charge,
            "daily_discharge_kwh": daily_discharge,
        }

    # ------------------------------------------------------------------
    # 经济性计算
    # ------------------------------------------------------------------
    def _calculate_economics(self, capacity_kwh: float, power_kw: float,
                              strategy: dict, df: pd.DataFrame, load_profile: dict) -> OptimalConfig:
        """计算储能系统的经济指标。"""
        # 1. 投资成本
        battery_cost = capacity_kwh * self.storage.battery_cost_per_kwh
        inverter_cost = power_kw * self.storage.inverter_cost_per_kw
        pcs_cost = power_kw * self.storage.pcs_cost_per_kw
        equipment_cost = battery_cost + inverter_cost + pcs_cost
        installation_cost = equipment_cost * self.storage.installation_rate
        other_cost = equipment_cost * self.storage.other_cost_rate
        total_investment = equipment_cost + installation_cost + other_cost

        # 2. 年节省电费计算
        annual_savings = self._calculate_annual_savings(strategy, df, load_profile)

        # 3. 年运维成本
        annual_om = total_investment * self.storage.annual_om_cost_rate
        annual_insurance = total_investment * self.storage.insurance_rate
        annual_cost = annual_om + annual_insurance

        # 4. 年净收益
        annual_revenue = annual_savings - annual_cost

        # 5. 静态回收期
        payback = total_investment / annual_revenue if annual_revenue > 0 else float('inf')

        # 6. NPV 和 IRR
        npv, irr, yearly_data = self._calculate_npv_irr(
            total_investment, annual_revenue, annual_savings, annual_cost
        )

        # 7. 度电成本 LCOE
        total_discharge_kwh = sum(y["年放电量(kWh)"] for y in yearly_data)
        total_cost = total_investment + sum(y["年成本(元)"] for y in yearly_data)
        lcoe = total_cost / total_discharge_kwh if total_discharge_kwh > 0 else 0

        config = OptimalConfig(
            battery_capacity_kwh=round(capacity_kwh, 2),
            inverter_power_kw=round(power_kw, 2),
            charge_discharge_ratio=round(power_kw / capacity_kwh, 4) if capacity_kwh > 0 else 0,
            duration_hours=round(capacity_kwh / power_kw, 1) if power_kw > 0 else 0,
            charge_start_hour=strategy["charge_start"],
            charge_end_hour=strategy["charge_end"],
            discharge_start_hour=strategy["discharge_start"],
            discharge_end_hour=strategy["discharge_end"],
            daily_charge_kwh=round(strategy["daily_charge_kwh"], 2),
            daily_discharge_kwh=round(strategy["daily_discharge_kwh"], 2),
            total_investment=round(total_investment, 2),
            annual_savings=round(annual_savings, 2),
            annual_revenue=round(annual_revenue, 2),
            simple_payback_years=round(payback, 2),
            npv=round(npv, 2),
            irr=round(irr, 4),
            lcoe=round(lcoe, 4),
            yearly_data=yearly_data,
        )

        return config

    def _calculate_annual_savings(self, strategy: dict, df: pd.DataFrame, load_profile: dict) -> float:
        """计算年节省电费。"""
        daily_discharge = strategy["daily_discharge_kwh"]
        if daily_discharge <= 0:
            return 0

        # 峰谷价差
        peak_price = max(self.rate.peak_price, self.rate.high_price)
        valley_price = self.rate.valley_price
        price_spread = peak_price - valley_price

        # 年运行天数
        operating_days = 365

        # 年节省 = 日放电量 * 峰谷价差 * 年运行天数
        annual_energy_savings = daily_discharge * price_spread * operating_days

        # 需量电费节省（如有需量管理需求）
        demand_savings = 0
        max_demand = load_profile.get("max_demand_kw", 0)
        if max_demand > 0:
            # 假设储能可降低需量10-20%
            demand_reduction = max_demand * 0.15
            demand_savings = demand_reduction * self.rate.demand_charge * 12

        total_savings = annual_energy_savings + demand_savings
        logger.info("年节省电费: %.2f 元 (电量节省: %.2f, 需量节省: %.2f)",
                     total_savings, annual_energy_savings, demand_savings)
        return total_savings

    def _calculate_npv_irr(self, investment: float, annual_net_revenue: float,
                            annual_savings: float, annual_cost: float) -> tuple:
        """计算NPV和IRR。"""
        years = self.storage.project_life_years
        discount_rate = self.storage.discount_rate
        inflation = self.storage.electricity_inflation
        degradation = self.storage.annual_degradation

        yearly_data = []
        cash_flows = [-investment]

        cumulative_cf = -investment
        npv = -investment
        payback_year = None

        for year in range(1, years + 1):
            # 考虑电池衰减和电价增长
            capacity_factor = (1 - degradation) ** year
            price_factor = (1 + inflation) ** year

            year_savings = annual_savings * capacity_factor * price_factor
            year_cost = annual_cost
            year_net = year_savings - year_cost
            year_discharge = annual_net_revenue / (annual_savings - annual_cost) * year_savings if annual_savings > 0 else 0

            # 计算年放电量
            daily_discharge_est = year_savings / ((self.rate.peak_price - self.rate.valley_price) * 365) if (self.rate.peak_price - self.rate.valley_price) > 0 else 0
            annual_discharge = daily_discharge_est * 365

            cash_flows.append(year_net)
            cumulative_cf += year_net
            npv += year_net / (1 + discount_rate) ** year

            if payback_year is None and cumulative_cf >= 0:
                payback_year = year

            yearly_data.append({
                "年份": year,
                "年节省(元)": round(year_savings, 2),
                "年成本(元)": round(year_cost, 2),
                "年净收益(元)": round(year_net, 2),
                "累计现金流(元)": round(cumulative_cf, 2),
                "年放电量(kWh)": round(annual_discharge, 2),
                "容量保持率(%)": round(capacity_factor * 100, 2),
                "电价系数": round(price_factor, 4),
            })

        # 计算IRR
        irr = self._compute_irr(cash_flows)

        return npv, irr, yearly_data

    @staticmethod
    def _compute_irr(cash_flows: list, max_iter: int = 1000, tol: float = 1e-8) -> float:
        """使用牛顿法计算IRR。"""
        rate = 0.1  # 初始猜测
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

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    def export_config_to_excel(self, config: OptimalConfig, output_path: str) -> Path:
        """将储能配置结果导出为Excel。"""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with pd.ExcelWriter(str(path), engine="openpyxl") as writer:
            # 配置参数表
            config_data = {
                "参数": [
                    "电池容量(kWh)", "逆变器功率(kW)", "充放电倍率(C)", "储能时长(h)",
                    "充电开始时间", "充电结束时间", "放电开始时间", "放电结束时间",
                    "日充电量(kWh)", "日放电量(kWh)",
                    "总投资(元)", "年节省电费(元)", "年净收益(元)",
                    "静态回收期(年)", "净现值NPV(元)", "内部收益率IRR", "度电成本LCOE(元/kWh)",
                ],
                "数值": [
                    config.battery_capacity_kwh, config.inverter_power_kw,
                    config.charge_discharge_ratio, config.duration_hours,
                    f"{config.charge_start_hour}:00", f"{config.charge_end_hour}:00",
                    f"{config.discharge_start_hour}:00", f"{config.discharge_end_hour}:00",
                    config.daily_charge_kwh, config.daily_discharge_kwh,
                    config.total_investment, config.annual_savings, config.annual_revenue,
                    config.simple_payback_years, config.npv, config.irr, config.lcoe,
                ],
            }
            pd.DataFrame(config_data).to_excel(writer, sheet_name="储能配置参数", index=False)

            # 投资成本明细
            cost_data = {
                "项目": ["电池系统", "逆变器", "PCS", "设备小计", "安装费", "其他费用", "总投资"],
                "单价(元)": [
                    self.storage.battery_cost_per_kwh,
                    self.storage.inverter_cost_per_kw,
                    self.storage.pcs_cost_per_kw,
                    "-", "-", "-", "-",
                ],
                "数量": [
                    config.battery_capacity_kwh,
                    config.inverter_power_kw,
                    config.inverter_power_kw,
                    "-", "-", "-", "-",
                ],
                "金额(元)": [
                    config.battery_capacity_kwh * self.storage.battery_cost_per_kwh,
                    config.inverter_power_kw * self.storage.inverter_cost_per_kw,
                    config.inverter_power_kw * self.storage.pcs_cost_per_kw,
                    "-", "-", "-",
                    config.total_investment,
                ],
            }
            pd.DataFrame(cost_data).to_excel(writer, sheet_name="投资成本明细", index=False)

            # 年度收益预测
            if config.yearly_data:
                pd.DataFrame(config.yearly_data).to_excel(writer, sheet_name="年度收益预测", index=False)

        logger.info("储能配置已导出: %s", path)
        return path
