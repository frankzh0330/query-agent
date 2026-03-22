# Lark 动态进度通知 + 混合报表实施计划

## Context

用户希望在 Lark 中实现：
1. **解析过程动态通知**：各阶段展示进度信息（如"正在解析 TTP_EU 地区的 app_launch 事件..."），类似进度条效果
2. **混合报表**：最终展示 DSL 配置摘要 + 预留查询结果区域

## 实施方案

### 一、进度通知设计

使用飞书 **update_card** API，更新同一条消息，实现进度条效果。

**进度消息内容**：
- Layer 1: "🔍 正在分析您的查询..."
- Layer 2: "🔗 正在解析实体：地区=TTP_EU, 事件=app_launch..."
- Layer 3: "📝 正在生成语义 DSL..."
- Layer 4: "📋 正在渲染执行 DSL..."

### 二、最终混合报表设计

```
┌─────────────────────────────────────────────────────┐
│  查询结果 ✅                                          │
├─────────────────────────────────────────────────────┤
│  📊 统计摘要（3列布局）                               │
│  ┌─────────┬─────────┬─────────┐                   │
│  │ 📍 地区  │ 📊 指标  │ ⚡ 事件  │                   │
│  │ TTP_EU  │   pv    │app_launch│                   │
│  └─────────┴─────────┴─────────┘                   │
├─────────────────────────────────────────────────────┤
│  📅 时间范围: 近7天 | 🎯 项目: app_id 55            │
├─────────────────────────────────────────────────────┤
│  📋 DSL 配置详情                                     │
│  [可折叠区域或表格展示关键配置项]                     │
├─────────────────────────────────────────────────────┤
│  📈 查询结果（预留）                                 │
│  [此处展示实际查询返回的数据，后续接入]               │
└─────────────────────────────────────────────────────┘
```

---

## 修改文件清单

### 1. app.py

#### 1.1 添加进度通知方法

**位置**: `LarkClient` 类中（约第 411 行后）

```python
async def update_card(self, message_id: str, card: Dict[str, Any]) -> Dict[str, Any]:
    """更新已发送的消息卡片"""
    token = await self.get_access_token()
    url = f"{self.base_url}/im/v1/messages/{message_id}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    payload = {
        "msg_type": "interactive",
        "content": json.dumps(card)
    }

    async with httpx.AsyncClient() as client:
        response = await client.patch(url, headers=headers, json=payload)
        result = response.json()
        if result.get("code") != 0:
            logger.error(f"更新飞书消息失败: {result}")
        return result
```

#### 1.2 添加进度卡片生成器

**位置**: `CardBuilder` 类中

```python
@staticmethod
def progress_card(stage: str, details: Dict[str, Any]) -> Dict[str, Any]:
    """生成进度通知卡片"""
    stage_messages = {
        "layer1": "🔍 正在分析您的查询...",
        "layer2": f"🔗 正在解析实体：地区={details.get('region')}, 事件={details.get('event')}...",
        "layer3": "📝 正在生成语义 DSL...",
        "layer4": "📋 正在渲染执行 DSL...",
    }

    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": stage_messages.get(stage, "⏳ 处理中...")
                }
            }
        ]
    }
```

#### 1.3 添加混合报表卡片生成器

**位置**: `CardBuilder` 类中（替换或新增）

