# NL2DSL Agent 完整架构图 (当前 + 优化方案)

## 整体架构图

```mermaid
graph TB
    subgraph Client["客户端"]
        U["用户"]
    end

    subgraph API["API Layer (FastAPI)"]
        A["POST /nl2dsl<br/>{text, user_id, project_id, session_id}"]
    end

    subgraph History["📊 用户历史知识库 (优化方案)"]
        H1["UserHistoryService<br/>user_history.py"]
        H2["load_user_context<br/>(加载用户上下文)"]
        H3["save_query<br/>(异步保存历史)"]
        H4["get_user_patterns<br/>(获取用户模式)"]
        H5["get_user_aliases<br/>(获取用户别名)"]
    end

    subgraph Storage["💾 数据存储"]
        DB1[(PostgreSQL<br/>查询历史/用户配置)]
        DB2[(Redis<br/>用户偏好缓存)]
        DB3[(Vector DB<br/>可选/语义检索)]
    end

    subgraph L1["Layer 1: LLM 提取"]
        B["extract_extractions_llm<br/>(GLM-4.6)"]
        P1["EXTRACTIONS_PROMPT<br/>+ FEW_SHOT_EXAMPLES"]
        P2["🎯 个性化 Few-shot<br/>(优化方案)"]
        P3["用户上下文注入<br/>(常用术语/默认值)"]
    end

    subgraph L2["Layer 2: Resolver 解析"]
        C1["resolve_metric_id<br/>用户别名优先 🎯"]
        C2["resolve_event_name<br/>历史权重调整 🎯"]
        C3["resolve_group_by<br/>用户别名优先 🎯"]
        C4["resolve_last_n_days<br/>用户默认值 🎯"]
    end

    subgraph Catalog["Catalog 配置 (YAML)"]
        CFG1["metrics.yaml<br/>metric_alias_lookup"]
        CFG2["events.yaml<br/>event_alias_lookup"]
        CFG3["dimensions.yaml<br/>dimension_alias_lookup"]
    end

    subgraph UserCatalog["📝 用户配置 (优化方案)"]
        UCFG1["用户自定义别名<br/>UserAlias"]
        UCFG2["用户偏好设置<br/>UserPreferences"]
        UCFG3["用户查询模式<br/>UserPattern"]
    end

    subgraph Embedding["🔮 Embedding 匹配"]
        E1["EventMatcher<br/>sentence-transformers"]
        E2["DimensionMatcher<br/>sentence-transformers"]
    end

    subgraph L3["Layer 3: DSL 生成"]
        D["SemanticDSL<br/>(语义DSL)"]
    end

    subgraph L4["Layer 4: 执行 DSL 渲染"]
        F["render_exec_dsl<br/>(Finder/CNCH格式)"]
    end

    subgraph Output["返回结果"]
        R["NL2DSLResponse<br/>{extraction, semantic,<br/>exec_dsl, explain}"]
    end

    subgraph Async["⚡ 异步处理 (优化方案)"]
        AS1["后台聚合统计<br/>更新 UserPattern"]
        AS2["别名学习<br/>自动提取新别名"]
    end

    U --> A
    A --> H1

    H1 --> DB2
    H1 --> H2
    H1 --> H4
    H1 --> H5

    H2 --> P3
    P3 --> B
    P4 -.动态生成.-> P2
    P1 --> B
    P2 --> B

    H5 --> UCFG1
    H5 --> UCFG2
    H5 --> UCFG3

    B --> C1
    B --> C2
    B --> C3
    B --> C4

    CFG1 --> C1
    CFG2 --> C2
    CFG3 --> C3

    UCFG1 -.优先级.-> C1
    UCFG1 -.优先级.-> C3
    UCFG2 -.默认值.-> C4
    UCFG3 -.权重.-> C2

    C2 --> E1
    C3 --> E2

    C1 --> D
    C2 --> D
    C3 --> D
    C4 --> D

    D --> F
    D --> H3
    H3 --> DB1

    D --> R
    F --> R
    R --> U

    H3 --> AS1
    AS1 --> DB1
    AS1 --> AS2
    AS2 --> UCFG1

    style H1 fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
    style H2 fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
    style H3 fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
    style P2 fill:#fff9c4,stroke:#f57f17,stroke-width:2px
    style P3 fill:#fff9c4,stroke:#f57f17,stroke-width:2px
    style UCFG1 fill:#e1bee7,stroke:#6a1b9a,stroke-width:1px
    style UCFG2 fill:#e1bee7,stroke:#6a1b9a,stroke-width:1px
    style UCFG3 fill:#e1bee7,stroke:#6a1b9a,stroke-width:1px
    style DB1 fill:#ffccbc,stroke:#bf360c,stroke-width:1px
    style DB2 fill:#ffccbc,stroke:#bf360c,stroke-width:1px
    style DB3 fill:#bdbdbd,stroke:#424242,stroke-width:1px,dash:3
    style AS1 fill:#b3e5fc,stroke:#01579b,stroke-width:2px,dash:3
    style AS2 fill:#b3e5fc,stroke:#01579b,stroke-width:2px,dash:3
```

