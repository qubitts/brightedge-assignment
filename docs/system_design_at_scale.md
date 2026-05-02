# Scaling to Billions of URLs

## 1. Starting Point — The PoC

The current system is a single-process FastAPI application that accepts a URL, fetches the page, extracts metadata, classifies the content via NLP or LLM, and returns structured results. It works well for single URLs and small batches (up to 50). Redis caches results (key: `crawl:{sha256(url)}`, TTL: 24h) — if a URL was crawled recently, the cached result is returned immediately without re-fetching. PostgreSQL persists results long-term. Both are optional — the app runs fine without them.

This architecture handles hundreds of URLs per day. It cannot handle billions.

The rest of this document walks through every point where the current system breaks under billions-scale load, and designs the solution for each. By the end, we arrive at a complete architecture that can ingest, crawl, classify, and store billions of URLs organized by domain and year-month.

---

## 2. Where the System Breaks

### Breakpoint 1: Single-Process API

**The problem.** The entire pipeline — fetch, parse, validate, classify, cache, persist — runs inside a single FastAPI request handler (`_crawl_single()` in `src/api/routes.py`). One process, one event loop, one machine.

At ~10–20 URLs/second, the process saturates. Fetching is I/O-bound (waiting on target servers), classification is CPU-bound (KeyBERT BERT inference), and they compete for the same resources. A slow target server blocks the event loop for other requests. A KeyBERT inference running in `run_in_executor()` consumes CPU that could serve other requests.

At 1,000 URLs/second — needed for billions-scale — a single process is orders of magnitude too slow.

**The fix.** Decompose the monolithic request handler into three independent worker types connected by a message queue (Kafka):

```
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│ Fetch Worker  │────────▶│  NLP Worker   │────────▶│  LLM Worker   │
│              │  Kafka   │              │  Kafka   │  (optional)   │
│ • HTTP fetch │  topic   │ • KeyBERT    │  topic   │ • OpenAI API  │
│ • HTML parse │         │ • spaCy NER  │         │ • Anthropic   │
│ • Validation │         │ • Wikipedia  │         │ • Budget mgmt │
└──────────────┘         └──────────────┘         └──────────────┘
  I/O-bound                CPU-bound                I/O-bound
  Scale: 10–200            Scale: 5–100             Scale: 2–20
```

Each worker type scales independently. Fetch workers are network-bound — add more to increase crawl throughput. NLP workers are CPU/memory-bound — add more to increase classification throughput. LLM workers are rate-limited by provider APIs — a small number with careful throttling.

Kafka topics between them (`urls-to-crawl`, `nlp-requests`, `crawl-results`, `llm-requests`) provide durability, backpressure, and replay capability. If an NLP worker crashes, Kafka redelivers unprocessed messages to another consumer. No data loss.

---

### Breakpoint 2: No URL Intake Pipeline

**The problem.** The PoC accepts URLs one at a time via `POST /crawl` or in small batches via `POST /crawl/batch` (max 50). There is no mechanism to ingest billions of URLs from files or databases. Submitting 1 billion URLs one by one through an HTTP API is not viable.

**The fix.** Build two intake pipelines that stream URLs into Kafka at high throughput:

**Text file ingestion:**
```
S3 bucket (newline-delimited .txt or .csv)
    → File Reader streams line-by-line (never loads full file into memory)
    → URL validation + normalization
    → Batch into groups of 10,000
    → Bloom filter dedup check (Redis-backed, 1B capacity, ~1.2GB)
    → Produce to Kafka: urls-to-crawl
    → 100K–500K URLs/sec ingestion rate
```

Files can be hundreds of gigabytes. The reader streams them — no buffering the entire file. A Bloom filter with 1% false positive rate catches obvious duplicates before Kafka. Exact deduplication happens at the storage layer via `url_hash` upsert.

**MySQL ingestion:**

The source table holds URLs organized by domain and year-month:

```sql
CREATE TABLE url_sources (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    url             TEXT NOT NULL,
    domain          VARCHAR(255) NOT NULL,
    year_month      VARCHAR(7) NOT NULL,          -- e.g., '2025-07'
    priority        ENUM('critical','high','normal','low') DEFAULT 'normal',
    status          ENUM('pending','enqueued','crawling','completed','failed') DEFAULT 'pending',
    enqueued_at     TIMESTAMP NULL,
    completed_at    TIMESTAMP NULL,
    error_message   TEXT NULL,
    INDEX idx_domain_ym_status (domain, year_month, status),
    INDEX idx_status (status),
    INDEX idx_domain (domain)
) ENGINE=InnoDB;
```

The `idx_domain_ym_status` composite index is the workhorse — every ingestion query filters by all three columns. Without it, the reader would full-scan a billion-row table on every batch.

**Status transitions:**
```
pending → enqueued (reader picks up, produces to Kafka)
enqueued → crawling (fetch worker begins processing)
crawling → completed (result stored successfully)
crawling → failed (exhausted retries, moved to DLQ)
```

If the reader crashes mid-batch, rows stay `pending` (never marked `enqueued`) and are picked up on restart. Rows stuck in `enqueued` for > 24 hours are reset to `pending` by a cleanup job — this handles the case where the reader marked rows but crashed before producing to Kafka.