```python
@staticmethod
def result_report_card(
    query: str,
    extraction: Dict[str, Any],
    semantic: Dict[str, Any],
    exec_dsl: Dict[str, Any]
) -> Dict[str, Any]:
    """生成混合报表卡片"""
    region = ", ".join(semantic.get("region_filter", []))
    metric = extraction.get("metric", "N/A")
    event = extraction.get("event", "N/A")
    n_days = semantic.get("time_range", {}).get("n", 0)
    project_id = semantic.get("project_id", "N/A")

    # 统计摘要（3列）
    summary_columns = [
        {
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**📍 地区**\n{region}"}
                }
            ]
        },
        {
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**📊 指标**\n{metric}"}
                }
            ]
        },
        {
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**⚡ 事件**\n{event}"}
                }
            ]
        }
    ]

    # DSL 配置详情（表格）
    dsl_config_rows = [
        ["配置项", "值"],
        ["查询语句", query],
        ["项目 ID", str(project_id)],
        ["地区过滤", region],
        ["指标", metric],
        ["事件", event],
        ["时间范围", f"近{n_days}天"],
        ["分组维度", str(semantic.get("group_by", []))],
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "查询结果 ✅"},
            "template": "green"
        },
        "elements": [
            # 查询文本
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**查询:** {query}"}
            },
            {"tag": "hr"},
            # 统计摘要（3列）
            {"tag": "column_set", "columns": summary_columns},
            {"tag": "hr"},
            # 时间和项目信息
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**📅 时间范围:** 近{n_days}天  |  **🎯 项目:** app_id {project_id}"
                }
            },
            {"tag": "hr"},
            # DSL 配置详情
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**📋 DSL 配置详情**"}
            },
            # 配置表格
            {
                "tag": "table",
                "table_columns": [
                    {
                        "key": "config_key",
                        "name": "配置项",
                        "width": 120
                    },
                    {
                        "key": "config_value",
                        "name": "值",
                        "width": 300
                    }
                ],
                "rows": [
                    {"config_key": row[0], "config_value": str(row[1])}
                    for row in dsl_config_rows[1:]  # 跳过表头
                ]
            },
            {"tag": "hr"},
            # 查询结果（预留）
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**📈 查询结果**\n_此处将展示实际查询返回的数据_"}
            }
        ]
    }
```

#### 1.4 修改 nl2dsl 函数支持进度回调

**位置**: `nl2dsl` 函数添加可选参数

```python
def nl2dsl(req: NL2DSLRequest, progress_callback: Optional[Callable] = None) -> NL2DSLResponse:
    # ... 原有代码 ...

    # Layer 1 完成后
    if progress_callback:
        await progress_callback("layer1", {"query": req.text})

    # Layer 2 完成后
    if progress_callback:
        await progress_callback("layer2", {
            "region": region_filter,
            "metric": metric_id,
            "event": event_name
        })
    # ... 其他阶段类似 ...
```

#### 1.5 修改 _process_lark_query 实现进度更新

**位置**: `_process_lark_query` 函数（约第 545 行）

```python
async def _process_lark_query(open_id: str, query: str):
    """处理飞书查询请求"""
    message_id = None  # 用于更新消息

    try:
        # 1. 发送初始进度消息
        initial_result = await lark_client.send_card(
            open_id,
            CardBuilder.progress_card("layer1", {"query": query})
        )
        message_id = initial_result.get("data", {}).get("message_id")

        # 2. 定义进度回调函数
        async def progress_callback(stage: str, details: Dict[str, Any]):
            nonlocal message_id
            if message_id:
                await lark_client.update_card(
                    message_id,
                    CardBuilder.progress_card(stage, details)
                )

        # 3. 调用 nl2dsl，传入进度回调
        req = NL2DSLRequest(text=query, project_id=55)
        result = await nl2dsl_async(req, progress_callback)

        # 4. 发送最终结果报表
        await lark_client.update_card(
            message_id,
            CardBuilder.result_report_card(
                query=query,
                extraction=result.extraction_json,
                semantic=result.semantic,
                exec_dsl=result.exec_dsl
            )
        )

    except Exception as e:
        logger.exception(f"处理飞书查询失败: {e}")
        error_card = CardBuilder.error_card(f"处理失败: {str(e)}")
        if message_id:
            await lark_client.update_card(message_id, error_card)
        else:
            await lark_client.send_card(open_id, error_card)
```

---

## 关键修改点总结

| 文件 | 修改内容 | 行号参考 |
|------|---------|---------|
| app.py | 添加 `LarkClient.update_card()` | ~411 |
| app.py | 添加 `CardBuilder.progress_card()` | ~160 |
| app.py | 添加 `CardBuilder.result_report_card()` | ~200 |
| app.py | 修改 `nl2dsl` 添加回调参数 | ~46 |
| app.py | 修改 `_process_lark_query()` 实现进度更新 | ~545 |

---

## 验证方式

1. 启动服务：`uvicorn app:app --reload`
2. 在飞书中发送查询：`德国近7天 app_launch 的 PV`
3. 观察：
   - 收到 "🔍 正在分析您的查询..."
   - 更新为 "🔗 正在解析实体..."
   - 最终显示混合报表（统计摘要 + 配置表格 + 预留结果区）
