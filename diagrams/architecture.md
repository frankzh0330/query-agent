graph TB
    subgraph Client["Client Layer"]
        TGUser["Telegram User"]
        HTTPClient["HTTP Client"]
    end

    subgraph Gateway["Gateway Layer"]
        TGGw["TelegramGateway<br/>Long Polling"]
        GwMgr["GatewayManager<br/>多渠道管理"]
        BaseGw["BaseGateway"]
    end

    subgraph Ingress["Ingress Layer"]
        TgAdapter["TelegramAdapter"]
        Cleaner["MessageCleaner<br/>文本清洗"]
        Dedup["MessageDeduplicator<br/>消息去重"]
        StdMsg["StandardMessage<br/>统一消息模型"]
    end

    subgraph API["API Layer"]
        FastAPI["FastAPI App<br/>app.py"]
        Models["NL2DSLRequest<br/>NL2DSLResponse"]
        TgNotify["Telegram Notification<br/>辅助通知"]
    end

    subgraph Service["Service Layer"]
        LLM["LLM Extractions<br/>LangChain / DSPy"]
        SessionMgr["SessionManager<br/>会话管理"]
        BearerSvc["BearerService<br/>查询执行 Mock"]
        CatSync["CatalogSync<br/>目录同步"]
        CatSched["CatalogScheduler<br/>定时调度"]
    end

    subgraph Memory["Memory Layer"]
        LTM["LongTermMemory<br/>长期记忆"]
        MemFile["MemoryFile<br/>文件持久化"]
        Profile["UserProfile<br/>用户画像"]
        Prefs["UserPreferences<br/>用户偏好"]
        EntityMem["EntityMemory<br/>实体记忆"]
    end

    subgraph Matcher["Matcher Layer"]
        MatcherSvc["MatcherService<br/>统一匹配服务"]
        EventM["EventMatcher"]
        MetricM["MetricMatcher"]
        DimM["DimensionMatcher"]
        TimeM["TimeMatcher"]
    end

    subgraph DSL["DSL Layer"]
        Semantic["SemanticModels<br/>语义DSL"]
        Renderer["Renderer<br/>DSL渲染器"]
        Validator["Validators<br/>验证器"]
    end

    subgraph Catalog["Catalog Layer"]
        CatMetric["metrics.yaml"]
        CatEvent["events.yaml"]
        CatDim["dimensions.yaml"]
        CatRegion["region_groups.yaml"]
        CatLoader["Catalog Loader"]
    end

    subgraph External["External Services"]
        TgAPI["Telegram Bot API"]
        LLMAPI["Zhipu AI / Ollama<br/>LLM Backend"]
        BearerAPI["Bearer Query Engine<br/>目标服务"]
        CatAPI["Catalog API<br/>外部目录源"]
    end

    TGUser -->|发送消息| TgAPI
    TgAPI -->|Long Polling| TGGw
    HTTPClient -->|HTTP POST /nl2dsl| FastAPI

    GwMgr -->|"register &amp; manage"| TGGw
    TGGw -.->|extends| BaseGw

    TGGw -->|raw update| TgAdapter
    TgAdapter -->|clean text| Cleaner
    TgAdapter -->|is duplicate| Dedup
    TgAdapter -->|produce| StdMsg

    TGGw -->|POST /nl2dsl| FastAPI

    FastAPI -->|create_or_get| SessionMgr
    FastAPI -->|get_enhanced_context| SessionMgr
    FastAPI -->|add_message| SessionMgr
    FastAPI -->|extract_llm| LLM
    FastAPI -->|execute_query| BearerSvc
    FastAPI -->|notify| TgNotify

    FastAPI -->|build SemanticDSL| Semantic
    FastAPI -->|render_exec_dsl| Renderer
    FastAPI -->|validate| Validator

    FastAPI -->|resolve_from_extractions| MatcherSvc
    MatcherSvc -->|match event| EventM
    MatcherSvc -->|match metric| MetricM
    MatcherSvc -->|match dimension| DimM
    MatcherSvc -->|resolve time| TimeM

    EventM -->|lookup aliases| CatEvent
    MetricM -->|lookup aliases| CatMetric
    DimM -->|lookup| CatDim
    CatLoader --> CatMetric
    CatLoader --> CatEvent
    CatLoader --> CatDim
    CatLoader --> CatRegion

    SessionMgr -->|get_enhanced_context| LTM
    SessionMgr -->|record_entity_usage| LTM
    LTM -->|get_or_create_profile| Profile
    LTM -->|get_preferences| Prefs
    LTM -->|learn_entity_usage| EntityMem
    LTM -->|read/write| MemFile

    LLM -->|调用 LLM| LLMAPI
    BearerSvc -->|POST query| BearerAPI
    CatSync -->|同步目录| CatAPI
    CatSched -->|定时触发| CatSync

    FastAPI -->|result JSON| TGGw
    TGGw -->|sendMessage| TgAPI
    TgAPI -->|查询结果| TGUser
