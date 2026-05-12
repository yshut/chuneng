"""Storage-related document analysis helpers.

Used for tariff files, policies, grid-connection requirements and other
documents that affect storage sizing or revenue assumptions.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import pandas as pd


STORAGE_KEYWORDS = [
    "尖峰", "高峰", "峰", "平段", "平时段", "谷", "低谷", "深谷",
    "电价", "需量", "容量电费", "容量", "补贴", "并网", "储能", "削峰",
    "消防", "备案", "调度", "收益",
]


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _num(value: Any) -> float | None:
    text = _text(value)
    if not text or text in {"/", "-", "—", "NaN", "nan"}:
        return None
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def _find_period(df: pd.DataFrame) -> str:
    for value in df.to_numpy().ravel():
        text = _text(value)
        match = re.search(r"执行时间[:：]?\s*([^）\)\n]+)", text)
        if match:
            return match.group(1).strip()
    return ""


def _find_header_row(df: pd.DataFrame) -> int | None:
    labels = ["尖峰时段", "高峰时段", "平时段", "低谷时段", "最大需量", "变压器容量"]
    for idx in range(len(df)):
        row_text = " ".join(_text(v) for v in df.iloc[idx].tolist())
        if sum(1 for label in labels if label in row_text) >= 3:
            return idx
    return None


def _column_map(df: pd.DataFrame, header_row: int) -> dict[str, int]:
    labels = {
        "voltage_level": ["电压等级"],
        "non_tou_price": ["非分时电度电价"],
        "peak_price": ["尖峰时段"],
        "high_price": ["高峰时段"],
        "flat_price": ["平时段"],
        "valley_price": ["低谷时段"],
        "deep_valley_price": ["深谷时段"],
        "demand_price": ["最大需量"],
        "capacity_price": ["变压器容量"],
    }
    out: dict[str, int] = {}
    scan_rows = [r for r in (header_row - 1, header_row, header_row + 1) if 0 <= r < len(df)]
    for col in range(df.shape[1]):
        cell_text = " ".join(_text(df.iat[row, col]) for row in scan_rows)
        for key, names in labels.items():
            if key in out:
                continue
            if any(name in cell_text for name in names):
                out[key] = col
    return out


def _extract_excel_tariffs(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    tariffs: list[dict[str, Any]] = []
    notes: list[str] = []
    try:
        xls = pd.ExcelFile(path)
    except Exception:
        return tariffs, notes

    for sheet in xls.sheet_names:
        try:
            df = pd.read_excel(xls, sheet_name=sheet, header=None)
        except Exception:
            continue
        execution_period = _find_period(df)
        header_row = _find_header_row(df)

        for value in df.to_numpy().ravel():
            text = _text(value)
            if len(text) >= 12 and any(keyword in text for keyword in STORAGE_KEYWORDS):
                notes.append(f"{path.name} / {sheet}: {text[:800]}")

        if header_row is None:
            continue
        cols = _column_map(df, header_row)
        category = ""
        billing_type = ""
        for row_idx in range(header_row + 1, len(df)):
            row = df.iloc[row_idx]
            first = _text(row.iat[0]) if len(row) > 0 else ""
            second = _text(row.iat[1]) if len(row) > 1 else ""
            if first and any(word in first for word in ["用电", "工商业"]):
                category = first
            if second and second in {"单一制", "两部制"}:
                billing_type = second
            voltage = _text(row.iat[cols["voltage_level"]]) if "voltage_level" in cols else ""
            if not voltage or voltage in {"nan", "NaN"}:
                continue
            item = {
                "source": path.name,
                "sheet": sheet,
                "execution_period": execution_period,
                "category": category,
                "billing_type": billing_type,
                "voltage_level": voltage,
                "non_tou_price": _num(row.iat[cols["non_tou_price"]]) if "non_tou_price" in cols else None,
                "peak_price": _num(row.iat[cols["peak_price"]]) if "peak_price" in cols else None,
                "high_price": _num(row.iat[cols["high_price"]]) if "high_price" in cols else None,
                "flat_price": _num(row.iat[cols["flat_price"]]) if "flat_price" in cols else None,
                "valley_price": _num(row.iat[cols["valley_price"]]) if "valley_price" in cols else None,
                "deep_valley_price": _num(row.iat[cols["deep_valley_price"]]) if "deep_valley_price" in cols else None,
                "demand_price": _num(row.iat[cols["demand_price"]]) if "demand_price" in cols else None,
                "capacity_price": _num(row.iat[cols["capacity_price"]]) if "capacity_price" in cols else None,
            }
            if any(item.get(key) is not None for key in (
                "peak_price", "high_price", "flat_price", "valley_price",
                "deep_valley_price", "demand_price", "capacity_price",
            )):
                tariffs.append(item)

    return tariffs, notes


def extract_storage_parameters(paths: list[Path], parsed_docs: list[dict] | None = None) -> dict[str, Any]:
    tariff_rows: list[dict[str, Any]] = []
    notes: list[str] = []
    for path in paths:
        if path.suffix.lower() in {".xls", ".xlsx"}:
            rows, excel_notes = _extract_excel_tariffs(path)
            tariff_rows.extend(rows)
            notes.extend(excel_notes)

    if parsed_docs:
        for doc in parsed_docs:
            if doc.get("error"):
                continue
            source = Path(doc.get("file", "")).name
            for line in str(doc.get("text") or "").splitlines():
                line = line.strip()
                if len(line) >= 6 and any(keyword in line for keyword in STORAGE_KEYWORDS):
                    notes.append(f"{source}: {line[:800]}")

    return {
        "tariff_rows": tariff_rows[:120],
        "storage_related_notes": notes[:120],
    }


def analyze_storage_documents(paths: list[Path], parser: Any, llm_client: Any = None,
                              kb: Any = None, topic: str = "", index_to_kb: bool = True,
                              use_llm: bool = False) -> dict:
    parsed = parser.parse_batch(paths)
    docs: list[dict] = []
    combined_parts: list[str] = []
    deterministic = extract_storage_parameters(paths, parsed)

    for doc in parsed:
        if doc.get("error"):
            docs.append({"file": Path(doc.get("file", "")).name, "ok": False, "error": doc.get("error")})
            continue
        text = str(doc.get("text") or "")
        tables = doc.get("tables") or []
        table_text = []
        for table in tables[:3]:
            try:
                table_text.append(table.head(30).to_csv(index=False))
            except Exception:
                continue
        sample = (text + "\n" + "\n".join(table_text)).strip()
        combined_parts.append(f"# 文件: {Path(doc.get('file', '')).name}\n{sample[:10000]}")
        docs.append({
            "file": Path(doc.get("file", "")).name,
            "ok": True,
            "type": doc.get("type"),
            "text_chars": len(text),
            "tables": len(tables),
            "preview": sample[:500],
        })

    kb_results = []
    if index_to_kb and kb is not None:
        for path in paths:
            try:
                chunks = kb.index_file(str(path), metadata={"topic": topic, "category": "储能相关文档"})
                kb_results.append({"source": path.name, "chunks": chunks})
            except Exception as e:
                kb_results.append({"source": path.name, "error": str(e)})

    if not combined_parts and not deterministic["tariff_rows"]:
        return {"ok": False, "msg": "未能从文件中解析出可分析文本", "documents": docs, "kb_index": kb_results}

    material = "\n\n".join(combined_parts)
    if len(material) > 24000:
        material = material[:24000] + "\n...(内容已截断)"

    analysis: dict[str, Any]
    if use_llm and llm_client is not None and getattr(llm_client, "available", False):
        prompt = f"""请从以下文件内容中提取与用户主题相关的储能测算信息。

