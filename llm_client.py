"""储能配置AGENT - 大模型统一客户端
支持通义千问(Qwen)和文心一言(Wenxin)，提供统一调用接口。
"""

import base64
import json
import logging
import os
from pathlib import Path
from typing import Generator, Optional

from config import LLMConfig

logger = logging.getLogger(__name__)

# 各厂商默认配置
PROVIDER_DEFAULTS = {
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-max",
        "vision_model": "qwen-vl-max",
        "env_key": "DASHSCOPE_API_KEY",
    },
    "wenxin": {
        "base_url": "https://qianfan.baidubce.com/v2",
        "model": "ernie-4.0-8k",
        "vision_model": "ernie-4.0-8k",
        "env_key": "BAIDU_API_KEY",
    },
    # 小米 MiMo（OpenAI Chat Completions 兼容）
    # 官方 Token Plan（新加坡）：https://token-plan-sgp.xiaomimimo.com/v1
    # 也可设环境变量 MIMO_BASE_URL 指向其它兼容入口（如自建/第三方中转）。
    "mimo": {
        "base_url": "https://token-plan-sgp.xiaomimimo.com/v1",
        "model": "MiMo-V2.5-Pro",
        "vision_model": "MiMo-V2.5-Pro",
        "env_key": "MIMO_API_KEY",
    },
    # 通用 OpenAI 兼容入口（中转/自部署），由 --llm-base-url 决定地址
    "openai_compat": {
        "base_url": "",
        "model": "gpt-4o-mini",
        "vision_model": "gpt-4o-mini",
        "env_key": "OPENAI_API_KEY",
    },
}


