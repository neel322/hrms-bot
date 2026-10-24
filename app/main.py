import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from datetime import date
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.agent import get_prompt_info_cached, run_agent
from app.auth import CallerContext, clear_caller_cache, get_current_caller, resolve_caller
from app.auth_routes import auth_router
from app.employee_routes import employee_router
from app.config import get_settings
from app.db import (
    ChatHistory,
    Employee,
    add_chat_message,
    delete_chat_session,
    delete_policy_document,
    get_admin_audit_logs,
    get_chat_history,
    get_engine,
    get_policy_documents,
    get_session,
    get_session_owner,
    get_table_counts,
    get_user_chat_sessions,
    init_db,
    log_admin_audit,
    ping_database_keepalive,
    record_policy_document,
    upsert_chat_session,
    warmup_database_pool,
)
from app.ingest import (
    EmptyDocument,
    PdfCorrupt,
    PdfEncrypted,
    PdfNoText,
    process_and_ingest_document,
)
from app.utils import get_current_date
from app.memory import clear_user_memories, get_user_memories_cached, list_user_memories
from app.timer import StageTimer
from app.models import (
    ChatMessageResponse,
    ChatRequest,
    EmployeeResponse,
    HealthResponse,
    IngestResponse,
    MemoryResponse,
    SearchPolicyResult,
)
from app.rate_limiter import chat_ip_limiter, chat_user_limiter, get_client_ip
from app.tools import get_audit_logs
from app.vectorstore import (
    QdrantTimeout,
    clear_policy_cache,
    delete_chunks_by_source,
    get_qdrant_client,
    init_vectorstore,
    search_policies,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hrms-bot")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SAMPLE_DOCS_DIR = os.path.join(BASE_DIR, "sample_docs")

SESSION_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_session_id(session_id: str) -> None:
    """Enforces unguessable, validated session IDs (max 64 chars, [A-Za-z0-9_-])."""
    if not session_id or not SESSION_ID_REGEX.match(session_id):
        raise HTTPException(
            status_code=400,
            detail="Invalid session_id. Must be 1-64 characters matching [A-Za-z0-9_-].",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initializes database schema, Qdrant collections, and pre-warms cache on startup."""
    settings = get_settings()

    # JWT secret enforcement
    if not settings.demo_mode and (not settings.jwt_secret or not settings.jwt_secret.strip()):
        raise RuntimeError("JWT_SECRET must be configured in .env when DEMO_MODE is false!")

    # Demo mode safety bind-check
    if settings.demo_mode:
        logger.warning("DEMO_MODE is ENABLED! /auth/demo-login is active. Do NOT use in production.")
        if not settings.allow_demo_on_network:
            host = os.environ.get("HOST", settings.host)
            for i, arg in enumerate(sys.argv):
                if arg == "--host" and i + 1 < len(sys.argv):
                    host = sys.argv[i + 1]
                elif arg.startswith("--host="):
                    host = arg.split("=", 1)[1]
            if host not in ("127.0.0.1", "localhost", "::1"):
                logger.error(
                    f"FATAL: DEMO_MODE is enabled on host '{host}'. "
                    f"Refusing startup: Demo mode must only bind to 127.0.0.1/localhost. "
                    f"Set ALLOW_DEMO_ON_NETWORK=true to override."
                )
                raise RuntimeError(
                    f"Refusing to start: DEMO_MODE is true on host '{host}'. "
                    f"Set ALLOW_DEMO_ON_NETWORK=true to override."
                )

    logger.info("Initializing database tables...")
    init_db()

    # Pre-warm DB connection pool
    try:
        warmup_database_pool(5)
    except Exception as e:
        logger.warning(f"Database pool warmup error: {e}")

    # Background Daily Status Sync job: flips onboarding -> active on join_date, applies scheduled terminations
    daily_job_task = asyncio.create_task(daily_status_sync_worker())
    keepalive_task = asyncio.create_task(db_keepalive_worker())

    logger.info("Initializing Qdrant vectorstore collections...")
    try:
        init_vectorstore()
    except Exception as e:
        logger.warning(f"Vectorstore init warning (will retry on demand): {e}")

    try:
        from app.embeddings import embed_query
        embed_query("warmup")
        for demo_code in ["ACM-008", "ACM-005", "ACM-002"]:
            try:
                resolve_caller(demo_code)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Warmup warning: {e}")

    try:
        from app.agent import init_agent_client
        await init_agent_client()
    except Exception as e:
        logger.warning(f"Agent client init warning: {e}")

    yield

    logger.info("Shutting down hrms-bot service...")
    try:
        from app.agent import close_agent_client
        await close_agent_client()
    except Exception as e:
        logger.warning(f"Agent client close warning: {e}")

    daily_job_task.cancel()
    keepalive_task.cancel()
    try:
        await asyncio.gather(daily_job_task, keepalive_task, return_exceptions=True)
    except Exception:
        pass


def run_daily_status_sync() -> Dict[str, Any]:
    """Flips onboarding -> active on join date, and applies scheduled terminations on last_working_date."""
    session = get_session()
    flipped_onboarding = []
    applied_terminations = []
    try:
        today = get_current_date()
        # 1. Flip 'onboarding' -> 'active' when join_date <= today
        onboarding_emps = (
            session.query(Employee)
            .filter(Employee.status == "onboarding", Employee.join_date <= today)
            .all()
        )
        for emp in onboarding_emps:
            emp.status = "active"
            flipped_onboarding.append(emp.emp_code)
            logger.info(f"[Daily Job] Flipped status for {emp.emp_code} ({emp.name}) from onboarding -> active (join_date={emp.join_date})")

        # 2. Apply scheduled terminations when last_working_date <= today and status is 'active'
        terminating_emps = (
            session.query(Employee)
            .filter(
                Employee.status == "active",
                Employee.last_working_date.isnot(None),
                Employee.last_working_date <= today,
            )
            .all()
        )
        for emp in terminating_emps:
            emp.status = "terminated"
            applied_terminations.append(emp.emp_code)
            logger.info(f"[Daily Job] Applied scheduled termination for {emp.emp_code} ({emp.name}) (last_working_date={emp.last_working_date})")

        if onboarding_emps or terminating_emps:
            session.commit()
            clear_caller_cache()

        return {
            "flipped_onboarding": flipped_onboarding,
            "applied_terminations": applied_terminations,
        }
    except Exception as exc:
        logger.error(f"[Daily Job] Error during daily status sync: {exc}")
        return {"error": str(exc)}
    finally:
        session.close()


async def daily_status_sync_worker():
    """Runs hourly background task to execute daily status sync, cancelling on shutdown."""
    while True:
        try:
            await asyncio.to_thread(run_daily_status_sync)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error(f"[Daily Job Worker] Exception: {exc}")

        try:
            await asyncio.sleep(3600)  # runs hourly
        except asyncio.CancelledError:
            break


async def db_keepalive_worker():
    """Periodically pings the database to keep pooled connections fresh."""
    while True:
        try:
            await asyncio.sleep(60)
            await asyncio.to_thread(ping_database_keepalive)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug(f"[DB Keepalive worker] error: {exc}")


app = FastAPI(
    title="HRMS Bot - Intelligent HR Assistant",
    description="Secure Agent loop, HR data tools, Long-term Memory, and Streaming Chat API for Acme Technologies HRMS",
    version="2.0.0",
    lifespan=lifespan,
)

# Record X-Response-Time header on every HTTP response
@app.middleware("http")
async def response_time_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start_time) * 1000
    response.headers["X-Response-Time"] = f"{duration_ms:.2f}ms"
    return response

# Global exception handler: never leak raw exception text to the browser
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        raise exc
    error_id = str(uuid.uuid4())
    logger.error(f"Unhandled exception [error_id={error_id}]: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal server error occurred.", "error_id": error_id},
    )

# Security headers middleware
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "frame-ancestors 'none';"
    )
    return response

# CORS: restrict origins to a configurable list (default localhost only). No wildcard with credentials.
settings = get_settings()
allowed_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
if not allowed_origins:
    allowed_origins = ["http://localhost:8000", "http://127.0.0.1:8000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount authentication router & employee management router
app.include_router(auth_router)
app.include_router(employee_router)

# Mount static folder
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
@app.get("/activate")
async def serve_index():
    """Serves the frontend single page application."""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "HRMS Bot API is running. Frontend static/index.html not found."}


@app.get("/health")
async def health_check():
    """Simple unauthenticated health check returning only status ok."""
    return {"status": "ok"}


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools_endpoint():
    """Cleanly responds to Chrome DevTools automated discovery."""
    return JSONResponse(status_code=204, content=None)


@app.get("/health/details", response_model=HealthResponse)
async def health_details(caller: CallerContext = Depends(get_current_caller)):
    """Health check endpoint reporting DB type, Qdrant mode, model name, and ingested chunks. HR Admin only."""
    if caller.role != "hr_admin":
        raise HTTPException(status_code=403, detail="Forbidden: HR Admin access required.")

    settings = get_settings()

    # Determine database type and status
    try:
        engine = get_engine()
        db_type = "postgresql" if engine.dialect.name == "postgresql" else "sqlite"
        table_counts = get_table_counts()
        db_status = "connected"
    except Exception as e:
        db_type = "unknown"
        table_counts = {}
        db_status = f"error: {str(e)}"

    # Determine Qdrant mode, connection status, and chunk count
    ingested_chunks = 0
    qdrant_mode = "cloud" if settings.qdrant_url and settings.qdrant_url.strip() else "local"
    try:
        client = get_qdrant_client()
        cols = client.get_collections()
        col_names = [c.name for c in cols.collections]
        vectorstore_status = f"connected ({', '.join(col_names)})"
        try:
            col_info = client.get_collection(settings.hr_policies_collection)
            ingested_chunks = col_info.points_count or 0
        except Exception:
            ingested_chunks = 0
    except Exception as e:
        vectorstore_status = f"error: {str(e)}"

    # Model name
    model_name = settings.groq_model

    return HealthResponse(
        status="healthy" if db_status == "connected" else "degraded",
        database=db_status,
        db_type=db_type,
        vectorstore=vectorstore_status,
        qdrant_mode=qdrant_mode,
        model_name=model_name,
        ingested_chunks=ingested_chunks,
        tables=table_counts,
    )


@app.post("/ingest", response_model=IngestResponse)
async def upload_and_ingest_file(
    file: UploadFile = File(...),
    replace: bool = Query(False, description="Whether to overwrite existing policy source"),
    caller: CallerContext = Depends(get_current_caller),
):
    """Uploads and ingests a document (PDF, TXT, MD) into hr_policies vectorstore. HR Admin only."""
    if caller.role != "hr_admin":
        raise HTTPException(status_code=403, detail="Forbidden: HR Admin access required.")

    filename = file.filename or "unknown_file"
    raw_base = os.path.basename(filename)
    clean_base = re.sub(r"[^a-zA-Z0-9_.-]", "_", raw_base)
    lower_name = clean_base.lower()

    if not (lower_name.endswith(".pdf") or lower_name.endswith(".txt") or lower_name.endswith(".md")):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file format. Please upload PDF, TXT, or MD files.",
        )

    try:
        content = await file.read()
        MAX_SIZE = 10 * 1024 * 1024  # 10 MB limit
        if len(content) > MAX_SIZE:
            return JSONResponse(
                status_code=413,
                content={
                    "code": "TOO_LARGE",
                    "message": f"File exceeds maximum allowed size of 10 MB ({len(content)} bytes).",
                    "detail": f"File exceeds maximum allowed size of 10 MB ({len(content)} bytes).",
                },
            )

        content_hash = hashlib.sha256(content).hexdigest()
        stem, ext = os.path.splitext(clean_base)
        if replace:
            source_name = clean_base
        else:
            source_name = f"{stem}_{content_hash[:8]}{ext}"

        result = await asyncio.to_thread(
            process_and_ingest_document,
            file_bytes=content,
            filename=clean_base,
            source=source_name,
            replace=replace,
        )
        chunks_count = getattr(result, "chunks", int(result))
        pages_count = getattr(result, "pages", 1)

        if chunks_count == 0:
            return JSONResponse(
                status_code=422,
                content={
                    "code": "PDF_NO_TEXT",
                    "message": "No readable text found. This looks like a scanned PDF; upload a text-based PDF or OCR it first.",
                    "detail": "No readable text found. This looks like a scanned PDF; upload a text-based PDF or OCR it first.",
                },
            )

        await asyncio.to_thread(
            record_policy_document,
            source=source_name,
            original_filename=clean_base,
            content_hash=content_hash,
            size_bytes=len(content),
            pages=pages_count,
            chunks=chunks_count,
            uploaded_by=caller.emp_code,
            replace=replace,
            status="ok",
        )

        log_admin_audit(
            actor_emp_code=caller.emp_code,
            action="policy_replace" if replace else "policy_upload",
            details={"filename": clean_base, "source": source_name, "chunks": chunks_count, "pages": pages_count, "replace": replace},
        )
        return IngestResponse(
            filename=clean_base,
            chunks_ingested=chunks_count,
            chunks=chunks_count,
            pages=pages_count,
            message=f"Successfully processed and ingested {chunks_count} chunks into hr_policies under source '{source_name}'.",
        )
    except PdfNoText:
        return JSONResponse(
            status_code=422,
            content={
                "code": "PDF_NO_TEXT",
                "message": "No readable text found. This looks like a scanned PDF; upload a text-based PDF or OCR it first.",
                "detail": "No readable text found. This looks like a scanned PDF; upload a text-based PDF or OCR it first.",
            },
        )
    except PdfEncrypted:
        return JSONResponse(
            status_code=422,
            content={
                "code": "PDF_ENCRYPTED",
                "message": "The PDF is password protected or encrypted and cannot be read.",
                "detail": "The PDF is password protected or encrypted and cannot be read.",
            },
        )
    except PdfCorrupt as e:
        return JSONResponse(
            status_code=422,
            content={
                "code": "PDF_CORRUPT",
                "message": f"The PDF file is corrupted or unreadable: {str(e)}",
                "detail": f"The PDF file is corrupted or unreadable: {str(e)}",
            },
        )
    except EmptyDocument:
        return JSONResponse(
            status_code=422,
            content={
                "code": "PDF_NO_TEXT",
                "message": "No readable text found. This looks like a scanned PDF; upload a text-based PDF or OCR it first.",
                "detail": "No readable text found. This looks like a scanned PDF; upload a text-based PDF or OCR it first.",
            },
        )
    except QdrantTimeout as e:
        return JSONResponse(
            status_code=504,
            content={
                "code": "QDRANT_TIMEOUT",
                "message": "Vector store timed out while storing document embeddings. Please retry.",
                "detail": "Vector store timed out while storing document embeddings. Please retry.",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        err_id = str(uuid.uuid4())
        logger.error(f"Error ingesting document {filename} [error_id={err_id}]: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "error_id": err_id,
                "message": f"Failed to ingest document. Reference error ID: {err_id}",
                "detail": f"Failed to ingest document. Reference error ID: {err_id}",
            },
        )


@app.get("/policies/files")
async def list_policy_files(caller: CallerContext = Depends(get_current_caller)):
    """Returns policy documents registered in policy_documents. Requires login."""
    docs = await asyncio.to_thread(get_policy_documents)
    return {"files": docs}


@app.delete("/policies/files/{source:path}")
async def delete_policy_file_endpoint(
    source: str,
    caller: CallerContext = Depends(get_current_caller),
):
    """Deletes policy document chunks and metadata. HR Admin only."""
    if caller.role != "hr_admin":
        raise HTTPException(status_code=403, detail="Forbidden: HR Admin access required.")

    settings = get_settings()
    await asyncio.to_thread(delete_chunks_by_source, settings.hr_policies_collection, source)
    deleted = await asyncio.to_thread(delete_policy_document, source)
    clear_policy_cache()
    log_admin_audit(
        actor_emp_code=caller.emp_code,
        action="policy_delete",
        details={"source": source, "deleted_row": deleted},
    )
    return {"status": "deleted", "source": source}


@app.get("/employees", response_model=List[EmployeeResponse])
async def list_employees(caller: CallerContext = Depends(get_current_caller)):
    """Returns employee directory. Full details for hr_admin; minimal directory for others."""
    def _fetch_employees():
        session = get_session()
        try:
            employees = session.query(Employee).order_by(Employee.id.asc()).all()
            results = []
            if caller.role == "hr_admin":
                for emp in employees:
                    results.append(
                        EmployeeResponse(
                            id=emp.id,
                            emp_code=emp.emp_code,
                            name=emp.name,
                            email=emp.email,
                            department=emp.department,
                            designation=emp.designation,
                            manager_id=emp.manager_id,
                            role=emp.role,
                            join_date=emp.join_date.isoformat(),
                            work_location=emp.work_location,
                            status=emp.status,
                        )
                    )
            else:
                for emp in employees:
                    results.append(
                        EmployeeResponse(
                            name=emp.name,
                            department=emp.department,
                            designation=emp.designation,
                        )
                    )
            return results
        finally:
            session.close()

    return await asyncio.to_thread(_fetch_employees)


@app.get("/policies/search", response_model=List[SearchPolicyResult])
async def search_hr_policies(
    query: str = Query(..., description="Query text to search HR policies"),
    top_k: int = Query(5, ge=1, le=20, description="Number of results to return"),
    caller: CallerContext = Depends(get_current_caller),
):
    """Semantic search endpoint over hr_policies collection. Requires login."""
    try:
        results = await asyncio.to_thread(search_policies, query=query, top_k=top_k)
        return [
            SearchPolicyResult(
                id=r["id"],
                text=r["text"],
                source=r["source"],
                section=r.get("section", "General"),
                chunk_index=r["chunk_index"],
                score=r["score"],
            )
            for r in results
        ]
    except Exception as e:
        err_id = str(uuid.uuid4())
        logger.error(f"Error querying policies [error_id={err_id}]: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Search failed. Reference error ID: {err_id}")


@app.post("/chat")
async def chat_endpoint(
    request: ChatRequest,
    req: Request,
    caller: CallerContext = Depends(get_current_caller),
):
    """Streaming chat endpoint using Server-Sent Events (SSE).

    Ignores any client-supplied user_id and always executes under authenticated caller identity.
    Enforces validated session IDs, exact session ownership, and parallel pre-LLM preparation.
    """
    validate_session_id(request.session_id)

    timer = StageTimer()
    timer.record("auth", getattr(req.state, "auth_elapsed_ms", 0.0))

    # Parallelize pre-LLM operations: session ownership, history, memories, system prompt info
    t_pre = time.perf_counter()
    owner_task = asyncio.to_thread(get_session_owner, request.session_id)
    history_task = asyncio.to_thread(get_chat_history, session_id=request.session_id, limit=None, exclude_failed=True)
    memories_task = asyncio.to_thread(get_user_memories_cached, user_id=caller.emp_code, limit=5)
    prompt_task = asyncio.to_thread(get_prompt_info_cached, employee_id=caller.employee_id)

    owner, history, memories, prompt_info = await asyncio.gather(
        owner_task, history_task, memories_task, prompt_task
    )
    pre_ms = (time.perf_counter() - t_pre) * 1000
    timer.record("session_owner_check", pre_ms)
    timer.record("history_load", pre_ms)
    timer.record("memory_fetch", pre_ms)
    timer.record("system_prompt_build", pre_ms)

    if owner is not None and owner != caller.emp_code:
        raise HTTPException(status_code=403, detail="Forbidden: You do not own this chat session.")

    client_ip = get_client_ip(req)
    chat_ip_limiter.enforce(client_ip, "Too many chat requests from this IP. Please slow down.")
    chat_user_limiter.enforce(caller.emp_code, "Too many chat requests for this user account.")

    # Start non-blocking user message and session metadata persistence immediately
    user_persisted_in_bg = False
    if not request.retry:
        async def _bg_save_user():
            t_u = time.perf_counter()
            try:
                await asyncio.to_thread(
                    add_chat_message,
                    session_id=request.session_id,
                    user_id=caller.emp_code,
                    role="user",
                    content=request.message,
                    status="ok",
                )
                await asyncio.to_thread(
                    upsert_chat_session,
                    session_id=request.session_id,
                    user_id=caller.emp_code,
                    message=request.message,
                )
            except Exception as u_err:
                logger.error(f"Failed to persist user message in bg: {u_err}")
            finally:
                timer.db_writes += round((time.perf_counter() - t_u) * 1000, 2)

        asyncio.create_task(_bg_save_user())
        user_persisted_in_bg = True

    async def event_generator():
        try:
            async with asyncio.timeout(30.0):
                async for event_payload in run_agent(
                    caller=caller,
                    session_id=request.session_id,
                    user_message=request.message,
                    retry=bool(request.retry),
                    preloaded_history=history,
                    preloaded_memories=memories,
                    preloaded_prompt_info=prompt_info,
                    timer=timer,
                    user_persisted_in_bg=user_persisted_in_bg,
                ):
                    event_name = event_payload.get("event", "message")
                    event_data = event_payload.get("data", {})
                    yield f"event: {event_name}\ndata: {json.dumps(event_data)}\n\n"
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(f"Agent execution timed out after 30s for session {request.session_id}")
            try:
                from app.agent import add_chat_message
                await asyncio.to_thread(
                    add_chat_message,
                    session_id=request.session_id,
                    user_id=caller.emp_code,
                    role="assistant",
                    content="",
                    status="failed",
                    error_code="TIMEOUT",
                )
            except Exception as db_err:
                logger.error(f"Failed to record timeout failed message: {db_err}")
            yield f"event: error\ndata: {json.dumps({'message': 'Request timed out after 30 seconds. Please try again.', 'retryable': True, 'error_code': 'TIMEOUT'})}\n\n"
        except asyncio.CancelledError:
            logger.warning(f"Client disconnected mid-stream for session {request.session_id}")
            try:
                from app.agent import add_chat_message
                await asyncio.to_thread(
                    add_chat_message,
                    session_id=request.session_id,
                    user_id=caller.emp_code,
                    role="assistant",
                    content="",
                    status="failed",
                    error_code="ClientDisconnected",
                )
            except Exception:
                pass
            raise
        except Exception as exc:
            err_id = str(uuid.uuid4())
            logger.error(f"Stream error [error_id={err_id}]: {exc}", exc_info=True)
            try:
                from app.agent import add_chat_message
                await asyncio.to_thread(
                    add_chat_message,
                    session_id=request.session_id,
                    user_id=caller.emp_code,
                    role="assistant",
                    content="",
                    status="failed",
                    error_code=type(exc).__name__,
                )
            except Exception:
                pass
            yield f"event: error\ndata: {json.dumps({'message': 'An internal error occurred while processing the request.', 'error_id': err_id, 'retryable': True})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/memories", response_model=List[MemoryResponse])
async def get_user_memories_endpoint(
    user_id: Optional[str] = Query(None, description="Employee code or ID"),
    caller: CallerContext = Depends(get_current_caller),
):
    """Retrieves all stored long-term memories for the caller. Only returns caller's own data."""
    if user_id and user_id.strip():
        uid_clean = user_id.strip().lower()
        if uid_clean not in (caller.emp_code.lower(), str(caller.employee_id).lower(), caller.name.lower()):
            raise HTTPException(status_code=403, detail="Forbidden: You can only view your own memories.")

    memories = await asyncio.to_thread(list_user_memories, user_id=caller.emp_code)
    return [
        MemoryResponse(
            id=m["id"],
            user_id=m["user_id"],
            text=m["text"],
            created_at=m.get("created_at"),
        )
        for m in memories
    ]


@app.delete("/memories")
async def delete_user_memories_endpoint(
    user_id: Optional[str] = Query(None, description="Employee code or ID"),
    caller: CallerContext = Depends(get_current_caller),
):
    """Deletes all stored long-term memories for the caller. Only deletes caller's own data."""
    if user_id and user_id.strip():
        uid_clean = user_id.strip().lower()
        if uid_clean not in (caller.emp_code.lower(), str(caller.employee_id).lower(), caller.name.lower()):
            raise HTTPException(status_code=403, detail="Forbidden: You can only delete your own memories.")

    await asyncio.to_thread(clear_user_memories, user_id=caller.emp_code)
    return {"status": "success", "message": f"Cleared memories for user {caller.emp_code}"}


@app.get("/sessions")
async def list_sessions_endpoint(
    limit: int = Query(20, ge=1, le=100),
    caller: CallerContext = Depends(get_current_caller),
):
    """Returns the caller's chat sessions only, newest first."""
    return await asyncio.to_thread(get_user_chat_sessions, user_id=caller.emp_code, limit=limit)


@app.get("/history", response_model=List[ChatMessageResponse])
async def get_history_endpoint(
    session_id: str = Query(..., description="Chat session ID"),
    limit: int = Query(50, ge=1, le=200),
    caller: CallerContext = Depends(get_current_caller),
):
    """Retrieves chronological chat history for a session owned by the caller, excluding failed turns."""
    validate_session_id(session_id)
    owner = await asyncio.to_thread(get_session_owner, session_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if owner != caller.emp_code:
        raise HTTPException(status_code=403, detail="Forbidden: You do not own this chat session.")

    history = await asyncio.to_thread(
        get_chat_history,
        session_id=session_id,
        limit=limit,
        user_id=caller.emp_code,
        exclude_failed=True,
    )
    return [
        ChatMessageResponse(
            id=h["id"],
            session_id=h["session_id"],
            user_id=h.get("user_id"),
            role=h["role"],
            content=h["content"],
            created_at=h.get("created_at"),
        )
        for h in history
    ]


@app.delete("/sessions/{session_id}")
async def delete_session_endpoint(
    session_id: str,
    caller: CallerContext = Depends(get_current_caller),
):
    """Owner only; deletes the session and its messages."""
    validate_session_id(session_id)
    owner = await asyncio.to_thread(get_session_owner, session_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if owner != caller.emp_code:
        raise HTTPException(status_code=403, detail="Forbidden: You do not own this chat session.")

    await asyncio.to_thread(delete_chat_session, session_id)
    return {"status": "deleted", "session_id": session_id}


@app.get("/audit")
async def get_audit_logs_endpoint(
    limit: int = Query(50, ge=1, le=100),
    caller: CallerContext = Depends(get_current_caller),
):
    """Returns access control denial logs for the frontend audit drawer. HR Admin only."""
    if caller.role != "hr_admin":
        raise HTTPException(status_code=403, detail="Forbidden: HR Admin access required.")
    logs = await asyncio.to_thread(get_audit_logs, limit=limit)
    return {"logs": logs}


@app.get("/audit/admin")
async def get_admin_audit_endpoint(
    limit: int = Query(100, ge=1, le=500),
    caller: CallerContext = Depends(get_current_caller),
):
    """Returns administrative audit logs (creations, updates, offboards, invites, policy changes). HR Admin only."""
    if caller.role != "hr_admin":
        raise HTTPException(status_code=403, detail="Forbidden: HR Admin access required.")
    logs = await asyncio.to_thread(get_admin_audit_logs, limit=limit)
    return {"logs": logs}
