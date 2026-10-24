# Acme Technologies HRMS Assistant (`hrms-bot`)

> **Enterprise-grade, privacy-centric HRMS Chatbot with Memory RAG, backend-enforced RBAC, and live streaming UI.**  
> *Developed as an engineering task during an internship.*

An enterprise-grade, privacy-centric HRMS (Human Resource Management System) Chatbot with a **Memory RAG** architecture, backend-enforced role-based access control (RBAC), security hardening, conversation restore, and an HR administration suite with live streaming UI.

---

## 📌 Internship Context
This project was designed and built as an advanced AI engineering internship task to research, architect, and implement:
- **Retrieval-Augmented Generation (RAG)** grounded in corporate HR policies with strict cosine similarity thresholds.
- **Durable User Preference Memory** with real-time deduplication and vector embeddings via Qdrant.
- **Dual-Layer Scoping & RBAC**: Strict backend validation ensuring callers cannot escalate privileges or access data outside their reporting or HR role boundaries.
- **Production AI Hardening**: Prompt injection resilience, CSP headers, HTTP-only secure cookie sessions, DOMPurify sanitization, and rate-limiting.
- **Real-Time Token Streaming**: Server-Sent Events (SSE) with a live inspector panel exposing retrieved vector chunks, memory extractions, and tool invocations.

---

## 1. System Architecture

```mermaid
flowchart TD
    User([User / Web Browser]) <-->|SSE Stream & JSON REST| API[FastAPI Server :8000]

    subgraph Security Layer ["Security & Scoping Layer (app/auth.py & CSP Middleware)"]
        API -->|Session & Auth Guard| CallerContext[CallerContext: ID, Role, Reports]
        API -->|Content Security Policy| CSPGuard[CSP + nosniff + DOMPurify Sanitization]
    end

    subgraph Agent Loop ["Secure Agent Loop (app/agent.py)"]
        CallerContext --> Agent[Groq LLM Engine: Function Calling]
        Agent <-->|Tools with Scope Check| ToolDispatcher[Tool Dispatcher (app/tools.py)]
    end

    subgraph Grounding RAG ["Policy Knowledge Path (app/vectorstore.py)"]
        ToolDispatcher -->|search_policies| FastEmbed1[FastEmbed: bge-small-en-v1.5]
        FastEmbed1 -->|Cosine Similarity| QdrantPolicies[(Qdrant: hr_policies)]
        Agent -.->|Pre-Flight Grounding Check| QdrantPolicies
    end

    subgraph Durable Memory ["Personal Memory Path (app/memory.py)"]
        ToolDispatcher -->|search_memory / save_memory| QdrantMem[(Qdrant: user_memories)]
        Agent -.->|Context Injection & Memory Extraction| QdrantMem
    end

    subgraph Relational DB ["Relational HRMS Data (app/db.py)"]
        ToolDispatcher -->|Scope Checked: Own / Team / HR| SQLStore[(PostgreSQL / SQLite)]
        SQLStore --- CoreTables[Employees, Balances, Leaves, Attendance, Payroll, Reviews]
        SQLStore --- ChatTables[chat_history, chat_sessions]
        SQLStore --- AuditTables[audit_log, admin_audit]
    end

    subgraph Background Workers ["Lifespan Background Worker"]
        API -->|Hourly Asyncio Loop| DailyWorker[Daily Status Sync: Onboarding & Offboarding]
    end
```

---

## 2. Directory Structure