class LLMClient:
    """大模型统一客户端"""

    def __init__(self, config: LLMConfig = None):
        self.config = config or LLMConfig()
        self._client = None
        self._init_client()

    def _init_client(self):
        """初始化OpenAI兼容客户端。"""
        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("openai SDK未安装，请运行: pip install openai")
            return

        provider = self.config.provider.lower()
        defaults = PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["qwen"])

        # API Key: 优先配置文件，其次环境变量
        api_key = self.config.api_key or os.environ.get(defaults["env_key"], "")
        if not api_key:
            logger.warning("未配置API Key，请设置环境变量 %s 或在配置中指定 api_key", defaults["env_key"])
            return

        # Base URL（MiMo：允许 MIMO_BASE_URL 覆盖默认官方地址）
        if provider == "mimo":
            base_url = (
                self.config.base_url
                or os.environ.get("MIMO_BASE_URL", "").strip()
                or defaults["base_url"]
            )
        else:
            base_url = self.config.base_url or defaults["base_url"]
        if not base_url:
            logger.warning("provider=%s 需要 base_url，请通过 --llm-base-url 或 LLMConfig.base_url 指定",
                            provider)
            return

        # 模型：仅当未显式设置（或还是初始默认 qwen-max/qwen-vl-max）时，
        # 才用 provider 默认。如果用户已经传了非 qwen 的模型名就尊重之。
        if not self.config.model or (provider != "qwen" and self.config.model == "qwen-max"):
            self.config.model = defaults["model"]
        if not self.config.vision_model or (provider != "qwen" and self.config.vision_model == "qwen-vl-max"):
            self.config.vision_model = defaults["vision_model"]

        try:
            self._client = OpenAI(api_key=api_key, base_url=base_url)
            logger.info("LLM客户端初始化成功: provider=%s, model=%s", provider, self.config.model)
        except Exception as e:
            logger.warning("LLM客户端初始化失败: %s", e)

    @property
    def available(self) -> bool:
        """检查LLM是否可用。"""
        return self._client is not None

    # ------------------------------------------------------------------
    # 核心调用方法
    # ------------------------------------------------------------------
    def chat(self, messages: list[dict], temperature: float = None,
             max_tokens: int = None, model: str = None) -> str:
        """发送对话请求。

        Args:
            messages: [{"role": "system"/"user"/"assistant", "content": "..."}]
            temperature: 温度参数
            max_tokens: 最大token数
            model: 指定模型

        Returns:
            LLM回复文本
        """
        if not self.available:
            raise RuntimeError("LLM客户端未初始化，请检查API Key配置")

        resp = self._client.chat.completions.create(
            model=model or self.config.model,
            messages=messages,
            temperature=temperature or self.config.temperature,
            max_tokens=max_tokens or self.config.max_tokens,
        )
        return resp.choices[0].message.content

    def chat_with_image(self, prompt: str, image_paths: list[str] = None,
                        image_base64_list: list[str] = None,
                        system_prompt: str = None,
                        max_tokens: int = None,
                        media_type: str = "png") -> str:
        """发送包含图片的对话请求（用于视觉模型）。

        Args:
            prompt: 文本提示
            image_paths: 图片文件路径列表
            image_base64_list: base64编码的图片列表
            system_prompt: 系统提示

        Returns:
            LLM回复文本
        """
        if not self.available:
            raise RuntimeError("LLM客户端未初始化")

        content = []

        # 添加图片
        images = image_base64_list or []
        if image_paths:
            for p in image_paths:
                b64 = self._encode_image(p)
                if b64:
                    images.append(b64)

        mime = (media_type or "png").lower().lstrip(".")
        if mime in ("jpg",):
            mime = "jpeg"
        for img_b64 in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/{mime};base64,{img_b64}"},
            })

        # 添加文本
        content.append({"type": "text", "text": prompt})

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content})

        resp = self._client.chat.completions.create(
            model=self.config.vision_model,
            messages=messages,
            temperature=self.config.temperature,
            max_tokens=max_tokens or self.config.max_tokens,
        )
        return resp.choices[0].message.content

    def chat_with_tools(self, messages: list[dict], tools: list[dict],
                        temperature: float = None, max_tokens: int = None,
                        tool_choice: str = "auto", model: str = None):
        """带工具调用（function calling）的对话请求。

        Args:
            messages: 对话历史
            tools: OpenAI 格式的 tool schema 列表
            tool_choice: "auto" / "none" / 指定工具名
            model: 指定模型

        Returns:
            完整的 message 对象，包含 .content 和 .tool_calls
        """
        if not self.available:
            raise RuntimeError("LLM客户端未初始化，请检查API Key配置")

        kwargs = dict(
            model=model or self.config.model,
            messages=messages,
            temperature=temperature if temperature is not None else self.config.temperature,
            max_tokens=max_tokens or self.config.max_tokens,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        resp = self._client.chat.completions.create(**kwargs)
        return resp.choices[0].message

    def chat_stream(self, messages: list[dict], temperature: float = None,
                    max_tokens: int = None) -> Generator[str, None, None]:
        """流式对话请求。"""
        if not self.available:
            raise RuntimeError("LLM客户端未初始化")

        stream = self._client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            temperature=temperature or self.config.temperature,
            max_tokens=max_tokens or self.config.max_tokens,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def chat_with_tools_stream(self, messages: list[dict], tools: list[dict],
                                temperature: float = None, max_tokens: int = None,
                                tool_choice: str = "auto", model: str = None
                                ) -> Generator[dict, None, None]:
        """带工具调用的**流式**对话。

        生成器产出事件字典：
            {"type": "text",       "delta": "..."}                 # 文本增量
            {"type": "tool_calls", "tool_calls": [{...}], "content": "..."}
                                                                    # 流结束时一次性给出完整工具调用
            {"type": "done",       "content": "..."}                # 流结束（无工具调用）

        Qwen / OpenAI 的流式 tool_calls 是分片返回的（id/name 在第一片，
        arguments 在后续片中），这里负责把它们组装成完整结构。
        """
        if not self.available:
            raise RuntimeError("LLM客户端未初始化，请检查API Key配置")

        kwargs = dict(
            model=model or self.config.model,
            messages=messages,
            temperature=temperature if temperature is not None else self.config.temperature,
            max_tokens=max_tokens or self.config.max_tokens,
            stream=True,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        stream = self._client.chat.completions.create(**kwargs)

        # tool_calls 分片累积器：index -> {id, type, function: {name, arguments}}
        tool_calls_buf: dict[int, dict] = {}
        text_buf: list[str] = []

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # 文本增量
            if getattr(delta, "content", None):
                text_buf.append(delta.content)
                yield {"type": "text", "delta": delta.content}

            # 工具调用增量
            tcs = getattr(delta, "tool_calls", None)
            if tcs:
                for tc in tcs:
                    idx = tc.index if tc.index is not None else 0
                    if idx not in tool_calls_buf:
                        tool_calls_buf[idx] = {
                            "id": tc.id or "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    if tc.id:
                        tool_calls_buf[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls_buf[idx]["function"]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls_buf[idx]["function"]["arguments"] += tc.function.arguments

        full_content = "".join(text_buf)
        if tool_calls_buf:
            ordered = [tool_calls_buf[i] for i in sorted(tool_calls_buf.keys())]
            yield {"type": "tool_calls", "tool_calls": ordered, "content": full_content}
        else:
            yield {"type": "done", "content": full_content}

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------
    def ask(self, prompt: str, system_prompt: str = None, **kwargs) -> str:
        """简单问答。"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, **kwargs)

    def ask_json(self, prompt: str, system_prompt: str = None, **kwargs) -> dict:
        """提问并要求返回JSON格式。"""
        json_prompt = prompt + "\n\n请直接返回JSON格式，不要包含markdown代码块标记。"
        response = self.ask(json_prompt, system_prompt, **kwargs)
        # 清理可能的markdown标记
        response = response.strip()
        if response.startswith("```"):
            response = response.split("\n", 1)[-1]
        if response.endswith("```"):
            response = response.rsplit("```", 1)[0]
        response = response.strip()
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            logger.warning("LLM返回的不是有效JSON: %s", response[:200])
            return {"raw": response, "error": "JSON解析失败"}

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    @staticmethod
    def _encode_image(image_path: str) -> Optional[str]:
        """将图片文件编码为base64。"""
        try:
            path = Path(image_path)
            if not path.exists():
                logger.warning("图片文件不存在: %s", image_path)
                return None
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            logger.warning("图片编码失败: %s", e)
            return None