**Reader implementation:**
```sql
-- Cursor-based pagination (never use OFFSET on large tables)
SELECT id, url, domain, priority FROM url_sources
WHERE domain = 'amazon.com'
  AND year_month = '2025-07'
  AND status = 'pending'
  AND id > @last_seen_id
ORDER BY priority DESC, id ASC
LIMIT 10000;
```

The reader always reads from a MySQL **replica** — never the primary. Connection pool capped at 10 connections via `aiomysql`. Max 5 concurrent reader workers to avoid overwhelming the replica.

Both pipelines produce to the same Kafka topic with partition key = `hash(domain)`, ensuring all URLs from a domain land on the same partition. This enables per-domain politeness (crawl delay, rate limiting) without cross-partition coordination.

**Campaign management** wraps this into an operational unit:
```json
{
    "campaign_id": "camp-2025-07-amazon",
    "source": "mysql",
    "domain": "amazon.com",
    "year_month": "2025-07",
    "total_urls": 150000000,
    "completed": 72000000,
    "failed": 1200000,
    "status": "running"
}
```

Campaigns can be paused, resumed, or cancelled. Progress is tracked via Redis counters and surfaced in the monitoring dashboard.

---

### Breakpoint 3: NLP Models Consume Too Much Memory

**The problem.** Each process that runs the NLP pipeline loads its own copy of the models:

| Model | RAM per process |
|-------|----------------|
| KeyBERT (`all-MiniLM-L6-v2`) | ~400–500 MB |
| spaCy (`en_core_web_sm`) | ~50 MB |
| Python runtime + libraries | ~200 MB |
| **Total** | **~700 MB–1 GB** |

The PoC runs this in-process. That's fine for a single worker. At scale, with 4 gunicorn workers per pod, a single pod needs **4 GB RAM minimum** just for models. At 50 NLP worker pods, that's 200 GB of RAM spent duplicating the same 500 MB BERT model.

Additionally, long pages amplify the problem: a 50,000-character page produces ~11 chunks, each requiring a separate KeyBERT forward pass. At 1B URLs averaging 3 chunks per page, that's **3 billion NLP inference calls**.

**The fix.** Three strategies, applied progressively:

1. **Dedicated NLP worker pods** (immediate): Separate NLP workers from fetch workers. Fetch workers are lightweight (512 MB). NLP workers get the memory they need (4–6 GB per pod with 2 gunicorn workers). Each scales independently.

2. **Batch inference** (medium-term): Process multiple chunks in a single forward pass through BERT. The `sentence-transformers` library supports this natively. Reduces per-chunk overhead significantly.

3. **GPU acceleration** (long-term): Move KeyBERT to GPU instances.

| Metric | CPU (c5.2xlarge) | GPU (g4dn.xlarge) |
|--------|------------------|---------------------|
| KeyBERT per chunk | ~50–100 ms | ~2–5 ms |
| Throughput per instance | ~10–20 chunks/sec | ~200–500 chunks/sec |
| Cost per 1M chunks | ~$5–10 | ~$0.30–0.75 |

GPU is 10–50x faster and 7–15x cheaper per chunk at volume, but has higher base instance cost — it makes economic sense above ~50M chunks/month.

4. **Content deduplication**: Compute `content_hash` (SHA-256 of cleaned text). If a page's content hasn't changed since the last crawl, skip classification entirely. Saves ~30% compute on recrawls.

---

### Breakpoint 4: PostgreSQL Write Throughput

**The problem.** The PoC writes one row at a time via SQLAlchemy ORM upsert (`ON CONFLICT url_hash DO UPDATE`). At 1,000 URLs/sec, that's 1,000 individual `INSERT` statements per second. Each requires a round-trip, a WAL flush, and a lock acquisition. PostgreSQL saturates at a few hundred individual inserts/sec before connection limits and WAL throughput become bottlenecks.

At 1B rows, the `crawl_results` table grows to ~5 TB (with body text). Queries slow down. Indexes become massive. `VACUUM` takes hours. Backups take forever.

**The fix.**

**Bulk inserts.** Batch 100–500 rows per transaction:
```sql
INSERT INTO crawl_results (url_hash, url, domain, ..., crawled_at)
VALUES ($1, $2, $3, ..., $N), ($4, $5, $6, ..., $M), ...  -- 500 rows
ON CONFLICT (url_hash, crawled_at) DO UPDATE SET ...;
```

A single 500-row INSERT is ~100x faster than 500 individual INSERTs — fewer round-trips, one WAL flush, one lock acquisition.

**Table partitioning.** Partition by `crawled_at` month:
```sql
CREATE TABLE crawl_results (...) PARTITION BY RANGE (crawled_at);

CREATE TABLE crawl_results_2025_07 PARTITION OF crawl_results
    FOR VALUES FROM ('2025-07-01') TO ('2025-08-01');
```

Why monthly partitions matter:
- **Query speed**: `WHERE crawled_at BETWEEN '2025-07-01' AND '2025-07-31'` scans one partition (~500M rows) instead of the entire table (billions). That's the difference between scanning 2 TB and scanning 170 GB.
- **Cleanup**: `DROP TABLE crawl_results_2024_01` is instant. `DELETE FROM` on a billion-row table would take hours and generate massive WAL.
- **Maintenance**: `VACUUM` and `ANALYZE` run per partition, reducing lock contention.

**Connection pooling.** PgBouncer in transaction mode between workers and PostgreSQL:
```
Worker pods (100+)  →  PgBouncer (50 real connections)  →  PostgreSQL
```

