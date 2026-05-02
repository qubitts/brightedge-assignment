# Production Roadmap: Architecture, Operations & Scale

## 1. Context

The PoC is a working single-process FastAPI crawler with dual-mode classification (NLP + LLM), Redis caching, and PostgreSQL persistence. It handles single URLs and small batches on one machine. For PoC scope, sign-off criteria, and tech stack rationale, see `docs/system_design_at_scale.md` Section 1.

This document covers everything required to take that PoC to production: architecture, operations, security, scaling, and release management.

---

## 2. Architecture

### System Diagram

```
                                    ┌─────────────────────────────────────────────────────────────┐
                                    │                        EKS Cluster                           │
                                    │                                                             │
  ┌──────────┐    ┌──────────┐     │  ┌─────────────┐      ┌──────────────────────────────────┐  │
  │  Clients │───▶│   ALB    │─────┼─▶│   API       │─────▶│           Kafka (MSK)            │  │
  │          │    │  + WAF   │     │  │  (FastAPI)  │      │                                  │  │
  └──────────┘    └──────────┘     │  │             │      │  urls.pending ─▶ urls.fetched    │  │
                                    │  │ • Auth      │      │  urls.fetched ─▶ urls.classified │  │
  ┌──────────┐                     │  │ • Rate limit│      │  urls.classified ─▶ urls.failed  │  │
  │  File    │─────────────────────┼─▶│ • Ingest    │      │  *.dlq (dead letter)             │  │
  │  (S3)    │                     │  └─────────────┘      └──────────┬───────────────────────┘  │
  └──────────┘                     │                                   │                          │
                                    │         ┌────────────────────────┼────────────────┐         │
  ┌──────────┐                     │         │                        ▼                │         │
  │  MySQL   │─────────────────────┼─▶ ┌───────────┐  ┌───────────────────┐  ┌──────────────┐   │
  │ (read    │                     │   │  Fetch    │  │    NLP Worker     │  │  LLM Worker  │   │
  │  replica)│                     │   │  Worker   │  │                   │  │              │   │
  └──────────┘                     │   │           │  │  • KeyBERT        │  │ • OpenAI     │   │
                                    │   │ • httpx   │  │  • spaCy NER     │  │ • Anthropic  │   │
                                    │   │ • robots  │  │  • Wikipedia     │  │ • Budget mgmt│   │
                                    │   │ • parse   │  │  • Classification │  │ • Fallback   │   │
                                    │   └─────┬─────┘  └────────┬──────────┘  └──────┬───────┘   │
                                    │         │                  │                     │          │
                                    │         └──────────────────┼─────────────────────┘          │
                                    │                            ▼                                │
                                    │  ┌──────────────────────────────────────────────────────┐   │
                                    │  │                   Data Layer                         │   │
                                    │  │                                                      │   │
                                    │  │  ┌────────────┐  ┌─────────┐  ┌──────────────────┐  │   │
                                    │  │  │ PostgreSQL │  │  Redis  │  │  Elasticsearch   │  │   │
                                    │  │  │ (RDS)      │  │ (Elasti-│  │  (OpenSearch)     │  │   │
                                    │  │  │            │  │  Cache) │  │                   │  │   │
                                    │  │  │ • Results  │  │         │  │ • Full-text search│  │   │
                                    │  │  │ • Campaigns│  │ • Cache │  │ • Keyword filter  │  │   │
                                    │  │  │ • Audit    │  │ • Rate  │  │ • Domain filter   │  │   │
                                    │  │  │            │  │   limits│  │                   │  │   │
                                    │  │  └──────┬─────┘  └─────────┘  └──────────────────┘  │   │
                                    │  │         │                                            │   │
                                    │  └─────────┼────────────────────────────────────────────┘   │
                                    │            │                                                │
                                    └────────────┼────────────────────────────────────────────────┘
                                                 │
                                                 ▼
                                    ┌─────────────────────────┐
                                    │    S3 (Cold Storage)     │
                                    │    • Parquet exports     │
                                    │    • Glacier archival    │
                                    └─────────────────────────┘
```

### Data Flow (Single URL — Synchronous)

```
Client ──POST /v1/crawl──▶ API ──▶ Redis cache check
                                        │
                          ┌─── HIT ─────┘───── MISS ───┐
                          ▼                             ▼
                    Return cached               Produce to Kafka
                    result (< 50ms)             (urls.pending)
                                                        │
                                                        ▼
                                               Fetch Worker consumes
                                               • DNS resolve + SSRF check
                                               • robots.txt check
                                               • HTTP GET (httpx)
                                               • HTML parse (trafilatura)
                                               • Content validation
                                               • Produce to urls.fetched
                                                        │
                                                        ▼
                                               NLP/LLM Worker consumes
                                               • Classify content
                                               • Produce to urls.classified
                                                        │
                                                        ▼
                                               Result Writer consumes
                                               • Upsert to PostgreSQL
                                               • Index to Elasticsearch
                                               • Cache in Redis
                                               • Notify API (webhook/polling)
                                                        │
                                                        ▼
                                               API returns result to client
```

### Data Flow (Bulk — Asynchronous)

```
Client ──POST /v1/ingest/file──▶ API returns 202 + campaign_id
                                        │
                                        ▼
                                 File Reader streams from S3
                                 • Line-by-line (no full load)
                                 • URL validation
                                 • Bloom filter dedup
                                 • Batch produce to Kafka
                                        │
                                        ▼
                                 Same pipeline as above
                                 (Fetch → NLP/LLM → Writer)
                                        │
                                        ▼
                                 Campaign progress updated in Redis
                                 Client polls GET /v1/campaigns/{id}
```

---

## 3. API Contract

### PoC Endpoints

#### `POST /crawl` — Crawl and classify a single URL

