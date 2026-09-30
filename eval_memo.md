```commandline
Question
    │
    ▼
Schema Retrieval Agent
    │        Eval: Recall@K / Precision@K / Join-Key Recall
    ▼
Metric Retrieval Agent
    │        Eval: Metric Accuracy / Formula Match
    ▼
Planning Agent
    │        Eval: Plan Coverage / Tool Selection / Step Accuracy
    ▼
SQL Generator
    │        Eval: Execution Accuracy / AST Similarity
    ▼
Repair Agent
    │        Eval: Repair Success Rate / Retry Count
    ▼
Final Answer
    │        Eval: End-to-End Success / Latency / Cost
```

| Agent | 输入 | 输出 | Eval |
|-------|------|------|------|
| Domain Retrieval | Question | Sales、User | Domain Accuracy |
| Subject Retrieval | Domain | Orders、Profile | Subject Recall@K |
| Table Retrieval | Subject | fact_orders、dim_users | Table Recall@K |
| Column Retrieval | Tables | payment_amount、country | Column Recall@K |
| Join Path Retrieval | Tables | `orders.user_id → users.user_id` | Join Path Accuracy |
| Metric Retrieval | Question | GMV Definition | Metric Accuracy |


SELECT *

FROM (

SELECT *,
ROW_NUMBER()
OVER(
PARTITION BY category
ORDER BY revenue DESC
)

FROM t

)
WHERE rn<=3

AST:
Window

Partition(category)

Order(revenue)

Function(Row_Number)


Planner
│
├── Scan Planner

├── Join Planner

├── Filter Planner

├── Aggregate Planner

├── Window Planner

├── Projection Planner

              Natural Language
                      │
                      ▼
              Intent Understanding
                      │
                      ▼
             Context Retrieval
      (Schema / Metric / Join Graph)
                      │
                      ▼
              Planning Agent
                      │
                      ▼
          Logical Plan Builder
                      │
                      ▼
             Plan Validation
                      │
                      ▼
             SQL Generator
                      │
                      ▼
             SQL Validator
                      │
                      ▼
                 Execute

Planner
│
├── Scan Planner

├── Join Planner

├── Filter Planner

├── Aggregate Planner

├── Window Planner

├── Projection Planner



                 Scan(fact_orders)
                        │
                        ▼
          Join(dim_product, product_id)
                        │
                        ▼
             Filter(order_date=last_month)
                        │
                        ▼
 Aggregate(group_by=[category, product],
           metrics=[SUM(payment_amount)])
                        │
                        ▼
 Window(ROW_NUMBER,
        partition=category,
        order=SUM(payment_amount) DESC)
                        │
                        ▼
             Filter(row_number <= 3)
                        │
                        ▼
     Project(category, product, revenue)
 
Select(
    projection=[...],
    from_table="orders",
    group_by=[...],
    window=Window(...),
    where=...
)



AST还能自动优化
例如Planner生成：
Filter

↓

Aggregate
AST Optimizer发现：
Filter其实可以Pushdown。
自动变成：
Filter

↓

Scan
再Render。
LLM根本不用参与。
这就是数据库Optimizer一直在做的事情。

                    Intent
                       │
      ┌────────────────┼───────────────┐
      │                │               │
      ▼                ▼               ▼
Metric Retriever   Schema Retriever  Example Retriever
      │                │               │
      └────────────┬───┴───────────────┘
                   ▼
            Join Graph Retriever
                   ▼
              Context Builder

Context Builder最后输出：
metric:
    GMV=SUM(payment_amount)

tables:
    fact_orders
    dim_product

columns:
    payment_amount
    category

joins:
    fact_orders.product_id
    =
    dim_product.product_id

examples:
    Top5 Product SQL

第一种方案：Constraint Validation（目前最常见）
Google、Databricks、Snowflake 都大量使用。
Intent 不是自由文本。
而是 Schema。
例如：
{
    "intent": "...",
    "metric": "...",
    "time": "...",
    "aggregation": "...",
    "ranking": "...",
    "entity": "..."
}
其中：
intent 只能取：
Aggregate
Ranking
Trend
Compare
Filter
Distribution
Metric
只能来自 Metric Catalog：
GMV

Revenue

DAU

Retention
Time
只能来自：
today

yesterday

last_month

custom_range
LLM如果输出：
{
    "intent":"sort_data"
}
Validator直接Reject。
重新让LLM生成。


那Intent需要Eval吗？
一定需要。
而且应该单独建Benchmark。
例如：
Dataset
Question:

Top 3 products by GMV

Golden Intent:

intent:

ranking

metric:

GMV

entity:

product

top:

3
Planner输出：
intent:

aggregation

metric:

Revenue
立即Fail。
Eval指标
和NER很像。
例如：
Metric Accuracy
GMV

×

Revenue
Entity Accuracy
Product

✓
Time Accuracy
Last Month

✓
TopK Accuracy
3

✓
Intent Accuracy
Ranking

×

