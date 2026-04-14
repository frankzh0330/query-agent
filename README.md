# query-agent

NL2DSL Agent — 自然语言到语义 DSL 的转换代理。支持中英文自然语言查询，自动解析 metric / event / dimension / time_range，生成可执行 DSL 并返回查询结果。

## 功能

- 中英文自然语言查询解析（LLM Extraction + Fuzzy Matching）
- 4-Stage Matcher Pipeline：Normalize → Exact Alias → Inverted Index Recall → RapidFuzz Rerank
- 多轮会话记忆（Session + Long-term Memory + Context Injection）
- Telegram Bot 集成（Long Polling）
- Message Bus 架构（Direct / Redis），支持多 Worker 水平扩展
- Catalog 动态同步与定时调度

## 架构

```
Gateway (Telegram / HTTP)
    ↓
Ingress (Clean → Deduplicate)
    ↓
Message Bus (Direct / Redis)
    ↓
Agent Worker
    ↓
Layer 1: LLM Extraction (GLM-4)
    ↓
Layer 2: Matcher Resolution (metric/event/dimension/time)
    ↓
Layer 3: Semantic DSL Build
    ↓
Layer 4: Exec DSL Render + Validate
    ↓
Response Dispatcher → Gateway → User
```

## 快速开始

### 环境要求

- Python 3.11+
- Redis 7+（仅 Redis 模式需要）

### 安装依赖

```bash
pip install -r requirements.txt
```

### 环境变量

复制并编辑环境变量文件：

```bash
cp .env.example .env
```

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `TELEGRAM_BOT_TOKEN` | 否 | - | Telegram Bot Token，不设置则跳过 Telegram Gateway |
| `MESSAGE_BUS_BACKEND` | 否 | `direct` | 消息总线模式：`direct`（单进程）或 `redis` |
| `REDIS_URL` | 否 | `redis://localhost:6379/0` | Redis 连接地址（Redis 模式） |
| `CATALOG_API_BASE` | 否 | - | Catalog 同步 API 地址，不设置则跳过定时同步 |
| `PORT` | 否 | `8000` | 服务端口 |
| `HOST` | 否 | `0.0.0.0` | 服务绑定地址 |

### 启动方式

**方式一：本地开发（Direct 模式）**

默认使用内存直连模式，无需 Redis：

```bash
python server.py
```

或通过 uvicorn 启动（支持热重载）：

```bash
uvicorn server:app_with_ws --reload --port 8000
```

**方式二：Docker Compose（Redis 模式）**

```bash
# 在 .env 中设置环境变量后
docker-compose up --build
```

默认 `MESSAGE_BUS_BACKEND=direct`。切换到 Redis 模式：

```bash
MESSAGE_BUS_BACKEND=redis docker-compose up --build
```

### 验证服务

```bash
# 健康检查
curl http://localhost:8000/docs

# NL2DSL 查询
curl -X POST "http://localhost:8000/nl2dsl" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "德国近7天 app_launch 的 PV",
    "project_id": 55
  }'

# 查看会话列表
curl http://localhost:8000/sessions
```

## API

### POST /nl2dsl

自然语言转 DSL 主接口。

**请求体：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | 是 | 自然语言查询 |
| `project_id` | int | 否 | 项目 ID，默认 55 |
| `session_id` | string | 否 | 会话 ID，首次不传，后续传入以保持上下文 |
| `user_id` | string | 否 | 用户 ID |
| `chat_id` | string | 否 | Telegram Chat ID，用于进度通知 |

**响应体：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `extraction_json` | object | LLM 提取结果 |
| `semantic` | object | 语义 DSL |
| `exec_dsl` | object | 可执行 DSL |
| `explain` | object | 解析过程与耗时 |
| `session_id` | string | 会话 ID（后续请求传回） |
| `status` | string | `success` 或 `early_exit` |
| `message` | string | early_exit 时的提示消息 |

### POST /query/bearer

执行 Bearer 数据查询。

### GET /sessions

列出所有会话（调试用）。

### GET /sessions/{session_id}

获取单个会话详情。

### DELETE /sessions/{session_id}

删除指定会话。

## 项目结构

```
query-agent/
├── server.py                # 启动入口，生命周期管理
├── app.py                   # FastAPI 路由，NL2DSL 主流程
├── gateway/                 # Gateway 层
│   ├── base.py              #   Gateway 基类
│   └── telegram_gateway.py  #   Telegram Bot 集成
├── ingress/                 # 入口预处理
│   ├── cleaner.py           #   文本清洗
│   ├── deduplicator.py      #   请求去重
│   └── telegram_adapter.py  #   Telegram 消息适配
├── bus/                     # 消息总线
│   ├── message_schema.py    #   BusMessage / BusResult 协议
│   ├── direct_call_bus.py   #   内存直连模式
│   └── redis_bus.py         #   Redis 队列模式
├── worker/
│   └── agent_worker.py      # Agent Worker（消费 Bus 消息）
├── dispatcher/
│   └── response_dispatcher.py  # 响应分派器
├── service/                 # 服务层
│   ├── llm_extractions.py   #   LLM 实体提取（Layer 1）
│   ├── session_manager.py   #   会话管理
│   ├── session_models.py    #   会话数据模型
│   ├── task_manager.py      #   任务管理（确认流程）
│   ├── bearer_service.py    #   Bearer 查询执行
│   └── catalog_sync.py      #   Catalog 同步
├── matcher/                 # 匹配层（Layer 2）
│   ├── matcher_service.py   #   Matcher 门面服务
│   ├── metric_matcher.py    #   指标匹配
│   ├── event_matcher.py     #   事件匹配
│   ├── dimension_matcher.py #   维度匹配
│   └── time_matcher.py      #   时间解析
├── dsl/                     # DSL 层（Layer 3-4）
│   ├── semantic_models.py   #   语义 DSL 模型
│   ├── renderer.py          #   Exec DSL 渲染器
│   └── validators.py        #   结果验证
├── memory/                  # 记忆系统
│   ├── long_term_memory.py  #   长期记忆（用户偏好）
│   └── models.py            #   记忆数据模型
├── catalog/                 # 配置数据（YAML）
│   ├── metrics.yaml
│   ├── events.yaml
│   ├── dimensions.yaml
│   └── region_groups.yaml
└── tests/                   # 测试
```