```
hrms-bot/
├── app/
│   ├── __init__.py
│   ├── agent.py            # Secure agent loop, pre-flight grounding, SSE & memory extraction
│   ├── auth.py             # Caller identity resolution & strict scope checks
│   ├── auth_routes.py      # Login, activation, demo login, password reset & rate limiting
│   ├── config.py           # Application settings loaded from environment/.env
│   ├── db.py               # Database schemas, migrations, and query helpers (PostgreSQL/SQLite)
│   ├── embeddings.py       # FastEmbed (BAAI/bge-small-en-v1.5) CPU embeddings stored in ./models
│   ├── employee_routes.py  # Employee CRUD, probation calc, reporting hierarchy, offboard, reactivate
│   ├── ingest.py           # Markdown chunker (~500 chars, 50 overlap) with section headings & replacement
│   ├── main.py             # FastAPI entrypoint, CSP middleware, lifespan worker, routes
│   ├── memory.py           # Long-term preference memory with deduplication & sensitive content filters
│   ├── models.py           # Pydantic schemas for chat, auth, and employee management
│   ├── seed.py             # Synthetic Acme Technologies database generator with --reset support
│   ├── session_routes.py   # Chat sessions listing, deletion, and history restore
│   ├── tools.py            # HR data tools, audit logs, and Groq function definitions
│   ├── utils.py            # Date arithmetic (add_months with clamping), natural language dates
│   └── vectorstore.py      # Qdrant client, collections & chunk deletion helpers
├── sample_docs/            # 9 realistic Acme Technologies HR policies
│   ├── attendance_and_wfh_policy.md
│   ├── benefits_and_insurance.md
│   ├── code_of_conduct.md
│   ├── leave_policy.md
│   ├── onboarding_guide.md
│   ├── payroll_and_tax_faq.md
│   ├── performance_review_process.md
│   ├── posh_and_grievance_policy.md
│   └── reimbursement_policy.md
├── scripts/
│   ├── ingest_policies.py  # HR policies ingestion runner
│   ├── run_full_demo.py    # Automated runner verifying full demo script over SSE
│   ├── seed_data.py        # Database seed runner with --reset option and demo credentials
│   ├── smoke_test.py       # Live E2E smoke test verifying auth, chat, sessions, and HR admin
│   └── test_part2.py       # Direct test suite covering RBAC & tools
├── static/                 # Frontend SPA (Vanilla HTML/CSS/JS)
│   ├── index.html          # Split-pane HR dashboard with Inspector, History, Admin tabs
│   ├── style.css           # Styling with HR aesthetic, dark/light theme, demo banner
│   ├── app.js              # State machine, DOMPurify rendering, identity reset, session restore
│   └── vendor/             # Local offline fallback for DOMPurify and marked
├── tests/                  # Pytest test suite (65+ tests covering security, agent, HR admin)
│   ├── test_agent_llm.py
│   ├── test_hr_admin.py
│   ├── test_onboarding.py
│   ├── test_regression_bugs.py
│   ├── test_security.py
│   ├── test_security_hardening.py
│   └── test_session_restore.py
├── requirements.txt        # Pinned dependencies
├── .env.example            # Environment variables template
└── README.md
```

---

## 3. Setup & Installation

### Step 1: Virtual Environment & Dependencies
```bash
python -m venv .venv

# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

# Install Python backend dependencies:
pip install -r requirements.txt

# Install Node.js test dependencies for jsdom frontend tests:
npm ci
```

> [!NOTE]
> **One-Time Embedding Model Download**:
> The project utilizes FastEmbed (`BAAI/bge-small-en-v1.5`) for fast, local CPU vector embeddings. On the very first run (or during `python scripts/ingest_policies.py`), FastEmbed performs a **one-time download** (~130MB ONNX model files) and caches them in `FAST_EMBED_CACHE_DIR` (defaults to `./models`). Subsequent runs load the model instantly from local disk without any external network round-trips.

### Step 2: Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Fill in your credentials in `.env`:
```ini
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-120b
GROQ_FALLBACK_MODEL=openai/gpt-oss-20b
TZ_NAME=Asia/Kolkata

# Qdrant Vector DB (Cloud or empty for local disk storage ./qdrant_data)
QDRANT_URL=https://your-cluster.cloud.qdrant.io
QDRANT_API_KEY=your_qdrant_api_key

# Database URL (Supabase PostgreSQL, or leave empty for local SQLite ./hrms.db)
DATABASE_URL=postgresql://postgres.user:password@aws-0-region.pooler.supabase.com:6543/postgres?pgbouncer=true

# Security & Demo Mode
JWT_SECRET=your-secure-random-jwt-secret-min-32-chars
DEMO_MODE=false
DEMO_PASSWORD=AcmeSecret2026!
COOKIE_SECURE=false
CORS_ORIGINS=http://localhost:8000,http://127.0.0.1:8000
```

