# Google ADK NL2SQL: A Study Comparison

This document studies how Google builds natural-language-to-SQL agents with
the Agent Development Kit, and compares that design against the one already
implemented in this repository.

It exists for learning. Nothing in the running service depends on it. The
companion code skeleton lives in [examples/adk_nl2sql/](../examples/adk_nl2sql/).

Primary sources:

- [NL2SQL Pipeline and Database Integration — google/adk-samples](https://deepwiki.com/google/adk-samples/6.2-nl2sql-pipeline-and-database-integration)
- [Agent Development Kit — NL2SQL with BigQuery](https://medium.com/google-cloud/agent-development-kit-nl2sql-with-bigquery-bd676dfd666b)
- [BigQuery tool for ADK](https://adk.dev/integrations/bigquery/)
- [ADK evaluation criteria](https://google.github.io/adk-docs/evaluate/criteria/)

## The headline

Google's public position is **not** "prompt a model to write SQL". Both
reference implementations put a governed layer between the question and the
query: a semantic catalog, an explicit understanding stage, a review pass, and
a capability-restricted execution tool.

That is the same bet this repository makes. The interesting work is therefore
not in adopting their architecture but in being able to say precisely where the
two designs diverge and why.

## What Google actually ships

| Layer | Component | Role |
|---|---|---|
| Agent framework | ADK | Multi-agent composition, session state, eval, deploy |
| Tools | `BigQueryToolset` | `execute_sql`, `ask_data_insights`, `search_catalog`, `forecast` |
| Semantic layer | Looker / LookML, Conversational Analytics API | Business vocabulary as the source of truth |
| Metadata | Dataplex (via `search_catalog`) | Enterprise catalog and taxonomy |
| Sandbox | Agent Engine Code Execution, `VertexAiCodeExecutor`, `GkeCodeExecutor` | Runs model-written Python |
| Eval | `adk eval` with `*.evalset.json` | Trajectory and response scoring |

## The two reference pipelines

### adk-samples: BASELINE and CHASE

The official `data-science` sample exposes two translation methods behind an
`NL2SQL_METHOD` environment variable.

**BASELINE** is a single-stage prompt. `bigquery_nl2sql()` retrieves schema and
sample rows via `get_bigquery_schema_and_samples()`, fills a template, and calls
the model at temperature 0.1. Schema is cached in a module-level
`database_settings` dict, populated by `get_database_settings()` and refreshed
by `update_database_settings()`. A hardcoded `MAX_NUM_ROWS = 10000` caps result
size, and the toolset runs with `WriteMode.BLOCKED` and a `tool_filter` of
exactly `["execute_sql"]`.

**CHASE** (Context-aware Hierarchical Adaptive SQL Engineering) trades tokens
for accuracy on hard queries. Its config in `chase_constants.py` includes:

- `generate_sql_type`: `"dc"` (Divide and Conquer — split a complex question
  into sub-problems) or `"qp"` (Query Plan — produce an execution strategy
  before emitting SQL)
- `number_of_candidates`: generate N candidate queries in parallel, then select
- `transpile_to_bigquery`: dialect translation
- `process_input_errors` / `process_tool_output_errors`: error recovery

The lever worth internalising: CHASE costs roughly N times BASELINE. It earns
that on wide multi-join schemas and wastes it on a three-table star schema.
Being able to say *when* you would turn it on is the answer; "CHASE is better"
is not.

### The Medium article: five delegating agents

1. **Orchestrator** — coordinates, uses a before-agent callback to seed state
   (project, dataset, location)
2. **Query Understanding** — identifies the relevant entities and columns;
   exposes `bigquery_metadata`
3. **Query Generation** — writes BigQuery Standard SQL from that output
4. **Query Review / Rewrite** — validates against review criteria, rewrites on
   failure
5. **Query Execution** — runs the approved query, renders markdown

Agents communicate through session state: an agent's `output_key` writes its
final response into state, and `{key}` in a later agent's instruction reads it
back. Stages are coupled by name, not by call graph, which is what makes them
individually replaceable.

## How this maps onto query-agent

| query-agent | ADK equivalent | Same idea? |
|---|---|---|
| `service/llm_extractions.py` | Query Understanding agent | Yes |
| `matcher/*` + metadata store | Dataplex Catalog, `search_catalog` | Yes — both are queryable registries |
| metric semantics (split across catalog + `dsl/renderer.py`) | LookML measures | Partial — see below |
| `dsl/semantic_models.py` (Semantic DSL) | Structured intent between stages | Yes |
| `dsl/renderer.py` (Exec DSL) | Query Generation agent | Yes |
| `dsl/validators.py` | Query Review agent + `WriteMode.BLOCKED` | Yes |
| `service/session_manager.py` | ADK session state | Yes |
| `tests/evals/nl2dsl_cases.yaml` | `*.evalset.json` + `adk eval` | Same purpose, different assertions |
| `service/task_manager.py` confirmation flow | **no ADK equivalent** | This repo goes further |

### Metadata store vs. semantic layer

These are two different things and the ADK material tends to blur them, so it
is worth separating.

A **metadata store** answers "what exists": canonical names, display names,
synonyms, types, ownership. Dataplex Catalog is this, and so is the MySQL
metadata store that backs this project in a real deployment. The checked-in
`catalog/*.yaml` is a sample used to prove the pipeline runs — real metadata
lives in relational tables at a scale YAML cannot serve.

A **semantic layer** answers "what does this mean and how is it computed":
LookML measures carry SQL expressions, join paths, and filters, version
controlled in git and changed through review. That is the part this repo
splits: naming and synonyms live in the catalog, while how PV and UV are
actually computed lives in `dsl/renderer.py`.

The split is defensible here because the metric vocabulary is small and the
aggregation is uniform across events. LookML's design pays for itself when
every metric needs bespoke SQL. Naming the trade explicitly is better than
claiming the two are equivalent.

The storage medium is not the architectural fact. `matcher/base.py` builds an
in-memory inverted index — `token_to_ids`, `exact_alias_map`, per-token posting
caps (`max_posting_per_token`), and a recall ceiling (`max_candidates`).
YAML or MySQL only changes `catalog_loader.py`; the index structure and recall
strategy are what actually determine whether the system scales.

What a relational metadata store does **not** inherit from LookML, and what
therefore has to be built separately:

- **Change governance.** Git plus pull requests give LookML review, history,
  and rollback for free. A metadata table has none of that by default; alias
  changes need an approval path and an audit trail.
- **Atomic index refresh.** The store is the source of truth but the matcher
  needs a materialised index. Rebuild should construct the new index and swap
  the reference atomically, never mutate the live one.
- **Per-tenant footprint.** One global index versus per-`project_id` indexes is
  a real memory trade once event counts reach five figures across many
  projects.

### Where the designs genuinely differ

**1. Deterministic matching vs. a second model call.**

ADK's understanding stage asks a model to pick columns. This repository asks a
model only to extract candidate phrases, then resolves them with RapidFuzz
against a known catalog in `matcher/base.py`.

The defensible principle: use a model where the input space is open-ended, use
code where the answer set is finite and known. A user's phrasing is unbounded;
the set of valid event names is not. Deterministic resolution is also
inspectable — `resolver_explain` can show the score for every candidate, which
no amount of prompt engineering gets you.

**2. Fixed pipeline vs. LLM delegation.**

`SequentialAgent` runs stages in a declared order. An `LlmAgent` root that
delegates at its own discretion is more flexible and less predictable — the
adk-samples issue tracker carries a report of a delegating root skipping its
`initial_bq_nl2sql` tool roughly 30% of the time and writing SQL directly.

When the correct order is known ahead of time, encoding it in the graph rather
than in a prompt removes that failure mode outright. Delegation earns its
unpredictability only when the routing genuinely depends on the input.

**3. Confirmation on low confidence.**

Neither ADK reference implementation has one. This repo's
`matcher/matcher_service.py` bands the decision:

- `score >= 80` — accept silently
- `40 <= score < 80` — ask the user, with candidates
- `score < 40` — do **not** ask; fall back to a default

The bottom band is the non-obvious part. The instinct is that low confidence
should always trigger a question, but a prompt listing five bad candidates
spends the user's attention and returns nothing. Confirmation is worth its
interruption only when the right answer is probably in the list.

**4. Cost control by retrieval.**

Injecting a full catalog stops working somewhere in the thousands of entries.
`memory/long_term_memory.py` already selects relevant snippets rather than
injecting every project memory fragment; the same pressure applies to catalog
context at enterprise scale, and Google answers it with Dataplex semantic
search rather than a bigger prompt.

## Evaluation: two styles

ADK scores two things by default:

- `tool_trajectory_avg_score` — did the agent call the expected tools, in the
  expected way? Match types are `EXACT` (default), `IN_ORDER`, `ANY_ORDER`.
  Default threshold 1.0.
- `response_match_score` — ROUGE-1 against a golden final response. Default
  threshold 0.8. `final_response_match_v2` is the LLM-as-judge variant.

Additional criteria include `hallucinations_v1` and `safety_v1`; some require
the Vertex Gen AI Evaluation Service.

```bash
adk eval --config_file_path examples/adk_nl2sql/eval/test_config.json --print_detailed_results examples/adk_nl2sql examples/adk_nl2sql/eval/nl2sql.evalset.json
```

Trajectory scoring is the idea worth stealing. This repo's
[EVALUATION.md](EVALUATION.md) already argues for asserting on semantic fields
rather than full `exec_dsl` strings — same instinct, applied to output. ADK
applies it to *behaviour*: two runs can produce identical text while one of
them skipped validation entirely, and only trajectory scoring catches that.

Two caveats on the defaults. A 1.0 trajectory threshold means a single harmless
extra tool call fails the case, which practitioners widely report as too
strict. And ROUGE-1 on a final response rewards vocabulary overlap, not
correctness — a fluent wrong answer that reuses the question's nouns can score
well. The evalset in this example lowers `response_match_score` to 0.7 for that
reason, and leans on trajectory for the cases that actually matter (see
`ambiguous_event_should_not_execute` and `write_attempt_must_be_refused`, both
of which assert an *empty* tool trajectory — the agent proving it did not
touch the warehouse).

## Safety notes worth knowing

From the Agent Engine Code Execution docs, since the JD asks about interpreter
sandboxes:

- The sandbox does not impersonate the agent or inherit its permissions
- Sandboxes default to **no network egress**, which is the control that
  actually blocks data exfiltration
- `AgentEngineCodeExecutor` and `VertexAiCodeExecutor` are stateful and suit
  multi-step analysis; `GkeCodeExecutor` is stateless, using an ephemeral pod
  per execution — higher isolation, no carried-over variables
- Data files up to 100MB can be handed to the sandbox directly, so large inputs
  never pass through the model's context window

## Reading order for the skeleton

1. [examples/adk_nl2sql/agent.py](../examples/adk_nl2sql/agent.py) — pipeline
   shape, state handoff, why four stages instead of one
2. [examples/adk_nl2sql/prompts.py](../examples/adk_nl2sql/prompts.py) — one job
   per prompt, schema as data
3. [examples/adk_nl2sql/tools.py](../examples/adk_nl2sql/tools.py) —
   deterministic guardrails, confirmation thresholds
4. [examples/adk_nl2sql/eval/](../examples/adk_nl2sql/eval/) — evalset format,
   negative cases

The skeleton needs `pip install google-adk`, deliberately kept out of
`requirements.txt`. ADK's import paths have shifted between releases; check
[the docs](https://google.github.io/adk-docs/) against your installed version
if an import fails.
