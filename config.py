"""储能配置AGENT - 全局配置"""

from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path


@dataclass
class ElectricityRateConfig:
    """电价配置"""
    # 尖峰平谷电价 (元/kWh)
    peak_price: float = 1.2           # 尖峰电价
    high_price: float = 1.0           # 高峰电价
    flat_price: float = 0.65          # 平段电价
    valley_price: float = 0.35        # 谷段电价

    # 尖峰平谷时段 (24小时制)
    peak_hours: tuple = (10, 12, 19, 22)       # 尖峰时段
    high_hours: tuple = (8, 10, 15, 19)        # 高峰时段
    valley_hours: tuple = (0, 8)               # 谷段时段
    # 其余为平段

    # 需量电费 (元/kW/月)
    demand_charge: float = 38.0

    # 基本电费 (元/kVA/月)
    capacity_charge: float = 28.0


@dataclass
class StorageConfig:
    """储能系统配置参数"""
    # 电池参数
    battery_cost_per_kwh: float = 1200.0       # 电池成本 (元/kWh)
    inverter_cost_per_kw: float = 500.0        # 逆变器成本 (元/kW)
    pcs_cost_per_kw: float = 300.0             # PCS成本 (元/kW)
    installation_rate: float = 0.15            # 安装费率 (占设备成本比例)
    other_cost_rate: float = 0.10              # 其他费用比例

    # 运行参数
    charge_efficiency: float = 0.95            # 充电效率
    discharge_efficiency: float = 0.95         # 放电效率
    depth_of_discharge: float = 0.90           # 放电深度(DOD)
    cycle_life: int = 6000                     # 循环寿命 (次)
    calendar_life_years: int = 15              # 日历寿命 (年)
    annual_degradation: float = 0.02           # 年衰减率

    # 运维参数
    annual_om_cost_rate: float = 0.02          # 年运维成本 (占投资比例)
    insurance_rate: float = 0.005              # 保险费率

    # 经济参数
    discount_rate: float = 0.06                # 折现率
    project_life_years: int = 15               # 项目寿命 (年)
    electricity_inflation: float = 0.03        # 电价年增长率

    # 约束条件
    min_capacity_kwh: float = 100.0            # 最小容量 (kWh)
    max_capacity_kwh: float = 100000.0         # 最大容量 (kWh)
    min_power_kw: float = 50.0                 # 最小功率 (kW)
    max_power_kw: float = 20000.0              # 最大功率 (kW)
    max_charge_discharge_ratio: float = 0.5    # 最大充放电倍率 (C-rate)


@dataclass
class LLMConfig:
    """大模型配置"""
    provider: str = "qwen"                     # qwen / wenxin
    api_key: str = ""                          # 从环境变量读取
    model: str = "qwen-max"                    # 文本模型
    vision_model: str = "qwen-vl-max"          # 视觉模型
    base_url: str = ""                         # 自定义base_url
    temperature: float = 0.1
    max_tokens: int = 4096
    enabled: bool = True                       # 是否启用LLM


@dataclass
class InvestorConfig:
    """资方/客户收益分配配置"""
    # 投资模式: self(自投) / loan(贷款) / emc(合同能源管理)
    investment_mode: str = "self"

    # 贷款参数
    loan_ratio: float = 0.7                    # 贷款比例 (70%贷款)
    loan_interest_rate: float = 0.045          # 贷款年利率 4.5%
    loan_years: int = 10                       # 贷款期限 (年)

    # EMC收益分成
    investor_share_ratio: float = 0.7          # 资方分成比例
    customer_share_ratio: float = 0.3          # 客户分成比例
    guaranteed_savings_rate: float = 0.0       # 给客户的保底节省率


@dataclass
class AgentConfig:
    """AGENT主配置"""
    # 路径配置
    input_dir: Path = Path("input")            # 输入文件目录
    output_dir: Path = Path("output")          # 输出文件目录
    temp_dir: Path = Path("temp")              # 临时文件目录

    # OCR配置
    ocr_language: str = "ch_sim+en"            # OCR语言 (easyocr)
    tesseract_lang: str = "chi_sim+eng"        # Tesseract语言

    # 电价配置
    rate_config: ElectricityRateConfig = field(default_factory=ElectricityRateConfig)

    # 储能配置
    storage_config: StorageConfig = field(default_factory=StorageConfig)

    # 大模型配置
    llm_config: LLMConfig = field(default_factory=LLMConfig)

    # 资方/客户配置
    investor_config: InvestorConfig = field(default_factory=InvestorConfig)

    def __post_init__(self):
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
