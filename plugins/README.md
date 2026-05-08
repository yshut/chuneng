# 插件目录

把任意 `.py` 文件放到本目录，文件中所有用 `@tool(...)` 装饰的函数会在
Agent 启动时自动注册为可调用工具。

## 写一个插件

```python
# plugins/my_plugin.py
from agent_tools import tool

@tool(
    name="say_hello",
    description="问候用户",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "用户姓名"}
        },
        "required": ["name"]
    }
)
def say_hello(state, name):
    return {"msg": f"你好，{name}！当前已加载电费数据：{state.electricity_df is not None}"}
```

## 函数签名约定

- 第一个参数固定是 `state` (AgentState)，由 Agent 注入
- 其余参数对应 `parameters` 中声明的字段
- 返回值可以是 dict / str / list / DataFrame，会自动转成字符串给 LLM 看

## 命名约定

- 文件以 `_` 开头会被忽略（如 `_helper.py`）
- 工具名（`@tool` 的 `name=`）必须唯一，重复会覆盖

## 现有内置插件

- `example_co2.py` - 示例：根据年放电量估算年减排 CO2
- `example_recall.py` - 示例：另一种 memory 关键词召回封装
