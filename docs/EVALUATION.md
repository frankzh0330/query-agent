# Evaluation Strategy

[English](EVALUATION.md) | [Chinese](EVALUATION.zh-CN.md)

This document explains how `query-agent` is evaluated today, what the current end-to-end eval harness covers, and how to expand it safely as the agent evolves.

## Why Evaluation Matters Here

`query-agent` is not just a matcher or a prompt wrapper. It has:

- turn-based follow-up handling
- session persistence
- confirmation flow
- project memory injection
- user preference rerank
- async memory learning

That means many regressions will not show up in isolated unit tests. A bug often appears only when multiple stages interact.

Examples:

- a follow-up is misclassified as a new query
- confirmation resumes but loses the partial query state
- project memory is loaded but not injected into the LLM context
- user preference changes ranking too aggressively
- restart recovery works for session but not for task confirmation

This is why the project needs both:

- unit / integration tests
- end-to-end golden-style eval cases

## Evaluation Layers

### 1. Unit Tests

Purpose:

- verify pure logic with narrow scope

Examples:

- `followup_resolver.py`
- `query_state_merger.py`
- `user_preference_store.py`
- `long_term_memory.py`

Good for:

- deterministic rules
- edge cases
- scoring / merge logic

### 2. Endpoint / Integration Tests

Purpose:

- verify the FastAPI entry path and orchestration logic

Examples:

- [tests/test_app_endpoints.py](../tests/test_app_endpoints.py)
- [tests/test_session_manager.py](../tests/test_session_manager.py)
- [tests/test_task_manager.py](../tests/test_task_manager.py)

Good for:

- confirmation flow
- session/task persistence
- explain payloads
- follow-up patch execution

### 3. End-to-End Eval Harness

Purpose:

- verify full query behavior from request to final agent outcome
- express realistic multi-turn scenarios in data rather than one test function at a time

Main files:

- [tests/evals/nl2sql_cases.yaml](../tests/evals/nl2sql_cases.yaml)
- [tests/test_end_to_end_evals.py](../tests/test_end_to_end_evals.py)

This harness is intentionally closer to “golden cases” than pure unit testing.

## Current E2E Eval Format

Each case is YAML-driven and may include:

- `setup`
- one or more `steps`
- expected status, turn mode, and resolved-intent fields

Example shape:

```yaml
cases:
  - name: followup_confirmation_flow
    setup:
      last_query_state:
        project_id: 55
        tables: ["orders"]
        metrics: ["revenue"]
        time_range:
          type: last_n_days
          n: 7
        group_by: ["orders.channel"]
        filters: []
        turn_type: new_query
    steps:
      - text: Compare with the products table
        project_id: 55
        extraction:
          table_extractions: ["products"]
        resolver: low_confidence_table
        expect:
          status: needs_confirmation
          turn_mode: followup_patch
          candidates_contains: tables
      - text: "1"
        project_id: 55
        expect:
          status: success
          turn_mode: confirmation
          resolved_intent:
            tables: ["products"]
            metrics: ["revenue"]
```

## What the Harness Can Simulate Today

The current runner supports:

- pre-seeded `last_query_state`
- mocked extraction output (`SQLIntentJson` fragments)
- mocked resolver scenarios (high-confidence / low-confidence table)
- mocked SQL generation (the harness pins the deterministic core:
  matchers, thresholds, state merging, confirmation flow, persistence)
- multi-step session continuity
- `project_memory` setup
- `restart_before: true` for restart simulation
- assertions on:
  - `status`
  - `turn_mode`
  - `resolved_intent.tables`
  - `resolved_intent.metrics`
  - `resolved_intent.group_by`
  - `resolved_intent.time_range.n`
  - `resolved_intent.window` (grouped ranking)
  - `resolved_intent.filters`
  - join inference into the SQL-generation intent (`sql_intent_contains_join`)
  - candidate presence

## Current Covered Scenarios

The current end-to-end cases cover:

- basic new query (explicit table; table inferred from a metric)
- follow-up change time
- follow-up change metric
- follow-up add grouped-ranking window (top-N per group)
- join inference (column on another table -> join from schema config)
- follow-up confirmation flow
- project memory context injection
- confirmation after restart

This means the harness already protects the most important “agent-like” paths.

## Why YAML-Driven Evals Help

Without a case file, every new scenario becomes another hand-written test function.

With YAML-driven evals:

- adding a case is cheap
- reviewing scenario coverage is easier
- product/behavior changes are easier to discuss in data form
- future replay against real logs becomes more natural

This is especially useful for turn-based systems, where the correctness lives in the sequence, not just one isolated function call.

## Recommended Next Eval Buckets

### 1. More Turn-Based Cases

Examples:

- "Not this event; change it to payment success"
- "Use country breakdown instead"
- "Compare with yesterday"
- "Continue with UV"

### 2. Memory Cases

Examples:

- project memory changes default event mapping
- project memory changes default region behavior
- conflicting memory pieces and relevance selection

### 3. User Preference Cases

Examples:

- preference rerank changes candidate order
- preference remains scoped by `project_id + user_id`
- preference does not override obviously better semantic matches

### 4. Restart / Recovery Cases

Examples:

- follow-up after restart
- confirmation after restart
- session restored but no pending task

## What E2E Eval Is Not Meant To Do

The current harness is not meant to:

- verify real LLM quality online
- verify actual downstream query correctness against production databases
- replace matcher unit tests

It is mainly a regression harness for agent behavior and orchestration.

## Running Evals

Run only the end-to-end harness:

```bash
./.venv311/bin/pytest -q tests/test_end_to_end_evals.py
```

Run it together with the main turn-based suite:

```bash
./.venv311/bin/pytest -q \
  tests/test_end_to_end_evals.py \
  tests/test_app_endpoints.py \
  tests/test_followup_resolver.py \
  tests/test_query_state_merger.py \
  tests/test_session_manager.py \
  tests/test_task_manager.py
```

## Expansion Guidelines

When adding a new eval case:

1. Prefer YAML when the new behavior is mostly a scenario, not a new algorithm.
2. Add unit tests as well if you introduce new pure logic.
3. Keep the expected assertion focused on stable fields.
4. Avoid asserting full `exec_dsl` strings unless necessary.
5. Prefer semantic assertions over surface formatting assertions.

## Long-Term Direction

The long-term goal is to evolve this from a small YAML harness into a richer replay-and-regression layer:

- more golden cases from real query logs
- category labels for cases
- optional offline scoring reports
- comparison between branches or model settings

But even the current lightweight harness already gives strong protection for the project’s most important turn-based and memory-heavy paths.
