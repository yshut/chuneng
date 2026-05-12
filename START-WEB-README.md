# 启动 Web UI 说明（`start-web.cmd`）

## 为什么 `.cmd` 文件里都是英文？

中文 Windows 的 `cmd.exe` 默认按 GBK（CP936）编码读取批处理文件。
如果 `.cmd` 文件保存成 UTF-8，中文注释里的全角字符（如 `（）`、`：`）会被解析错误，
导致 CMD 丢掉行首的 `REM` / `echo`，把注释当命令去执行，于是出现：

```
'Pro' is not recognized as an internal or external command
'""' is not recognized as an internal or external command
'行：set' is not recognized as an internal or external command
```

为了让脚本在任何编码下都能稳定工作，正文统一用 ASCII 英文。

## 准备工作

1. 安装 Python 3.10 及以上版本，确保 `python` 或 `py` 命令在 PATH 里。
2. 在仓库根目录运行：

   ```cmd
   pip install -r requirements.txt
   ```

3. **配置 API Key**：MiMo / OpenAI 兼容入口的 Key 需要预先放进环境变量。

   **方式 A：临时（仅当前 cmd 窗口有效）**

   ```cmd
   set MIMO_API_KEY=sk-yshut-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   start-web.cmd
   ```

   **方式 B：永久（推荐）**

   - Win + R 输入 `sysdm.cpl` → 高级 → 环境变量
   - 在「用户变量」中新建：
     - 变量名：`MIMO_API_KEY`
     - 变量值：你的真实 Key
   - 关掉当前 cmd 重开一个，再双击 `start-web.cmd` 即可。

   **可选变量：**

   - `MIMO_BASE_URL`：自定义 API 入口；不设置时用脚本里写好的默认地址。
   - `CHUNENG_DATA_ROOT`：数据目录；默认是仓库下 `./data`，无需改。
   - `CHUNENG_CORS_ORIGINS`：前端 CORS 来源白名单，默认 `127.0.0.1:7860,localhost:7860`。

## 端口被占用怎么办

打开 `start-web.cmd`，把这一行改成你想用的端口：

```cmd
set "PORT=7860"
```

## 怎么停止服务

直接关掉黑色 cmd 窗口，或者按 `Ctrl + C`。

## 怎么在网页里改 LLM 配置

打开浏览器后，点击右上角的 ⚙ 设置按钮 → 在弹出的窗口里：

- 切 provider（qwen / wenxin / mimo / openai_compat）
- 改 Base URL / 模型 / API Key
- 先点「测试连接」确认能通
- 再点「保存并重载」—— 配置会写到 `data/llm_config.json`，下一次发消息立即生效，不用重启。