Direct connections from 100+ worker pods would exhaust PostgreSQL's `max_connections`. PgBouncer multiplexes: a worker holds a real connection for ~10ms during a batch INSERT, then releases it. Even 100 workers share 50 connections efficiently.

**Read replicas.** Route all reads (Query API) to replicas. Zero read load on the primary. Replication lag is typically < 100ms with streaming replication.

---

### Breakpoint 5: Redis as a Single Point of Failure

**The problem.** The PoC connects to a single Redis instance. The application degrades gracefully when Redis is unavailable (no cache, continues crawling), but at scale, Redis is critical infrastructure — it stores the Bloom filter for deduplication, rate limit counters, circuit breaker state, and campaign progress. A single Redis instance maxes out at ~25 GB RAM and has no failover.

**The fix.** Redis Cluster:

```
6 nodes: 3 primary + 3 replica (cross-AZ)
16,384 hash slots distributed across 3 primaries
Instance type: r6g.xlarge (26 GB RAM each)
Total usable memory: ~75 GB
Eviction policy: volatile-lru (evict keys with TTL first)
```

Key namespace design:

| Key Pattern | Purpose | TTL |
|-------------|---------|-----|
| `crawl:{url_hash}` | Cached crawl results (same as PoC, but clustered) | 24h |
| `llm:{content_hash}` | Cached LLM classification | 7 days |
| `bloom:urls` | URL deduplication filter | Persistent |
| `rl:{domain}` | Per-domain crawl rate limiting | 10s rolling |
| `camp:{id}:*` | Campaign progress counters | Persistent |

The PoC's existing cache-first check in `_crawl_single()` continues to work — same key format, same logic. The difference is the backing store is now a 6-node cluster instead of a single instance. If a node fails, its replica promotes automatically.

---

### Breakpoint 6: LLM Costs at Billions Scale

**The problem.** The PoC calls an LLM API for every URL when `classification_mode: "llm"` is selected. LLM API costs scale linearly with volume — at 1B URLs, they dwarf all infrastructure costs combined. Additionally, OpenAI's rate limits (5,000 RPM at Tier 3) create a hard throughput ceiling. Calling an LLM for every URL at billions scale is neither economically viable nor operationally possible.

**The fix.**

**Decouple LLM from the critical path.** LLM classification runs asynchronously via a separate Kafka topic (`llm-requests`). The NLP pipeline always runs first. LLM enrichment happens after, at whatever pace the provider allows. If LLM is slow or down, NLP results are already stored.

**Selective LLM usage.** Only escalate to LLM when it adds value:
- NLP confidence is low (top KeyBERT keyword score < 0.3)
- Domain is high-priority (top 1,000 by traffic)
- Page is ambiguous (few entities found)

This reduces LLM volume to ~5–10% of URLs while maintaining quality where it matters.

**Batch API.** For non-real-time campaigns (bulk file/MySQL ingestion), use OpenAI's Batch API: 50% cost reduction, results within 24 hours.

**Budget circuit breaker.** Track spend in Redis. If hourly or daily spend exceeds configurable thresholds, LLM workers stop consuming and pages fall back to NLP-only.

**LLM response caching.** Cache by `content_hash` (SHA-256 of cleaned text). Same content always gets the same classification. On recrawl, if content hasn't changed, skip both NLP and LLM.

**Multi-provider failover.** OpenAI (primary) → Anthropic (secondary) → NLP-only (fallback). The PoC already supports all three providers — at scale, the LLM worker tries them in priority order.

---

### Breakpoint 7: No Per-Domain Politeness or robots.txt Compliance

**The problem.** The PoC has a concurrency semaphore for batch crawls, but no per-domain rate limiting and no robots.txt compliance. At scale, 200 fetch workers could hit the same domain with hundreds of concurrent requests — triggering rate limits, IP bans, and potentially taking down smaller sites. Without robots.txt, we also risk crawling pages that site owners have explicitly disallowed, which creates legal exposure and burns goodwill with target sites.

**The fix.**

**Per-domain rate limiting** via Redis token bucket:
- Default: 1 request/second per domain
- Configurable per domain (higher for sites with permission)
- Global cap: 1,000 outbound requests/second across all workers
- Kafka partition key = `hash(domain)`: all URLs from the same domain go to the same consumer, enabling per-domain throttling without coordination

**Circuit breaker per domain**: after 5 consecutive failures, stop crawling that domain for 60 seconds. If the probe request succeeds, resume. If it fails, double the cooldown (max 10 minutes).

**robots.txt compliance:**
- Fetch and parse `robots.txt` before the first crawl of any domain
- Cache parsed rules in Redis (key: `robots:{domain}`, TTL: 24h)
- Check each URL against rules before fetching — never crawl disallowed paths
- Respect `Crawl-delay` directives (override default rate limit if the directive is higher)
- Identify the crawler via `User-Agent: BrightEdgeCrawler/1.0 (+https://brightedge.com/crawler)`
- Handle missing robots.txt as "allow all"
- Handle robots.txt fetch errors gracefully (retry once, then assume "allow all")
- Honor `noindex` and `nofollow` meta tags on fetched pages

At billions scale, robots.txt is not just ethical — it's operational. Sites that ban our crawler IP ranges will crater our crawl success rate. Respecting robots.txt upfront avoids this.