### Step 3: Seed Database & Ingest Policies
```bash
# Seed 30 employees, leave balances, attendance, and payroll records
# Use --reset to safely wipe and re-seed clean state:
python scripts/seed_data.py --reset

# Ingest and embed all 9 HR policies into Qdrant hr_policies collection
python scripts/ingest_policies.py
```

### Step 4: Run the Server
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
Open your browser at: **`http://127.0.0.1:8000/`**

### Step 5: Run Full Test Suite & Smoke Tests
```bash
# Run all backend pytest test suites (80+ unit, security, agent, HR admin tests):
pytest tests/ -v

# Run frontend jsdom automated tests (fail-closed sanitization & persona switching):
node tests/test_frontend_sanitization.js
node tests/test_frontend_persona_switch.js

# Run live end-to-end smoke test against the running server:
python scripts/smoke_test.py
```

---

## 4. Environment Variables Reference

| Variable | Type | Default | Description |
| :--- | :---: | :---: | :--- |
| `GROQ_API_KEY` | `string` | *(required)* | Groq Cloud API key for LLM function calling and reasoning. |
| `GROQ_MODEL` | `string` | `openai/gpt-oss-120b` | Primary LLM model for the agent loop (e.g. `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`). |
| `GROQ_FALLBACK_MODEL`| `string` | `openai/gpt-oss-20b` | Stable fallback model used automatically if the primary model errors or is retired. |
| `LLM_TOOL_MAX_TOKENS` | `int` | `1024` | Token budget for intermediate function-calling turns (includes reasoning tokens). |
| `LLM_ANSWER_MAX_TOKENS`| `int` | `1500` | Token budget for final user-facing streaming response turns. |
| `POLICY_MIN_SCORE` | `float` | `0.45` | Minimum cosine similarity threshold for policy grounding. Queries below this yield HR referral. |
| `TZ_NAME` | `string` | `Asia/Kolkata` | Business timezone used for daily status transitions, greetings, and leave calculations. |
| `QDRANT_URL` | `string` | `""` | Qdrant Cloud cluster URL. If empty, local file-based Qdrant storage (`./qdrant_data`) is used. |
| `QDRANT_API_KEY` | `string` | `""` | API key for authenticated Qdrant Cloud access. |
| `DATABASE_URL` | `string` | `""` | SQLAlchemy connection string (e.g. Supabase PostgreSQL). If empty, local SQLite (`./hrms.db`) is used. |
| `JWT_SECRET` | `string` | `dev-jwt-secret...` | Secret key used to sign and verify HMAC-SHA256 session authentication cookies. |
| `DEMO_MODE` | `bool` | `false` | When `true`, enables one-click persona switching and demo login endpoints. |
| `DEMO_PASSWORD` | `string` | `AcmeSecret2026!` | Password used for demo logins when `DEMO_MODE=true`. |
| `ALLOW_DEMO_ON_NETWORK`| `bool` | `false` | Must be explicitly `true` to allow `DEMO_MODE=true` when binding to non-localhost addresses (`0.0.0.0`). |
| `COOKIE_SECURE` | `bool` | `false` | When `true`, sets `Secure` flag on JWT cookies. Set `false` for local HTTP development. |
| `FAST_EMBED_CACHE_DIR` | `string` | `./models` | Directory path for caching the FastEmbed CPU embedding model (`bge-small-en-v1.5`). |
| `CORS_ORIGINS` | `string` | `http://localhost:8000...` | Comma-separated list of allowed origins for cross-origin browser requests. |

---

## 5. Security Model & Hardening