主题：{topic}

下面是程序从表格中确定性抽取到的参数，请优先采用这些数值：
{json.dumps(deterministic, ensure_ascii=False)[:12000]}

需要输出 JSON，字段包括：
- tou_prices: 分时电价信息，含地区/月份/尖峰/峰/平/谷/深谷/执行时间
- demand_charges: 需量电价、容量电费、基本电费规则
- storage_relevance: 对储能容量、收益、削峰填谷、峰谷套利有影响的条款
- policy_or_grid_notes: 补贴、并网、备案、消防、调度等要求
- extracted_parameters: 可直接用于测算的参数键值
- risks: 需要人工确认或可能影响收益的风险点
- next_actions: 后续需要补充的数据或建议动作

文件内容：
{material}
"""
        analysis = llm_client.ask_json(
            prompt,
            system_prompt="你是工商业储能项目分析师，只抽取文件中有依据的信息；不确定的内容明确标注需确认。",
            max_tokens=4096,
        )
    else:
        analysis = {
            "storage_relevance": deterministic["storage_related_notes"],
            "note": "已返回程序确定性抽取的表格参数和关键词原文行；如需自然语言归纳，可设置 use_llm=true。",
        }
    analysis["deterministic_extract"] = deterministic

    return {
        "ok": True,
        "msg": f"已解析 {len(paths)} 个储能相关文件",
        "topic": topic,
        "documents": docs,
        "kb_index": kb_results,
        "analysis": analysis,
    }
