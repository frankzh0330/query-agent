# 架构总览

[English](ARCHITECTURE.md) | [简体中文](ARCHITECTURE.zh-CN.md)

本文总结 `query-agent` 当前的真实架构、各主要模块的职责，以及层间依赖方向。

## 分层视图

```text
┌──────────────────────────────────────────────────────────────┐
│                        Gateway Layer                        │
│   TelegramGateway · HTTP Client / FastAPI Entry            │
├──────────────────────────────────────────────────────────────┤
│                          API Layer                          │
│   app.py                                                    │
│   请求解析 · session bootstrap · NL2DSL orchestration       │
├──────────────────────────────────────────────────────────────┤
│                       Turn / State Layer                    │
│   session_manager.py · session_models.py                    │
│   task_manager.py · followup_resolver.py                    │
│   query_state_merger.py                                     │
├──────────────────────────────────────────────────────────────┤
│                       NL2DSL Pipeline                       │
│   llm_extractions.py                                        │
│   matcher_service.py + matchers                             │
│   semantic_models.py · renderer.py · validators.py          │
├──────────────────────────────────────────────────────────────┤
│                        Memory Layer                         │
│   long_term_memory.py · memory_writer.py                    │
│   user_preference_store.py                                  │
├──────────────────────────────────────────────────────────────┤
│                    Runtime / System Layer                   │
│   bus/* · worker/* · dispatcher/* · server.py               │
└──────────────────────────────────────────────────────────────┘
```

## 依赖方向

核心规则：依赖应当自上而下流动。

```text
server.py
  ├─ gateway/*
  ├─ bus/*
  ├─ worker/*
  ├─ dispatcher/*
  └─ app.py
       ├─ service/session_manager.py
       ├─ service/task_manager.py
       ├─ service/followup_resolver.py
       ├─ service/query_state_merger.py
       ├─ service/llm_extractions.py
       ├─ matcher/*
       ├─ dsl/*
       └─ memory/*
```

设计约束：

- `app.py` 负责请求编排，不负责长生命周期后台循环
- `server.py` 负责启动 wiring 和生命周期，不负责查询语义
- `matcher/*` 聚焦匹配与召回，不掺入 session 策略
- `memory/*` 提供可复用知识层和偏好信号，不直接耦合 FastAPI 行为

## 顶层运行链路

### HTTP 路径

```mermaid
flowchart TD
    U["用户"] --> API["POST /nl2dsl"]
    API --> S["SessionManager.create_or_get"]
    S --> C["Enhanced Context"]
    C --> L1["LLM Extraction"]
    L1 --> L2["Matcher Resolution"]
    L2 --> T{"Turn Logic"}
    T -->|new_query| D1["Semantic DSL"]
    T -->|followup_patch| M["QueryState Merge"]
    M --> D1
    T -->|needs_confirmation| K["TaskManager"]
    K --> R["User Reply"]
    R --> D1
    D1 --> D2["Exec DSL Render"]
    D2 --> V["Validation"]
    V --> O["Response"]
    V --> ML["Async MemoryWriter"]
```

### Telegram / Bus 路径

```mermaid
flowchart TD
    TG["Telegram Gateway"] --> IN["Ingress Adapter + Cleaner + Dedup"]
    IN --> BUS["Message Bus"]
    BUS --> W["Agent Worker"]
    W --> API["app.nl2dsl"]
    API --> DISP["Response Dispatcher"]
    DISP --> TG
```

## NL2DSL Pipeline

### Layer 1：LLM Extraction

主要文件：[service/llm_extractions.py](service/llm_extractions.py)

职责：

- 把自然语言转成结构化 extraction
- 注入 session context 和选出的 project memory
- 在 follow-up 场景下尽量提取 patch，而不是重建全量状态

输入：

- `text`
- recent session context
- `last_query_state`
- selected `memory_corrections`

输出：

- `ExtractionsJson`

### Layer 2：Matcher Resolution

主要文件：[matcher/matcher_service.py](matcher/matcher_service.py) 及具体 matcher。

职责：

- 解析 `metric / event / dimension / time`
- 返回 score、candidates、`needs_confirmation`
- 保持解析逻辑确定性、可解释

重要细节：

- user preference 只在 recall 后做小幅 rerank
- matcher 本身仍然是主要语义解析器

### Layer 3：Semantic DSL

主要文件：[dsl/semantic_models.py](dsl/semantic_models.py)

职责：

- 表达规范化后的查询意图
- 将查询语义与下游执行格式解耦

### Layer 4：Exec DSL

主要文件：[dsl/renderer.py](dsl/renderer.py) 和 [dsl/validators.py](dsl/validators.py)

职责：

- 渲染执行 payload
- 校验 region 与一致性约束

## Turn-Based Querying

Turn-based 行为是系统的一等公民，不是简单的 prompt 技巧。

### 核心模块

- [service/session_models.py](service/session_models.py)
  - `QueryState`
  - `SessionContext`
  - `TaskContext`
- [service/followup_resolver.py](service/followup_resolver.py)
- [service/query_state_merger.py](service/query_state_merger.py)
- [service/task_manager.py](service/task_manager.py)

### Turn 模式