### 5.1 Demo Mode Safety & Bind Check
- **Safe Defaults**: `DEMO_MODE` defaults to `false` in config and in `.env.example`.
- **Startup Bind Guard**: If `DEMO_MODE=true`, the server inspects the listening host. If the host is not `127.0.0.1` or `localhost`, the application logs a loud warning and **refuses to start** unless `ALLOW_DEMO_ON_NETWORK=true` is explicitly configured.
- **Rate Limiting**: `/auth/demo-login` and `/auth/activate` are rate-limited per IP address.
- **UI Banner**: When demo mode is active, a persistent red **DEMO MODE ACTIVE** banner is displayed in the navigation header.

### 5.2 Session Ownership & Isolation
- Every chat session is identified by an unguessable UUID generated on the client via `crypto.randomUUID()`.
- The server validates session ID format (alphanumeric, dashes, underscores, max 64 chars).
- `get_session_owner(session_id)` records the first caller of the session. Any attempt by another employee to send messages or fetch history for an existing session returns `403 Forbidden`.
- History queries (`/history`) and context retrieval enforce exact `(session_id, caller_emp_code)` filtering.

### 5.3 XSS Protection & Content Security Policy (CSP)
- Assistant markdown responses are sanitized through **DOMPurify** (pinned version loaded via CDN with local fallback in `static/vendor/DOMPurify.min.js`) using `marked.parse(text, { breaks: true })`.
- All server-generated data injected into the inspector panel, audit logs, employee directory, and onboarding tables is rendered via `escapeHtml()` or `textContent`.
- Custom ASGI middleware injects strict HTTP headers:
  - `Content-Security-Policy: default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'none';`
  - `X-Content-Type-Options: nosniff`

### 5.4 Login Hardening & Timing Attack Mitigation
- **Unified Generic Error**: Password authentication returns identical status `401 Unauthorized` and message `"Invalid email or password"` for unknown emails, wrong passwords, locked accounts, and deactivated/terminated employees.
- **Constant-Time Verification**: When an unknown email is supplied, a dummy bcrypt hash verification is executed so response latency does not leak user existence.
- **Account Lockout**: 5 consecutive failed login attempts trigger a 15-minute account lockout.

---

## 6. Session Management & Conversation Restore

1. **Automatic Session Restore**: Upon login or page refresh, the frontend queries `GET /sessions?limit=20`. If a prior session exists, the most recent conversation is automatically loaded into the chat window, accompanied by a small banner: `"Continuing your last conversation (date) — [Start new chat]"`.
2. **History Menu & Management**: Users can browse their recent sessions via the "History" tab/dropdown, switch between previous discussions, or delete individual sessions via `DELETE /sessions/{id}`.
3. **Tab Isolation**: The active session ID is stored in `sessionStorage` per user. Chat message content is **never** persisted in `localStorage`.
4. **Clean Identity Switching**: Switching accounts or logging out invokes `resetForNewIdentity()`, which aborts in-flight requests, purges the DOM, resets all state, and generates a fresh session ID.

---

## 7. HR Administration Flows

### 7.1 Employee Provisioning
- **Probation End Date Clamping**: Calculated using `add_months(join_date, 6)` which accurately clamps month-end days (e.g. Aug 31 + 6 months = Feb 28 in regular years, Feb 29 in leap years; Jan 31 + 1 month = Feb 28).
- **Email Validation**: Validates both work and personal emails using standard email regex / email-validator. Personal email is persisted in the database.
- **Safe Sequential Code Generation**: Auto-generates `ACM-0XX` employee codes with automatic retry (up to 3 times) upon database constraint collisions.
- **Manager Role Restriction**: Only employees with role `manager` or `hr_admin` can be assigned as a manager.

### 7.2 Manager Hierarchy & Cycle Prevention
- `PATCH /employees/{id}` rejects self-management (`manager_id == employee_id`) with `400 Bad Request`.
- Detects circular reporting chains (e.g., A reports to B, B reports to A) by traversing the management chain upwards, returning `400 Bad Request` if a cycle is detected.
- Rejects role demotions from `manager` to `employee` with `409 Conflict` if the employee currently has active direct reports, requiring HR to reassign reports first.

