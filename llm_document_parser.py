"""储能配置AGENT - LLM智能文档解析模块
使用大模型理解任意格式的电费账单，替代硬编码的正则表达式。
LLM不可用时自动回退到现有的DocumentParser + DataExtractor。
"""

from __future__ import annotations

import base64
import io
import json
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from config import LLMConfig
from data_extractor import DataExtractor, ElectricityBillData
from document_parser import DocumentParser
from llm_client import LLMClient

logger = logging.getLogger(__name__)

# 电费数据提取的系统提示
BILL_EXTRACTION_SYSTEM_PROMPT = """你是一个专业的电力行业数据分析师。你的任务是从电费账单/发票中提取结构化数据。

你需要提取以下字段（如果账单中有的话）：
- month: 月份，格式 YYYY-MM
- total_kwh: 总用电量 (kWh)
- peak_kwh: 尖峰电量 (kWh)
- high_kwh: 高峰电量 (kWh)
- flat_kwh: 平段电量 (kWh)
- valley_kwh: 谷段电量 (kWh)
- max_demand_kw: 最大需量 (kW)
- contract_capacity_kva: 合同容量 (kVA)
- total_amount: 总电费 (元)
- energy_charge: 电量电费 (元)
- demand_charge: 需量电费 (元)
- capacity_charge: 容量电费 (元)
- power_factor: 功率因数
- peak_price: 尖峰电价 (元/kWh)
- high_price: 高峰电价/峰电价 (元/kWh)
- flat_price: 平段电价 (元/kWh)
- valley_price: 谷段电价 (元/kWh)
- demand_price: 需量电价 (元/kW·月)

注意：
1. 如果某个字段在账单中找不到，设为 0
2. 电量单位统一为 kWh，金额单位统一为元
3. 如果有多个月份的数据，返回一个数组
4. 有些账单可能用"峰/谷/平"代替"尖峰/高峰/平段/谷段"，请正确映射
5. "有功总"对应总电量，"无功总"忽略
6. "力率"或"cosφ"对应功率因数"""

BILL_EXTRACTION_PROMPT = """请从以下电费账单内容中提取结构化数据。

账单内容：
{text}

请以JSON格式返回，格式如下：
{{
  "bills": [
    {{
      "month": "2024-01",
      "total_kwh": 500000,
      "peak_kwh": 50000,
      "high_kwh": 150000,
      "flat_kwh": 175000,
      "valley_kwh": 125000,
      "max_demand_kw": 2000,
      "contract_capacity_kva": 2500,
      "total_amount": 350000,
      "energy_charge": 320000,
      "demand_charge": 30000,
      "capacity_charge": 0,
      "power_factor": 0.92,
      "peak_price": 1.2,
      "high_price": 1.0,
      "flat_price": 0.65,
      "valley_price": 0.35,
      "demand_price": 40.8
    }}
  ]
}}"""

IMAGE_BILL_PROMPT_MULTIPAGE = """这是一份电费账单 PDF 的整页扫描图（可能多页连发），请像视觉OCR那样\
逐页阅读表格和文字，提取所有月份的电费数据。

要点：
- 表格里"分时电量"通常分为 尖/峰/平/谷 四段，与文字描述的"高峰/平段/低谷"对应。
- 数字可能带括号、千分位逗号、或在合并单元格里 —— 请正确还原。
- 如果有多个月份/计费周期，**全部返回为数组**。

请返回 JSON：{"bills":[{...}, {...}]}，每条 bill 包含以下字段（找不到的设 0）：
- month (YYYY-MM)
- total_kwh, peak_kwh, high_kwh, flat_kwh, valley_kwh
- max_demand_kw, contract_capacity_kva
- total_amount, energy_charge, demand_charge, capacity_charge
- power_factor
- peak_price, high_price, flat_price, valley_price, demand_price

只返回 JSON，不要 markdown 代码块。"""


IMAGE_BILL_PROMPT = """请仔细查看这张电费账单/发票图片，提取其中的结构化数据。

你需要提取以下字段（如果图中有的话）：
- month: 月份 (YYYY-MM格式)
- total_kwh: 总用电量 (kWh)
- peak_kwh: 尖峰电量 (kWh)
- high_kwh: 高峰电量 (kWh)
- flat_kwh: 平段电量 (kWh)
- valley_kwh: 谷段电量 (kWh)
- max_demand_kw: 最大需量 (kW)
- contract_capacity_kva: 合同容量 (kVA)
- total_amount: 总电费 (元)
- energy_charge: 电量电费 (元)
- demand_charge: 需量电费 (元)
- capacity_charge: 容量电费 (元)
- power_factor: 功率因数
- peak_price, high_price, flat_price, valley_price: 分时电价 (元/kWh)
- demand_price: 需量电价 (元/kW·月)

如果有多个时间段的数据，返回数组。找不到的字段设为0。
请直接返回JSON格式，不要包含markdown代码块标记。"""