---

### Breakpoint 8: No Search Capability

**The problem.** The PoC stores results in PostgreSQL and supports basic queries (list by domain, get by `url_hash`). But at billions scale, users need full-text search — find all pages about "machine learning," find pages mentioning "OpenAI," search by topic. PostgreSQL `LIKE '%machine learning%'` on a billion-row table is a full table scan.

**The fix.** Elasticsearch as a search tier alongside PostgreSQL:

```
PostgreSQL (source of truth)     Elasticsearch (search index)
├── Structured queries           ├── Full-text search on body, title
├── Upsert by url_hash           ├── Keyword/topic/entity faceted search
├── Monthly partitions           ├── Aggregations (counts by domain, by topic)
└── Exact lookups                └── Monthly indexes with ILM
```

A storage writer consumes from `crawl-results` Kafka topic and writes to both PostgreSQL (bulk INSERT) and Elasticsearch (bulk index API) in parallel. If ES is unavailable, PostgreSQL writes continue — ES is backfilled when it recovers.

Index lifecycle management (ILM) ages indexes automatically:

| Phase | Age | Actions |
|-------|-----|---------|
| Hot | 0–7 days | SSD nodes, 1 replica |
| Warm | 7–90 days | Merge segments, HDD nodes |
| Cold | 90–365 days | Freeze, S3-backed snapshots |
| Delete | > 365 days | Remove |

---

### Breakpoint 9: No JavaScript Rendering

**The problem.** The PoC fetches raw HTML via `httpx` and extracts text with Trafilatura/BS4. This works for server-rendered pages (news sites, blogs, most product pages). But a significant fraction of the modern web — SPAs built with React, Angular, Vue, and Next.js — renders content client-side. The raw HTML contains only a skeleton `<div id="root"></div>` and JavaScript bundles. Trafilatura returns nothing. The content validator flags it as `EMPTY_CONTENT`, and no classification happens.

At billions scale, this isn't an edge case. Entire domains (e.g., many e-commerce sites, dashboards, documentation portals) are JS-rendered. Skipping them means missing a meaningful share of the corpus.

**The fix.** A dedicated rendering tier using headless browsers (Playwright):

```
Fetch Worker
    → Raw HTML fetch (httpx, fast, cheap)
    → Content Validator
        ├── Body text ≥ 200 chars → proceed to NLP (no rendering needed)
        └── Body text < 200 chars AND JS signals detected → route to Render Worker
                                                              │
                                                              ▼
                                                    ┌──────────────────┐
                                                    │  Render Worker    │
                                                    │  (Playwright)     │
                                                    │  • Launch browser │
                                                    │  • Wait for DOM   │
                                                    │  • Extract HTML   │
                                                    │  • Re-parse       │
                                                    └──────────────────┘
```

**JS detection signals** (checked on raw HTML when body text is thin):
- `<div id="root">` or `<div id="app">` with minimal inner content
- `<script>` tags referencing React, Angular, Vue, or Next.js bundles
- `<noscript>` tag present (sites that know they need JS often include this)
- `type="module"` script tags dominating the `<head>`

**Why not render everything?** Headless browsers are 10–50x slower than raw HTTP fetches (~2–5 seconds per page vs ~200ms) and consume significantly more memory (~200–500 MB per browser instance). Rendering all 1B URLs would require a massive browser farm. The detection step ensures we only render the ~10–20% of pages that actually need it.

**Render Worker design:**
- Pool of Playwright browser instances (one per worker process, reused across pages)
- Navigate to URL, wait for `networkidle` or DOM content loaded (configurable timeout: 10s)
- Extract rendered HTML from `page.content()`
- Re-run Trafilatura/BS4 parsing on rendered HTML
- Produce result to `nlp-requests` (same downstream pipeline as raw fetches)
- Scale: 5–30 pods (heavy resource usage, fewer instances needed due to selective routing)

---

## 3. The Complete Architecture

After fixing all breakpoints, the system looks like this:

```
                                ┌─────────────────────┐
                                │   Load Balancer      │
                                │   (ALB)              │
                                └──────────┬──────────┘
                                           │
                      ┌────────────────────┼────────────────────┐
                      │                    │                    │
               ┌──────▼──────┐     ┌───────▼──────┐    ┌───────▼──────┐
               │  Ingestion  │     │  Query API   │    │  Admin API   │
               │  Service    │     │  (reads from │    │  (campaigns, │
               │  (file,     │     │   replicas   │    │   config)    │
               │   MySQL,    │     │   + ES)      │    │              │
               │   API)      │     └──────────────┘    └──────────────┘
               └──────┬──────┘
                      │ Bloom filter dedup
               ┌──────▼──────┐
               │  Kafka       │
               │  urls-to-    │
               │  crawl       │
               └──────┬──────┘
                      │
          ┌───────────┼───────────┐
          │           │           │
    ┌─────▼────┐┌─────▼────┐┌─────▼────┐     Per-domain rate
    │  Fetch   ││  Fetch   ││  Fetch   │     limiting + robots.txt
    │ Worker 1 ││ Worker 2 ││ Worker N │     via Redis
    └─────┬────┘└─────┬────┘└─────┬────┘
          │           │           │
          └───────────┼───────────┘
                      │
               ┌──────▼──────┐
               │  Kafka       │
               │  nlp-        │
               │  requests    │
               └──────┬──────┘
                      │
          ┌───────────┼───────────┐
          │           │           │
    ┌─────▼────┐┌─────▼────┐┌─────▼────┐
    │  NLP     ││  NLP     ││  NLP     │
    │ Worker 1 ││ Worker 2 ││ Worker N │
    └─────┬────┘└─────┬────┘└─────┬────┘
          │           │           │
          └───────────┼───────────┘
                      │
         ┌────────────┼────────────┐
         │            │            │
  ┌──────▼──────┐ ┌───▼────┐ ┌────▼─────────┐
  │ Kafka:      │ │ Kafka: │ │   Kafka:     │
  │ crawl-      │ │ llm-   │ │   urls-      │
  │ results     │ │requests│ │   dead-letter │
  └──────┬──────┘ └───┬────┘ └──────────────┘
         │            │
         │       ┌────▼────────┐
         │       │ LLM Workers │
         │       │ (budget     │
         │       │  tracked)   │
         │       └─────────────┘
         │
  ┌──────▼────────────────────────────────────┐
  │            Storage Writer                  │
  │  ┌──────────┐ ┌────────────┐ ┌──────────┐ │
  │  │PostgreSQL│ │Elasticsearch│ │  Redis   │ │
  │  │(primary) │ │  (search)  │ │ (cache)  │ │
  │  └────┬─────┘ └────────────┘ └──────────┘ │
  │       │                                    │
  │  ┌────▼─────┐                              │
  │  │Replicas  │                              │
  │  │(Query API│                              │
  │  │ reads)   │                              │
  │  └──────────┘                              │
  └────────────────────────────────────────────┘
         │
  ┌──────▼──────┐
  │  Nightly    │
  │  ETL → S3   │
  │  (Parquet)  │
  └─────────────┘
```

---

## 4. Storage Design

### Tiered Storage

| Tier | Store | What Lives Here | Retention |
|------|-------|----------------|-----------|
| **Hot** | Redis Cluster (75 GB) | Crawl result cache, Bloom filter, rate limits, robots.txt cache, campaign progress, LLM response cache | 24h (cache), persistent (Bloom) |
| **Warm** | PostgreSQL (partitioned) + Elasticsearch | All crawl results — structured queries in PG, full-text search in ES | 90 days |
| **Cold** | S3 (Parquet + Glacier) | Archived results for analytics (Athena/Spark). Raw HTML in Glacier for compliance. | Parquet: 3 years. Glacier: indefinite. |

### Body Text Storage Decision

Storing full body text for 1B URLs at ~5 KB/row = ~5 TB in PostgreSQL. That's large but manageable. However, body text inflates row size, slows `VACUUM`, and increases backup time.

**Recommendation**: Externalize body text to S3. Store a `body_s3_key` reference in PostgreSQL. Row size drops to ~1 KB. PostgreSQL stays lean at ~700 GB for 1B rows. Body text is archived as compressed objects in S3 (`s3://crawl-data/{domain}/{year_month}/{url_hash}.txt.gz`).

### Capacity Estimates (1 Billion URLs)

| Resource | Estimate |
|----------|----------|
| PostgreSQL (metadata only, compressed) | ~700 GB |
| Elasticsearch (indexed fields) | ~2 TB |
| Redis (cluster total) | ~100 GB |
| S3 body text (compressed) | ~1 TB |
| S3 Parquet archive | ~350 GB |
| S3 raw HTML (Glacier) | ~7 TB |
| Kafka (7-day retention) | ~500 GB |

---

## 5. Unified Data Schema

Every crawl result across all tiers conforms to this schema. PostgreSQL columns, Elasticsearch mappings, and Parquet columns are all projections of it.

```json
{
    "url_hash":             "SHA-256 of normalized URL — primary key everywhere",
    "url":                  "https://example.com/page",
    "domain":               "example.com",
    "year_month":           "2025-07",

    "status":               "success | failed | blocked | rate_limited",
    "http_status":          200,
    "error_code":           null,
    "error_message":        null,

    "title":                "Page Title",
    "description":          "Meta description",
    "body_s3_key":          "s3://crawl-data/example.com/2025-07/{hash}.txt.gz",
    "canonical_url":        "https://example.com/page",
    "language":             "en",
    "og_tags":              { "og:title": "...", "og:type": "article" },

    "classification_mode":  "nlp",
    "topics":               ["Technology", "Machine Learning"],
    "keywords":             ["neural networks", "deep learning"],
    "entities":             ["Google", "OpenAI"],
    "category":             "Technology/AI",
    "warnings":             [],

    "content_hash":         "SHA-256 of cleaned body text — for change detection",
    "response_time_ms":     450,
    "content_length":       15234,
    "retry_count":          0,
    "worker_id":            "fetch-worker-7",
    "campaign_id":          "camp-2025-07-amazon",

    "crawled_at":           "2025-07-15T10:30:00Z",
    "created_at":           "2025-07-15T10:30:01Z",
    "updated_at":           "2025-07-15T10:30:01Z",
    "schema_version":       1
}
```