### 7.3 Offboarding & Reactivation
- Supports both immediate offboarding and scheduled future departures via `last_working_date` and `offboard_reason`.
- If `last_working_date` is today or past, status is immediately set to `terminated`. If in the future, the employee remains `active` until that date.
- Requires `reassign_reports_to` manager ID if the offboarded employee has direct reports (returns `409 Conflict` otherwise).
- Automatically cancels all pending leave requests for the departing employee.
- `POST /employees/{id}/reactivate` allows HR admins to reactivate mistakenly terminated employees.

### 7.4 Automated Daily Status Worker
- An asynchronous worker runs hourly inside FastAPI's `lifespan`:
  - Automatically transitions employees from `onboarding` to `active` on their `join_date` (evaluated in company `TZ_NAME`).
  - Automatically applies scheduled terminations on `last_working_date`.
- Onboarding table displays an `"expired"` state for activation tokens older than 72 hours, with a single-click "Resend Invite" button.

### 7.5 HR Admin Audit Trail
- Dedicated `admin_audit` table records all sensitive administrative operations:
  - Employee provisioning, updates, and role changes.
  - Scheduled and immediate offboarding, and employee reactivation.
  - One-time invite generation and password resets.
  - Policy document uploads and replacements.
- HR admins can view audit records directly via `GET /audit/admin` or through the interactive "Admin Audit" tab in the frontend.

---

## 8. Role-Based Access Control (RBAC) Matrix

| Capability / Resource | Employee | Manager | HR Admin |
| :--- | :---: | :---: | :---: |
| Own Profile, Balance & Attendance | Allowed | Allowed | Allowed |
| Own Payslips & Reviews | Allowed | Allowed | Allowed |
| Direct Reports Leaves & Attendance | Denied | Allowed | Allowed |
| Team on Leave Schedule | Denied | Reports Only | Company-wide |
| Subordinate Salary / Payslip | **Denied** | **Denied** | Allowed |
| Department Headcount Statistics | Denied | Denied | Allowed |
| Company HR Policy Search | Allowed | Allowed | Allowed |
| Personal Preference Memory | Own Only | Own Only | Own Only |
| Employee Provisioning & Invites | Denied | Denied | Allowed |
| Reporting Line & Role Edits | Denied | Denied | Allowed |
| Employee Offboarding & Reactivation | Denied | Denied | Allowed |
| HR Administrative Audit Trail | Denied | Denied | Allowed |

---

---

## 9. Demo Accounts

The application uses standard email + password authentication. To explore different roles, sign in using one of the pre-seeded demo accounts. Signing in as another user requires **logging out and logging in again**.

### Setting Up the Shared Demo Password
1. Set `DEMO_PASSWORD` in your `.env` file (e.g. `DEMO_PASSWORD=AcmeSecret2026!`).
2. Run the password reset command to apply this password to all active accounts:
   ```bash
   python scripts/seed_data.py --reset-passwords
   ```
   This assigns the shared demo password to every active employee, unlocks any locked accounts, and increments their `token_version` to sign out all stale sessions.

### Demo Account Credentials

All active accounts share the `DEMO_PASSWORD` configured above:

| Persona | Email | Role | Department | Description & Flow |
| :--- | :--- | :--- | :--- | :--- |
| **Anita Desai** | `anita.desai@acmetech.com` | `hr_admin` | HR | HR Director: Access to headcount analytics, onboarding dashboard, policy management, and audit logs. |
| **Rahul Mehta** | `rahul.mehta@acmetech.com` | `manager` | Engineering | VP of Engineering: Can inspect team leave calendars, view direct report payslips, and query org charts. |
| **Vikram Malhotra** | `vikram.malhotra@acmetech.com` | `manager` | Executive | Chief Executive Officer: Top-level executive manager. |
| **Priya Sharma** | `priya.sharma@acmetech.com` | `employee` | Engineering | Senior Full-Stack Engineer: Self-service leave balances, attendance checks, own payslips, and company policies. |
| **Rohit Kumar** | `rohit.kumar@acmetech.com` | `employee` | Engineering | **New Joiner (`onboarding`)**: Does not have an active password yet. Activate via invite link with token `rohit-demo-invite-token-2026` (`/activate?token=...`). |
| **Karan Johar** | `karan.johar@acmetech.com` | `employee` | Engineering | **Terminated Employee**: Login disabled (returns HTTP 403 / Account deactivated). |