**Request:**
```json
{
  "url": "https://example.com/article",
  "mode": "nlp",
  "options": {
    "timeout": 30,
    "max_retries": 3,
    "include_body": false
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | Yes | URL to crawl (must be http/https) |
| `mode` | enum | No | `"nlp"` (default) or `"llm"` |
| `options.timeout` | int | No | Request timeout in seconds (default: 30) |
| `options.max_retries` | int | No | Retry count (default: 3) |
| `options.include_body` | bool | No | Include raw body text in response (default: false) |

**Response (200):**
```json
{
  "url": "https://example.com/article",
  "status": "success",
  "metadata": {
    "title": "Article Title",
    "description": "Meta description",
    "og_tags": {"og:title": "...", "og:image": "..."}
  },
  "classification": {
    "keywords": ["machine learning", "neural networks", "AI"],
    "entities": [
      {"text": "OpenAI", "label": "ORG"},
      {"text": "GPT-4", "label": "PRODUCT"}
    ],
    "categories": ["Technology", "Artificial Intelligence"],
    "topics": ["deep learning", "language models"],
    "confidence": 0.85
  },
  "timing": {
    "fetch_ms": 1200,
    "parse_ms": 45,
    "classify_ms": 320,
    "total_ms": 1565
  },
  "cached": false,
  "crawled_at": "2024-01-15T10:30:00Z"
}
```

**Error Response (200 with error status):**
```json
{
  "url": "https://example.com/blocked",
  "status": "error",
  "error": {
    "code": "BOT_DETECTION",
    "message": "Page returned CAPTCHA challenge",
    "retryable": false
  },
  "cached": false,
  "crawled_at": "2024-01-15T10:30:00Z"
}
```

Error codes: `FETCH_TIMEOUT`, `FETCH_CLIENT_ERROR`, `FETCH_SERVER_ERROR`, `BOT_DETECTION`, `CONTENT_TOO_SHORT`, `PARSE_ERROR`, `LLM_ERROR`.

#### `POST /crawl/batch` — Crawl multiple URLs

**Request:**
```json
{
  "urls": ["https://example.com/a", "https://example.com/b"],
  "mode": "nlp",
  "options": {
    "concurrency": 10,
    "timeout": 30
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `urls` | string[] | Yes | URLs to crawl (max 100) |
| `mode` | enum | No | `"nlp"` (default) or `"llm"` |
| `options.concurrency` | int | No | Max parallel requests (default: 10) |

**Response (200):** Array of single-crawl responses.

#### `GET /health` — Service health check

**Response (200):**
```json
{
  "status": "healthy",
  "dependencies": {
    "redis": {"status": "connected", "latency_ms": 1},
    "postgres": {"status": "connected", "latency_ms": 3}
  },
  "version": "0.1.0"
}
```

### Production Endpoints (Added Post-PoC)

#### `POST /v1/crawl` — Authenticated crawl (Phase 1+)

Same as `/crawl` but requires `X-API-Key` header. Returns `401` without valid key, `429` when rate limited.

#### `POST /v1/ingest/file` — File ingestion (Phase 3+)

```json
{
  "source": "s3://bucket/urls.txt",
  "campaign_id": "camp_abc123",
  "mode": "nlp",
  "options": {"concurrency": 50}
}
```

Returns `202 Accepted` with a campaign ID for tracking.

#### `GET /v1/search` — Search results (Phase 4+)

```
GET /v1/search?q=machine+learning&domain=example.com&category=Technology&limit=20&offset=0
```

**Response (200):**
```json
{
  "total": 1523,
  "results": [
    {
      "url": "https://example.com/article",
      "title": "...",
      "keywords": ["..."],
      "category": "Technology",
      "crawled_at": "2024-01-15T10:30:00Z",
      "score": 0.95
    }
  ]
}
```

#### `GET /v1/campaigns/{id}` — Campaign status (Phase 4+)

```json
{
  "id": "camp_abc123",
  "status": "processing",
  "progress": {"total": 50000, "completed": 12340, "failed": 23, "pending": 37637},
  "created_at": "2024-01-15T10:00:00Z",
  "updated_at": "2024-01-15T10:35:00Z"
}
```

---

## 4. Service Level Agreements

### SLA Tiers

| Tier | Availability | Latency (p50) | Latency (p99) | Throughput | Applies From |
|------|-------------|---------------|---------------|------------|--------------|
| **Internal (Beta)** | 99.0% monthly | < 2s | < 10s | ≥ 500 URLs/sec | Week 5 |
| **Standard (GA)** | 99.9% monthly | < 1.5s | < 8s | ≥ 1,000 URLs/sec | Week 13 |
| **Premium (GA+)** | 99.95% monthly | < 1s | < 5s | ≥ 2,000 URLs/sec | Week 18+ |

Availability is measured as: `(total_minutes - minutes_with_error_rate_>5%) / total_minutes`.

Latency is measured from request receipt to response sent, excluding time spent waiting in Kafka queues (async ingestion). Async endpoints (`/v1/ingest/file`) are measured by acknowledgement latency only (< 500ms p99); processing time is tracked separately via campaign progress.

### Exclusions

SLA does not cover:
- Scheduled maintenance (announced ≥ 72 hours in advance, max 4 hours/month)
- Third-party failures (LLM provider outages, proxy provider downtime)
- Force majeure (AWS regional outages)
- Client-caused issues (exceeding rate limits, invalid input)

### Breach Response

| Severity | Condition | Response | Escalation |
|----------|-----------|----------|------------|
| **P1 — Service down** | Availability < 95% over 5 minutes | PagerDuty page on-call, 15-min acknowledge, 1-hr resolution target | Engineering manager at 30 min |
| **P2 — Degraded** | Availability 95–99% or p99 latency > 15s for 10 min | PagerDuty alert, 30-min acknowledge, 4-hr resolution target | Engineering manager at 2 hr |
| **P3 — SLA at risk** | Monthly SLA budget consumed > 50% with ≥ 15 days remaining | Slack alert to team, investigate within business hours | Team standup |
| **P4 — Metric drift** | Any SLA metric trending toward breach (linear projection) | Dashboard warning, proactive investigation | Weekly review |

### Error Budget

Monthly error budget = `100% - SLA target`. For 99.9% availability: 43.2 minutes/month of allowed downtime.

- If > 50% budget consumed in first half of month: freeze non-critical deploys, focus on reliability
- If > 80% budget consumed: only emergency fixes deployed, all hands on reliability
- If budget fully consumed: post-incident review mandatory, feature freeze until root cause addressed

### Alert Thresholds (Derived from SLA)

These thresholds inform the monitoring dashboards and PagerDuty rules:

| Metric | Warning | Critical | PagerDuty |
|--------|---------|----------|-----------|
| Error rate (5xx) | > 2% for 5 min | > 5% for 3 min | Critical |
| p99 latency | > 8s for 5 min | > 15s for 3 min | Critical |
| Consumer lag | > 10K messages for 10 min | > 50K messages for 5 min | Critical |
| Pod restarts | > 3 in 10 min | > 5 in 5 min | Warning |
| Memory usage | > 80% for 10 min | > 90% for 5 min | Critical |
| Disk usage (PG) | > 70% | > 85% | Critical |
| Redis hit rate | < 60% for 30 min | < 40% for 10 min | Warning |
| DLQ growth | > 100 messages/hour | > 500 messages/hour | Warning |

---

## 5. Capacity Planning Model

### Assumptions

| Parameter | Value | Source |
|-----------|-------|--------|
| Target throughput (Beta) | 500 URLs/sec sustained | SLA (Section 4) |
| Target throughput (GA) | 1,000 URLs/sec sustained | SLA (Section 4) |
| Avg fetch time per URL | 1.5s (p50), 5s (p99) | PoC measurements |
| Avg NLP classification time | 200ms per URL | PoC measurements (KeyBERT + spaCy) |
| Avg LLM classification time | 800ms per URL | PoC measurements (OpenAI gpt-4o-mini) |
| LLM escalation rate | 20% of URLs | Design target |
| Redis cache hit rate | 30% (conservative) | Estimate for diverse corpus |
| Fetch worker memory | 512 MB per pod | httpx + trafilatura + response buffer |
| NLP worker memory | 1.5 GB per pod | KeyBERT model (400MB) + spaCy (100MB) + working memory |
| LLM worker memory | 256 MB per pod | Stateless HTTP client |
| Fetch worker CPU | 0.5 vCPU per pod | I/O-bound, low CPU |
| NLP worker CPU | 1.5 vCPU per pod | BERT inference is CPU-heavy |
| LLM worker CPU | 0.25 vCPU per pod | I/O-bound (API calls) |

### Scaling Math

**Effective throughput needed** (after cache hits):
- Incoming: 500 URLs/sec
- Cache hit (30%): 150 URLs/sec served from Redis directly
- Pipeline load: 350 URLs/sec need full processing

**Fetch workers:**
- Each pod handles ~20 concurrent fetches (async httpx, 1.5s avg)
- Effective throughput per pod: 20 / 1.5 = ~13 URLs/sec
- Pods needed: 350 / 13 = **27 fetch worker pods**
- With 25% headroom: **34 pods**
- Node capacity: m5.xlarge (4 vCPU, 16 GB) fits ~7 fetch pods → **5 nodes**

**NLP workers:**
- Each pod processes 1 URL at a time (CPU-bound BERT inference): ~5 URLs/sec
- NLP processes 80% of pipeline: 350 × 0.8 = 280 URLs/sec
- Pods needed: 280 / 5 = **56 NLP worker pods**
- With 25% headroom: **70 pods**
- Node capacity: m5.xlarge fits ~2 NLP pods (CPU-limited) → **35 nodes** (too expensive)
- **Optimization**: Batch inference (4 URLs/batch) → 15 URLs/sec/pod → **24 pods** → **12 nodes**

**LLM workers:**
- Each pod handles ~10 concurrent API calls (I/O-bound): ~12 URLs/sec
- LLM processes 20%: 350 × 0.2 = 70 URLs/sec
- Pods needed: 70 / 12 = **6 LLM worker pods**
- With 25% headroom: **8 pods**
- Node capacity: fits many per node (low resource) → **shared with fetch nodes**

### Resource Summary (Beta — 500 URLs/sec)

| Worker Type | Pods | CPU (total) | Memory (total) | Nodes (m5.xlarge) |
|-------------|------|-------------|----------------|-------------------|
| API | 3 | 1.5 vCPU | 1.5 GB | shared |
| Fetch | 34 | 17 vCPU | 17 GB | 5 |
| NLP (batched) | 24 | 36 vCPU | 36 GB | 10 |
| LLM | 8 | 2 vCPU | 2 GB | shared |
| Result Writer | 4 | 2 vCPU | 2 GB | shared |
| **Total** | **73** | **58.5 vCPU** | **58.5 GB** | **~15 nodes** |

At GA (1,000 URLs/sec): double the above — ~30 nodes. Spot instances for fetch/LLM workers reduce node cost by 60-70%.

### HPA Configuration (Derived)

| Worker | Scale Metric | Scale Target | Min Pods | Max Pods |
|--------|-------------|--------------|----------|----------|
| API | CPU utilization | 60% | 2 | 10 |
| Fetch | Kafka consumer lag (`urls.pending`) | < 5,000 messages | 10 | 50 |
| NLP | Kafka consumer lag (`urls.fetched`) | < 5,000 messages | 8 | 40 |
| LLM | Kafka consumer lag (`urls.fetched` LLM partition) | < 2,000 messages | 3 | 15 |
| Result Writer | Kafka consumer lag (`urls.classified`) | < 5,000 messages | 2 | 10 |

### Kafka Sizing

| Parameter | Beta | GA |
|-----------|------|-----|
| `urls.pending` partitions | 30 (matches fetch pod count) | 60 |
| `urls.fetched` partitions | 24 (matches NLP pod count) | 48 |
| `urls.classified` partitions | 6 | 12 |
| Message size (avg) | ~2 KB | ~2 KB |
| Throughput | ~700 KB/sec per topic | ~1.4 MB/sec |
| Retention | 72 hours | 72 hours |
| Broker count | 3 (kafka.m5.large) | 3 (kafka.m5.xlarge) |

### When to Scale Vertically vs Horizontally

| Signal | Action |
|--------|--------|
| NLP worker CPU at 90%, lag low | Vertical: larger instance (more CPU per pod) |
| NLP worker lag growing, CPU at 60% | Horizontal: add more pods |
| Fetch workers at max pods, lag growing | Horizontal: add nodes; check if target servers are the bottleneck |
| Redis memory > 80% | Vertical: larger cache instance |
| PG connections near max | Add PgBouncer replicas or increase max_connections |
| ES search latency growing | Horizontal: add data nodes |

---

## 6. Security Threat Model

### Attack Surface

This service fetches arbitrary user-provided URLs — a textbook SSRF (Server-Side Request Forgery) vector. A malicious actor could attempt to:
- Access AWS instance metadata (`169.254.169.254`) to steal IAM credentials
- Scan internal VPC services (databases, caches, admin panels)
- Hit localhost services on the crawler host
- Trigger outbound requests to amplify DDoS attacks
- Submit URLs that return extremely large responses (memory exhaustion)
- Submit millions of URLs to exhaust compute (resource exhaustion beyond rate limiting)

### SSRF Mitigations

Implemented in the fetch layer (`fetcher.py`), enforced before any HTTP request is made:

**URL validation (pre-request):**
- Reject non-http/https schemes (no `file://`, `ftp://`, `gopher://`, `dict://`)
- Resolve DNS and reject if IP falls in private ranges:
  - `127.0.0.0/8` (loopback)
  - `10.0.0.0/8` (private)
  - `172.16.0.0/12` (private)
  - `192.168.0.0/16` (private)
  - `169.254.0.0/16` (link-local, AWS metadata)
  - `::1/128`, `fc00::/7`, `fe80::/10` (IPv6 equivalents)
- Reject URLs with IP addresses in the host (only allow domain names)
- Reject URLs with non-standard ports (only 80 and 443)
- Resolve CNAME chains and re-check IP at each hop (DNS rebinding defense)

**Network-level (production):**
- Fetch workers run in an isolated subnet with no route to internal services
- Security group allows outbound HTTP/HTTPS only (ports 80, 443) to `0.0.0.0/0`
- No inbound access to fetch worker subnet from internet
- VPC endpoint for AWS services (S3, Secrets Manager) — metadata endpoint blocked via IMDSv2 with hop limit = 1

**Response handling:**
- Maximum response body: 10 MB (stream and abort beyond limit)
- Maximum redirect count: 5 (re-validate each redirect target against IP blocklist)
- Connection timeout: 10s, read timeout: 30s
- No cookie jar persistence between requests (prevent session fixation)

### Input Sanitization

| Input | Validation |
|-------|-----------|
| URL | RFC 3986 compliant, http/https only, max 2048 chars, UTF-8 normalized |
| Batch URLs | Max 100 per request, each individually validated |
| Mode | Enum: `"nlp"` or `"llm"` only |
| API key | Alphanumeric + hyphens, fixed length, constant-time comparison |
| Campaign ID | UUID v4 format only |
| Search query | Max 500 chars, HTML-escaped before Elasticsearch query |

### DoS Protections (Beyond Rate Limiting)

| Vector | Mitigation |
|--------|-----------|
| Many URLs from one client | Per-API-key rate limit (requests/min) + daily quota |
| Many URLs targeting one domain | Per-domain rate limit (shared across all clients) |
| Large response bodies | 10 MB stream limit, abort on exceed |
| Slow responses (slowloris) | Connection timeout 10s, read timeout 30s |
| CPU exhaustion via NLP | Queue-based processing with bounded concurrency per worker |
| Memory exhaustion via large pages | Body size cap before parsing; parser timeout |
| LLM cost exhaustion | Per-key daily LLM budget cap; circuit breaker at global level |
| Kafka flooding | Producer rate limit; backpressure via bounded queue |

### Domain Blocklist

Maintained as a Redis set, updatable without deploy:
- Known malicious domains (updated from threat intelligence feeds)
- Internal-only domains (if submitted via API, reject immediately)
- Domains that have issued cease-and-desist or robots.txt denial

### Security Review Checklist (Phase 6 Gate)

- [ ] SSRF: Confirm private IP blocking works with DNS rebinding test
- [ ] SSRF: Verify metadata endpoint inaccessible from worker pods
- [ ] Input validation: Fuzz all API endpoints with malformed input
- [ ] Auth: Verify API keys cannot be enumerated (timing attacks)
- [ ] Auth: Verify rate limiter cannot be bypassed (header spoofing)
- [ ] Secrets: Confirm no secrets in logs, container images, or environment dumps
- [ ] Dependencies: `pip-audit` clean (no known CVEs in dependencies)
- [ ] TLS: All internal communication uses TLS (Kafka SASL_SSL, Redis TLS, PG SSL)
- [ ] Network: Fetch worker subnet isolation verified via VPC flow logs

---

## 7. Crawling Ethics & robots.txt Policy

### robots.txt Compliance

The service respects `robots.txt` by default. This is both a legal safeguard and a reputation requirement — being blocked by major domains due to non-compliance would undermine the product.

**Behaviour:**

1. Before crawling any URL, the fetch worker checks `robots.txt` for the target domain
2. `robots.txt` is fetched once per domain and cached in Redis with a 24-hour TTL
3. If `robots.txt` returns 4xx (not found): crawling is allowed (standard interpretation per RFC 9309)
4. If `robots.txt` returns 5xx or times out: crawling is **deferred** — retry `robots.txt` later, do not assume permission
5. If `Disallow: /` applies to our user-agent: the URL is rejected with error code `ROBOTS_BLOCKED`, `retryable: false`
6. `Crawl-delay` directives are respected and fed into the per-domain rate limiter

**User-Agent:**

```
User-Agent: BrightEdgeCrawler/1.0 (+https://[public-docs-url]/crawler-info)
```

A public page at the above URL explains what the crawler does, how to contact us, and how to block it.

**Configuration:**

| Setting | Default | Override |
|---------|---------|---------|
| Respect robots.txt | Yes (enforced) | Can be disabled per-campaign by admin with justification |
| Cache TTL for robots.txt | 24 hours | Configurable via `ROBOTS_CACHE_TTL` |
| User-agent string | `BrightEdgeCrawler/1.0` | Configurable via `CRAWLER_USER_AGENT` |
| Crawl-delay max | 60 seconds | Longer delays treated as `Disallow` |

### Ethical Crawling Principles

| Principle | Implementation |
|-----------|---------------|
| **Rate limiting per domain** | Redis token bucket: max 2 req/sec per domain by default, configurable per domain |
| **No credential stuffing** | Never submit forms, never follow login flows, never use stolen cookies |
| **No session hijacking** | No cookie jar persistence between requests |
| **Respect `noindex` meta tags** | If `<meta name="robots" content="noindex">` is present, crawl result is not indexed in Elasticsearch |
| **Respect `X-Robots-Tag` headers** | Same as meta tag — honoured for `noindex`, `nofollow` |
| **Bandwidth awareness** | 10 MB response cap; abort early for large files (PDFs, videos) |
| **Identify ourselves** | User-agent string always set; never impersonate browsers |
| **Honour cease-and-desist** | Domain blocklist (Redis set) updated immediately on legal request |

### Override Policy

Robots.txt override requires:
- Written justification from the requesting team
- Approval from Senior BE + legal review for high-profile domains
- Override logged in audit table with timestamp, approver, and expiry date
- Override expires after 30 days unless renewed

---

## 8. Blockers

The PoC handles single URLs on one machine. To reach production at billions-scale, we need to address the following blockers across infrastructure, reliability, and scale.

### Trivial — Known solutions, standard patterns

| Blocker | Fix | Effort |
|---------|-----|--------|
| No API authentication | API key middleware + Redis key store | < 1 week |
| No inbound rate limiting | Redis sliding window per API key | < 1 week |
| Text logging (not structured) | Switch to `structlog`, output JSON | < 1 week |
| Health check doesn't verify deps | Add Redis/PG/Kafka ping to `/health` | < 1 week |
| Large Docker image (~2 GB) | Multi-stage build, runtime-only stage | < 1 week |
| No URL normalization | Lowercase scheme/host, strip fragments, sort params | < 1 week |
| No graceful shutdown under SIGTERM | Drain in-flight, commit Kafka offsets, close pools | < 1 week |
| PostgreSQL partitions not auto-created | pg_partman or cron `CREATE TABLE IF NOT EXISTS` | < 1 week |

### Non-Trivial — Known problem, requires design work

| Blocker | Why It's Hard | Effort |
|---------|--------------|--------|
| **Kafka integration** | Replacing direct API-to-pipeline flow with message-passing. Topics, consumer groups, offset management, poison message handling. | 3–4 weeks |
| **Worker separation** | Splitting `_crawl_single()` into fetch/NLP/LLM consumers. Each must independently scale and handle failures. | 2–3 weeks |
| **File + MySQL ingestion** | Streaming readers for 500M-line files, cursor-based MySQL pagination, Bloom filter dedup, campaign status tracking. | 2–3 weeks |
| **Elasticsearch integration** | Cluster deployment, bulk indexing writer, search API endpoints, index lifecycle management. | 2–3 weeks |
| **Monitoring stack** | Prometheus + Grafana + Loki + PagerDuty. Instrumenting every service, building 4 dashboards, defining alert rules. | 2–3 weeks |
| **Kubernetes deployment** | EKS cluster, Helm charts, HPA policies, resource limits, PodDisruptionBudgets, rolling deploys. | 2–3 weeks |
| **CI/CD pipeline** | GitHub Actions: lint → test → build → push ECR → deploy staging → integration tests → promote to production. | 1–2 weeks |
| **Per-domain rate limiting** | Redis token bucket per domain, circuit breaker per domain, domain-aware Kafka partitioning. | 1–2 weeks |
| **Campaign management** | CRUD API, Redis progress counters, pause/resume/cancel, campaign-level metrics. | 1–2 weeks |
| **Dead letter queue** | DLQ consumer, failure aggregation, admin replay API, alerting on growth. | 1 week |
| **PgBouncer** | Transaction-mode connection pooling between workers and PostgreSQL. | < 1 week |
| **S3 cold storage ETL** | Nightly PG → Parquet → S3 export, lifecycle rules for Glacier transition. | 1–2 weeks |
| **Distributed tracing** | OpenTelemetry SDK, trace propagation via Kafka headers, Jaeger deployment, sampling strategy. | 1–2 weeks |

### Research Required — Solution uncertain, needs investigation

| Blocker | What We Don't Know | How to Find Out |
|---------|-------------------|----------------|
| **Anti-bot at scale** | Which proxy providers work best? Cost per million requests? What fraction of our corpus needs proxies? | Evaluate 3 providers (Bright Data, Oxylabs, SmartProxy) on 10K URLs from top 1000 domains. Measure success rate and cost. |
| **JS rendering** | What % of our URLs need rendering? Per-page cost (time + memory)? How to detect which pages need it? | Compare Trafilatura output on raw vs rendered HTML for 1K URLs. Build a lightweight detector from HTML signals. |
| **NLP quality at scale** | How does keyword quality vary across domains (legal, medical, financial)? Memory stability under 72h sustained load? | Run pipeline on 100K diverse URLs. Manual evaluation on 500. Long-duration soak test. |
| **LLM cost-quality tradeoff** | Optimal confidence threshold for NLP → LLM escalation? Cost-quality Pareto curve? | A/B test on 10K URLs: NLP-only vs LLM-only vs hybrid. Plot cost vs quality. Determine threshold. |
| **Cross-tier data consistency** | How to detect and repair drift between PostgreSQL and Elasticsearch? Max acceptable lag? | Build reconciliation job: compare row counts and sample checksums daily. Target < 0.1% drift. |
| **Multi-language NLP** | Quality of multilingual models vs English-only? Memory footprint of multiple language models? | Evaluate `xlm-roberta-base` and `xx_ent_wiki_sm` on 100 non-English URLs. |

### Research Decision Trees

Each research blocker has a pre-defined decision tree so findings translate directly into action:

**Anti-bot at scale:**
- If success rate ≥ 85% at < $2/1K requests → adopt that provider for all crawls
- If all providers < 70% success → build domain allowlist, skip hostile domains, document limitation
- If cost > $5/1K requests → proxy only top-value domains (determined by campaign priority), accept lower success on others

**JS rendering:**
- If < 10% of URLs need rendering → add Playwright pool for flagged URLs only (detector-gated)
- If 10–30% need rendering → deploy dedicated rendering workers with separate HPA scaling
- If > 30% need rendering → render all by default, use raw HTML only for known-static domains

**NLP quality at scale:**
- If quality ≥ 75% across all domains → ship as-is with current models
- If quality < 60% on specific verticals → add domain-specific keyword filters or escalate those domains to LLM automatically
- If memory leak detected in soak test → profile, fix if possible; otherwise restart workers on 24h schedule

**LLM cost-quality tradeoff:**
- If NLP-only quality ≥ 80% → use LLM only on explicit user request (paid tier)
- If hybrid at threshold T gives 90%+ quality with < 20% LLM usage → adopt threshold T as default
- If no clean threshold exists → offer LLM as separate paid tier, NLP as default

**Cross-tier data consistency:**
- If drift < 0.1% with async indexing → keep current async design
- If drift 0.1–1% → add write-ahead log replay for ES updates
- If drift > 1% → switch to synchronous dual-write with saga pattern and compensating transactions

**Multi-language NLP:**
- If multilingual model quality ≥ 70% with < 500MB extra memory → replace English-only model
- If quality < 70% → keep English model as default, route detected non-English pages to LLM
- If memory > 1GB extra per model → load language-specific models on-demand, evict after idle timeout

---

## 9. Implementation Phases

### Phase 1: Harden the PoC (Weeks 1–2)

Add API authentication, rate limiting, structured logging, config validation, graceful shutdown. Set up CI pipeline (lint + test + build on every PR).

**Week 1:**

| Task | Owner | Days | Depends On |
|------|-------|------|------------|
| API key middleware + Redis key store | Senior BE | 3 | — |
| Redis sliding window rate limiter | Mid BE | 2 | — |
| Switch to structlog, JSON output | Mid BE | 2 | — |
| CI pipeline (GitHub Actions) | DevOps | 3 | — |
| Multi-stage Dockerfile | DevOps | 1 | CI pipeline |

**Week 2:**

| Task | Owner | Days | Depends On |
|------|-------|------|------------|
| Config validation + health check | Senior BE | 2 | — |
| Graceful shutdown (SIGTERM handling) | Senior BE | 2 | — |
| URL normalization | Mid BE | 2 | — |
| Alembic setup + initial migration | Mid BE | 2 | — |
| Secrets Manager integration (staging) | DevOps | 2 | CI pipeline |
| Integration tests for auth + rate limiting | Mid BE | 1 | Auth + rate limiter done |

**Cut line**: If week 1 slips, URL normalization and Alembic move to Phase 2. Auth and CI are non-negotiable for exit criteria.

**Exit**: API requires keys, logs are structured JSON, CI runs on every PR.

### Phase 2: Kafka Backbone (Weeks 3–4)

Deploy Kafka. Integrate producer into ingestion path. Build fetch worker as a standalone Kafka consumer. Set up staging environment with auto-deploy on merge to `main`.

**Week 3:**

| Task | Owner | Days | Depends On |
|------|-------|------|------------|
| Kafka cluster setup (MSK) | DevOps | 3 | — |
| Topic design + schema registry | Senior BE | 2 | — |
| Producer integration in API routes | Senior BE | 3 | Kafka cluster ready |
| Staging environment (ECS or EC2) | DevOps | 2 | — |

**Week 4:**

| Task | Owner | Days | Depends On |
|------|-------|------|------------|
| Fetch worker (Kafka consumer) | Senior BE | 4 | Producer done |
| Poison message handling + DLQ topic | Mid BE | 2 | Fetch worker started |
| Integration tests (testcontainers) | Mid BE | 3 | Fetch worker done |
| Auto-deploy on merge to main | DevOps | 2 | Staging ready |
| Schema migration: add message tracking | Mid BE | 1 | Alembic setup |

**Cut line**: If MSK provisioning is delayed, use Docker Kafka locally and defer staging deploy. Fetch worker is non-negotiable.

**Exit**: URLs flow through Kafka. Fetch worker consumes and processes independently from the API process.

### Phase 3: Intake + Worker Separation (Weeks 5–6)

Build text file and MySQL ingestion pipelines with Bloom filter dedup. Split NLP and LLM into separate Kafka consumers. Add per-domain rate limiting.

**Week 5:**

| Task | Owner | Days | Depends On |
|------|-------|------|------------|
| NLP worker (Kafka consumer) | Senior BE | 3 | Fetch worker pattern |
| LLM worker + budget circuit breaker | Senior BE | 3 | NLP worker pattern |
| File ingestion (streaming reader) | Mid BE | 3 | — |
| Per-domain rate limiting (Redis token bucket) | Mid BE | 2 | — |
| S3 bucket + IAM setup | DevOps | 1 | — |

**Week 6:**

| Task | Owner | Days | Depends On |
|------|-------|------|------------|
| MySQL ingestion (cursor pagination) | Mid BE | 3 | — |
| Bloom filter dedup | Senior BE | 2 | — |
| End-to-end integration test (file → Kafka → workers → PG) | Mid BE | 2 | Workers done |
| 1M URL dry run | DevOps + Senior BE | 2 | All workers done |
| Schema migration: campaign tables | Mid BE | 1 | — |

**Cut line**: If MySQL credentials are unavailable, defer MySQL ingestion. File ingestion + worker separation are non-negotiable.

**Exit**: Can ingest 1M URLs from a file and process them through the distributed pipeline.

### Phase 4: Search + Campaigns (Weeks 7–8)

Deploy Elasticsearch. Build bulk indexing writer and search API endpoints. Build campaign management API with progress tracking. Set up DLQ processing. Deploy PgBouncer.

**Week 7:**

| Task | Owner | Days | Depends On |
|------|-------|------|------------|
| Elasticsearch cluster deployment | DevOps | 3 | — |
| Bulk indexing writer (Kafka → ES) | Senior BE | 4 | ES cluster ready |
| Campaign CRUD API | Mid BE | 3 | Campaign tables |
| PgBouncer deployment | DevOps | 1 | — |

**Week 8:**

| Task | Owner | Days | Depends On |
|------|-------|------|------------|
| Search API endpoints | Mid BE | 3 | ES cluster + indexer |
| Campaign progress tracking (Redis counters) | Senior BE | 2 | Campaign CRUD |
| DLQ consumer + replay API | Mid BE | 2 | — |
| Schema migration: search metadata indexes | Mid BE | 1 | — |
| Load test: 100K URLs through full pipeline | DevOps | 2 | All Phase 4 work |

**Cut line**: DLQ replay API can slip to Phase 5. Search and campaigns are non-negotiable.

**Exit**: Crawl results searchable via Elasticsearch. Campaigns can be created, tracked, and paused.

### Phase 5: Monitoring + Kubernetes (Weeks 9–10)

Deploy Prometheus + Grafana + Loki. Instrument all services. Build 4 dashboards and alert rules. Deploy everything to EKS with HPA. Set up S3 cold storage ETL.

**Week 9:**

| Task | Owner | Days | Depends On |
|------|-------|------|------------|
| EKS cluster provisioning | DevOps | 4 | — |
| Helm charts for all services | DevOps | 3 | EKS ready (can start in parallel) |
| Service instrumentation (prometheus_client) | Senior BE | 3 | — |
| Prometheus + Grafana deployment | DevOps | 2 | EKS ready |

**Week 10:**

| Task | Owner | Days | Depends On |
|------|-------|------|------------|
| HPA policies (CPU + consumer lag) | DevOps | 2 | Helm charts deployed |
| 4 dashboards + alert rules | Senior BE + DevOps | 3 | Prometheus + instrumentation |
| Loki for log aggregation | DevOps | 2 | EKS ready |
| S3 cold storage ETL (nightly PG → Parquet) | Mid BE | 3 | — |
| Zero-downtime migration validation | Mid BE | 1 | All services on K8s |

**Cut line**: S3 ETL can slip to Phase 6. EKS + monitoring are non-negotiable.

**Exit**: Full observability. All services on Kubernetes with auto-scaling. P1 alerts trigger PagerDuty.

### Phase 6: Research + Hardening (Weeks 11–12)

Run anti-bot proxy evaluation, NLP quality assessment at scale, LLM cost-quality A/B test. Load test at 1M URLs. Security review. Chaos testing (kill workers, disconnect Redis/Kafka/PG). Write runbooks.

| Task | Owner | Days | Depends On |
|------|-------|------|------------|
| Anti-bot proxy evaluation (3 providers × 10K URLs) | Mid BE | 4 | — |
| NLP quality at scale (100K URLs + manual eval) | Senior BE | 5 | — |
| LLM cost-quality A/B test | Senior BE | 3 | — |
| 1M URL load test | DevOps + Mid BE | 3 | — |
| Security review coordination | Senior BE | 2 | — |
| Chaos testing | DevOps | 3 | — |
| Runbook documentation | All | 2 | Chaos testing reveals gaps |

**Exit**: Research questions answered with decisions made per decision trees (Section 8). System survives chaos tests. Security review passed.

### Phase 7+: GA Preparation (Weeks 13–18)

JS rendering pipeline (Playwright). Multi-language NLP. 10M URL stress test. Penetration test. Disaster recovery test. On-call rotation setup.

| Task | Owner | Days | Depends On |
|------|-------|------|------------|
| Playwright rendering pool | Senior BE | 8 | JS rendering research |
| Multi-language NLP | Senior BE | 5 | Multi-language research |
| 10M URL stress test | DevOps + Mid BE | 5 | — |
| Penetration test (external vendor) | External | 5 | Security review |
| Disaster recovery test (AZ failover) | DevOps | 3 | — |
| On-call rotation setup | All | 2 | Runbooks complete |

---

## 10. Database Migration Strategy

### Tooling

**Alembic** for schema migrations, integrated into CI/CD.

```
migrations/
├── alembic.ini
├── env.py
└── versions/
    ├── 001_initial_schema.py
    ├── 002_add_campaign_tables.py
    ├── 003_add_search_metadata.py
    └── ...
```

### Migration Workflow

1. **Author**: Developer creates migration with `alembic revision --autogenerate -m "description"`
2. **Review**: Migration file reviewed in PR (SQL inspected for locking, data loss)
3. **CI**: `alembic upgrade head` runs against a disposable test database in CI
4. **Staging**: Applied automatically on deploy to staging; verified before promotion
5. **Production**: Applied as a pre-deploy step in the deployment pipeline (before new pods start)

### Zero-Downtime Migration Rules

All migrations against live tables must follow these constraints:

| Operation | Safe Approach |
|-----------|--------------|
| Add column | `ALTER TABLE ADD COLUMN ... DEFAULT NULL` (no lock on PG 11+) |
| Add NOT NULL column | Add as nullable → backfill → add constraint with `NOT VALID` → validate separately |
| Drop column | Deploy code that ignores column first → drop column in next release |
| Rename column | Add new column → dual-write → migrate reads → drop old column (3 deploys) |
| Add index | `CREATE INDEX CONCURRENTLY` (doesn't lock table) |
| Change column type | Add new column → backfill → swap reads → drop old (never `ALTER TYPE` on live tables) |

### Rollback

- Every migration has a `downgrade()` function
- If a migration fails mid-apply: `alembic downgrade -1` to revert
- Migrations that are destructive (drop column, drop table) are flagged in review and require explicit sign-off

---

## 11. Secrets Management

### By Environment

| Environment | Solution | Rotation |
|-------------|----------|----------|
| **Local dev** | `.env` file (git-ignored), `.env.example` for structure | Manual |
| **CI** | GitHub Actions encrypted secrets | Manual, per-secret |
| **Staging** | AWS Secrets Manager | Automatic 90-day rotation |
| **Production** | AWS Secrets Manager + IAM role-based access | Automatic 30-day rotation for DB creds |

### Implementation

- Kubernetes pods access secrets via **External Secrets Operator** (ESO), which syncs AWS Secrets Manager → Kubernetes Secrets
- Application reads secrets from environment variables (no file mounts, no hardcoded paths)
- Secret values never appear in logs (structlog filters configured to redact `*_KEY`, `*_SECRET`, `*_PASSWORD` patterns)
- No secrets in Docker images — all injected at runtime

### Secret Inventory

| Secret | Used By | Source |
|--------|---------|--------|
| `DATABASE_URL` | API, workers | Secrets Manager (auto-rotated) |
| `REDIS_URL` | API, workers | Secrets Manager |
| `KAFKA_SASL_PASSWORD` | API, workers | Secrets Manager |
| `OPENAI_API_KEY` | LLM worker | Secrets Manager |
| `ANTHROPIC_API_KEY` | LLM worker | Secrets Manager |
| `API_KEY_SIGNING_SECRET` | API | Secrets Manager |
| `ES_PASSWORD` | Indexer, search API | Secrets Manager |

### Access Controls

- Production secrets: accessible only by production EKS pods (IAM role scoping)
- Staging secrets: separate secret set, never shared with production
- Developers: no direct access to production secrets; use `aws secretsmanager get-secret-value` for staging only with MFA
- Rotation: database credentials rotated via Lambda function that updates both Secrets Manager and PG password simultaneously

---

## 12. API Versioning & Deprecation Policy

### Versioning Scheme

- All production endpoints are prefixed with `/v1/`
- The PoC's unversioned endpoints (`/crawl`, `/health`) are internal-only and removed at Beta
- Version is in the URL path, not headers (simpler for clients, easier to route)

### When to Increment Version

A new version (`/v2/`) is required when:
- Removing a field from a response
- Changing the type of an existing field
- Changing error code semantics
- Removing an endpoint

A new version is NOT required when:
- Adding a new optional field to a response
- Adding a new endpoint
- Adding a new optional request parameter
- Changing internal behavior without changing the contract

### Deprecation Process

1. **Announce**: Mark endpoint as deprecated in OpenAPI spec (`deprecated: true`) and response header (`Deprecation: true`)
2. **Sunset window**: 90 days minimum from deprecation announcement to removal
3. **Usage tracking**: Log all calls to deprecated endpoints; notify consumers via email/webhook if they're still using them
4. **Removal**: After sunset window + zero traffic for 14 days, endpoint is removed

### Header Contract

All responses include:
```
X-API-Version: v1
X-Request-ID: req_abc123
```

Deprecated endpoints additionally include:
```
Deprecation: true
Sunset: Sat, 15 Jun 2025 00:00:00 GMT
Link: </v2/crawl>; rel="successor-version"
```

### Change Communication Process

API changes must be communicated to consumers through multiple channels:

**Changelog (source of truth):**
- Maintained at `CHANGELOG.md` in the repository root
- Follows [Keep a Changelog](https://keepachangelog.com/) format
- Every PR that changes the API contract must update the changelog
- Sections: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`

**Notification channels:**

| Event | Channel | Timing |
|-------|---------|--------|
| New endpoint added | Changelog + release notes | On deploy |
| New optional field added | Changelog | On deploy |
| Deprecation notice issued | Changelog + email to affected API key owners + webhook | 90 days before removal |
| Deprecation reminder | Email to consumers still calling deprecated endpoint | 30 days before removal |
| Breaking change (new version) | Changelog + email + webhook + banner in developer portal | On deploy of new version |
| Endpoint removed | Changelog + email | On removal |

**Developer portal:**
- OpenAPI spec auto-published on every deploy (Swagger UI at `/docs`)
- Deprecation notices shown as banners on affected endpoints
- Migration guides published for version transitions (e.g., `/v1/` → `/v2/`)

**Webhook notifications:**
- Consumers can register a webhook URL via the campaign/settings API
- Events: `api.deprecated`, `api.version_released`, `api.endpoint_removed`
- Payload includes: affected endpoints, sunset date, migration guide URL

**Process for breaking changes:**
1. RFC written and reviewed internally (Senior BE + affected consumers)
2. New version deployed alongside old version (both active)
3. Deprecation notice issued on old version (90-day clock starts)
4. Migration guide published
5. Active outreach to consumers with > 100 req/day on old version
6. Old version removed after sunset window + 14 days zero traffic

---

## 13. Infrastructure Cost Estimates

### Alpha (Weeks 1–4) — ~$200/month

| Resource | Spec | Monthly Cost |
|----------|------|-------------|
| Staging EC2 (API + workers) | 1× t3.large | $60 |
| RDS PostgreSQL | db.t3.medium, 50 GB | $70 |
| ElastiCache Redis | cache.t3.small | $25 |
| ECR (container registry) | < 10 GB | $1 |
| GitHub Actions CI | ~2000 mins/month | $0 (free tier) |
| **Total** | | **~$156** |

Not included: developer machines, LLM API costs (pass-through, ~$0.01–0.03 per LLM-classified URL).

### Beta (Weeks 5–12) — ~$3,500–5,000/month

| Resource | Spec | Monthly Cost |
|----------|------|-------------|
| EKS control plane | 1 cluster | $73 |
| EKS worker nodes | 3× m5.xlarge (on-demand) | $430 |
| MSK (Kafka) | kafka.m5.large, 3 brokers | $650 |
| RDS PostgreSQL | db.r5.large, 200 GB, Multi-AZ | $450 |
| OpenSearch (Elasticsearch) | 3× m5.large.search, 150 GB | $700 |
| ElastiCache Redis | cache.r5.large, Multi-AZ | $300 |
| ALB (load balancer) | 1 ALB + data processing | $50 |
| S3 (cold storage + ingestion) | ~500 GB | $12 |
| CloudWatch / Grafana | Logs + metrics | $100 |
| Data transfer | ~500 GB outbound | $45 |
| PgBouncer | Runs on EKS (no extra cost) | $0 |
| **Total** | | **~$2,810** |
| **With overhead (20% buffer)** | | **~$3,400** |

LLM costs at Beta (if 20% URLs escalate to LLM): ~$200–600/month depending on volume.

### GA (Weeks 13–18) — ~$8,000–12,000/month

| Resource | Spec | Monthly Cost |
|----------|------|-------------|
| EKS worker nodes | 6× m5.xlarge + 3× c5.2xlarge (rendering) | $1,500 |
| MSK (Kafka) | kafka.m5.xlarge, 3 brokers, higher throughput | $1,200 |
| RDS PostgreSQL | db.r5.xlarge, 1 TB, Multi-AZ + read replica | $1,200 |
| OpenSearch | 3× m5.xlarge.search, 500 GB | $1,500 |
| ElastiCache Redis | cache.r5.xlarge cluster, 3 shards | $900 |
| Proxy provider | ~5M requests/month at $2/1K | $10,000* |
| S3 + Glacier | ~2 TB | $50 |
| ALB + WAF | Load balancer + web firewall | $150 |
| Monitoring (Prometheus/Grafana/Loki) | Self-hosted on EKS | $0 (node cost included) |
| **Total (excl. proxy)** | | **~$6,500** |
| **Total (incl. proxy at full volume)** | | **~$16,500** |

*Proxy costs scale linearly with crawl volume and the fraction of hostile domains. At 1M URLs/month with 30% needing proxies: ~$600. At 10M URLs/month with 50%: ~$10,000. The research phase (Phase 6) will determine actual proxy needs.

### Cost Reduction Levers

| Lever | Savings | Trade-off |
|-------|---------|-----------|
| Reserved instances (1yr) | 30–40% on EC2/RDS/ES | Commitment |
| Spot instances for workers | 60–70% on worker nodes | Interruptions (Kafka redelivers) |
| Right-size after load test | 10–30% | Requires observability data |
| NLP-only (no LLM) | Eliminates LLM cost | Lower quality on complex pages |
| Selective proxy usage | 50–80% proxy cost reduction | Lower success on hostile domains |

---

## 14. Quality Practices

### Code Review Standards

- Every PR requires 1 approval from another team member
- Senior BE reviews all architectural changes (new services, schema changes, Kafka topic design)
- DevOps reviews all infrastructure changes (Helm charts, CI config, Dockerfiles)
- PR must pass CI (lint + tests + build) before review
- No self-merging except for trivial fixes (typos, comment updates)

### Test Strategy

| Level | What | Coverage Target | Runs When |
|-------|------|----------------|-----------|
| **Unit** | Individual functions, classes, parsers | ≥ 85% line coverage | Every PR (CI) |
| **Integration** | Service with real Redis/PG (testcontainers) | Key flows covered | Every PR (CI) |
| **Contract** | API request/response schema validation | All endpoints | Every PR (CI) |
| **E2E** | Full pipeline: URL in → result out | 10 representative URLs | Merge to `main` |
| **Load** | Sustained throughput under target load | Pass/fail thresholds | Weekly + pre-release |
| **Chaos** | Kill components, verify recovery | All failure modes | Pre-release |

**Test growth plan:**
- Phase 1: 88 → ~120 tests (add auth, rate limiting, structured logging tests)
- Phase 2: ~120 → ~180 tests (Kafka producer/consumer, integration flows)
- Phase 3: ~180 → ~250 tests (ingestion pipelines, worker separation)
- Phase 4+: ~250 → ~350 tests (search, campaigns, DLQ)

### Definition of Done (per ticket)

A ticket is done when:
1. Code is written and passes local tests
2. Unit tests added/updated for new behavior (not just happy path — include error cases)
3. Integration test added if the change touches external systems (Redis, PG, Kafka, ES)
4. PR passes CI
5. PR reviewed and approved
6. Merged to `main` and deployed to staging
7. Verified working in staging (manual smoke test for non-trivial changes)

### Linting and Formatting

- `ruff` for linting and formatting (replaces flake8 + black + isort)
- `mypy` for type checking (strict mode on new code)
- Pre-commit hooks enforce lint + format locally
- CI rejects PRs that fail lint/type checks

---

## 15. Monitoring Dashboards

The Beta quality gate requires 4 operational Grafana dashboards. This section defines their contents.

### Dashboard 1: API Gateway

**Purpose**: Real-time health of the public-facing API layer.

| Panel | Metric | Visualization |
|-------|--------|---------------|
| Request rate | `http_requests_total` by status code | Time series (stacked) |
| Error rate | `http_requests_total{status=~"5.."}` / total | Single stat + time series |
| Latency percentiles | `http_request_duration_seconds` p50, p90, p99 | Time series (multi-line) |
| Active connections | `http_active_connections` | Gauge |
| Rate limit rejections | `rate_limit_rejected_total` by API key | Time series |
| Auth failures | `auth_failures_total` | Time series |
| Request body size | `http_request_size_bytes` p50, p99 | Time series |
| Top endpoints by traffic | `http_requests_total` by path | Table (top 10) |

**Alerts**: Error rate > 5% for 3 min (P1), p99 latency > 15s for 3 min (P1), rate limit rejections > 1000/min (P3).

### Dashboard 2: Workers

**Purpose**: Health and throughput of fetch, NLP, and LLM Kafka consumers.

| Panel | Metric | Visualization |
|-------|--------|---------------|
| URLs processed/sec | `worker_urls_processed_total` by worker type | Time series (stacked) |
| Processing latency | `worker_processing_duration_seconds` p50, p99 by type | Time series |
| Success/failure rate | `worker_results_total` by status and worker type | Time series (stacked) |
| Active workers | `worker_instances_active` by type | Gauge per type |
| Error breakdown | `worker_errors_total` by error code | Pie chart |
| LLM API latency | `llm_request_duration_seconds` by provider | Time series |
| LLM cost accumulator | `llm_cost_dollars_total` (daily reset) | Single stat + time series |
| LLM budget remaining | `llm_budget_remaining_dollars` | Gauge (red < 10%) |
| Memory per worker | `process_resident_memory_bytes` by pod | Time series |
| Crawl success rate (7-day) | Successful / total over 7 days | Single stat |

**Alerts**: Worker success rate < 80% for 10 min (P2), LLM budget < 10% (P3), memory > 90% for 5 min (P1).

### Dashboard 3: Kafka

**Purpose**: Message flow, lag, and partition health.

| Panel | Metric | Visualization |
|-------|--------|---------------|
| Consumer lag by group | `kafka_consumer_group_lag` | Time series per group |
| Messages produced/sec | `kafka_producer_messages_total` by topic | Time series |
| Messages consumed/sec | `kafka_consumer_messages_total` by topic | Time series |
| Partition distribution | Messages per partition | Heatmap |
| DLQ message count | `kafka_topic_messages{topic="*.dlq"}` | Single stat (red > 100) |
| DLQ growth rate | Rate of DLQ messages/hour | Time series |
| Producer errors | `kafka_producer_errors_total` | Time series |
| Consumer rebalances | `kafka_consumer_rebalances_total` | Event annotations |
| Broker disk usage | `kafka_broker_disk_usage_bytes` | Gauge per broker |
| Topic throughput (bytes/sec) | `kafka_topic_bytes_in` / `bytes_out` | Time series per topic |

**Alerts**: Consumer lag > 10K for 10 min (P2), lag > 50K for 5 min (P1), DLQ growth > 500/hr (P2), broker disk > 80% (P2).

### Dashboard 4: Infrastructure

**Purpose**: Underlying resource health (databases, cache, compute).

| Panel | Metric | Visualization |
|-------|--------|---------------|
| PostgreSQL connections | Active / max connections | Gauge |
| PG query latency | `pg_query_duration_seconds` p50, p99 | Time series |
| PG disk usage | Bytes used / allocated | Gauge (red > 80%) |
| PG replication lag | `pg_replication_lag_seconds` | Time series |
| Redis memory usage | `redis_memory_used_bytes` / max | Gauge |
| Redis hit rate | `redis_hits / (hits + misses)` | Single stat + time series |
| Redis connected clients | `redis_connected_clients` | Time series |
| Elasticsearch cluster health | Green/Yellow/Red status | Status indicator |
| ES indexing rate | `es_indexing_total` docs/sec | Time series |
| ES search latency | `es_search_duration_seconds` p50, p99 | Time series |
| ES disk usage | Per-node disk usage | Gauge per node |
| Node CPU utilization | `node_cpu_usage` by instance | Time series |
| Node memory utilization | `node_memory_usage` by instance | Time series |
| Pod restart count | `kube_pod_container_status_restarts_total` | Time series |

**Alerts**: PG connections > 80% max (P2), PG disk > 85% (P1), Redis memory > 80% (P2), ES cluster Yellow for 30 min (P3), ES cluster Red (P1), pod restarts > 5 in 5 min (P2).

---

## 16. Runbook Structure

### Required Runbooks (Phase 6 Deliverable)

Each operational scenario requires a documented runbook. The following list is the minimum set:

| # | Runbook | Trigger |
|---|---------|---------|
| 1 | API high error rate | Error rate > 5% alert fires |
| 2 | API high latency | p99 > 15s alert fires |
| 3 | Kafka consumer lag spike | Lag > 50K alert fires |
| 4 | Kafka consumer rebalance storm | > 5 rebalances in 10 min |
| 5 | DLQ growth spike | DLQ > 500 messages/hour |
| 6 | PostgreSQL disk full | Disk > 85% alert fires |
| 7 | PostgreSQL replication lag | Lag > 30s |
| 8 | PostgreSQL connection pool exhaustion | Active connections > 80% max |
| 9 | Redis memory pressure | Memory > 80% max |
| 10 | Redis connection failure | Health check reports Redis down |
| 11 | Elasticsearch red cluster | Cluster health = Red |
| 12 | Elasticsearch indexing backlog | Indexing lag > 10 min behind PG |
| 13 | Worker OOM (Out of Memory) | Pod killed by OOM killer |
| 14 | Worker stuck (no progress) | Worker produces 0 messages for 5 min |
| 15 | LLM provider outage | LLM error rate > 50% |
| 16 | LLM budget exhausted | Budget remaining = $0 |
| 17 | Deployment rollback | Canary error rate > 5% |
| 18 | Full service recovery | After P1 incident resolved |

### Runbook Template

Every runbook follows this structure:

```
# [Title]

## Trigger
What alert or condition activates this runbook.

## Impact
What users/systems are affected and how.

## Diagnosis
Step-by-step commands to determine root cause:
1. Check [specific metric/dashboard]
2. Run [specific command]
3. Look for [specific pattern in logs]

## Resolution
For each root cause identified in Diagnosis:

### Cause A: [description]
1. [Action step]
2. [Verification step]

### Cause B: [description]
1. [Action step]
2. [Verification step]

## Escalation
- If unresolved after [time]: escalate to [person/team]
- If customer-impacting: notify [channel]

## Post-Resolution
- [ ] Verify metrics have returned to normal
- [ ] Update incident timeline
- [ ] Determine if follow-up work is needed
```

### Ownership

- Senior BE authors runbooks 1–2, 5, 13–16
- Mid BE authors runbooks 6–8, 12
- DevOps authors runbooks 3–4, 9–11, 17–18
- All runbooks reviewed by at least one other team member

---

## 17. Data Retention Policy

### Retention Tiers

| Tier | Storage | Retention | Data |
|------|---------|-----------|------|
| **Hot** | PostgreSQL | 90 days | Full crawl results (URL, metadata, classification, timing) |
| **Warm** | Elasticsearch | 60 days | Searchable index (URL, title, keywords, category, crawled_at) |
| **Cold** | S3 (Standard) | 1 year | Parquet exports (nightly from PG, full record) |
| **Archive** | S3 Glacier | 3 years | Annual compaction from S3 Standard |
| **Deleted** | — | After archive retention | Permanently removed |

### Lifecycle Rules

**PostgreSQL:**
- Partitioned by month (`crawl_results_2024_01`, `crawl_results_2024_02`, etc.)
- After 90 days: partition exported to S3 as Parquet, then dropped
- Campaign metadata retained for 1 year (campaigns table is small)

**Elasticsearch:**
- Index per month (`crawl-results-2024-01`)
- ILM (Index Lifecycle Management) policy:
  - Hot phase: 0–30 days (all replicas, full resources)
  - Warm phase: 30–60 days (reduce to 1 replica, force merge)
  - Delete phase: after 60 days (index deleted; data still in PG/S3)

**S3:**
- Lifecycle policy on cold storage bucket:
  - Standard: 0–365 days
  - Glacier: 365 days – 3 years
  - Delete: after 3 years

**Redis:**
- Cache entries: TTL-based (default 1 hour, configurable per key type)
- Rate limit counters: TTL = window size (1 min or 1 hour)
- Campaign progress counters: TTL = 7 days after campaign completion

### Compliance Considerations

- No PII is stored (we crawl public URLs; we don't store user data beyond API keys)
- If crawled content is later subject to DMCA/right-to-be-forgotten: delete by `url_hash` across all tiers (PG, ES, S3)
- Audit log of deletions maintained for 1 year
- API keys and access logs retained for 1 year for security audit purposes

### Storage Growth Projections

| Phase | Monthly Ingest | PG Hot (90d) | ES Warm (60d) | S3 Cold (1yr) | Total Active |
|-------|---------------|--------------|---------------|---------------|-------------|
| Beta (1M URLs/month) | ~5 GB/month | ~15 GB | ~10 GB | ~60 GB | ~85 GB |
| GA (10M URLs/month) | ~50 GB/month | ~150 GB | ~100 GB | ~600 GB | ~850 GB |
| GA+1yr (10M URLs/month) | ~50 GB/month | ~150 GB | ~100 GB | ~600 GB + 600 GB Glacier | ~1.45 TB |

These projections assume ~5 KB per crawl result (metadata + classification, no raw body). If `include_body` is used, multiply by 5–10x for those records.

---

## 18. Evaluating the PoC

### Does it work?

Crawl these against the running system:
- A news article (CNN/BBC/Reuters) → should return title, body, ≥ 3 keywords, ≥ 2 entities
- A product page (Amazon) → should return OG tags and product keywords, or a clean `BOT_DETECTION` error
- A blog post (Medium/REI) → should return relevant keywords and entities
- A non-existent URL → should return `FETCH_CLIENT_ERROR`, `retryable: false`
- A timeout URL → should return `FETCH_TIMEOUT`, `retryable: true`
- Batch of 50 mixed URLs → all should return results, ≥ 80% success
- Same URL twice → second request should return from cache in < 100ms
- LLM mode → should return topics, keywords, entities, and a category
- LLM with mocked API failure → should fall back to NLP with a warning

### Is the quality acceptable?

Sample 200 URLs from a diverse corpus (news, product, blog, documentation, government). Two evaluators independently rate:
- **Keyword relevance**: ≥ 75% of top-5 keywords judged relevant
- **Entity precision**: ≥ 80% of extracted entities are real named entities
- **Topic accuracy (LLM)**: ≥ 80% of assigned topics match page content

### Does it perform?

- Single URL: p50 < 2s, p99 < 10s
- Cache hit: p50 < 50ms
- Memory under load: < 2 GB per worker pod, stable over 72 hours (no leak)
- Distributed throughput: ≥ 500 URLs/sec sustained (at Beta)

### Is it reliable?

- Kill a fetch worker mid-crawl → Kafka redelivers, no data loss
- Disconnect Redis → service continues without cache
- LLM API down → falls back to NLP
- Disconnect Elasticsearch → PostgreSQL writes continue, ES backfills on recovery

---

## 19. Release Plan

### Alpha (Weeks 1–4)

**Audience**: Engineering team, assignment reviewers.

**What ships**: Hardened PoC with auth, rate limiting, CI/CD, Kafka integration. Docker Compose for local, staging environment deployed.

**Quality gate**:
- All 88+ tests pass, coverage ≥ 85%
- Crawls 100 diverse URLs successfully
- CI pipeline on every PR
- Structured logging active
- No critical security issues

### Beta (Weeks 5–12)

**Audience**: Internal teams, select partners.

**What ships**: Distributed pipeline (Kafka + separated workers), file/MySQL ingestion, Elasticsearch search, campaign management, full monitoring, Kubernetes deployment.

**Quality gate**:
- Ingest and process 1M URLs end-to-end
- ≥ 95% crawl success rate on 10K URL corpus
- p99 latency < 10s
- Consumer lag < 10K sustained
- All 4 Grafana dashboards operational
- System survives chaos tests
- Security review passed
- Runbooks documented

### GA (Weeks 13–18)

**Audience**: Production workloads.

**What ships**: JS rendering, multi-language NLP, anti-bot proxy integration, multi-AZ deployment, SLA commitments.

**Quality gate**:
- Process 10M URLs in stress test
- 99.9% API availability over 30-day window
- Classification accuracy ≥ 80% on manual evaluation
- ≥ 85% success rate on top 1000 domains (with proxy support)
- Penetration test passed
- Disaster recovery tested
- On-call rotation active

---

## 20. How We Deploy

### Application Rollout

**Rolling deployment with canary**:

1. Deploy to 1 canary pod (5% traffic). Monitor 10 minutes.
2. If error rate < 2%: roll to 25% of pods. Monitor 15 minutes.
3. If stable: roll to 100%. Monitor 30 minutes.
4. If degraded at any step: automatic rollback.

**Automatic rollback triggers**:
- Health check fails 3 consecutive times
- Error rate > 5% within 10 minutes of deploy
- Memory > 90% within 5 minutes of deploy

Kafka consumers automatically rebalance after rollback. No manual intervention needed.

### Data Rollback Plan

A bad deploy can corrupt data, not just cause errors. Here's how we handle each data tier:

**Kafka:**
- Consumer offsets are committed only after successful processing (at-least-once semantics)
- On rollback: consumers restart from last committed offset — messages are reprocessed, not lost
- If a bad deploy produces corrupt messages to downstream topics: reset consumer group offsets to timestamp before deploy using `kafka-consumer-groups --reset-offsets --to-datetime`
- Corrupt messages already in topics: publish tombstones or route to a quarantine topic via a cleanup consumer

**PostgreSQL:**
- All writes use idempotent upserts (keyed on `url_hash`) — reprocessing the same URL overwrites with correct data
- If schema migration corrupts data: restore from point-in-time recovery (PITR) — RDS continuous backups with 5-minute granularity
- If bad classification logic writes incorrect results: run a correction job that re-crawls affected URLs (identified by `updated_at` within the bad deploy window)
- Partitioned tables allow dropping an entire partition if a time range is fully corrupt

**Elasticsearch:**
- ES is a derived index, not source of truth — can always be rebuilt from PostgreSQL
- If bad deploy writes corrupt documents: delete the affected index, recreate from PG using the bulk indexing writer
- Index aliases allow deploying a new index in parallel, then swapping the alias atomically
- If partial corruption: use `_update_by_query` to fix specific fields, filtered by `indexed_at` timestamp

**Recovery playbook (ordered steps):**
1. Trigger application rollback (automatic or manual)
2. Identify corruption window: `deploy_start_time` to `rollback_time`
3. Kafka: reset consumer offsets to `deploy_start_time` if downstream topics are affected
4. PostgreSQL: if data is fixable, run correction job; if not, PITR restore to `deploy_start_time`
5. Elasticsearch: reindex from PostgreSQL for the affected time window
6. Verify: run reconciliation job, compare PG vs ES row counts and checksums

---

## 21. Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Anti-bot blocking reduces crawl success | High | Medium | Accept for Beta. Evaluate proxy providers in Sprint 6. Have NLP-only fallback. |
| NLP quality too low for some domains | Medium | High | Manual evaluation early (Sprint 6). LLM escalation for high-value pages. |
| Kafka operational complexity | Medium | Medium | Use managed Kafka (MSK). Start simple — add complexity incrementally. |
| LLM costs exceed budget | Medium | Medium | Budget circuit breaker from day one. Selective usage. Batch API. |
| JS-rendered pages return empty content | High | Medium | Document limitation. OG tags as fallback. Playwright in GA phase. |
| Kubernetes learning curve | Medium | Low | Start with simple deployments. Add HPA/PDB incrementally. Use managed EKS. |
| Infrastructure costs exceed budget | Medium | High | Reserved instances, spot workers, right-sizing after load test. Monthly cost reviews. |

---

## 22. Team & Dependencies

### Team Roles and Ownership

**Team**: 1 Senior Backend (Senior BE), 1 Mid Backend (Mid BE), 1 DevOps Engineer (DevOps).

| Area | Primary Owner | Secondary |
|------|--------------|-----------|
| API layer, auth, rate limiting | Senior BE | Mid BE |
| Kafka integration, consumer design | Senior BE | Mid BE |
| Ingestion pipelines (file, MySQL) | Mid BE | Senior BE |
| NLP/LLM classification workers | Senior BE | — |
| Search (Elasticsearch) | Mid BE | Senior BE |
| Campaign management | Mid BE | Senior BE |
| CI/CD pipeline | DevOps | Senior BE |
| Kubernetes, Helm charts, HPA | DevOps | — |
| Monitoring (Prometheus/Grafana/Loki) | DevOps | Senior BE |
| Database migrations | Mid BE | Senior BE |
| Secrets management | DevOps | Senior BE |
| Security review coordination | Senior BE | DevOps |
| Load/chaos testing | DevOps | Mid BE |

### Parallelism Opportunities

Phases are designed so team members can work in parallel:

- **Phase 1**: Senior BE (auth, shutdown, health) ∥ Mid BE (rate limiting, logging, normalization) ∥ DevOps (CI, Docker, secrets)
- **Phase 2**: Senior BE (Kafka producer, fetch worker) ∥ Mid BE (poison handling, integration tests) ∥ DevOps (Kafka cluster, staging)
- **Phase 3**: Senior BE (Bloom filter, NLP/LLM workers) ∥ Mid BE (file/MySQL ingestion, rate limiting) ∥ DevOps (S3 setup)
- **Phase 4**: Senior BE (bulk indexer, campaign progress) ∥ Mid BE (search API, campaign CRUD, DLQ) ∥ DevOps (ES cluster, PgBouncer)
- **Phase 5**: Senior BE (instrumentation, dashboards) ∥ Mid BE (S3 ETL) ∥ DevOps (EKS, Helm, HPA, Loki)

### Bottleneck Risks

| Risk | Why | Mitigation |
|------|-----|------------|
| Senior BE is on critical path for Kafka design | Consumer architecture decisions gate all workers | Timebox design to 3 days; Mid BE shadows and can take over implementation |
| DevOps is sole owner of Kubernetes | No redundancy | Senior BE learns basics; DevOps documents all runbooks by Phase 5 |
| Mid BE ramping on Kafka | May slow Phase 2 contributions | Senior BE pairs with Mid BE on first consumer; Mid BE owns subsequent consumers |

### External Dependencies

| What | From Whom | When | Fallback if Delayed |
|------|-----------|------|---------------------|
| MySQL read replica credentials | Data Engineering | Phase 3 | Mock with file-based ingestion |
| S3 bucket + IAM roles | Platform | Phase 3 | Local filesystem for dev/test |
| EKS cluster | Platform | Phase 5 | Continue on Docker Compose staging |
| PagerDuty service | SRE | Phase 5 | Email alerts as interim |
| Security review | Security | Phase 6 | Internal self-review, defer external |
| DNS for public API | Platform | Phase 6 | IP-based access for Beta |
