"""储能配置AGENT - 电费数据提取模块
从解析后的文档中提取电费相关数据，转换为结构化Excel。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ElectricityBillData:
    """电费账单数据结构"""
    month: str = ""                      # 月份 (YYYY-MM)
    total_kwh: float = 0.0              # 总用电量 (kWh)
    peak_kwh: float = 0.0               # 尖峰电量
    high_kwh: float = 0.0               # 高峰电量
    flat_kwh: float = 0.0               # 平段电量
    valley_kwh: float = 0.0             # 谷段电量
    max_demand_kw: float = 0.0          # 最大需量 (kW)
    contract_capacity_kva: float = 0.0  # 合同容量 (kVA)
    total_amount: float = 0.0           # 总电费 (元)
    energy_charge: float = 0.0          # 电量电费
    demand_charge: float = 0.0          # 需量电费
    capacity_charge: float = 0.0        # 容量电费
    power_factor: float = 0.9           # 功率因数
    voltage_level: str = ""             # 电压等级
    peak_price: float = 0.0             # 尖峰电价
    high_price: float = 0.0             # 高峰电价
    flat_price: float = 0.0             # 平段电价
    valley_price: float = 0.0           # 谷段电价
    demand_price: float = 0.0           # 需量电价 元/kW·月


@dataclass
class LoadProfileData:
    """负荷曲线数据"""
    timestamps: list = field(default_factory=list)
    power_kw: list = field(default_factory=list)


class DataExtractor:
    """电费数据提取器"""

    # 关键词映射
    KEYWORD_MAP = {
        "总电量": ["总电量", "总用电量", "合计电量", "总表电量", "有功总"],
        "尖峰": ["尖峰", "尖", "尖峰电量", "尖峰电能"],
        "高峰": ["高峰", "峰", "峰段", "高峰电量", "峰电能"],
        "平段": ["平段", "平", "平段电量", "平电能"],
        "谷段": ["谷段", "谷", "谷段电量", "谷电能"],
        "最大需量": ["最大需量", "需量", "最高需量", "最大负荷"],
        "合同容量": ["合同容量", "容量", "变压器容量", "装接容量"],
        "总电费": ["总电费", "电费合计", "应付电费", "电费总额"],
        "电量电费": ["电量电费", "电度电费", "电能电费"],
        "需量电费": ["需量电费", "基本电费(需量)", "按需量"],
        "容量电费": ["容量电费", "基本电费(容量)", "按容量"],
        "功率因数": ["功率因数", "力率", "cosφ"],
        "月份": ["月份", "账单月份", "日期", "账期", "抄表日期"],
        "尖峰电价": ["尖峰电价", "尖电价", "尖峰单价", "尖段电价"],
        "高峰电价": ["高峰电价", "峰电价", "高峰单价", "峰段电价"],
        "平段电价": ["平段电价", "平电价", "平段单价"],
        "谷段电价": ["谷段电价", "谷电价", "低谷电价", "谷段单价"],
        "需量电价": ["需量电价", "需量单价", "基本电费单价"],
    }

    def __init__(self):
        self.bill_data: list[ElectricityBillData] = []
        self.load_profiles: list[LoadProfileData] = []

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def extract_from_parsed(self, parsed_docs: list[dict]) -> pd.DataFrame:
        """从解析后的文档列表中提取电费数据。

        Args:
            parsed_docs: DocumentParser.parse_batch() 的返回结果

        Returns:
            包含所有月份电费数据的DataFrame
        """
        self.bill_data = []

        for doc in parsed_docs:
            if "error" in doc:
                logger.warning("跳过解析失败的文件: %s", doc.get("file"))
                continue

            doc_bills = []

            # 尝试从表格中提取
            for table in doc.get("tables", []):
                bills = self._extract_from_table(table)
                doc_bills.extend(bills)

            # 尝试从文本中提取
            if not doc_bills:
                text = doc.get("text", "")
                bills = self._extract_from_text(text)
                doc_bills.extend(bills)

            self.bill_data.extend(doc_bills)

        # 去重并排序
        self.bill_data = self._deduplicate(self.bill_data)
        self.bill_data.sort(key=lambda x: x.month)

        return self.to_dataframe()

    def extract_from_excel(self, file_path: str | Path) -> pd.DataFrame:
        """直接从已有的Excel/CSV文件提取电费数据。"""
        path = Path(file_path)
        if path.suffix.lower() == ".csv":
            df = pd.read_csv(str(path))
        else:
            df = pd.read_excel(str(path))

        bills = self._extract_from_table(df)
        self.bill_data.extend(bills)
        self.bill_data = self._deduplicate(self.bill_data)
        self.bill_data.sort(key=lambda x: x.month)
        return self.to_dataframe()

    # ------------------------------------------------------------------
    # 表格提取
    # ------------------------------------------------------------------
    def _extract_from_table(self, df: pd.DataFrame) -> list[ElectricityBillData]:
        """从DataFrame中提取电费数据。"""
        bills = []

        # 标准化列名
        col_mapping = self._map_columns(df.columns.tolist())
        if not col_mapping:
            # 尝试转置后识别
            df_t = df.T
            col_mapping = self._map_columns(df_t.columns.tolist())
            if col_mapping:
                df = df_t
            else:
                return bills

        # 逐行提取
        for _, row in df.iterrows():
            bill = ElectricityBillData()

            for field_name, col_idx in col_mapping.items():
                try:
                    value = row.iloc[col_idx] if isinstance(col_idx, int) else row.get(col_idx)
                    value = self._parse_number(value)

                    if field_name == "月份":
                        bill.month = self._parse_month(row.iloc[col_idx] if isinstance(col_idx, int) else row.get(col_idx))
                    elif field_name == "总电量":
                        bill.total_kwh = value
                    elif field_name == "尖峰":
                        bill.peak_kwh = value
                    elif field_name == "高峰":
                        bill.high_kwh = value
                    elif field_name == "平段":
                        bill.flat_kwh = value
                    elif field_name == "谷段":
                        bill.valley_kwh = value
                    elif field_name == "最大需量":
                        bill.max_demand_kw = value
                    elif field_name == "合同容量":
                        bill.contract_capacity_kva = value
                    elif field_name == "总电费":
                        bill.total_amount = value
                    elif field_name == "电量电费":
                        bill.energy_charge = value
                    elif field_name == "需量电费":
                        bill.demand_charge = value
                    elif field_name == "容量电费":
                        bill.capacity_charge = value
                    elif field_name == "功率因数":
                        bill.power_factor = value
                    elif field_name == "尖峰电价":
                        bill.peak_price = value
                    elif field_name == "高峰电价":
                        bill.high_price = value
                    elif field_name == "平段电价":
                        bill.flat_price = value
                    elif field_name == "谷段电价":
                        bill.valley_price = value
                    elif field_name == "需量电价":
                        bill.demand_price = value
                except Exception:
                    continue

            # 只保留有效数据
            if bill.total_kwh > 0 or bill.total_amount > 0:
                # 如果没有分时电量，按比例估算
                if bill.total_kwh > 0 and bill.peak_kwh == 0 and bill.high_kwh == 0:
                    bill = self._estimate_time_of_use(bill)
                bills.append(bill)

        return bills

    def _map_columns(self, columns: list) -> dict:
        """将表头列映射到标准字段。"""
        mapping = {}
        for i, col in enumerate(columns):
            col_str = str(col).strip()
            for field_name, keywords in self.KEYWORD_MAP.items():
                for kw in keywords:
                    if kw in col_str:
                        if field_name not in mapping:
                            mapping[field_name] = i
                        break
        return mapping

    # ------------------------------------------------------------------
    # 文本提取
    # ------------------------------------------------------------------
    def _extract_from_text(self, text: str) -> list[ElectricityBillData]:
        """从纯文本中提取电费数据（OCR结果）。"""
        utility_bill = self._extract_utility_bill_text(text)
        if utility_bill:
            return [utility_bill]

        bills = []
        bill = ElectricityBillData()

        patterns = {
            "总电量": r"总[用]?电量[：:]\s*([\d,.]+)\s*kWh",
            "尖峰": r"尖峰[电量]*[：:]\s*([\d,.]+)",
            "高峰": r"高峰[电量]*[：:]\s*([\d,.]+)",
            "平段": r"平段[电量]*[：:]\s*([\d,.]+)",
            "谷段": r"谷段[电量]*[：:]\s*([\d,.]+)",
            "最大需量": r"最大需量[：:]\s*([\d,.]+)\s*kW",
            "总电费": r"总电费[：:]\s*([\d,.]+)\s*元",
            "功率因数": r"功率因数[：:]\s*([\d.]+)",
            "月份": r"(\d{4}[-/年]\d{1,2}[-月]?)",
        }

        for field_name, pattern in patterns.items():
            match = re.search(pattern, text)
            if match:
                value_str = match.group(1).replace(",", "")
                try:
                    value = float(value_str)
                    if field_name == "总电量":
                        bill.total_kwh = value
                    elif field_name == "尖峰":
                        bill.peak_kwh = value
                    elif field_name == "高峰":
                        bill.high_kwh = value
                    elif field_name == "平段":
                        bill.flat_kwh = value
                    elif field_name == "谷段":
                        bill.valley_kwh = value
                    elif field_name == "最大需量":
                        bill.max_demand_kw = value
                    elif field_name == "总电费":
                        bill.total_amount = value
                    elif field_name == "功率因数":
                        bill.power_factor = value
                    elif field_name == "月份":
                        bill.month = self._parse_month(value_str)
                except ValueError:
                    pass

        if bill.total_kwh > 0 or bill.total_amount > 0:
            if bill.total_kwh > 0 and bill.peak_kwh == 0:
                bill = self._estimate_time_of_use(bill)
            bills.append(bill)

        return bills

    def _extract_utility_bill_text(self, text: str) -> Optional[ElectricityBillData]:
        """识别国网/电网电子账单的 Markdown/OCR 文本。

        PyMuPDF4LLM 会把账单转成 Markdown，原始正则不容易命中跨行字段。
        这里提取账单首页汇总，并结合详情页的分时电量、需量、电费项。
        """
        if not text:
            return None
        normalized = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        normalized = normalized.replace("&nbsp;", " ")
        compact = re.sub(r"[ \t]+", " ", normalized)

        bill = ElectricityBillData()
        bill.month = self._parse_month_value(compact, [
            r"账单年月\s*(20\d{2})\s*[-年/]?\s*([01]?\d)",
            r"账单周期\s*(20\d{2})[-年/]([01]?\d)",
        ])
        bill.total_kwh = self._first_number(compact, [
            r"本期电量\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*(?:千瓦时|kWh)?",
            r"本期您的电量为\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*千瓦时",
        ])
        bill.total_amount = self._first_number(compact, [
            r"本期电费\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*元",
            r"本月应付账款\s*([-+]?\d[\d,]*(?:\.\d+)?)",
        ])
        bill.demand_charge = self._first_number(compact, [
            r"容需量电费\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*([-+]?\d[\d,]*(?:\.\d+)?)",
        ], group=2)
        bill.energy_charge = self._first_number(compact, [
            r"工商业电费\s*[-+]?\d[\d,]*(?:\.\d+)?\s*([-+]?\d[\d,]*(?:\.\d+)?)",
        ])
        bill.capacity_charge = 0.0
        bill.max_demand_kw = self._first_number(compact, [
            r"实际最大需量值为\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*千瓦",
            r"实际最大需量\s*实际最大需量\s*([-+]?\d[\d,]*(?:\.\d+)?)",
            r"容需量电费\s*实际最大需量\s*实际最大需量\s*([-+]?\d[\d,]*(?:\.\d+)?)",
        ])
        bill.contract_capacity_kva = self._first_number(compact, [
            r"总合同容量为\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*千瓦/?千伏安",
            r"合同容量[:：]\s*([-+]?\d[\d,]*(?:\.\d+)?)",
        ])
        bill.power_factor = self._first_number(compact, [
            r"功率因数实际值\s*([-+]?\d[\d,]*(?:\.\d+)?)",
        ])

        ratio_match = re.search(
            r"峰谷分时比例为\s*([\d.]+)%[，,\s]+([\d.]+)%[，,\s]+([\d.]+)%[，,\s]+([\d.]+)%",
            compact,
        )
        if ratio_match and bill.total_kwh > 0:
            ratios = [float(ratio_match.group(i)) / 100 for i in range(1, 5)]
            bill.peak_kwh = round(bill.total_kwh * ratios[0], 2)
            bill.high_kwh = round(bill.total_kwh * ratios[1], 2)
            bill.flat_kwh = round(bill.total_kwh * ratios[2], 2)
            bill.valley_kwh = round(bill.total_kwh * ratios[3], 2)
        else:
            tou = self._extract_tou_from_detail(compact)
            if tou:
                bill.peak_kwh, bill.high_kwh, bill.flat_kwh, bill.valley_kwh = tou

        if bill.power_factor == 0:
            bill.power_factor = 0.9

        if bill.total_kwh > 0 or bill.total_amount > 0:
            return bill
        return None

    @staticmethod
    def _first_number(text: str, patterns: list[str], group: int = 1) -> float:
        for pattern in patterns:
            match = re.search(pattern, text, re.S)
            if not match:
                continue
            try:
                return float(match.group(group).replace(",", ""))
            except Exception:
                continue
        return 0.0

    @staticmethod
    def _parse_month_value(text: str, patterns: list[str]) -> str:
        for pattern in patterns:
            match = re.search(pattern, text, re.S)
            if not match:
                continue
            try:
                year = int(match.group(1))
                month = int(match.group(2))
                if 1 <= month <= 12:
                    return f"{year:04d}-{month:02d}"
            except Exception:
                continue
        return ""

    @staticmethod
    def _extract_tou_from_detail(text: str) -> tuple[float, float, float, float] | None:
        values = []
        pattern = (
            r"计费电量\s*([-+]?\d[\d,]*(?:\.\d+)?)\s+"
            r"([-+]?\d[\d,]*(?:\.\d+)?)\s+"
            r"([-+]?\d[\d,]*(?:\.\d+)?)\s+"
            r"([-+]?\d[\d,]*(?:\.\d+)?)\s+"
            r"([-+]?\d[\d,]*(?:\.\d+)?)"
        )
        for match in re.finditer(pattern, text, re.S):
            nums = [float(match.group(i).replace(",", "")) for i in range(1, 6)]
            total, peak, high, flat, valley = nums
            if total > 0 and abs((peak + high + flat + valley) - total) <= max(total * 0.03, 5):
                values.append((peak, high, flat, valley))
        if not values:
            return None
        return tuple(round(sum(v[i] for v in values), 2) for i in range(4))

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _parse_number(self, value) -> float:
        """安全地将值转换为数字。"""
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        try:
            s = str(value).replace(",", "").replace(" ", "")
            s = re.sub(r"[^\d.\-]", "", s)
            return float(s) if s else 0.0
        except (ValueError, TypeError):
            return 0.0

    def _parse_month(self, value) -> str:
        """解析月份为标准格式 YYYY-MM。"""
        if value is None:
            return ""
        s = str(value).strip()
        if not s:
            return ""
        if re.fullmatch(r"\d{6}", s):
            return f"{s[:4]}-{s[4:6]}"
        normalized = (
            s.replace("年", "-")
             .replace("月", "-")
             .replace("日", "")
             .replace("/", "-")
             .replace(".", "-")
        )
        normalized = re.sub(r"-+", "-", normalized).strip("-")
        for fmt in ["%Y-%m-%d", "%Y-%m", "%Y%m%d", "%Y%m"]:
            try:
                dt = datetime.strptime(normalized[:10], fmt)
                return dt.strftime("%Y-%m")
            except ValueError:
                continue
        # 尝试正则
        m = re.search(r"(\d{4})\D*(\d{1,2})", normalized)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}"
        return ""

    def _estimate_time_of_use(self, bill: ElectricityBillData) -> ElectricityBillData:
        """在缺少分时电量时，按典型比例估算。"""
        total = bill.total_kwh
        if total <= 0:
            return bill
        # 典型工商业用电比例
        bill.peak_kwh = round(total * 0.10, 2)
        bill.high_kwh = round(total * 0.30, 2)
        bill.flat_kwh = round(total * 0.35, 2)
        bill.valley_kwh = round(total * 0.25, 2)
        return bill

    def _deduplicate(self, bills: list[ElectricityBillData]) -> list[ElectricityBillData]:
        """按月份去重，保留数据更完整的记录。"""
        seen = {}
        for bill in bills:
            key = bill.month
            if key not in seen or self._completeness(bill) > self._completeness(seen[key]):
                seen[key] = bill
        return list(seen.values())

    @staticmethod
    def _completeness(bill: ElectricityBillData) -> int:
        score = 0
        for attr in ["total_kwh", "peak_kwh", "high_kwh", "flat_kwh", "valley_kwh",
                      "max_demand_kw", "total_amount", "energy_charge"]:
            if getattr(bill, attr, 0) > 0:
                score += 1
        return score

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    def to_dataframe(self) -> pd.DataFrame:
        """将提取的电费数据转为DataFrame。"""
        if not self.bill_data:
            return pd.DataFrame()

        records = []
        for b in self.bill_data:
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
                "尖峰电价(元/kWh)": b.peak_price,
                "高峰电价(元/kWh)": b.high_price,
                "平段电价(元/kWh)": b.flat_price,
                "谷段电价(元/kWh)": b.valley_price,
                "需量电价(元/kW·月)": b.demand_price,
            })

        return pd.DataFrame(records)

    def export_to_excel(self, output_path: str | Path, include_analysis: bool = True) -> Path:
        """导出电费数据到Excel文件。"""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        df = self.to_dataframe()
        if df.empty:
            logger.warning("无数据可导出")
            return path

        with pd.ExcelWriter(str(path), engine="openpyxl") as writer:
            # 原始数据表
            df.to_excel(writer, sheet_name="电费数据", index=False)

            if include_analysis and len(df) > 0:
                # 统计汇总表
                summary = self._generate_summary(df)
                summary.to_excel(writer, sheet_name="统计汇总", index=False)

                # 分时电量占比
                if df["总电量(kWh)"].sum() > 0:
                    total = df["总电量(kWh)"].sum()
                    ratio_df = pd.DataFrame({
                        "时段": ["尖峰", "高峰", "平段", "谷段"],
                        "总电量(kWh)": [
                            df["尖峰电量(kWh)"].sum(),
                            df["高峰电量(kWh)"].sum(),
                            df["平段电量(kWh)"].sum(),
                            df["谷段电量(kWh)"].sum(),
                        ],
                        "占比(%)": [
                            round(df["尖峰电量(kWh)"].sum() / total * 100, 2),
                            round(df["高峰电量(kWh)"].sum() / total * 100, 2),
                            round(df["平段电量(kWh)"].sum() / total * 100, 2),
                            round(df["谷段电量(kWh)"].sum() / total * 100, 2),
                        ],
                    })
                    ratio_df.to_excel(writer, sheet_name="分时电量占比", index=False)

        logger.info("电费数据已导出: %s", path)
        return path

    def _generate_summary(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成统计汇总。"""
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        summary_data = []

        for col in numeric_cols:
            summary_data.append({
                "指标": col,
                "平均值": round(df[col].mean(), 2),
                "最大值": round(df[col].max(), 2),
                "最小值": round(df[col].min(), 2),
                "总和": round(df[col].sum(), 2),
                "标准差": round(df[col].std(), 2),
            })

        return pd.DataFrame(summary_data)