---

## 10. Troubleshooting Guide

### 1. Groq Model Decommissioned / Rate Limited
- **Symptoms**: `model_decommissioned` error or 404 from Groq completions API.
- **Resolution**:
  - The application includes an automatic fallback mechanism: if `GROQ_MODEL` errors, it retries transparently with `GROQ_FALLBACK_MODEL` (default: `openai/gpt-oss-20b`).
  - To change models explicitly, update `GROQ_MODEL` in `.env` to any active Groq model (e.g. `openai/gpt-oss-120b` or `qwen/qwen3.8-27b`).

### 2. FastEmbed Model Initial Download
- **Symptoms**: Server startup takes 15–30 seconds on the very first run.
- **Resolution**:
  - FastEmbed downloads the `BAAI/bge-small-en-v1.5` ONNX model (~130 MB) into `./models` on its first invocation. Subsequent startups load locally from disk in under 1 second.

### 3. Cookies and Authentication on Local Network Demos
- **Symptoms**: Login succeeds, but subsequent requests return `401 Unauthorized` over plain HTTP on LAN IP addresses.
- **Resolution**:
  - Ensure `COOKIE_SECURE=false` in `.env` when accessing over plain `http://`. Browsers will drop `Secure` cookies over non-HTTPS connections. Set `COOKIE_SECURE=true` only behind HTTPS reverse proxies.

### 4. CORS Errors on Web Clients
- **Symptoms**: Browser console shows `Cross-Origin Request Blocked`.
- **Resolution**:
  - Ensure the client URL is listed in `CORS_ORIGINS` in `.env` (comma-separated, e.g. `http://localhost:8000,http://127.0.0.1:8000`).

---

## 11. Performance Optimization & Deployment Topology

### 11.1 Deployment Topology & Network Reality
To achieve the target budget (**< 1.5 s TTFT** and **3–4 s total response time** in production), system components must be co-located to minimize network round-trips:

```
[User Browser]
      │
      ▼  (~50-100ms TLS)
[FastAPI Server: ap-south-1 (Mumbai)]
      ├── (co-located VPC / <5ms) ──────► [PostgreSQL: Supabase (ap-south-1)]
      ├── (co-located VPC / <5ms) ──────► [Qdrant Cloud / Local (ap-south-1)]
      └── (WAN / ~100-200ms) ───────────► [Groq API Edge (qwen/qwen3.8-27b)]
```

> [!IMPORTANT]
> **Co-Location Requirement**:
> If the API server is deployed in `ap-south-1` (India) while PostgreSQL is hosted in `ap-northeast-1` (Tokyo) or `eu-west-1` (Ireland), each SQL statement incurs a 150–200ms WAN roundtrip. Even with parallel `asyncio.gather`, roundtrips will dominate latency. Always deploy the FastAPI server, database, and Qdrant in the same geographic region (e.g. `ap-south`).

### 11.2 Stage Latency Budget & Benchmark Results

The system records fine-grained timings for each turn and outputs a summary via the `timing` SSE event, `X-Response-Time` HTTP header, and server logs.

