# System Architecture

> All diagrams are Mermaid and render natively on GitHub.

## 1. Complete System Architecture

```mermaid
flowchart TB
    subgraph Sources["Data Sources"]
        GH[Greenhouse Board API]
        AB[Ashby Board API]
        PDF[Resume PDF]
    end

    subgraph ResumePipeline["Resume Intelligence"]
        RP[resume_parser] --> CP[(candidate_profile.json)]
        CP --> PC[parse_confidence]
        PC --> RE[Review & Edit]
        RE --> CP
        CP --> RH[resume_health]
        CP --> SG[skill_gap]
        CP --> CR[career_recommender]
        RH --> SU[suggestions]
        SG --> SU
    end

    subgraph JobPipeline["Job Intelligence"]
        GH --> AC[ats_collector]
        AB --> AC
        AC --> DD[dedupe_jobs]
        DD --> JJ[(jobs.json)]
        JJ --> HG[Hard Gates]
        HG --> SC[Weighted Scorer]
        SC --> EX[Explainer]
        EX --> RJ[(ranked_jobs.json)]
    end

    subgraph AppPipeline["Application Intelligence"]
        RJ --> RT[resume_tailor]
        RT --> LLM[backend.llm provider<br>Groq prod / Ollama dev]
        LLM --> HV[Hallucination Validator]
        HV --> DX[docx generator]
        RJ --> CL[cover_letter]
        CL --> LLM
        RJ --> EV[(PostgreSQL events)]
        EV --> TRK[Tracker]
    end

    subgraph Clients["Clients"]
        L[Streamlit: Landing → Review → Dashboard → Jobs → Tracker]
        API[FastAPI /v1 · /docs]
    end

    PDF --> RP
    CP --> SC
    JJ -. market demand .-> SG
    CR -. target roles .-> SG
```

## 2. Resume Processing Pipeline

```mermaid
flowchart LR
    PDF[PDF] --> T[Text Extraction<br>PyMuPDF blocks]
    PDF --> LK[Link Extraction]
    T --> CL[Text Cleaner]
    CL --> EN[Entity Extractor<br>name/email/phone]
    CL --> SK[Skill Extractor<br>skills_database.csv]
    CL --> ED[Education Extractor]
    CL --> PJ[Project Extractor]
    LK --> LI[LinkedIn / GitHub]
    EN & SK & ED & PJ & LI --> P[(candidate_profile.json)]
    P --> CF[Parse Confidence<br>per-section % + warnings]
    CF --> UI[Review & Edit UI]
    UI -->|user-corrected| P
```

## 3. Job Ranking Pipeline

```mermaid
flowchart TB
    J[(jobs.json)] --> D{Duplicate?<br>company+title}
    D -->|yes| X1[drop]
    D -->|no| G1{Title gate<br>sales/recruiter/PM...}
    G1 -->|fail| X2[drop]
    G1 -->|pass| G2{Years gate<br>required > 3?}
    G2 -->|fail| X3[drop · 54.3% of corpus]
    G2 -->|pass| G3{Clearance gate}
    G3 -->|fail| X4[drop]
    G3 -->|pass| G4{Sponsorship gate}
    G4 -->|fail| X5[drop]
    G4 -->|pass| S[Weighted Score]
    S --> F["0.40·skill + 0.30·semantic + 0.20·role + 0.10·seniority"]
    F --> E[Explainer<br>strong/likely/missing]
    E --> R[(ranked_jobs.json)]
```

**Design note:** gates *remove*, they never down-rank. A job requiring security clearance is not "a worse match" — it is not an option at all, and pretending otherwise wastes ranked slots.

## 4. Semantic & Skill Matching

```mermaid
flowchart TB
    subgraph ModeA["Mode A — exact / alias (credit 1.0)"]
        A1["candidate: RAG"] --- A2["JD: 'Retrieval-Augmented Generation'"]
        A3[skill_taxonomy.SKILL_ALIASES]
    end
    subgraph ModeB["Mode B — category (credit 0.6)"]
        B1["candidate: CLIP"] --- B2["JD: 'vision-language models'"]
        B3[skill_categories.SKILL_CATEGORIES]
    end
    subgraph Semantic["Semantic sub-score (weight 0.30)"]
        C1[candidate text] --> C3[all-MiniLM-L6-v2]
        C2[JD text] --> C3
        C3 --> C4[cosine similarity]
    end
```

