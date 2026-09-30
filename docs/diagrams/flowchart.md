# End-To-End Flowchart

```mermaid
flowchart TD
    Start(("User sends query")) --> Channel{"Channel?"}

    Channel -->|Telegram| Poll["TelegramGateway<br/>Long polling"]
    Poll --> Adapt["TelegramAdapter<br/>Convert to StandardMessage"]
    Adapt --> Clean["MessageCleaner<br/>Clean text"]
    Clean --> Dup{"Duplicate?"}
    Dup -->|Yes| DropDup(("Discard"))
    Dup -->|No| Empty{"Empty after cleaning?"}
    Empty -->|Yes| DropEmpty(("Discard"))
    Empty -->|No| Session

    Channel -->|HTTP| HTTP["FastAPI POST /nl2sql"]
    HTTP --> Session

    Session["SessionManager<br/>create_or_get"]
    Session --> Context["Enhanced Context<br/>Session + Project Memory"]
    Context --> Extract

    subgraph Extract["Layer 1: LLM Intent Extraction"]
        Prompt["Build context-aware prompt"]
        LLM["Call LLM"]
        Parsed["Parse SQLIntentJson"]
        Prompt --> LLM --> Parsed
    end

    Parsed --> Followup["Follow-up Detection"]
    Followup --> Turn{"Turn mode?"}
    Turn -->|new_query| Resolve
    Turn -->|followup_patch| Patch["Extract patch"]
    Patch --> Merge["Merge with last_query_state"]
    Merge --> Resolve
    Turn -->|confirmation| Confirm["Restore pending task"]
    Confirm --> Resolve

    subgraph Resolve["Layer 2: Matcher Resolution"]
        Table["TableMatcher"]
        Metric["SQLMetricMatcher"]
        Column["ColumnMatcher"]
        Time["TimeMatcher"]
        Infer["Table inference +<br/>join inference"]
        Table --> Metric --> Column --> Time --> Infer
    end

    Infer --> Ambiguous{"Needs confirmation?"}
    Ambiguous -->|Yes| Task["Create TaskContext<br/>Return candidates"]
    Task --> EndConfirm(("Wait for user reply"))
    Ambiguous -->|No| GenSQL

    subgraph GenSQL["Layer 3-4: SQL Generation And Validation"]
        Intent["Assemble grounded intent<br/>tables / metric exprs / joins / time expr"]
        Generate["LLM generates ClickHouse SQL"]
        Validate["sqlglot validate<br/>readonly / whitelist / auto LIMIT"]
        Repair["Repair loop with error feedback"]
        Intent --> Generate --> Validate
        Validate -->|invalid| Repair --> Generate
    end

    Validate --> Persist["Persist session state<br/>Record preferences"]
    Persist --> Learn["Async MemoryWriter"]
    Persist --> Caller{"Caller?"}
    Caller -->|Telegram| Format["Format SQL + intent"]
    Format --> Send["Send Telegram response"]
    Send --> EndTG(("End"))
    Caller -->|HTTP| Return["Return NL2SQLResponse"]
    Return --> EndHTTP(("End"))
```