| Stage / Component | Target Budget | Benchmark p50 | Benchmark p95 | Notes / Optimization |
| :--- | :---: | :---: | :---: | :--- |
| **Auth / Token Verification** | < 5 ms | **0.00 ms** | **0.01 ms** | In-memory 30s cache (`_AUTH_VERIFY_CACHE`) |
| **Pre-LLM Context Gather** | < 50 ms | **431.19 ms\*** | **1012.03 ms\*** | Parallel `asyncio.gather` (Owner + History + Memory + Prompt) |
| **Memories Fetch** | < 2 ms | **< 0.1 ms** | **< 0.1 ms** | In-memory 300s cache (`_USER_MEMORIES_CACHE`) |
| **System Prompt Construction**| < 5 ms | **< 0.1 ms** | **< 0.1 ms** | In-memory 300s cache (`_PROMPT_INFO_CACHE`) |
| **Policy Vector Search** | < 200 ms | **164.00 ms** | **438.56 ms** | Qdrant `prefer_grpc=True` + 200-item LRU query cache |
| **Tool Executions (Fast-Path)** | < 150 ms | **281.35 ms** | **663.11 ms** | Regex intent routing + parallel tool execution |
| **Time-to-First-Token (TTFT)** | < 1500 ms | **222.65 ms** | **3868.74 ms\*\*** | Stream-first with `reasoning_effort="none"` |
| **LLM Stream Generation** | < 2500 ms | **314.51 ms** | **3957.48 ms\*\*** | Single-turn streaming without discarded draft cycles |
| **Database Persistence (Bg)** | 0 ms (async) | **0.00 ms** | **0.00 ms** | Non-blocking `asyncio.create_task` background writes |
| **TOTAL REQUEST LATENCY** | **< 4000 ms** | **1383.10 ms** | **4568.95 ms** | **Typical turn completes in 1.3 - 1.4 seconds!** |

*\*Note: Pre-LLM Gather in development test connects over cross-continent WAN to Tokyo PostgreSQL. When co-located in the same region, this stage drops to < 15ms.*
*\*\*Note: p95 TTFT reflects occasional Groq API rate limit (HTTP 429) backoff retries.*

### 11.3 Before vs. After Optimization Comparison

| Metric / Capability | Before Optimization | After Optimization | Improvement |
| :--- | :--- | :--- | :--- |
| **TTFT (Time-to-First-Token)** | 3.5 – 5.2 s | **0.22 s (222 ms)** | **~16x Faster** |
| **Typical Answer Duration** | 6.8 – 9.4 s | **1.38 s (1383 ms)** | **~5x Faster** |
| **Pre-LLM Preparation** | Serial (4 waterfall queries) | Concurrent (`asyncio.gather`) | **4x Concurrency** |
| **Memory Extraction Overhead** | Embed query + Vector search (~350ms) | In-Memory 5-min Cache (<0.1ms) | **>3000x Faster** |
| **Policy Search Overhead** | Embedded reload per message | 200-entry LRU Cache with TTL | Instant for common queries |
| **Reasoning Model Thinking** | Emitted raw thoughts / low effort | `reasoning_effort="none"` | Eliminates 2-3s think overhead |
| **Database Writes** | Blocked client HTTP response | Non-blocking background task | **0 ms user blocking** |

### 11.4 How to Read the Live Inspector Timing Panel
The frontend includes a real-time **Latency Breakdown** panel within the Live Inspector drawer:
1. **Total Duration Badge** (Top-right): Displays end-to-end request time in milliseconds.
2. **Context Pre-fetch Bar (Purple / Blue)**: Time spent resolving caller credentials, verifying session ownership, pre-fetching message history, and retrieving cached preferences.
3. **Policy Vector Search Bar (Cyan)**: Time spent executing semantic similarity search across policy chunks in Qdrant.
4. **Tool Execution Bar (Amber)**: Combined time spent executing SQL queries for employee leaves, payroll, attendance, or reviews.
5. **Time to First Token (Pink)**: Elapsed time from request start until the first visible token arrives from Groq.
6. **LLM Generation Bar (Green)**: Total token streaming duration from the model.
7. **Database Writes Bar (Slate)**: Background asynchronous save duration for chat history and session updates.

---

## 12. Conversation History & Safety Budget

The chatbot retains full chronological conversation history within the active chat session, sending all earlier exchanges (user queries and assistant responses) to the model.