---

## 完整时序图 (当前 + 优化方案)

```mermaid
sequenceDiagram
    autonumber
    actor User as 用户
    participant API as FastAPI<br/>app.py
    participant HS as UserHistoryService<br/>(优化方案)
    participant Cache as Redis<br/>缓存
    participant DB as PostgreSQL<br/>历史存储
    participant LLM as LLM Service<br/>llm_extractions.py
    participant RES as Resolver Layer<br/>resolver/
    participant CAT as Catalog<br/>catalog_loader.py
    participant EMB as Embedding Matcher<br/>Event/Dimension Matcher
    participant DSL as DSL Layer<br/>dsl/
    participant ASYNC as 异步处理<br/>(优化方案)

    User->>API: POST /nl2dsl<br/>{text, user_id, project_id}

    rect rgb(200, 255, 200)
        Note over API,DB: 📊 用户历史知识库 (优化方案)
        API->>HS: load_user_context(user_id)

        par 并行加载
            HS->>Cache: 查询用户偏好
        and
            HS->>DB: 查询用户历史
        end

        alt 缓存命中
            Cache-->>HS: UserContext
        else 缓存未命中
            DB-->>HS: UserContext
            HS->>Cache: 写入缓存
        end

        HS-->>API: UserContext<br/>{preferences, patterns, aliases}
    end

    rect rgb(255, 255, 200)
        Note over API,LLM: 🎯 Layer 1: 个性化 LLM 提取
        API->>HS: get_user_patterns(user_id)
        HS-->>API: UserPattern

        API->>LLM: extract_extractions_llm(text, user_context)

        par 并行处理
            LLM->>LLM: 生成个性化 Few-shot 示例
        and
            LLM->>LLM: 注入用户常用术语
        end

        LLM-->>API: ExtractionsJson
    end

    rect rgb(200, 200, 255)
        Note over API,EMB: Layer 2: Resolver 解析 (用户优先)
        API->>HS: get_user_aliases(user_id)
        HS-->>API: UserAliases

        par 并行解析
            API->>RES: resolve_metric_id(catalog, extraction, user_aliases)
            RES->>CAT: 用户别名优先
            alt 用户别名匹配
                CAT-->>RES: 用户定义的 metric_id
            else 全局别名
                CAT-->>RES: 全局 metric_id
            end
        and
            API->>RES: resolve_event_name(catalog, extraction, user_patterns)
            RES->>CAT: event_alias_lookup
            CAT-->>RES: event_id
            alt 未匹配
                RES->>EMB: EventMatcher.match()
                EMB-->>RES: event_id + score
            end
        and
            API->>RES: resolve_group_by(catalog, extraction, user_aliases)
            RES->>CAT: 用户别名优先
            CAT-->>RES: dimension_id
            alt 未匹配
                RES->>EMB: DimensionMatcher.match()
                EMB-->>RES: dimension_id + score
            end
        and
            API->>RES: resolve_last_n_days(extraction, user_preferences)
            alt 用户有默认值
                RES-->>API: 用户默认 n_days
            else 正则匹配
                RES-->>API: 匹配的 n_days
            end
        end

        RES-->>API: resolved results
    end

    rect rgb(255, 200, 200)
        Note over API: Layer 3-4: DSL 生成与渲染
        API->>DSL: SemanticDSL(...)
        DSL-->>API: SemanticDSL
        API->>DSL: render_exec_dsl(semantic, catalog)
        DSL-->>API: exec_dsl
    end

    rect rgb(200, 255, 255)
        Note over API,ASYNC: ⚡ 异步保存 (优化方案)
        API->>HS: save_query(record)
        HS->>DB: INSERT query_history

        par 后台异步处理
            DB->>ASYNC: 触发聚合
            ASYNC->>DB: 更新 UserPattern
        and
            ASYNC->>ASYNC: 别名学习
            ASYNC->>DB: 发现新别名
        end
    end

    API-->>User: NL2DSLResponse

    style HS fill:#c8e6c9,stroke:#2e7d32,stroke-width:2px
    style ASYNC fill:#b3e5fc,stroke:#01579b,stroke-width:2px,dash:3
```

