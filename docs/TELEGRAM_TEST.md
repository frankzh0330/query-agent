# Telegram Testing Guide

[English](TELEGRAM_TEST.md) | [Chinese](TELEGRAM_TEST.zh-CN.md)

## Start The Service

### 1. Set Environment Variables

```bash
# Required: Telegram Bot token
export TELEGRAM_BOT_TOKEN="your_bot_token_here"

# Enable mock mode when the Bearer API is not available
export BEARER_MOCK="true"

# LLM configuration
export LLM_BACKEND="zhipu"  # or "ollama"
export ZHIPU_API_KEY="your_zhipu_key"  # when using zhipu
export OLLAMA_BASE_URL="http://localhost:11434"  # when using ollama

# Optional: API service URL
export API_BASE_URL="http://localhost:8000"
```

### 2. Run

```bash
python server.py
```

## Test Flow

1. Send a message to the Telegram bot, for example:

- `PV in Germany`
- `UV in France`
- `app_launch in California over the last 7 days`

2. The bot should return a formatted query result:

```text
Query Result
Region: EUTTP
Metric: pv
Event: app_launch
Time: last 7 days

Data Result
2024-03-24: pv=12345, region=EUTTP
2024-03-23: pv=11234, region=EUTTP
2024-03-22: pv=10234, region=EUTTP
...
```

## Mock Data

In mock mode, the service:

- extracts `region / metric / event` from `exec_dsl`
- returns three example rows for recent days
- includes date and metric value in each row

## Debugging

### API Docs

```text
http://localhost:8000/docs
```

### List Sessions

```bash
curl http://localhost:8000/sessions
```

### Test The NL2DSL API Directly

```bash
curl -X POST http://localhost:8000/nl2dsl \
  -H "Content-Type: application/json" \
  -d '{"text": "PV in Germany", "project_id": 55}'
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
- Check `data/memory/`, `data/session/`, and `data/user_preferences/`.