Category matches deliberately earn **0.6**, not 1.0: a JD asking for "vision-language models" in general is weaker evidence than one naming CLIP specifically. This asymmetry is documented in `skill_categories.py` and keeps scoring honest.

## 5. Resume Tailoring & Truthfulness

```mermaid
sequenceDiagram
    participant U as User (job card button / CLI)
    participant T as resume_tailor
    participant P as backend.llm provider
    participant L as Groq (prod) / Ollama (dev)
    participant V as validate_tailored
    participant D as docx generator

    U->>T: Optimize resume
    T->>P: generate(JD-analysis prompt)
    P->>L: route by GROQ_API_KEY presence
    T->>P: generate(rewrite prompts, strict rules)
    L-->>T: tailored bullets (JSON)
    T->>V: check every bullet vs original resume
    alt fabricated tool/metric found
        V-->>T: flagged terms (e.g. Databricks, LangChain)
        T->>T: fall back to ORIGINAL bullets
    else all verified
        V-->>T: ok
    end
    T->>D: content JSON
    D-->>U: ATS-safe .docx + per-project truthfulness report
```

The validator is deterministic (regex + alias table, no LLM) and provider-agnostic: fabrication is *detected* on the output of whichever backend generated it, never just discouraged by prompt.

## 6. Career Intelligence Data Flow

```mermaid
flowchart LR
    P[(profile)] --> CR[career_recommender<br>archetype coverage]
    CR -->|top-3 target roles| SG[skill_gap]
    J[(jobs.json)] -->|title-matched subset| SG
    SG -->|"role demand % (guarded: needs ≥30 jobs)"| UI[Dashboard]
    CR -->|"fit % + evidence + missing"| UI
    P --> RH[resume_health<br>6 explainable dimensions] --> UI
    RH --> SU[suggestions<br>quotes actual bullets] --> UI
```

## 7. Application Flow & Events

```mermaid
sequenceDiagram
    participant R as run_real_ranking
    participant PG as PostgreSQL
    participant U as User
    participant CLI as log_outcome_cli

    R->>PG: log impressions (score + breakdown snapshot)
    U->>U: apply to job
    U->>CLI: company / title / event
    CLI->>PG: applied | interview | rejected | offer
    Note over PG: Events snapshot match_score at log time,<br>so future weight changes never corrupt history.
    PG-->>PG: accumulating training data for learning-to-rank
```

## 8. Future Auto-Apply Agent (planned)

```mermaid
flowchart TB
    RJ[(ranked_jobs.json)] --> Q[Application Queue]
    Q --> AG[Browser Agent]
    AG --> F1[Detect ATS form type]
    F1 --> F2[Fill fields from profile]
    F2 --> F3[Attach tailored resume]
    F3 --> HR{Human review gate}
    HR -->|approve| SUB[Submit]
    HR -->|reject| EDIT[Edit & retry]
    SUB --> EV[(events: applied)]
```

**Not yet implemented.** The human-review gate is a design commitment, not an afterthought: the agent prepares applications; the human approves them.

## 9. Component Diagram

```mermaid
flowchart TB
    subgraph Presentation["Presentation (two clients, zero business logic)"]
        APP[ui/app.py router + views + theme]
        FAPI[api/ routers + schemas + deps]
    end
    subgraph Domain["Domain Logic (pure, client-agnostic)"]
        AN[analysis/*]
        ME[matching_engine/*]
        RPp[resume_parser/*]
        REn[resume_engine/*]
    end
    subgraph Infrastructure
        LLM[llm/ provider: Groq / Ollama]
        JS[job_scraper/*]
        DBl[database/*]
        CF[config.py + version.py]
    end
    APP --> AN & ME & RPp & REn & DBl
    FAPI --> AN & ME & RPp & DBl
    REn --> LLM
    JS --> ME
    AN & ME & RPp & JS & REn & DBl --> CF
```

Dependency rule: **Presentation → Domain → Infrastructure → config. Domain never imports Presentation, and engines never know which LLM provider serves them.** This is why the FastAPI layer was a pure addition, and why swapping Ollama→Groq for deployment touched no business logic.