---

## 数据模型完整图 (当前 + 优化方案)

```mermaid
classDiagram
    class Extraction {
        +str text
    }

    class ExtractionsJson {
        +List~Extraction~ metric_extractions
        +List~Extraction~ time_extractions
        +List~Extraction~ event_extractions
        +List~str~ region_filter
        +List~Extraction~ group_by_extractions
    }

    class SemanticDSL {
        +str dsl_version
        +str registry_version
        +str timezone
        +int project_id
        +List~str~ region_filter
        +Metric metric
        +Event event
        +TimeRange time_range
        +List~GroupBy~ group_by
        +List~Filter~ filters
    }

    class Metric {
        +str metric_id
    }

    class Event {
        +str event_name
    }

    class TimeRange {
        +str type
        +int n
    }

    class GroupBy {
        +str dimension_id
    }

    class Filter {
        +str property_name
        +str op
        +Any value
    }

    class Catalog {
        +Dict metrics
        +Dict events
        +Dict dimensions
        +Dict metric_alias_lookup
        +Dict event_alias_lookup
        +Dict dimension_alias_lookup
    }

    %% 优化方案 - 用户历史知识库
    class User {
        +str user_id
        +List~int~ project_ids
        +UserPreferences preferences
        +datetime created_at
        +datetime last_active
    }

    class UserPreferences {
        +str default_metric
        +List~str~ default_region
        +str default_event
        +int default_time_range
    }

    class QueryHistory {
        +str history_id
        +str user_id
        +int project_id
        +str query_text
        +ExtractionsJson extraction
        +ResolvedResult resolved
        +SemanticDSL dsl
        +datetime timestamp
        +bool success
        +int latency_ms
    }

    class UserPattern {
        +str user_id
        +List~Tuple~ most_common_metrics
        +List~Tuple~ most_common_events
        +List~Tuple~ most_common_regions
        +List~Tuple~ most_common_groups
        +Dict query_frequency
        +float avg_time_range
    }

    class UserAlias {
        +str user_id
        +str alias_type
        +str standard_id
        +str custom_alias
        +int usage_count
        +datetime created_at
    }

    class UserContext {
        +User user
        +UserPattern pattern
        +List~UserAlias~ aliases
        +UserPreferences preferences
    }

    %% 当前架构关系
    ExtractionsJson "1" --> "n" Extraction
    SemanticDSL "1" --> "1" Metric
    SemanticDSL "1" --> "1" Event
    SemanticDSL "1" --> "1" TimeRange
    SemanticDSL "1" --> "n" GroupBy
    SemanticDSL "1" --> "n" Filter

    %% 优化方案关系
    User "1" --> "1" UserPreferences
    User "1" --> "n" QueryHistory
    User "1" --> "1" UserPattern
    User "1" --> "n" UserAlias
    UserContext "1" --> "1" User
    UserContext "1" --> "1" UserPattern
    UserContext "1" --> "n" UserAlias
    QueryHistory "1" --> "1" ExtractionsJson
    QueryHistory "1" --> "1" SemanticDSL
```

---

## 分层架构对比图