Aggregate
最后Dashboard：
Field	Accuracy
Intent	98.2%
Metric	94.6%
Entity	97.8%
Time	99.1%
Ranking	92.3%


这比只有SQL Accuracy有意义得多。


                     User Question
                           │
                           ▼
                  Intent Agent (LLM)
                           │
            ┌──────────────┴──────────────┐
            ▼                             ▼
   Intent Validator               Confidence Scorer
            │                             │
            └──────────────┬──────────────┘
                           ▼
                 Intent Evaluation
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
  Metric Accuracy     Entity Accuracy    Time Accuracy
                           │
                           ▼
                  Context Retrieval
   

explore: orders {

join: users {

sql_on:

${orders.user_id}

=

${users.id};;

}

}

measure: revenue {

type: sum

sql: payment_amount ;;

}

| Metric ID | Name | Aliases | Description |
|-----------|------|---------|-------------|
| m001 | GMV | Gross Merchandise Value, Merchandise Value | Sum(payment_amount), exclude refund |
| m002 | Revenue | Sales Revenue | Sum(net_amount) |
| m003 | DAU | Daily Active User | Count(distinct user_id) |
| m004 | ARPU | Average Revenue Per User | Revenue / DAU |

sematic layer
| 阶段 | 优势 | 劣势 | 作用 |
|------|------|------|------|
| Exact Match | 极快（O(1)） | 只能匹配完全一致 | 处理标准术语（GMV、DAU） |
| BM25 | 快、可解释 | 不理解语义 | 处理别名、缩写、关键词匹配 |
| Embedding Recall | 理解语义 | 容易召回很多相似概念 | 扩大候选集，避免漏召回 |
| Cross Encoder | 精度最高 | 较慢 | 对少量候选做精排 |


| Doc | 模块 | 主要内容 |
|------|------|---------|
| 01 | Intent Understanding | Intent Parser、Slot、Validator、Retry、Eval、API、数据结构 |
| 02 | Semantic Layer Resolver | Metric Resolver、Entity Resolver、Dimension Resolver、Hybrid Search、Metadata Store、Eval |
| 03 | Context Retrieval | Schema Retrieval、Join Retrieval、Example Retrieval、Hybrid Recall、Rerank、Embedding、Context Builder |
| 04 | Planning Agent | Operation Planner、Dependency Planner、Logical Plan、Operator、Planner Eval |
| 05 | SQL AST Builder | AST Node、Builder、AST Validation、Rule Engine、Database Agnostic Design |
| 06 | SQL Generator | Renderer、Dialect Adapter（Spark、ClickHouse、Snowflake、BigQuery）、Template Engine |
| 07 | SQL Validation & Repair | Parser、Execution Error Repair、Retry Strategy、AST Diff |
| 08 | Evaluation Platform | Benchmark、Trajectory Eval、Component Eval、Dashboard、CI/CD Regression |
| 09 | Metadata & Context Platform | Metric Store、Join Graph、DataHub/OpenMetadata、LookML、dbt Semantic Layer |
| 10 | Overall Architecture | 整体流程、Agent 通信、数据流、日志、事件、部署方式 |


1. Background
2. Responsibilities
3. Input / Output
4. Internal Architecture
5. Core Components
6. Data Structures
7. Processing Flow
8. Algorithms
9. Evaluation
10. Failure Handling
11. API Design
12. Storage Design
13. Sequence Diagram
14. Future Extension

我建议每份文档控制在 3000~5000 行左右的设计深度，里面包含：
完整架构图（Mermaid）
Sequence Diagram
State Machine
JSON Schema
Class Diagram
Interface Design
API Definition
Evaluation Design
Retry Strategy
Failure Case
为什么采用这种设计，而不是其他方案
与 Google、Snowflake、Databricks、LookML、dbt、Spark Catalyst、Apache Calcite 等方案的对比
后续 Codex 实现时需要回答的问题（Implementation Checklist）
我建议我们按顺序写，而不是一次生成全部。
因为每一层大概都会有 40~60 页 的内容，而且后面的设计会依赖前面的数据结构。
建议顺序如下：
Intent Understanding Design（整个系统入口）
Semantic Layer Resolver Design
Context Retrieval Design
Planning Agent Design
SQL AST Builder Design
SQL Generator Design
Evaluation Platform Design
Overall System Design
这样每完成一份，我们都可以 review、修改，然后再继续下一层


| AST Builder | SQL Generator |
|------------|--------------|
| 构建SQL语法树 | 输出SQL字符串 |
| Rule Check | Dialect Translation |
| AST Validation | Pretty Print |
| SQL Optimization | Render |

                 Evaluation Platform
                         │
      ┌──────────────────┼──────────────────┐
      │                  │                  │
      ▼                  ▼                  ▼
 Component Test     Trajectory Test   End-to-End Test
      │                  │                  │
      └──────────────────┼──────────────────┘
                         ▼
                 Human Review Queue
                         │
                         ▼
                  Benchmark Update
                         │
                         ▼
                 Regression Pipeline
 

