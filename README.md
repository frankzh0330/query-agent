# query-agent

NL2DSL Agent - 自然语言到语义DSL的转换代理。

## 功能

- 🌐 支持中英文自然语言查询
- 🎯 智能语义解析（LLM + Embedding）
- 📊 数据查询DSL生成
- 🔄 支持 metric/event/dimension/time_range 解析

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
uvicorn app:app --reload --port 8000
```

## API 使用

```bash
curl -X POST "http://localhost:8000/nl2dsl" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "查询德国近7天 app_launch 的 PV",
    "project_id": 55
  }'
```

## 架构

```
用户输入
    ↓
Layer 1: LLM 提取 (GLM-4.6)
    ↓
Layer 2: Resolver 解析 (metric/event/groupby/time)
    ↓
Layer 3: SemanticDSL 生成
    ↓
Layer 4: 执行 DSL 渲染
```

## 项目结构

```
query-agent/
├── app.py                  # FastAPI 入口
├── service/                # 服务层
│   └── llm_extractions.py  # LLM 提取
├── resolver/               # 解析层
│   ├── metric_resolver.py
│   ├── event_resolver.py
│   ├── groupby_resolver.py
│   └── time_resolver.py
├── dsl/                    # DSL 层
│   ├── semantic_models.py
│   └── renderer.py
└── catalog/                # 配置文件 (YAML)
    ├── metrics.yaml
    ├── events.yaml
    └── dimensions.yaml
```
