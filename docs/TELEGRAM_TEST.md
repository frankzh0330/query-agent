# Telegram Testing Guide

[English](TELEGRAM_TEST.md) | [Chinese](TELEGRAM_TEST.zh-CN.md)

## Start The Service

### 1. Set Environment Variables

```bash
# Required: Telegram Bot token
export TELEGRAM_BOT_TOKEN="your_bot_token_here"

# LLM configuration
export LLM_BACKEND="zhipu"  # or "ollama"
export ZHIPU_API_KEY="your_zhipu_key"  # when using zhipu
export OLLAMA_BASE_URL="http://localhost:11434"  # when using ollama
```

### 2. Run

```bash
python server.py
```

## Test Flow

1. Send a message to the Telegram bot, for example:

- `近7天各地区的销售额`
- `改成只看VIP用户`
- `每个地区前3`

2. The bot should return the generated SQL and resolved intent:

```text
📄 已生成 ClickHouse SQL
表: orders
指标: revenue
分组: users.region
时间: last_n_days n=7

SELECT users.region AS region, sum(orders.amount) AS revenue
FROM orders JOIN users ON orders.user_id = users.id
WHERE orders.created_at >= now() - INTERVAL 7 DAY
GROUP BY users.region ORDER BY revenue DESC LIMIT 100
```

Note: query execution and result rendering are intentionally out of scope for
this demo — the bot returns the validated SQL only (see Production Notes in the README).

3. If a table or metric is ambiguous, the bot will ask for confirmation:

```text
请选择表:
  1. products (匹配度 55%)
  2. orders (匹配度 48%)
回复编号或名称即可
```

Reply `1` (or the name) to continue; the pending confirmation survives restarts.

## Debugging

### API Docs

```text
http://localhost:8000/docs
```

### List Sessions

```bash
curl http://localhost:8000/sessions
```

### Test The NL2SQL API Directly

```bash
curl -X POST http://localhost:8000/nl2sql \
  -H "Content-Type: application/json" \
  -d '{"text": "近7天各地区的销售额", "project_id": 55}'
```

## Troubleshooting

1. Telegram bot does not respond.

- Check whether `TELEGRAM_BOT_TOKEN` is correct.
- Check service logs for errors.

2. LLM calls fail.

- Check `LLM_BACKEND`.
- If using zhipu, check `ZHIPU_API_KEY`.

3. Memory files are not visible.

- Runtime memory is stored under the configured `data/` directory.
- Check `data/memory/`, `data/sessions/`, and `data/user_preferences/`.