| 层级 | 目标 | 典型指标 |
|------|------|----------|
| Component Test | 每个 Agent 是否正确 | Intent Accuracy、Metric Accuracy、Recall@K、Planner Accuracy、AST Accuracy |
| Trajectory Test | 每一步推理是否合理 | Tool Selection、Operator Sequence、Plan Coverage、Trajectory Match |
| End-to-End Test | 用户最终是否得到正确结果 | Execution Accuracy、Result Accuracy、Latency、Cost |
| Human Review | 无法自动判断或线上反馈 | SQL 可读性、业务正确性、Join 合理性、Metric 定义是否符合业务 |


               API Gateway
                     │
       ┌─────────────┴─────────────┐
       ▼                           ▼
Intent Service              Retrieval Service
       │                           │
       └─────────────┬─────────────┘
                     ▼
             Planning Service
                     ▼
             SQL Generator Service
                     ▼
            Security Policy Service
                     ▼
             Sandbox Executor
                     ▼
             Result Cache
                     ▼
                  Client

每个都是独立微服务，可以：
单独扩容
单独发布
单独A/B Test
单独Rollback

SQL
    │
    ▼
EXPLAIN
    │
    ▼
Dry Run
    │
    ▼
LIMIT 10
    │
    ▼
Full Execute

这样可以提前发现：
全表扫描
成本过高
返回行数过多
权限问题

             Gemini / Text-to-SQL
                     │
                     ▼
              Generated SQL
                     │
                     ▼
          BigQuery Policy Engine
                     │
     ┌───────────────┼────────────────┐
     ▼               ▼                ▼
 IAM Check      Data Policy      Resource Policy
     │               │                │
     ▼               ▼                ▼
 Execute        Rewrite SQL      Cost Control
 

Question
      │
      ▼
Schema Retriever
      │
      ▼
Permission Filter
      │
      ▼
Visible Schema
      │
      ▼
Planner

                User Question
                      │
                      ▼
             Context Assembly
                      │
         Permission-aware Retrieval
          （只能召回用户可见对象）
                      │
                      ▼
                  Planner
                      │
                      ▼
               Generated SQL
                      │
                      ▼
           Database Policy Engine
        （IAM / Row Policy / Column Policy）
                      │
                      ▼
                   Execute


第一种：同步 Pipeline（目前最主流）
绝大多数 Text-to-SQL 系统都是一个 DAG（Directed Acyclic Graph）。
例如：
API Gateway
      │
      ▼
Intent Service
      │
      ▼
Semantic Resolution Service
      │
      ▼
Context Assembly Service
      │
      ▼
Planning Service
      │
      ▼
AST Builder Service
      │
      ▼
SQL Generator Service
      │
      ▼
Security Policy Service
      │
      ▼
Sandbox Executor
      │
      ▼
Result Formatter
      │
      ▼
Client
这里就是一个严格的 DAG。
例如：
Planning

depends on

Context Assembly
没有完成。
Planning不会开始。
所以：
Intent一定先于Planner。
为什么能保证顺序？
其实很简单。
不是靠Kafka。
不是靠Event。
而是：
RPC。
例如：
Planner Service
收到：
{
   "context":...
}
如果：
没有Context。
Planner根本不能执行。
例如：
context = ContextService.build(...)

plan = Planner.plan(context)
这就是：
同步调用。
Google内部很多Agent都是这种。


第三种：Future / Async DAG
真正能并行的是：
没有依赖的节点。
例如：
Context Assembly：
其实里面：
Metric Retriever

Schema Retriever

Join Retriever

Example Retriever
它们之间：
没有依赖。
所以：
可以：
                Context Assembly
                      │
      ┌───────────────┼────────────────┐
      ▼               ▼                ▼
Schema Retriever   Join Retriever   Example Retriever
这里：
三个Future。
例如：
schema_future = ...

join_future = ...

example_future = ...
最后：
await all(...)
Google、OpenAI、Anthropic大量Agent都是这样。


大规模生产怎么保证不会乱？
真正不是：
靠：
sleep

lock
而是：
每个Node都有：
Input Schema。
例如：
Planner：
要求：
{
  "semantic_context":...
}
如果：
没有：
Workflow：
不会调Planner。
所以：
实际上：
Workflow就是：
DAG。
例如：
Planning:

depends_on:

- Context Assembly
这就是：
Argo。
Temporal。
Dagster。
都一样。



                    API Gateway
                         │
                         ▼
                  Intent Understanding
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
      Semantic Resolution    Example Retrieval
              │                     │
              ├──────────┐          │
              ▼          ▼          ▼
     Schema Retriever  Join Retriever
              │          │
              └──────┬───┘
                     ▼
             Context Assembly
                     │
                     ▼
              Planning Agent
                     │
                     ▼
               AST Builder
                     │
                     ▼
              SQL Generator
                     │
                     ▼
             Security Policy
                     │
                     ▼
             Sandbox Executor