**PostgreSQL table:**
```sql
CREATE TABLE crawl_results (
    url_hash            CHAR(64) NOT NULL,
    url                 TEXT NOT NULL,
    domain              VARCHAR(255) NOT NULL,
    year_month          VARCHAR(7) NOT NULL,
    status              VARCHAR(20) NOT NULL,
    http_status         SMALLINT,
    content_hash        CHAR(64),
    title               TEXT,
    description         TEXT,
    body_s3_key         TEXT,
    canonical_url       TEXT,
    language            VARCHAR(10),
    og_tags             JSONB,
    classification_mode VARCHAR(10),
    topics              JSONB,
    keywords            JSONB,
    entities            JSONB,
    category            VARCHAR(255),
    warnings            JSONB,
    error_code          VARCHAR(50),
    error_message       TEXT,
    response_time_ms    FLOAT,
    content_length      INTEGER,
    retry_count         SMALLINT DEFAULT 0,
    worker_id           VARCHAR(50),
    campaign_id         VARCHAR(50),
    crawled_at          TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    schema_version      SMALLINT DEFAULT 1,
    PRIMARY KEY (url_hash, crawled_at)
) PARTITION BY RANGE (crawled_at);

-- Indexes (created per partition automatically)
CREATE INDEX idx_domain ON crawl_results (domain);
CREATE INDEX idx_domain_ym ON crawl_results (domain, year_month);
CREATE INDEX idx_status ON crawl_results (status);
CREATE INDEX idx_category ON crawl_results (category);
CREATE INDEX idx_content_hash ON crawl_results (content_hash);
CREATE INDEX idx_topics ON crawl_results USING GIN (topics);
CREATE INDEX idx_keywords ON crawl_results USING GIN (keywords);
```

**Note on the primary key.** The composite PK `(url_hash, crawled_at)` is intentional. It allows the same URL to have multiple rows across monthly partitions — one per recrawl. This preserves crawl history: you can see how a page's classification changed over time. If you only need the latest result, query with `ORDER BY crawled_at DESC LIMIT 1`. The `content_hash` field enables efficient change detection without comparing full body text.

**Schema evolution**: Additive only. New fields get `NULL` defaults. Never rename or remove columns — deprecate by stopping writes. Every record carries `schema_version` for forward compatibility. Parquet handles new columns via schema merge in Spark/Athena.

---

## 6. Disaster Recovery

### Backup Strategy

| Component | Backup Method | Frequency | RPO | Restore Time |
|-----------|-------------|-----------|-----|-------------|
| **PostgreSQL** | RDS automated snapshots + continuous WAL archiving to S3 | Snapshots: daily. WAL: continuous (every 5 min). | < 5 min (point-in-time recovery) | ~30 min for 1 TB |
| **Elasticsearch** | Snapshot to S3 via snapshot lifecycle management | Daily | < 24 hours | ~1 hour per index |
| **Redis** | ElastiCache automatic backups + AOF persistence | Daily snapshot. AOF fsync every second. | < 1 min (AOF). Cache is rebuildable — loss is acceptable. | ~10 min |
| **Kafka** | Replication factor = 3 (data survives 2 broker failures). Topic data is ephemeral — retention = 7 days. | Continuous replication | 0 (replicated) | Automatic (ISR failover) |
| **S3** | Cross-region replication (CRR) to a secondary region | Continuous | 0 (replicated within minutes) | Instant (switch bucket endpoint) |

### Cross-Region Strategy

```
Primary Region (us-east-1)              DR Region (us-west-2)
├── RDS Primary + 2 replicas            ├── RDS cross-region read replica
├── ElastiCache cluster                 ├── (rebuild from snapshot if needed)
├── MSK cluster                         ├── (rebuild from S3 snapshots)
├── ES cluster                          ├── (restore from S3 snapshots)
├── S3 buckets ──── CRR ──────────────▶ ├── S3 replicated buckets
└── EKS cluster                         └── EKS standby cluster (scaled to 0)
```

**Failover procedure:**
1. Promote RDS cross-region replica to primary (~5 min)
2. Scale up DR EKS cluster, point workers at new RDS
3. Restore Redis from latest snapshot (cache is warm within hours via normal traffic)
4. Restore ES from S3 snapshots (~1 hour per index)
5. Create new Kafka cluster, replay from source MySQL/S3 (URLs not yet completed)

**RTO target: < 1 hour** for API availability. Full pipeline recovery (including ES and Kafka) within 4 hours.

### What's Rebuildable vs What's Not

| Component | Rebuildable? | From What? |
|-----------|-------------|-----------|
| Redis cache | Yes | Rebuilds naturally as URLs are crawled/queried |
| Elasticsearch | Yes | Backfill from PostgreSQL (source of truth) |
| Kafka topics | Partially | Unprocessed URLs can be re-ingested from source (MySQL/S3 files) |
| PostgreSQL | **No — this is the source of truth** | Must be restored from backup |
| S3 (Parquet/HTML) | Partially | Parquet can be regenerated from PostgreSQL. Raw HTML would need recrawl. |

---

## 7. Observability

### What to Track

Monitoring exists to answer three questions at any moment:
1. **Is the system working?** (throughput, error rate)
2. **How fast is it working?** (latency, consumer lag)
3. **Is it about to stop working?** (resource saturation, budget)

#### Pipeline Health

