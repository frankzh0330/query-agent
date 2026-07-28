# 评估策略

[English](EVALUATION.md) | [简体中文](EVALUATION.zh-CN.md)

本文说明 `query-agent` 当前如何做评估、现有 end-to-end eval harness 覆盖了什么，以及后续应该如何安全扩展。

## 为什么这个项目必须做评估

`query-agent` 已经不只是一个 matcher 或 prompt wrapper。它现在有：

- turn-based follow-up handling
- session persistence
- confirmation flow
- project memory injection
- user preference rerank
- async memory learning

这意味着很多回归并不会出现在单个函数的 unit test 里，而是只会在多层逻辑接起来之后暴露。

例子：

- follow-up 被误判成 new query
- confirmation 恢复了，但 partial query state 丢了
- project memory 读到了，但没有真正注入 LLM context
- user preference 权重过大，把候选排序带偏
- session 恢复了，但 task confirmation 没恢复

所以这个项目需要两类评估同时存在：

- unit / integration tests
- end-to-end golden-style eval cases

## 评估分层

### 1. Unit Tests

目的：

- 验证范围很窄的纯逻辑

例子：

- `followup_resolver.py`
- `query_state_merger.py`
- `user_preference_store.py`
- `long_term_memory.py`

适合测：

- 确定性规则
- 边界情况
- merge / scoring 逻辑

### 2. Endpoint / Integration Tests

目的：

- 验证 FastAPI 入口和编排逻辑

例子：

- [tests/test_app_endpoints.py](../tests/test_app_endpoints.py)
- [tests/test_session_manager.py](../tests/test_session_manager.py)
- [tests/test_task_manager.py](../tests/test_task_manager.py)

适合测：

- confirmation flow
- session/task persistence
- explain payload
- follow-up patch 执行路径

### 3. End-to-End Eval Harness

目的：

- 从请求到 agent 最终行为，验证整条链路是否符合预期
- 用数据驱动方式表达真实多轮场景，而不是每次都手写一条测试函数

主要文件：

- [tests/evals/nl2dsl_cases.yaml](../tests/evals/nl2dsl_cases.yaml)
- [tests/test_end_to_end_evals.py](../tests/test_end_to_end_evals.py)

这套 harness 更接近“golden cases”，不是单纯 unit test。

## 当前 E2E Eval 格式

每条 case 使用 YAML 描述，可包含：

- `setup`
- 一个或多个 `steps`
- 预期的 `status / turn_mode / semantic fields`

结构示例：

```yaml
cases:
  - name: followup_confirmation_flow
    setup:
      last_query_state:
        project_id: 55
        event: app_launch
        metric: pv
        time_range:
          type: last_n_days
          n: 7
        region_filter: ["ROW"]
        group_by: ["country"]
        filters: []
        turn_type: new_query
    steps:
      - text: 对比 purchase
        project_id: 55
        extraction:
          event_extractions: ["purchase"]
          region_filter: ["ROW"]
        resolver: low_confidence_event
        expect:
          status: needs_confirmation
          turn_mode: followup_patch
          candidates_contains: event
      - text: "1"
        project_id: 55
        expect:
          status: success
          turn_mode: confirmation
          semantic:
            event: purchase_success
            metric: pv
```

## Harness 现在能模拟什么

当前 runner 已支持：

- 预置 `last_query_state`
- mocked extraction output
- mocked resolver scenario
- 多步 session 连续性
- `project_memory` setup
- `restart_before: true` 重启模拟
- 对以下内容做断言：
  - `status`
  - `turn_mode`
  - `semantic.event`
  - `semantic.metric`
  - `semantic.region_filter`
  - `semantic.group_by`
  - `semantic.time_range.n`
  - candidate presence

## 当前已覆盖场景

当前 end-to-end case 已覆盖：

- basic new query
- follow-up change time
- follow-up change metric
- follow-up confirmation flow
- project memory context injection
- confirmation after restart

这意味着，最重要的“agent 化”链路现在已经有了回归保护。

## 为什么 YAML 驱动 eval 很有用

如果没有 case 文件，每加一个场景都要再写一条专门的测试函数。

有了 YAML 驱动 eval 以后：

- 加 case 成本更低
- 回看覆盖面更容易
- 讨论行为变化时更接近产品场景
- 未来接真实日志 replay 也更自然

这对 turn-based 系统尤其重要，因为正确性往往存在于“多步序列”里，而不是某一个孤立函数调用里。

## 建议下一步扩的 Eval 桶

### 1. 更多 Turn-Based 场景

例如：

- “不是这个 event，换成支付成功”
- “还是按国家看吧”
- “和昨天比一下”
- “继续看 UV”

### 2. Memory 场景

例如：

- project memory 改变默认 event mapping
- project memory 改变默认 region 行为
- 多条 memory 冲突时的相关片段选择

### 3. User Preference 场景

例如：

- preference rerank 改变候选顺序
- preference 严格限制在 `project_id + user_id`
- preference 不应该覆盖明显更强的语义匹配

### 4. Restart / Recovery 场景

例如：

- follow-up after restart
- confirmation after restart
- session 恢复了但 pending task 不存在

## E2E Eval 不打算做什么

当前 harness 不打算：

- 在线评估真实 LLM 质量
- 对生产数据库做真实下游查询正确性验证
- 替代 matcher 的 unit tests

它的主要定位是：

- agent behavior regression harness
- orchestration regression harness

## 如何运行

只跑 end-to-end harness：

```bash
./.venv311/bin/pytest -q tests/test_end_to_end_evals.py
```

和 turn-based 主测试一起跑：

```bash
./.venv311/bin/pytest -q \
  tests/test_end_to_end_evals.py \
  tests/test_app_endpoints.py \
  tests/test_followup_resolver.py \
  tests/test_query_state_merger.py \
  tests/test_session_manager.py \
  tests/test_task_manager.py
```

## 扩展原则

新增 eval case 时建议：

1. 如果主要是“场景变化”，优先写进 YAML。
2. 如果新增了纯逻辑，也要补 unit test。
3. 断言尽量聚焦稳定字段。
4. 除非必要，不要断完整 `exec_dsl` 字符串。
5. 优先断 semantic 层，而不是表面格式。

## 长期方向

长期来看，这套轻量 YAML harness 可以继续演进成更完整的 replay-and-regression 层：

- 更多来自真实 query log 的 golden cases
- case 分类标签
- 离线评分报告
- branch / model setting 对比

但即使是现在这个轻量版本，也已经足够保护项目里最重要的 turn-based 和 memory-heavy 主路径。