```mermaid
graph TB
    subgraph CURRENT["当前架构"]
        direction TB
        C_L1["Layer 1: LLM 提取<br/>静态 Few-shot 示例"]
        C_L2["Layer 2: Resolver<br/>固定别名优先级"]
        C_L3["Layer 3: DSL 生成"]
        C_L4["Layer 4: 渲染"]
        C_L1 --> C_L2 --> C_L3 --> C_L4
    end

    subgraph ENHANCED["优化后架构"]
        direction TB
        E_L1["Layer 1: LLM 提取<br/>🎯 个性化 Few-shot<br/>🎯 用户上下文注入"]
        E_L2["Layer 2: Resolver<br/>🎯 用户别名优先<br/>🎯 历史权重调整"]
        E_L3["Layer 3: DSL 生成"]
        E_L4["Layer 4: 渲染"]
        E_Async["⚡ 异步历史保存<br/>⚡ 用户模式学习"]
        E_L1 --> E_L2 --> E_L3 --> E_L4
        E_L4 -.异步.-> E_Async
    end

    subgraph ADDITIONS["新增组件"]
        direction TB
        A1["📊 UserHistoryService"]
        A2["💾 PostgreSQL + Redis"]
        A3["🔮 Vector DB (可选)"]
    end

    ADDITIONS -.用户上下文.-> E_L1
    ADDITIONS -.用户别名/权重.-> E_L2
    E_Async -.保存.-> A2

    style C_L1 fill:#e0e0e0,stroke:#424242
    style E_L1 fill:#c8e6c9,stroke:#2e7d32,stroke-width:2px
    style E_L2 fill:#c8e6c9,stroke:#2e7d32,stroke-width:2px
    style E_Async fill:#b3e5fc,stroke:#01579b,stroke-width:2px,dash:3
    style A1 fill:#fff9c4,stroke:#f57f17,stroke-width:2px
    style A2 fill:#ffccbc,stroke:#bf360c,stroke-width:2px
    style A3 fill:#bdbdbd,stroke:#424242,stroke-width:1px,dash:3
```

---

## 决策流程图 (加入用户历史)

```mermaid
flowchart TD
    Start([用户查询]) --> Input{有 user_id?}

    Input -->|否| NoUser[使用全局配置]
    Input -->|是| LoadCtx[📊 加载用户上下文]

    NoUser --> StaticPrompt[使用静态 Few-shot]
    LoadCtx --> CacheHit{缓存命中?}
    CacheHit -->|是| UseCache[使用缓存上下文]
    CacheHit -->|否| LoadDB[从数据库加载]
    LoadDB --> UseCache

    UseCache --> DynamicPrompt[🎯 生成个性化 Few-shot]
    DynamicPrompt --> LLMExtract[LLM 提取]

    StaticPrompt --> LLMExtract

    LLMExtract --> Extract{提取成功?}
    Extract -->|否| Fallback[使用默认值]
    Extract -->|是| HasUser{有用户别名?}

    Fallback --> GenDSL[生成 DSL]

    HasUser -->|否| GlobalAlias[使用全局别名]
    HasUser -->|是| UserAlias[🎯 用户别名优先]

    UserAlias --> Match{匹配成功?}
    Match -->|是| HighWeight[高权重匹配]
    Match -->|否| HasPattern{有历史权重?}

    HasPattern -->|是| PatternWeight[🎯 历史权重调整]
    HasPattern -->|否| Embedding[🔮 Embedding 匹配]

    GlobalAlias --> AliasMatch{匹配成功?}
    AliasMatch -->|是| StdWeight[标准权重]
    AliasMatch -->|否| FallbackAlias[使用默认值]

    HighWeight --> GenDSL
    PatternWeight --> GenDSL
    Embedding --> GenDSL
    StdWeight --> GenDSL
    FallbackAlias --> GenDSL

    GenDSL --> SaveHistory[⚡ 异步保存历史]
    SaveHistory --> UpdatePattern[📊 更新用户模式]
    UpdatePattern --> LearnAlias[🎓 自动学习别名]
    LearnAlias --> End([返回结果])

    style LoadCtx fill:#c8e6c9,stroke:#2e7d32,stroke-width:2px
    style UserAlias fill:#c8e6c9,stroke:#2e7d32,stroke-width:2px
    style DynamicPrompt fill:#c8e6c9,stroke:#2e7d32,stroke-width:2px
    style PatternWeight fill:#c8e6c9,stroke:#2e7d32,stroke-width:2px
    style SaveHistory fill:#b3e5fc,stroke:#01579b,stroke-width:2px,dash:3
    style UpdatePattern fill:#b3e5fc,stroke:#01579b,stroke-width:2px,dash:3
    style LearnAlias fill:#b3e5fc,stroke:#01579b,stroke-width:2px,dash:3
```

---

## 图例说明

| 图标/颜色 | 含义 |
|----------|------|
| 📊 绿色 | 用户历史知识库服务 |
| 🎯 黄色 | 个性化优化点 |
| 💾 橙色 | 数据存储 |
| 🔮 紫色 | Embedding 匹配 |
| ⚡ 蓝色虚线 | 异步处理 |
| 🎓 学习 | 自动别名学习 |
| 📝 紫色 | 用户配置 |