| Metric | What It Tells You | Alert If |
|--------|-------------------|----------|
| `crawl_fetches_per_sec` | How fast we're crawling | Drops to 0 while Kafka has messages |
| `crawl_nlp_per_sec` | How fast we're classifying | Falls below 50% of fetch rate (backlog building) |
| `crawl_error_rate` | What fraction of crawls fail | > 15% for 5 minutes |
| `kafka_consumer_lag` (per topic) | How far behind workers are | > 100K and growing |
| `dlq_messages_total` | How many URLs exhausted retries | > 1,000 in an hour |
| `campaign_completion_pct` | Campaign progress | Behind schedule (ETA > deadline) |

#### Latency

| Metric | Healthy Range | Investigate If |
|--------|--------------|---------------|
| HTTP fetch latency (p95) | < 5s | > 10s sustained |
| NLP pipeline latency (p95) | < 800ms | > 2s sustained |
| LLM API latency (p95) | < 3s | > 5s sustained |
| End-to-end per-URL latency (p95) | < 5s | > 15s sustained |
| PostgreSQL batch write (p95) | < 100ms | > 500ms sustained |
| Elasticsearch bulk index (p95) | < 2s | > 5s sustained |

#### Resource Saturation

| Metric | Healthy | Critical |
|--------|---------|----------|
| Kafka disk usage | < 70% | > 85% |
| PostgreSQL connections (via PgBouncer) | < 70% pool | > 90% pool (waiting clients) |
| PostgreSQL replication lag | < 1s | > 5s |
| Redis memory usage | < 80% maxmemory | > 95% (eviction starts) |
| Redis cache hit rate | > 30% | < 10% (cache isn't helping) |
| Worker CPU utilization | < 70% | > 90% sustained |
| Worker memory | < 80% limit | Near OOMKill threshold |
| LLM daily spend | < 80% budget | > budget (circuit breaker fires) |

#### Business Metrics

| Metric | Purpose |
|--------|---------|
| URLs ingested per day | Track intake progress |
| Crawl success rate by domain | Identify domains with anti-bot issues |
| Content dedup savings | How much compute we're saving by skipping unchanged pages |
| LLM cost per URL | Track unit economics |
| Classification coverage | % of successful crawls that got ≥ 1 keyword |

### Tooling

| Tool | Role |
|------|------|
| **Prometheus** | Collect all metrics. Scrape interval: 15s. Retention: 30 days. |
| **Grafana** | Dashboards: pipeline overview, campaign tracker, infrastructure health, cost/budget. |
| **Loki** | Centralized structured JSON logs from all services. Retention: 30 days. |
| **Jaeger** | Distributed tracing — follow a URL from ingestion through every pipeline stage. Sample: 1% normal, 100% errors. |
| **PagerDuty** | On-call alerting. Escalation: engineer → TL → EM. |
| **CloudWatch** | AWS-managed service metrics (RDS, ElastiCache, MSK). Feed into Grafana. |

### Key Dashboards

**Dashboard 1 — Pipeline Overview**: Real-time view of ingest rate, crawl rate, classification rate, consumer lag (stacked by topic), error rate by type, active workers per type, DLQ depth.

**Dashboard 2 — Campaign Tracker**: Per-campaign progress bars, completion percentage over time, ETA to completion, top error domains per campaign, throughput by domain.

**Dashboard 3 — Infrastructure Health**: PostgreSQL connections/replication lag/disk, Elasticsearch bulk rate/rejections/index sizes, Redis memory/hit rate/evictions, Kafka disk/ISR/partition lag, worker CPU/memory/restarts heatmap.

**Dashboard 4 — Cost & Budget**: LLM spend (hourly and daily) with budget threshold lines, spend by provider, cost per URL, content dedup savings counter, budget utilization gauge.

### Alerting Tiers

**P1 — Page on-call immediately:**
- Pipeline halted (fetch rate = 0 while Kafka has messages)
- Error rate > 15% sustained
- PostgreSQL down
- Redis OOM (> 95% memory)
- Worker crash loop (> 5 restarts in 10 min)

**P2 — Slack alert, investigate within business hours:**
- Consumer lag growing (> 100K, trend increasing)
- PostgreSQL replication lag > 5s
- LLM spend > 80% of daily budget
- Elasticsearch bulk rejections
- Low cache hit rate (< 10%)

**P3 — Slack notification, address in next sprint:**
- Campaign completed
- DLQ slow accumulation (> 100/hour)
- New domain blocked by anti-bot
- LLM fallback rate elevated

---

## 8. SLOs and SLAs

### Service Level Objectives (Internal Targets)

| Category | SLO | How We Measure |
|----------|-----|---------------|
| **API Availability** | Ingestion API: 99.9% uptime (43 min downtime/month) | Health check success rate over 30-day window |
| | Query API: 99.95% uptime (22 min downtime/month) | |
| **Latency** | Single URL crawl: p50 < 2s, p99 < 10s | Histogram from API response time |
| | Query API lookup: p50 < 50ms, p99 < 500ms | |
| | Full-text search: p50 < 100ms, p99 < 1s | |
| | End-to-end (ingest → queryable): p95 < 2 min | Trace duration from ingestion to storage write |
| **Throughput** | URL intake: ≥ 500K URLs/min | Kafka produce rate |
| | Crawl completion: ≥ 30K URLs/min | Crawl success counter rate |
| | NLP classification: ≥ 20K URLs/min | Classification counter rate |
| **Data Quality** | Crawl success rate: ≥ 92% (of valid, reachable URLs) | `success / total` excluding known-blocked domains |
| | Classification coverage: ≥ 95% of successful crawls get ≥ 1 keyword | Keywords array non-empty |
| | Keyword relevance (NLP): ≥ 75% precision | Monthly manual evaluation, n=200 |
| | Topic accuracy (LLM): ≥ 80% precision | Monthly manual evaluation, n=200 |
| **Reliability** | Zero data loss on worker crash | Kafka offset commits + upsert writes |
| | RPO (Recovery Point Objective): < 5 min | Continuous WAL archiving to S3 |
| | RTO (Recovery Time Objective): < 1 hour | RDS point-in-time recovery + EKS cluster failover |
| | MTTD (Mean Time to Detect): < 5 min | Alert firing latency |
| | MTTR (Mean Time to Recover): < 30 min (P1) | Detection to resolution |
| **Data Freshness** | Re-crawl staleness: ≤ 30 days | Max age of most recent crawl per URL |
| | Duplicate rate: < 0.5% | Duplicate `url_hash` entries in storage |

### Service Level Agreements (External Commitments)

SLAs are set below SLOs to provide buffer. Breaching an SLA triggers contractual consequences.

| SLA | Commitment |
|-----|-----------|
| API availability | 99.9% monthly |
| Campaign completion | 95% of URLs completed within 30 days |
| Data retention (warm tier) | 90 days minimum |
| Data retention (cold tier) | 3 years minimum |
| P1 incident response | Acknowledge < 15 min, resolve < 4 hours |
| P2 incident response | Acknowledge < 1 hour, resolve < 24 hours |

### Error Budget Policy

The gap between SLO and 100% is the error budget. For a 99.9% availability SLO: 0.1% = 43 min/month.

| Budget Remaining | What Changes |
|-----------------|-------------|
| > 50% | Normal development velocity. Deploy freely. |
| 25–50% | Require staging validation for all deploys. |
| 10–25% | Freeze non-critical deploys. Prioritize reliability work. |
| < 10% | Emergency mode. Only ship fixes. |

---

## 9. Next Steps — Phased Rollout

This is the order in which we build the system described above. Each phase has a clear deliverable and can be validated independently before moving on.

### Phase 1: Decompose the Monolith + Kafka

Take the single-process PoC and split it into Kafka-connected workers.

- Deploy Kafka (3-broker cluster via Docker Compose locally, MSK in staging)
- Refactor `_crawl_single()` → Kafka producer (ingestion API produces to `urls-to-crawl`)
- Build Fetch Worker as a standalone Kafka consumer (fetch → parse → validate → produce to `nlp-requests`)
- Build NLP Worker as a standalone consumer (classify → produce to `crawl-results`)
- Build Storage Writer (consume `crawl-results` → bulk INSERT to PostgreSQL)
- Add graceful shutdown (SIGTERM → drain in-flight → commit offsets)
- Add API authentication and rate limiting

**Validates**: Workers scale independently. Kafka handles backpressure. No data loss on worker restart.

### Phase 2: URL Intake + Politeness

Enable billions-scale URL ingestion and ethical crawling.

- Build text file ingestion (S3 → streaming reader → Bloom filter → Kafka)
- Build MySQL ingestion (cursor-based reader → status tracking → Kafka)
- Deploy Redis Bloom filter for deduplication
- Implement per-domain rate limiting (Redis token bucket)
- Implement robots.txt compliance (fetch, parse, cache in Redis, check before crawl)
- Build campaign management API (create, track progress, pause/resume)
- Add circuit breaker per domain

**Validates**: Can ingest 1M+ URLs from a file. Domains are crawled politely. Campaigns are trackable.

### Phase 3: Search + LLM Scaling

Add search capability and cost-controlled LLM enrichment.

- Deploy Elasticsearch cluster
- Build ES bulk indexing writer (consume from `crawl-results`)
- Build search API endpoints (topics, entities, full-text)
- Split LLM into separate Kafka consumer with budget circuit breaker
- Implement selective LLM escalation (confidence threshold)
- Implement LLM response caching by `content_hash`
- Deploy PgBouncer for connection pooling

**Validates**: Results searchable within 30 seconds. LLM spend stays within budget.

### Phase 4: JS Rendering + Kubernetes + Monitoring + DR

Production-grade deployment, observability, JS rendering, and disaster recovery.

- Build Render Worker (Playwright browser pool, selective routing from fetch workers)
- Build JS detection heuristics (empty body + React/Angular/Vue signals)
- Deploy EKS cluster with Helm charts for all services
- Define HPA policies (Kafka consumer lag for workers, CPU for APIs)
- Deploy Prometheus + Grafana + Loki + Jaeger
- Instrument all services with metrics, build 4 dashboards
- Configure alerting rules + PagerDuty
- Deploy S3 cold storage ETL (nightly PostgreSQL → Parquet)
- Set up cross-region RDS replica and S3 replication

**Validates**: JS-heavy pages get rendered and classified. Auto-scaling responds to load. Alerts fire within 5 minutes. DR failover tested.

### Phase 5: Hardening + GA

Stress testing, security, and operational readiness.

- Load test: 10M URLs end-to-end
- Chaos testing (kill workers, disconnect Redis/Kafka/PG)
- Security review + penetration test
- Multi-language NLP evaluation
- Anti-bot proxy provider evaluation
- Runbooks for all P1/P2 alerts, on-call rotation setup

**Validates**: System handles 10M URLs without degradation. Survives component failures. Security review passed.