系统区分：

- `new_query`
- `followup_patch`
- `confirmation`

### Follow-Up 流程

```mermaid
flowchart TD
    Q["Incoming text"] --> D["detect_followup()"]
    D -->|new_query| N["Run full NL2DSL path"]
    D -->|followup_patch| P["Extract patch"]
    P --> M["merge_query_state()"]
    M --> S["Semantic DSL"]
    D -->|confirmation_reply| T["TaskManager / pending task"]
```

### 为什么重要

它能支持这类多轮查询：

```text
Q1: 德国 app_launch 的 PV
Q2: 昨天
Q3: 改成 UV
Q4: 再按渠道拆一下
Q5: 那美国呢
```

而不要求用户每轮都把所有字段重说一遍。

## Memory 架构

当前项目实际上已经形成了三层 memory。

### 1. Session Memory

主要文件：

- [service/session_manager.py](service/session_manager.py)
- [service/session_models.py](service/session_models.py)

存储内容：

- `last_query_state`
- `pending_task_id`
- recent messages
- turn metadata

作用：

- follow-up patch
- confirmation continuation
- session restart recovery

### 2. Project Memory

主要文件：[memory/long_term_memory.py](memory/long_term_memory.py)

存储内容：

- 项目级 corrections
- 业务约束
- 默认映射
- 领域 caveats

重要细节：

- memory 从 `project_{id}/MEMORY.md` 加载
- 会结合当前 query text 选择更相关的 memory 片段
- 不再需要每轮都注入整份项目 memory

### 3. User Preference Signal

主要文件：[memory/user_preference_store.py](memory/user_preference_store.py)

存储内容：

- 用户级 `event / metric / group_by` 使用计数
- 作用域严格限制为 `project_id + user_id`

作用：

- 在 recall 后对 top candidates 做 rerank
- 绝不替代 matcher 主语义判断
- 保持为弱信号，而不是主解析器

## Confirmation Flow

低置信度歧义通过显式确认处理，而不是 silent guessing。

### Confirmation 生命周期

```mermaid
flowchart TD
    A["ResolvedResult.needs_confirmation"] --> B["Create TaskContext"]
    B --> C["Persist task + pending_task_id"]
    C --> D["Return candidates to user"]
    D --> E["User reply"]
    E --> F["Restore task"]
    F --> G["Apply confirmed value"]
    G --> H["Continue query build"]
```

### 持久化

Session 和 Task 分开持久化：

- session：JSONL append-only session log
- task：JSONL append-only task log

这样可以支持：

- 进程重启恢复
- 聊天渠道里的延迟确认

## Async Memory Learning

主要文件：[memory/memory_writer.py](memory/memory_writer.py)

职责：

- 异步判断某次成功查询是否值得沉淀为长期记忆
- 将学习结果追加到项目级 memory 文件
- 自动维护 `MEMORY.md` 索引

它是一个 sidecar 行为：

- 非阻塞
- 容错
- 不影响主查询路径

## 系统组件

### `app.py`

职责：

- request/response models
- session bootstrap
- follow-up / confirmation routing
- matcher orchestration
- semantic / exec DSL 构建
- 最终 `explain` 组装

### `server.py`

职责：

- 应用生命周期
- matcher service 初始化
- bus / worker / dispatcher 装配
- Telegram gateway 启动
- catalog scheduler 启动

### `gateway/*`, `ingress/*`, `bus/*`, `worker/*`, `dispatcher/*`

职责：

- 渠道适配
- 文本清洗与去重
- 异步请求传输
- worker 消费
- 响应路由

这些组件让系统不只是一个 HTTP API，而是可以作为消息驱动 agent 运行。

## Validation 与 Debugging

系统当前暴露了比较丰富的 explain / debug 信息：

- `resolver_explain`
- `turn_explain`
- candidate scores
- follow-up decision signals
- patch fields
- confirmed fields
- 各层 timing

这很重要，因为项目已经更像 agent，而不是一次性翻译器。

## Evaluation 策略

项目同时使用常规测试和数据驱动 end-to-end eval。

### End-to-End Eval Harness

主要文件：

- [tests/evals/nl2dsl_cases.yaml](tests/evals/nl2dsl_cases.yaml)
- [tests/test_end_to_end_evals.py](tests/test_end_to_end_evals.py)

当前覆盖包括：

- basic new query
- follow-up patch
- follow-up + confirmation
- project memory injection
- restart + confirmation recovery

这套 harness 更接近“golden cases”，而不是单纯 unit test。

## Catalog 与 Metadata

仓库里的 YAML catalog 主要是 demo/development 态输入。

更接近生产的方向是：

- metadata 从 HTTP 源获取
- 同步到本地 catalog
- matcher indexes 在刷新后重建

这一点重要，因为真实环境可能会有：

- 数万级 event
- 每个 event 多个 dimension/property
- 强项目语义和业务约束

## 当前架构定位

现在的项目更适合被描述为：

- 一个分层 NL2DSL 引擎
- 加上 turn-based query-state handling
- 加上 scoped memory
- 加上 explicit ambiguity handling
- 加上 agent-style async learning

它已经不是一个 stateless prompt wrapper。