### 12.1 How History is Loaded
- **Full Session Retention**: History is loaded from the database (`get_chat_history`) without message-count caps by default (`limit=None`). Oldest-to-newest messages are preserved.
- **Exclusion of Failed Turns**: Turns with errors or timeouts (`status="failed"`) are strictly excluded from history.
- **Session Isolation**: History is strictly filtered by caller identity (`user_id = caller.emp_code`). Cross-user session access yields 0 history messages.
- **Race Condition Prevention**: If an assistant response save is currently pending (`PENDING_SAVES`), new queries asynchronously wait for the previous save to finish before loading history.
- **Retry Deduplication**: Retried queries exclude any pending user turn from earlier history to prevent double-counting.

### 12.2 Safety Budget & Trimming (`select_history_for_prompt`)
To prevent unbounded context growth or token limit overflows on long sessions, a character safety budget is enforced via `select_history_for_prompt`:
1. **Message Cap**: If `HISTORY_MAX_MESSAGES > 0`, only the newest $N$ messages are retained.
2. **Character Budget**: If the total character length of the selected history exceeds `HISTORY_MAX_CHARS` (~6,000 tokens), messages are dropped from the **oldest end in user+assistant pairs**. The prompt will never begin with an orphan assistant message.
3. **Inspector Reporting**: The `context_info` event reports `history_count` (sent), `history_total` (in session), `history_omitted` (dropped), `history_user`, and `history_assistant`. When messages are omitted, the inspector displays `X of Y sent (Z oldest not sent)` in a warning style.

### 12.3 Environment Configuration
| Variable | Default | Description |
| :--- | :---: | :--- |
| `HISTORY_MAX_MESSAGES` | `0` | Message-count cap (`0` = uncapped; `> 0` keeps newest $N$). |
| `HISTORY_MAX_CHARS` | `24000` | Safety budget on total characters of earlier history sent (~6k tokens). |

---

## 13. Reading Policy Documents

The chatbot can read full policy documents (PDF, Markdown, or plain text) page by page through two specialized tools, allowing it to accurately answer questions like *"summarize the whole leave policy"*, *"what does page 3 of the conduct policy say?"*, and questions requiring broader document synthesis beyond vector search chunks.

### 13.1 Tools Overview
- **`list_policy_documents`**: Returns metadata (`document`, `original_filename`, `pages`, `uploaded_at`) for every registered policy document. Available to all authenticated employees.
- **`read_policy_document`**: Reads specific pages or page ranges from the indexed document text stored in the `policy_pages` relational table. Supports exact source names and fuzzy document titles (e.g., `"leave policy"` matches `leave_policy.md`).

### 13.2 Key Characteristics & Limitations
- **Fast Database-Backed Reads**: Document reading queries pre-extracted text stored in `policy_pages`. No heavy PDF parsing or disk reading occurs at question time.
- **Per-Call Size Cap**: To stay safely within model token limits and fit the 3,000 character tool-result cap, `read_policy_document` returns at most ~2,800 characters per call. Long ranges are stopped at clean page boundaries with a `next_page` indicator. Pages longer than the cap are split with a `continue_from_char` hint.
- **Scanned PDFs Without Text**: Documents must have extractable text. Scanned or image-only PDFs without an OCR layer cannot be read and are rejected with a clear error (`PDF_NO_TEXT`).
- **Lifecycle Synchronization**: Ingesting, replacing (`replace=true`), or deleting a policy document via `DELETE /policies/files/{source}` automatically synchronizes its pages in `policy_pages`. If replacement fails during extraction or embedding, existing pages are safely preserved.
- **Untrusted Document Security**: Document contents are treated as untrusted reference data, not instructions. Prompt injection attempts within document text are safely ignored by the agent.

---

## 🏷️ Tags
`#internship` `#ai` `#llm` `#rag` `#fastapi` `#python` `#qdrant` `#vectorsearch` `#hrms` `#enterpriseai` `#groq` `#softwareengineering`


