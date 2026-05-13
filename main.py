"""储能配置AGENT - 主程序入口

功能：
1. 读取各种格式的参考资料（PDF、Word、图片等），通过OCR/LLM提取电费数据
2. 将电费数据转换为结构化Excel表格
3. 根据用电数据生成最优储能配置参数
4. 根据最优配置计算收益分析，生成详细的收益报告表格
5. 支持资方/客户收益分配（含贷款/EMC模式）
6. 集成大模型实现智能文档解析、自然语言交互、报告生成
7. **智能体（Tool-Calling Agent）模式**：基于 LLM Function Calling，
   用户用自然语言描述需求，Agent 自主选择并调用工具完成任务。

使用方法：
    # 批处理模式
    python main.py                          # 处理 input/ 目录下的所有文件
    python main.py --input path/to/file.pdf # 处理指定文件
    python main.py --demo                   # 使用示例数据运行演示
    python main.py --demo --no-llm          # 不使用LLM，纯规则模式
    python main.py --input file --investment-mode loan --loan-ratio 0.7
    python main.py --input file --investment-mode emc --investor-share 0.7

    # 智能体交互模式（推荐）
    python main.py --chat                   # 进入 Tool-Calling Agent 对话（终端 + 流式）
        > '我们工厂月用电50万度，最大需量2000kW，帮我配储能'
        > '试试EMC模式 7:3 分成'
        > '电池降到800元再算一次'
        > '出个完整报告并导出 Excel'
        > '还记得我之前问什么吗'           # 触发长期记忆检索

    # Web UI（Gradio，推荐图形化使用）
    python main.py --web                    # 默认 127.0.0.1:7860
    python main.py --web --port 8080
    python main.py --web --share            # 公网链接
    python main.py --web --user alice       # 指定默认 user_id

    # 多用户隔离
    python main.py --chat --user alice      # alice 的记忆与 main 完全隔离
    python main.py --chat --user bob

    # MCP Server（stdio）— 让 Claude Desktop / Cursor 调用本项目的全部工具
    python main.py --mcp                    # 默认 user_id=main
    python main.py --mcp --user alice
    # 或直接：python mcp_server.py

环境变量（启用 LLM）：
    DASHSCOPE_API_KEY=...   # 通义千问 (默认)
    BAIDU_API_KEY=...       # 文心一言

特性：
    ✓ Tool-Calling Agent（LLM 自主选择工具调用）
    ✓ 流式响应（边生成边显示）
    ✓ 长期记忆：文本（小端风格）+ 向量（ChromaDB + Qwen embedding）
    ✓ 多用户隔离（按 user_id 子目录）
    ✓ 插件式扩展（plugins/ 目录自动发现 @tool 装饰函数）
    ✓ 图表可视化（年度收益、敏感性热力、成本饱图、现金流瀑布）
    ✓ MCP 协议导出（其他客户端可接入本项目工具）
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from config import AgentConfig, LLMConfig, InvestorConfig
from data_extractor import DataExtractor
from demo_data import create_demo_df
from document_parser import DocumentParser
from revenue_analyzer import RevenueAnalyzer
from storage_optimizer import StorageOptimizer

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("储能AGENT")


class EnergyStorageAgent:
    """储能配置AGENT主类"""

    def __init__(self, config: AgentConfig = None):
        self.config = config or AgentConfig()
        self.parser = DocumentParser(
            ocr_language=self.config.ocr_language,
            tesseract_lang=self.config.tesseract_lang,
        )
        self.extractor = DataExtractor()
        self.optimizer = StorageOptimizer(
            rate_config=self.config.rate_config,
            storage_config=self.config.storage_config,
        )
        self.analyzer = RevenueAnalyzer(
            rate_config=self.config.rate_config,
            storage_config=self.config.storage_config,
        )
        # LLM组件（延迟初始化）
        self._llm_agent = None
        self._llm_parser = None
        self._report_gen = None

    def _init_llm(self):
        """初始化LLM组件。"""
        if self._llm_agent is not None:
            return True
        if not self.config.llm_config.enabled:
            return False
        try:
            from llm_client import LLMClient
            from llm_document_parser import LLMDocumentParser
            from llm_report_generator import LLMReportGenerator
            from llm_agent import LLMAgent

            llm = LLMClient(self.config.llm_config)
            if not llm.available:
                logger.warning("LLM客户端不可用，将使用纯规则模式")
                return False

            self._llm_agent = LLMAgent(self.config)
            self._llm_parser = LLMDocumentParser(llm, self.parser, self.extractor)
            self._report_gen = LLMReportGenerator(llm)
            logger.info("LLM组件初始化成功")
            return True
        except ImportError as e:
            logger.warning("LLM依赖未安装: %s，将使用纯规则模式", e)
            return False
        except Exception as e:
            logger.warning("LLM初始化失败: %s，将使用纯规则模式", e)
            return False

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self, input_paths: list[str] = None) -> dict:
        """运行完整的储能配置分析流程。"""
        results = {}

        # Step 1: 确定输入文件
        files = self._get_input_files(input_paths)
        if not files:
            logger.error("未找到任何输入文件")
            return results

        logger.info("=" * 60)
        logger.info("储能配置AGENT 开始运行")
        logger.info("=" * 60)

        # Step 2: 解析文档
        logger.info("[Step 1/5] 解析输入文档...")
        has_llm = self._init_llm()

        if has_llm and self._llm_parser:
            logger.info("使用LLM智能解析文档...")
            bills = self._llm_parser.parse_batch(files)
            electricity_df = self._llm_parser._bills_to_dataframe(bills) if bills else pd.DataFrame()
        else:
            parsed_docs = self.parser.parse_batch(files)
            logger.info("成功解析 %d 个文件", len([d for d in parsed_docs if "error" not in d]))
            electricity_df = self.extractor.extract_from_parsed(parsed_docs)

        if electricity_df.empty:
            logger.warning("未能从文档中提取到有效的电费数据")
            return results

        logger.info("提取到 %d 个月的电费数据", len(electricity_df))
        results["electricity_df"] = electricity_df

        # Step 3: 导出电费数据
        excel_path = self.config.output_dir / "电费数据.xlsx"
        electricity_df.to_excel(str(excel_path), index=False)
        results["electricity_excel"] = str(excel_path)

        # Step 4: 优化储能配置
        logger.info("[Step 2/5] 生成最优储能配置...")
        config = self.optimizer.optimize(electricity_df)
        logger.info("最优配置: 容量=%s kWh, 功率=%s kW, 回收期=%s 年",
                     config.battery_capacity_kwh, config.inverter_power_kw, config.simple_payback_years)
        config_path = self.config.output_dir / "储能配置参数.xlsx"
        self.optimizer.export_config_to_excel(config, config_path)
        results["config_excel"] = str(config_path)
        results["config"] = config

        # Step 5: 收益分析
        logger.info("[Step 3/5] 生成收益分析报告...")
        report = self.analyzer.analyze(config, electricity_df)
        report_path = self.config.output_dir / "收益分析报告.xlsx"
        self.analyzer.export_report(report, report_path)
        results["report_excel"] = str(report_path)
        results["report"] = report

        # Step 6: 资方/客户收益分析
        logger.info("[Step 4/5] 生成资方/客户收益分析...")
        investor_report = self.analyzer.analyze_investor_customer(
            config, electricity_df, self.config.investor_config
        )
        investor_path = self.config.output_dir / "资方客户收益.xlsx"
        self.analyzer.export_investor_report(investor_report, investor_path)
        results["investor_excel"] = str(investor_path)
        results["investor_report"] = investor_report

        # Step 7: LLM智能报告
        if has_llm and self._report_gen:
            logger.info("[Step 5/5] 生成LLM智能分析报告...")
            ic_dict = {
                "investment_mode": investor_report.investment_mode,
                "investor_irr": investor_report.investor_summary.get("资方IRR",
                                 investor_report.investor_summary.get("IRR", "-")),
                "customer_annual_savings": investor_report.customer_summary.get("年均分成(元)",
                                              investor_report.customer_summary.get("年节省电费(元)", "-")),
            }
            md_report = self._report_gen.generate_full_report(
                config, report.summary, ic_dict, electricity_df
            )
            md_path = self.config.output_dir / "智能分析报告.md"
            self._report_gen.export_report(md_report, str(md_path))
            results["md_report"] = str(md_path)
        else:
            logger.info("[Step 5/5] LLM未启用，跳过智能报告生成")

        # 输出汇总
        self._print_summary(config, report, investor_report)

        logger.info("=" * 60)
        logger.info("分析完成！输出文件位于: %s", self.config.output_dir)
        logger.info("=" * 60)

        return results

    def run_with_demo_data(self) -> dict:
        """使用示例数据运行演示。"""
        logger.info("使用示例数据运行演示...")
        demo_df = self._create_demo_data()

        results = {}
        has_llm = self._init_llm()

        # 导出演示电费数据
        excel_path = self.config.output_dir / "电费数据_演示.xlsx"
        demo_df.to_excel(str(excel_path), index=False)
        results["electricity_excel"] = str(excel_path)
        results["electricity_df"] = demo_df

        # 优化储能配置
        config = self.optimizer.optimize(demo_df)
        config_path = self.config.output_dir / "储能配置参数_演示.xlsx"
        self.optimizer.export_config_to_excel(config, config_path)
        results["config_excel"] = str(config_path)
        results["config"] = config

        # 收益分析
        report = self.analyzer.analyze(config, demo_df)
        report_path = self.config.output_dir / "收益分析报告_演示.xlsx"
        self.analyzer.export_report(report, report_path)
        results["report_excel"] = str(report_path)
        results["report"] = report

        # 资方/客户收益分析
        investor_report = self.analyzer.analyze_investor_customer(
            config, demo_df, self.config.investor_config
        )
        investor_path = self.config.output_dir / "资方客户收益_演示.xlsx"
        self.analyzer.export_investor_report(investor_report, investor_path)
        results["investor_excel"] = str(investor_path)
        results["investor_report"] = investor_report

        # LLM智能报告
        if has_llm and self._report_gen:
            ic_dict = {
                "investment_mode": investor_report.investment_mode,
                "investor_irr": investor_report.investor_summary.get("资方IRR",
                                 investor_report.investor_summary.get("IRR", "-")),
                "customer_annual_savings": investor_report.customer_summary.get("年均分成(元)",
                                              investor_report.customer_summary.get("年节省电费(元)", "-")),
            }
            md_report = self._report_gen.generate_full_report(
                config, report.summary, ic_dict, demo_df
            )
            md_path = self.config.output_dir / "智能分析报告_演示.md"
            self._report_gen.export_report(md_report, str(md_path))
            results["md_report"] = str(md_path)

        self._print_summary(config, report, investor_report)

        return results

    def run_chat(self):
        """进入交互式智能体对话模式（基于 LLM Function Calling）。"""
        try:
            from agent_core import StorageAgent
        except ImportError as e:
            print(f"错误：智能体模块加载失败: {e}")
            sys.exit(1)

        agent = StorageAgent(self.config)
        if not agent.available:
            print("错误：交互模式需要LLM支持。请配置API Key后重试。")
            print("  设置环境变量: set DASHSCOPE_API_KEY=your_key  (Windows)")
            print("              export DASHSCOPE_API_KEY=your_key  (Linux/Mac)")
            print("  或使用文心一言: BAIDU_API_KEY=your_key")
            sys.exit(1)

        agent.run_interactive()

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _get_input_files(self, input_paths: list[str] = None) -> list[Path]:
        """获取输入文件列表。"""
        files = []
        if input_paths:
            for p in input_paths:
                path = Path(p)
                if path.is_file():
                    files.append(path)
                elif path.is_dir():
                    files.extend(self._scan_directory(path))
        else:
            files = self._scan_directory(self.config.input_dir)
        return files

    def _scan_directory(self, directory: Path) -> list[Path]:
        """扫描目录中的所有支持文件。"""
        files = []
        if not directory.exists():
            logger.warning("输入目录不存在: %s", directory)
            return files
        for ext in DocumentParser.SUPPORTED_EXTENSIONS:
            files.extend(directory.glob(f"*{ext}"))
            files.extend(directory.glob(f"*{ext.upper()}"))
        return sorted(set(files))

    def _create_demo_data(self) -> pd.DataFrame:
        """创建示例电费数据（12个月）。"""
        return create_demo_df()

    def _print_summary(self, config, report, investor_report=None):
        """打印分析结果摘要。"""
        print("\n" + "=" * 60)
        print("           储能配置分析结果摘要")
        print("=" * 60)

        print(f"\n【储能配置】")
        print(f"  电池容量:     {config.battery_capacity_kwh:>10,.0f} kWh")
        print(f"  逆变器功率:   {config.inverter_power_kw:>10,.0f} kW")
        print(f"  储能时长:     {config.duration_hours:>10.1f} h")
        print(f"  充放电倍率:   {config.charge_discharge_ratio:>10.4f} C")

        print(f"\n【充放电策略】")
        print(f"  充电时段:     {config.charge_start_hour:02d}:00 - {config.charge_end_hour:02d}:00 (谷段)")
        print(f"  放电时段:     {config.discharge_start_hour:02d}:00 - {config.discharge_end_hour:02d}:00 (峰段)")
        print(f"  日充电量:     {config.daily_charge_kwh:>10,.0f} kWh")
        print(f"  日放电量:     {config.daily_discharge_kwh:>10,.0f} kWh")

        print(f"\n【经济指标】")
        print(f"  总投资:       {config.total_investment:>12,.0f} 元")
        print(f"  年节省电费:   {config.annual_savings:>12,.0f} 元")
        print(f"  年净收益:     {config.annual_revenue:>12,.0f} 元")
        print(f"  静态回收期:   {config.simple_payback_years:>10.2f} 年")
        print(f"  净现值NPV:    {config.npv:>12,.0f} 元")
        print(f"  内部收益率:   {config.irr*100:>10.2f} %")
        print(f"  度电成本:     {config.lcoe:>10.4f} 元/kWh")

        # 资方/客户收益
        if investor_report:
            print(f"\n【资方/客户收益】 (模式: {investor_report.investment_mode})")
            for k, v in investor_report.investor_summary.items():
                print(f"  资方-{k}: {v}")
            for k, v in investor_report.customer_summary.items():
                print(f"  客户-{k}: {v}")

        print(f"\n【输出文件】")
        print(f"  电费数据:     output/电费数据.xlsx")
        print(f"  储能配置:     output/储能配置参数.xlsx")
        print(f"  收益报告:     output/收益分析报告.xlsx")
        print(f"  资方客户收益: output/资方客户收益.xlsx")
        print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="储能配置AGENT工具")
    parser.add_argument("--input", nargs="+", help="输入文件或目录路径")
    parser.add_argument("--output", default="output", help="输出目录 (默认: output)")
    parser.add_argument("--demo", action="store_true", help="使用示例数据运行演示")
    parser.add_argument("--chat", action="store_true", help="进入交互式对话模式（终端 + 流式）")
    parser.add_argument("--web", action="store_true",
                        help="启动新版 FastAPI Web UI（异步无锁、思考时不卡 UI，推荐）")
    parser.add_argument("--web-gradio", action="store_true",
                        help="（兼容）启动旧版 Gradio Web UI")
    parser.add_argument("--port", type=int, default=7860, help="Web UI 端口 (默认: 7860)")
    parser.add_argument("--host", default="127.0.0.1", help="Web UI 监听地址 (默认: 127.0.0.1)")
    parser.add_argument("--share", action="store_true", help="（仅 --web-gradio）公网分享链接")
    parser.add_argument("--mcp", action="store_true", help="启动 MCP (Model Context Protocol) Server")
    parser.add_argument("--user", default="main", help="user_id，多用户记忆隔离 (默认: main)")
    parser.add_argument("--rerank", action="store_true",
                          help="启用 BGE 重排器（首次会下载约 600MB 模型，需 sentence-transformers）")
    parser.add_argument("--rerank-model", default="BAAI/bge-reranker-v2-m3",
                          help="重排器模型名（默认: BAAI/bge-reranker-v2-m3）")
    parser.add_argument("--max-tool-retries", type=int, default=2,
                          help="单工具失败后的最大自动修正重试次数（默认: 2）")

    # 电价参数
    parser.add_argument("--peak-price", type=float, default=1.2, help="尖峰电价 (默认: 1.2)")
    parser.add_argument("--valley-price", type=float, default=0.35, help="谷段电价 (默认: 0.35)")
    parser.add_argument("--battery-cost", type=float, default=1200, help="电池成本 元/kWh (默认: 1200)")

    # LLM参数
    parser.add_argument("--llm-provider",
                        choices=["qwen", "wenxin", "mimo", "openai_compat"],
                        default="qwen",
                        help="大模型供应商：qwen / wenxin / mimo（小米 MiMo，默认走 api.yshut.cn 中转）/ openai_compat（通用OpenAI兼容）")
    parser.add_argument("--llm-model", default=None,
                        help="覆盖模型名，例如 MiMo-V2.5-Pro / MiMo-V2.5 / qwen-plus / gpt-4o-mini")
    parser.add_argument("--llm-base-url", default=None,
                        help="覆盖 base_url，例如 https://api.yshut.cn/v1 或官方 https://token-plan-sgp.xiaomimimo.com/v1")
    parser.add_argument("--llm-key", default=None,
                        help="覆盖 API Key（不建议在命令行明文传，优先用环境变量）")
    parser.add_argument("--no-llm", action="store_true", help="禁用LLM，纯规则模式运行")

    # 资方/客户参数
    parser.add_argument("--investment-mode", choices=["self", "loan", "emc"], default="self",
                        help="投资模式: self(自投)/loan(贷款)/emc(合同能源管理)")
    parser.add_argument("--loan-ratio", type=float, default=0.7, help="贷款比例 (默认: 0.7)")
    parser.add_argument("--loan-rate", type=float, default=0.045, help="贷款年利率 (默认: 0.045)")
    parser.add_argument("--loan-years", type=int, default=10, help="贷款期限/年 (默认: 10)")
    parser.add_argument("--investor-share", type=float, default=0.7,
                        help="资方分成比例 (EMC模式，默认: 0.7)")
    parser.add_argument("--customer-share", type=float, default=0.3,
                        help="客户分成比例 (EMC模式，默认: 0.3)")

    args = parser.parse_args()

    # 构建配置
    config = AgentConfig()
    config.output_dir = Path(args.output)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.rate_config.peak_price = args.peak_price
    config.rate_config.valley_price = args.valley_price
    config.storage_config.battery_cost_per_kwh = args.battery_cost

    # LLM配置
    config.llm_config.provider = args.llm_provider
    config.llm_config.enabled = not args.no_llm
    if args.llm_model:
        config.llm_config.model = args.llm_model
    if args.llm_base_url:
        config.llm_config.base_url = args.llm_base_url
    if args.llm_key:
        config.llm_config.api_key = args.llm_key

    # 资方/客户配置
    config.investor_config.investment_mode = args.investment_mode
    config.investor_config.loan_ratio = args.loan_ratio
    config.investor_config.loan_interest_rate = args.loan_rate
    config.investor_config.loan_years = args.loan_years
    config.investor_config.investor_share_ratio = args.investor_share
    config.investor_config.customer_share_ratio = args.customer_share

    # MCP 服务模式（最优先，因为是 stdio 通信）
    if args.mcp:
        try:
            import asyncio
            from mcp_server import run_server, _check_mcp
        except ImportError as e:
            print(f"错误：mcp_server 模块加载失败: {e}", file=sys.stderr)
            sys.exit(1)
        if not _check_mcp():
            sys.exit(1)
        # 日志打到 stderr，stdout 留给 MCP 协议
        logging.basicConfig(
            level=logging.INFO,
            format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
            stream=sys.stderr,
            force=True,
        )
        asyncio.run(run_server(user_id=args.user, config=config))
        return

    # Web UI 模式（新版 FastAPI，异步无锁）
    if args.web:
        try:
            import webserver
        except ImportError as e:
            print(f"错误：webserver 模块加载失败: {e}")
            print("请安装：pip install fastapi 'uvicorn[standard]' python-multipart")
            sys.exit(1)
        webserver.launch(config=config, host=args.host, port=args.port,
                          default_user=args.user,
                          enable_reranker=args.rerank,
                          reranker_model=args.rerank_model,
                          max_tool_retries=args.max_tool_retries)
        return

    # Web UI 兼容模式（旧 Gradio）
    if getattr(args, "web_gradio", False):
        try:
            import webui
        except ImportError as e:
            print(f"错误：webui 模块加载失败: {e}")
            sys.exit(1)
        webui.launch(config=config, port=args.port, share=args.share,
                      host=args.host, default_user=args.user,
                      enable_reranker=args.rerank,
                      reranker_model=args.rerank_model,
                      max_tool_retries=args.max_tool_retries)
        return

    # 智能体对话模式（新版，含流式 + 记忆 + 多用户）
    if args.chat:
        try:
            from agent_core import StorageAgent
        except ImportError as e:
            print(f"错误：agent_core 加载失败: {e}")
            sys.exit(1)
        sa = StorageAgent(
            config, user_id=args.user,
            enable_reranker=args.rerank,
            reranker_model=args.rerank_model,
            max_tool_retries=args.max_tool_retries,
        )
        if not sa.available:
            print("错误：交互模式需要LLM支持。请配置API Key后重试。")
            print("  Windows:  set DASHSCOPE_API_KEY=your_key")
            print("  Linux/Mac: export DASHSCOPE_API_KEY=your_key")
            sys.exit(1)
        sa.run_interactive()
        return

    # 创建批处理 AGENT 并运行
    agent = EnergyStorageAgent(config)

    if args.demo:
        results = agent.run_with_demo_data()
    else:
        results = agent.run(args.input)

    if results:
        print("\n分析完成！请查看 output/ 目录下的Excel文件。")
    else:
        print("\n分析未产生结果。请检查输入文件。")
        sys.exit(1)


if __name__ == "__main__":
    main()