class LLMDocumentParser:
    """LLM增强的文档解析器"""

    def __init__(self, llm_client: LLMClient,
                 fallback_parser: DocumentParser = None,
                 fallback_extractor: DataExtractor = None):
        self.llm = llm_client
        self.fallback_parser = fallback_parser or DocumentParser()
        self.fallback_extractor = fallback_extractor or DataExtractor()

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------
    def parse_and_extract(self, file_path: str | Path) -> list[ElectricityBillData]:
        """解析文件并提取电费数据。

        优先使用LLM解析，失败时回退到传统方法。

        Returns:
            提取到的电费数据列表
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {path}")

        ext = path.suffix.lower()

        # 图片文件优先用视觉模型
        if ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"):
            bills = self._extract_from_image(path)
            if bills:
                return bills

        # 其他文件先提取文本，再用LLM解析
        try:
            parsed = self.fallback_parser.parse(path)
            text = parsed.get("text", "")
            tables = parsed.get("tables", [])

            # 1) 文本 LLM 提取（最快、token 最省）
            if self.llm.available and text:
                bills = self._extract_with_llm(text)
                if bills:
                    logger.info("LLM成功从 %s 提取到 %d 条记录", path.name, len(bills))
                    return bills

            # 2) 仅 PDF：文本失败 → 视觉 LLM（GPT-4o 那套，对扫描件/复杂表格强）
            if ext == ".pdf" and self.llm.available:
                logger.info("文本 LLM 失败，尝试视觉 LLM 解析 PDF: %s", path.name)
                bills = self._extract_from_pdf_via_vision(path)
                if bills:
                    return bills

            # 3) 回退到传统正则方法
            logger.info("LLM提取失败或不可用，回退到传统方法: %s", path.name)
            bills = self._fallback_extract(parsed)
            return bills

        except Exception as e:
            logger.warning("解析文件 %s 失败: %s", path.name, e)
            return []

    # ------------------------------------------------------------------
    # PDF → 图片 → 视觉 LLM（GPT-4o 那套，对扫描件/表格密集 PDF 强）
    # ------------------------------------------------------------------
    @staticmethod
    def _render_pdf_to_jpegs(pdf_path: Path, dpi: int = 180,
                              max_pages: int = 12) -> list[str]:
        """渲染 PDF 每页为 JPEG base64。优先用 PyMuPDF（无系统依赖），
        退化到 pdf2image（需要 poppler）。返回 base64 列表，失败返回空列表。"""
        # 1) 优先 PyMuPDF（pip install pymupdf 即可，无 poppler 依赖）
        try:
            import fitz  # type: ignore
            doc = fitz.open(str(pdf_path))
            results: list[str] = []
            scale = dpi / 72.0
            mat = fitz.Matrix(scale, scale)
            for i, page in enumerate(doc):
                if i >= max_pages:
                    logger.info("PDF %s 共 %d 页，截取前 %d 页用于视觉解析",
                                 pdf_path.name, len(doc), max_pages)
                    break
                pix = page.get_pixmap(matrix=mat, alpha=False)
                # 通过 PIL 转 JPEG 以省 token（vs PNG 通常小 50%~70%）
                try:
                    from PIL import Image
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    img.thumbnail((1700, 2400))  # 保护 token
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=82, optimize=True)
                    results.append(base64.b64encode(buf.getvalue()).decode())
                except Exception:
                    # PIL 不可用 → 直接用 pix 自带 png
                    results.append(base64.b64encode(pix.tobytes("png")).decode())
            doc.close()
            return results
        except ImportError:
            pass
        except Exception as e:
            logger.warning("PyMuPDF 渲染 %s 失败: %s", pdf_path.name, e)

        # 2) 退化到 pdf2image
        try:
            from pdf2image import convert_from_path  # type: ignore
            from PIL import Image  # noqa
        except ImportError:
            logger.info("pdf2image / PyMuPDF 都不可用，跳过 PDF 视觉解析。"
                          "建议: pip install pymupdf")
            return []
        try:
            images = convert_from_path(str(pdf_path), dpi=dpi)
        except Exception as e:
            logger.warning("pdf2image 转换 %s 失败（可能缺 poppler）: %s",
                           pdf_path.name, e)
            return []
        if not images:
            return []
        if len(images) > max_pages:
            logger.info("PDF %s 共 %d 页，截取前 %d 页用于视觉解析",
                         pdf_path.name, len(images), max_pages)
            images = images[:max_pages]
        out: list[str] = []
        for img in images:
            try:
                img.thumbnail((1700, 2400))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=82, optimize=True)
                out.append(base64.b64encode(buf.getvalue()).decode())
            except Exception:
                continue
        return out

    def _extract_from_pdf_via_vision(self, pdf_path: Path) -> list[ElectricityBillData]:
        """把 PDF 渲染为整页图片后，用视觉 LLM 提取电费数据。

        分批调用（每批 3 页）以避免单次 token 超限。
        """
        if not self.llm.available:
            return []

        b64_pages = self._render_pdf_to_jpegs(pdf_path, dpi=180, max_pages=12)
        if not b64_pages:
            return []

        BATCH = 3
        all_bills: list[ElectricityBillData] = []
        for i in range(0, len(b64_pages), BATCH):
            chunk = b64_pages[i:i + BATCH]
            try:
                raw = self.llm.chat_with_image(
                    prompt=IMAGE_BILL_PROMPT_MULTIPAGE,
                    image_base64_list=chunk,
                    system_prompt=("你是专业的电力行业数据分析师。请逐页阅读电费账单"
                                    "扫描件并以严格 JSON 返回。"),
                    max_tokens=8192,
                    media_type="jpeg",
                )
            except Exception as e:
                logger.warning("PDF 视觉调用失败（页 %d-%d）: %s",
                               i + 1, i + len(chunk), e)
                continue

            data = self._safe_parse_json(raw)
            if not data:
                continue
            bills_data = data.get("bills", [])
            if isinstance(bills_data, dict):
                bills_data = [bills_data]
            try:
                bills = self._convert_to_bill_data(bills_data)
                all_bills.extend(bills)
            except Exception as e:
                logger.warning("PDF 视觉返回数据转换失败: %s", e)

        if all_bills:
            logger.info("✓ 视觉 LLM 从 %s 提取到 %d 条记录",
                         pdf_path.name, len(all_bills))
        return all_bills

    @staticmethod
    def _safe_parse_json(text: str) -> dict:
        """容错解析 LLM 返回的 JSON 文本，兼容带 markdown 代码块的情况。"""
        if not text:
            return {}
        s = text.strip()
        if s.startswith("```"):
            s = s.split("\n", 1)[-1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
        s = s.strip()
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            # 尝试找第一个 { 到最后一个 } 之间的内容
            l, r = s.find("{"), s.rfind("}")
            if 0 <= l < r:
                try:
                    return json.loads(s[l:r + 1])
                except json.JSONDecodeError:
                    pass
        return {}

    def parse_batch(self, file_paths: list[str | Path]) -> list[ElectricityBillData]:
        """批量解析多个文件。"""
        all_bills = []
        for fp in file_paths:
            try:
                bills = self.parse_and_extract(fp)
                all_bills.extend(bills)
            except Exception as e:
                logger.warning("解析 %s 失败: %s", fp, e)
        return all_bills

    # ------------------------------------------------------------------
    # LLM提取
    # ------------------------------------------------------------------
    def _extract_with_llm(self, text: str) -> list[ElectricityBillData]:
        """用LLM从文本中提取电费数据。"""
        try:
            # 截断过长文本
            if len(text) > 15000:
                text = text[:15000] + "\n...(文本已截断)"

            prompt = BILL_EXTRACTION_PROMPT.format(text=text)
            # max_tokens 8192：单月电费完整 JSON 一般 < 1500 token，但多月或多税目时
            # 4096 容易截断，提到 8192 给足缓冲
            result = self.llm.ask_json(
                prompt,
                system_prompt=BILL_EXTRACTION_SYSTEM_PROMPT,
                max_tokens=8192,
            )

            # 一次没拿到合法 JSON → 重试一次（同样调用，但提示更严格）
            if ("error" in result and "bills" not in result):
                raw = result.get("raw", "")
                logger.warning("LLM返回JSON不完整(可能被截断,长度=%d)，尝试重试: %s",
                               len(raw), raw[:200])
                strict_prompt = (
                    prompt
                    + "\n\n【重要】上一次返回被截断，请只输出最关键字段："
                    "month、total_kwh、peak_kwh、high_kwh、flat_kwh、valley_kwh、"
                    "max_demand_kw、total_cost。其它字段省略。务必返回完整 JSON。"
                )
                result = self.llm.ask_json(
                    strict_prompt,
                    system_prompt=BILL_EXTRACTION_SYSTEM_PROMPT,
                    max_tokens=8192,
                )
                if "error" in result and "bills" not in result:
                    logger.warning("LLM重试仍失败，回退传统方法")
                    return []

            bills_data = result.get("bills", [])
            if isinstance(bills_data, dict):
                bills_data = [bills_data]

            return self._convert_to_bill_data(bills_data)

        except Exception as e:
            logger.warning("LLM提取失败: %s", e)
            return []

    def _extract_from_image(self, image_path: Path) -> list[ElectricityBillData]:
        """用视觉LLM从图片中提取电费数据。"""
        if not self.llm.available:
            return []

        try:
            result = self.llm.chat_with_image(
                prompt=IMAGE_BILL_PROMPT,
                image_paths=[str(image_path)],
                system_prompt="你是专业的电力行业数据分析师，请从图片中提取电费账单数据，返回JSON格式。"
            )

            # 解析JSON
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[-1]
            if result.endswith("```"):
                result = result.rsplit("```", 1)[0]
            result = result.strip()

            data = json.loads(result)
            bills_data = data.get("bills", [])
            if isinstance(bills_data, dict):
                bills_data = [bills_data]

            bills = self._convert_to_bill_data(bills_data)
            if bills:
                logger.info("视觉LLM成功从图片提取到 %d 条记录", len(bills))
            return bills

        except json.JSONDecodeError:
            logger.warning("视觉LLM返回的不是有效JSON")
            return []
        except Exception as e:
            logger.warning("视觉LLM提取失败: %s", e)
            return []

    # ------------------------------------------------------------------
    # 回退提取
    # ------------------------------------------------------------------
    def _fallback_extract(self, parsed_doc: dict) -> list[ElectricityBillData]:
        """使用传统方法提取电费数据。"""
        df = self.fallback_extractor.extract_from_parsed([parsed_doc])
        if df.empty:
            return []

        # DataFrame 转回 ElectricityBillData
        bills = []
        for _, row in df.iterrows():
            bill = ElectricityBillData(
                month=str(row.get("月份", "")),
                total_kwh=float(row.get("总电量(kWh)", 0)),
                peak_kwh=float(row.get("尖峰电量(kWh)", 0)),
                high_kwh=float(row.get("高峰电量(kWh)", 0)),
                flat_kwh=float(row.get("平段电量(kWh)", 0)),
                valley_kwh=float(row.get("谷段电量(kWh)", 0)),
                max_demand_kw=float(row.get("最大需量(kW)", 0)),
                contract_capacity_kva=float(row.get("合同容量(kVA)", 0)),
                total_amount=float(row.get("总电费(元)", 0)),
                energy_charge=float(row.get("电量电费(元)", 0)),
                demand_charge=float(row.get("需量电费(元)", 0)),
                capacity_charge=float(row.get("容量电费(元)", 0)),
                power_factor=float(row.get("功率因数", 0.9)),
                peak_price=float(row.get("尖峰电价(元/kWh)", 0)),
                high_price=float(row.get("高峰电价(元/kWh)", 0)),
                flat_price=float(row.get("平段电价(元/kWh)", 0)),
                valley_price=float(row.get("谷段电价(元/kWh)", 0)),
                demand_price=float(row.get("需量电价(元/kW·月)", 0)),
            )
            if bill.total_kwh > 0 or bill.total_amount > 0:
                bills.append(bill)
        return bills

    # ------------------------------------------------------------------
    # 数据转换
    # ------------------------------------------------------------------
    @staticmethod
    def _convert_to_bill_data(bills_data: list[dict]) -> list[ElectricityBillData]:
        """将LLM返回的JSON转换为ElectricityBillData。"""
        bills = []
        field_map = {
            "month": "month",
            "total_kwh": "total_kwh",
            "peak_kwh": "peak_kwh",
            "high_kwh": "high_kwh",
            "flat_kwh": "flat_kwh",
            "valley_kwh": "valley_kwh",
            "max_demand_kw": "max_demand_kw",
            "contract_capacity_kva": "contract_capacity_kva",
            "total_amount": "total_amount",
            "energy_charge": "energy_charge",
            "demand_charge": "demand_charge",
            "capacity_charge": "capacity_charge",
            "power_factor": "power_factor",
            "peak_price": "peak_price",
            "high_price": "high_price",
            "flat_price": "flat_price",
            "valley_price": "valley_price",
            "demand_price": "demand_price",
        }

        for item in bills_data:
            bill = ElectricityBillData()
            for json_key, attr_name in field_map.items():
                value = item.get(json_key, 0)
                if value is None:
                    value = 0
                if attr_name == "month":
                    bill.month = str(value)
                else:
                    try:
                        setattr(bill, attr_name, float(value))
                    except (ValueError, TypeError):
                        pass

            if bill.total_kwh > 0 or bill.total_amount > 0:
                bills.append(bill)

        return bills
