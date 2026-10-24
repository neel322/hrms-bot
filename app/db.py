from datetime import datetime
import json
from typing import Any, Dict, List, Optional
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    desc,
    func,
    text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from app.config import get_settings
from app.utils import get_current_datetime
import logging

logger = logging.getLogger("hrms-bot.db")

Base = declarative_base()


class Employee(Base):
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True, autoincrement=True)
    emp_code = Column(String(32), unique=True, nullable=False, index=True)
    name = Column(String(128), nullable=False)
    email = Column(String(128), unique=True, nullable=False, index=True)
    personal_email = Column(String(128), nullable=True)
    department = Column(String(64), nullable=False)
    designation = Column(String(128), nullable=False)
    manager_id = Column(Integer, ForeignKey("employees.id"), nullable=True, index=True)
    role = Column(String(32), nullable=False)  # 'employee' | 'manager' | 'hr_admin'
    join_date = Column(Date, nullable=False)
    work_location = Column(String(64), nullable=False)
    status = Column(String(32), default="active", nullable=False)
    probation_end_date = Column(Date, nullable=True)  # default: join_date + 6 months
    last_working_date = Column(Date, nullable=True)
    offboard_reason = Column(Text, nullable=True)

    # Relationships
    manager = relationship("Employee", remote_side=[id], backref="subordinates")
    credentials = relationship("AuthCredentials", back_populates="employee", uselist=False, cascade="all, delete-orphan")
    invites = relationship("Invite", back_populates="employee", cascade="all, delete-orphan")
    leave_balances = relationship("LeaveBalance", back_populates="employee", cascade="all, delete-orphan")
    leave_requests = relationship("LeaveRequest", foreign_keys="LeaveRequest.employee_id", back_populates="employee")
    attendances = relationship("Attendance", back_populates="employee", cascade="all, delete-orphan")
    payrolls = relationship("Payroll", back_populates="employee", cascade="all, delete-orphan")
    reviews = relationship("PerformanceReview", foreign_keys="PerformanceReview.employee_id", back_populates="employee")


class LeaveBalance(Base):
    __tablename__ = "leave_balances"
    __table_args__ = (
        UniqueConstraint("employee_id", "leave_type", "year", name="uq_leave_balances_emp_type_year"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    leave_type = Column(String(32), nullable=False)  # Casual, Sick, Earned, Maternity/Paternity
    total = Column(Integer, nullable=False)
    used = Column(Integer, nullable=False, default=0)
    remaining = Column(Integer, nullable=False)
    year = Column(Integer, nullable=False)

    employee = relationship("Employee", back_populates="leave_balances")


class LeaveRequest(Base):
    __tablename__ = "leave_requests"
    __table_args__ = (
        Index("ix_leave_requests_employee_start_date", "employee_id", "start_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    leave_type = Column(String(32), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    days = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False, default="pending")  # pending / approved / rejected
    reason = Column(Text, nullable=True)
    approver_id = Column(Integer, ForeignKey("employees.id"), nullable=True)

    employee = relationship("Employee", foreign_keys=[employee_id], back_populates="leave_requests")
    approver = relationship("Employee", foreign_keys=[approver_id])


class Attendance(Base):
    __tablename__ = "attendance"
    __table_args__ = (
        Index("ix_attendance_employee_date", "employee_id", "date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    check_in = Column(String(16), nullable=True)
    check_out = Column(String(16), nullable=True)
    status = Column(String(32), nullable=False)  # present / absent / wfh / leave / holiday

    employee = relationship("Employee", back_populates="attendances")


class Payroll(Base):
    __tablename__ = "payroll"
    __table_args__ = (
        Index("ix_payroll_employee_year_month", "employee_id", "year", "month"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    month = Column(Integer, nullable=False)
    year = Column(Integer, nullable=False)
    basic = Column(Float, nullable=False)
    hra = Column(Float, nullable=False)
    allowances = Column(Float, nullable=False)
    deductions = Column(Float, nullable=False)
    net_pay = Column(Float, nullable=False)
    payslip_status = Column(String(32), default="generated", nullable=False)

    employee = relationship("Employee", back_populates="payrolls")


class PerformanceReview(Base):
    __tablename__ = "performance_reviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    cycle = Column(String(64), nullable=False)
    rating = Column(Float, nullable=False)
    reviewer_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    comments = Column(Text, nullable=True)

    employee = relationship("Employee", foreign_keys=[employee_id], back_populates="reviews")
    reviewer = relationship("Employee", foreign_keys=[reviewer_id])


class Holiday(Base):
    __tablename__ = "holidays"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, index=True)
    name = Column(String(128), nullable=False)
    region = Column(String(64), nullable=False, default="All")


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ix_chat_sessions_user_last_msg", "user_id", "last_message_at"),
    )

    session_id = Column(String(128), primary_key=True)
    user_id = Column(String(64), nullable=False, index=True)
    title = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    last_message_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class ChatHistory(Base):
    __tablename__ = "chat_history"
    __table_args__ = (
        Index("ix_chat_history_sess_created", "session_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(128), index=True, nullable=False)
    user_id = Column(String(64), index=True, nullable=True)
    role = Column(String(32), nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String(32), default="ok", nullable=False)  # 'ok' | 'failed'
    error_code = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)


class AuthCredentials(Base):
    __tablename__ = "auth_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    must_change_password = Column(Boolean, default=False, nullable=False)
    token_version = Column(Integer, default=1, nullable=False)
    failed_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)

    employee = relationship("Employee", back_populates="credentials")


class Invite(Base):
    __tablename__ = "invites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    token_hash = Column(String(255), unique=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)

    employee = relationship("Employee", back_populates="invites")


class AdminAudit(Base):
    __tablename__ = "admin_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_emp_code = Column(String(32), nullable=False, index=True)
    action = Column(String(64), nullable=False, index=True)
    target_emp_code = Column(String(32), nullable=True, index=True)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)


class PolicyDocument(Base):
    __tablename__ = "policy_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(255), unique=True, nullable=False, index=True)
    original_filename = Column(String(255), nullable=False)
    content_hash = Column(String(64), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    pages = Column(Integer, nullable=False, default=1)
    chunks = Column(Integer, nullable=False)
    uploaded_by = Column(String(32), nullable=True, index=True)
    uploaded_at = Column(DateTime, default=func.now(), nullable=False)
    status = Column(String(32), default="ok", nullable=False)


class PolicyPage(Base):
    __tablename__ = "policy_pages"
    __table_args__ = (
        Index("ix_policy_pages_source_page", "source", "page_number"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(255), nullable=False, index=True)
    page_number = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    char_count = Column(Integer, nullable=False)


def log_admin_audit(
    actor_emp_code: str,
    action: str,
    target_emp_code: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> int:
    """Records an administrative audit entry in the admin_audit table."""
    session = get_session()
    try:
        entry = AdminAudit(
            actor_emp_code=actor_emp_code,
            action=action,
            target_emp_code=target_emp_code,
            details=json.dumps(details or {}),
            created_at=get_current_datetime(),
        )
        session.add(entry)
        session.commit()
        session.refresh(entry)
        return entry.id
    except Exception as exc:
        session.rollback()
        return 0
    finally:
        session.close()


def record_policy_document(
    source: str,
    original_filename: str,
    content_hash: str,
    size_bytes: int,
    pages: int,
    chunks: int,
    uploaded_by: Optional[str] = None,
    replace: bool = False,
    status: str = "ok",
) -> PolicyDocument:
    """Inserts or updates a policy_documents row."""
    session = get_session()
    try:
        existing = session.query(PolicyDocument).filter(PolicyDocument.source == source).first()
        if existing:
            existing.original_filename = original_filename
            existing.content_hash = content_hash
            existing.size_bytes = size_bytes
            existing.pages = pages
            existing.chunks = chunks
            if uploaded_by:
                existing.uploaded_by = uploaded_by
            existing.uploaded_at = get_current_datetime()
            existing.status = status
            session.commit()
            session.refresh(existing)
            return existing
        else:
            doc = PolicyDocument(
                source=source,
                original_filename=original_filename,
                content_hash=content_hash,
                size_bytes=size_bytes,
                pages=pages,
                chunks=chunks,
                uploaded_by=uploaded_by,
                uploaded_at=get_current_datetime(),
                status=status,
            )
            session.add(doc)
            session.commit()
            session.refresh(doc)
            return doc
    finally:
        session.close()


def delete_policy_document(source: str) -> bool:
    """Deletes a policy document metadata row and its pages by source."""
    delete_policy_pages(source)
    session = get_session()
    try:
        doc = session.query(PolicyDocument).filter(PolicyDocument.source == source).first()
        if doc:
            session.delete(doc)
            session.commit()
            return True
        return False
    finally:
        session.close()


def record_policy_pages(source: str, pages_data: List[Dict[str, Any]]) -> None:
    """Inserts rows into policy_pages for a given source."""
    if not pages_data:
        return
    session = get_session()
    try:
        for p in pages_data:
            p_text = p.get("text", "")
            page_obj = PolicyPage(
                source=source,
                page_number=p.get("page_number", 1),
                text=p_text,
                char_count=p.get("char_count", len(p_text)),
            )
            session.add(page_obj)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.error(f"Failed to record policy pages for {source}: {exc}")
        raise
    finally:
        session.close()


def delete_policy_pages(source: str) -> int:
    """Deletes all policy_pages rows for a given source."""
    session = get_session()
    try:
        deleted = session.query(PolicyPage).filter(PolicyPage.source == source).delete()
        session.commit()
        return deleted
    except Exception as exc:
        session.rollback()
        logger.error(f"Failed to delete policy pages for {source}: {exc}")
        return 0
    finally:
        session.close()


def get_policy_pages(
    source: str,
    start_page: int = 1,
    end_page: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Retrieves pages for source ordered by page_number, within [start_page, end_page]."""
    session = get_session()
    try:
        query = session.query(PolicyPage).filter(
            PolicyPage.source == source,
            PolicyPage.page_number >= start_page,
        )
        if end_page is not None:
            query = query.filter(PolicyPage.page_number <= end_page)
        rows = query.order_by(PolicyPage.page_number.asc()).all()
        return [
            {
                "page_number": r.page_number,
                "text": r.text,
                "char_count": r.char_count,
            }
            for r in rows
        ]
    finally:
        session.close()


def get_policy_page_count(source: str) -> int:
    """Returns the total number of pages recorded for a policy document source."""
    session = get_session()
    try:
        cnt = session.query(func.count(PolicyPage.id)).filter(PolicyPage.source == source).scalar() or 0
        return cnt
    finally:
        session.close()


def get_policy_documents() -> List[Dict[str, Any]]:
    """Returns all rows from policy_documents."""
    session = get_session()
    try:
        docs = session.query(PolicyDocument).order_by(PolicyDocument.id.asc()).all()
        return [
            {
                "id": d.id,
                "name": d.source,
                "source": d.source,
                "original_filename": d.original_filename,
                "content_hash": d.content_hash,
                "size": round(d.size_bytes / 1024, 1),
                "size_kb": round(d.size_bytes / 1024, 1),
                "size_bytes": d.size_bytes,
                "pages": d.pages,
                "chunks": d.chunks,
                "uploaded_by": d.uploaded_by,
                "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
                "status": d.status,
            }
            for d in docs
        ]
    finally:
        session.close()


def backfill_sample_docs() -> None:
    """Startup backfill: for sample_docs and any policy_documents row with no pages, create pages. Idempotent and logged."""
    import os
    import hashlib
    from app.ingest import extract_text_and_pages, chunk_document_with_sections, create_virtual_pages
    from app.config import get_settings
    settings = get_settings()

    sample_docs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "sample_docs"))

    session = get_session()
    try:
        existing_docs = {row.source: row for row in session.query(PolicyDocument).all()}

        # 1. Backfill sample_docs to policy_documents if missing
        if os.path.exists(sample_docs_dir):
            for filename in sorted(os.listdir(sample_docs_dir)):
                if filename.endswith((".md", ".txt", ".pdf")) and filename not in existing_docs:
                    file_path = os.path.join(sample_docs_dir, filename)
                    try:
                        with open(file_path, "rb") as f:
                            content = f.read()
                        size_bytes = len(content)
                        content_hash = hashlib.sha256(content).hexdigest()[:8]
                        try:
                            ext_res = extract_text_and_pages(content, filename)
                            text = ext_res.text if hasattr(ext_res, "text") else ext_res[0]
                            pages = ext_res.pages if hasattr(ext_res, "pages") else ext_res[1]
                            pages_data = ext_res.pages_data if hasattr(ext_res, "pages_data") else None
                            if not pages_data:
                                pages_data = create_virtual_pages(text) if not filename.lower().endswith(".pdf") else []
                            chunks = len(chunk_document_with_sections(text, settings.chunk_size, settings.chunk_overlap))
                        except Exception:
                            pages = 1
                            chunks = 1
                            pages_data = [{"page_number": 1, "text": "", "char_count": 0}]

                        doc = record_policy_document(
                            source=filename,
                            original_filename=filename,
                            content_hash=content_hash,
                            size_bytes=size_bytes,
                            pages=len(pages_data) if pages_data else pages,
                            chunks=chunks,
                            uploaded_by="system",
                            status="ok",
                        )
                        existing_docs[filename] = doc
                        if pages_data:
                            delete_policy_pages(filename)
                            record_policy_pages(filename, pages_data)
                            logger.info(f"Backfilled sample doc '{filename}' with {len(pages_data)} pages.")
                    except Exception as exc:
                        logger.warning(f"Backfill error for {filename}: {exc}")

        # 2. Check for any policy_documents rows with 0 pages in policy_pages
        all_docs = session.query(PolicyDocument).all()
        for doc in all_docs:
            page_count = session.query(func.count(PolicyPage.id)).filter(PolicyPage.source == doc.source).scalar() or 0
            if page_count > 0:
                continue

            # No pages found! Create them.
            pages_data = []
            candidate_path = os.path.join(sample_docs_dir, doc.source) if os.path.exists(sample_docs_dir) else None
            if candidate_path and os.path.exists(candidate_path):
                try:
                    with open(candidate_path, "rb") as f:
                        c_bytes = f.read()
                    ext_res = extract_text_and_pages(c_bytes, doc.source)
                    pages_data = ext_res.pages_data if hasattr(ext_res, "pages_data") and ext_res.pages_data else create_virtual_pages(ext_res[0])
                except Exception as e:
                    logger.warning(f"Could not read sample doc for backfill of {doc.source}: {e}")

            if not pages_data:
                try:
                    from app.vectorstore import get_qdrant_client
                    from qdrant_client.http import models as q_models
                    client = get_qdrant_client()
                    scroll_res = client.scroll(
                        collection_name=settings.hr_policies_collection,
                        scroll_filter=q_models.Filter(
                            must=[q_models.FieldCondition(key="source", match=q_models.MatchValue(value=doc.source))]
                        ),
                        limit=500,
                        with_payload=True,
                    )
                    points = scroll_res[0] if scroll_res else []
                    if points:
                        points.sort(key=lambda pt: (pt.payload or {}).get("chunk_index", 0))
                        accumulated = ""
                        for pt in points:
                            chunk_text = (pt.payload or {}).get("text", "")
                            if "\n" in chunk_text:
                                first_line, body = chunk_text.split("\n", 1)
                                if (pt.payload or {}).get("section") and first_line.strip() == (pt.payload or {}).get("section", "").strip():
                                    chunk_text = body.strip()
                            if not accumulated:
                                accumulated = chunk_text
                            else:
                                overlap_found = False
                                for ov_len in range(min(len(accumulated), len(chunk_text), 80), 10, -1):
                                    if accumulated.endswith(chunk_text[:ov_len]):
                                        accumulated += chunk_text[ov_len:]
                                        overlap_found = True
                                        break
                                if not overlap_found:
                                    accumulated += "\n\n" + chunk_text
                        if accumulated.strip():
                            pages_data = create_virtual_pages(accumulated.strip())
                except Exception as e:
                    logger.warning(f"Failed to scroll Qdrant chunks for backfill of {doc.source}: {e}")

            if pages_data:
                record_policy_pages(doc.source, pages_data)
                doc.pages = len(pages_data)
                session.commit()
                logger.info(f"Backfilled {len(pages_data)} pages for policy document '{doc.source}'.")
            else:
                pages_data = [{"page_number": 1, "text": "", "char_count": 0}]
                record_policy_pages(doc.source, pages_data)
                session.commit()
                logger.info(f"Created placeholder page for empty policy document '{doc.source}'.")
    except Exception as exc:
        logger.warning(f"Startup backfill exception: {exc}")
    finally:
        session.close()


def get_admin_audit_logs(limit: int = 100) -> List[Dict[str, Any]]:
    """Fetches recent admin audit logs, newest first."""
    session = get_session()
    try:
        rows = (
            session.query(AdminAudit)
            .order_by(desc(AdminAudit.id))
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "actor_emp_code": r.actor_emp_code,
                "action": r.action,
                "target_emp_code": r.target_emp_code,
                "details": json.loads(r.details) if r.details else {},
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    finally:
        session.close()


_engine = None
_SessionLocal = None


def get_engine():
    """Initializes and returns the SQLAlchemy engine (Supabase PostgreSQL or SQLite fallback)."""
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        if settings.database_url and settings.database_url.strip():
            url = settings.database_url.strip()
            # Strip pgbouncer query param which psycopg2 does not recognize as a DSN param
            if "pgbouncer=true" in url:
                url = url.replace("?pgbouncer=true", "").replace("&pgbouncer=true", "")

            # Ensure SQLAlchemy uses psycopg2 driver
            if url.startswith("postgres://"):
                url = "postgresql+psycopg2://" + url[len("postgres://"):]
            elif url.startswith("postgresql://"):
                url = "postgresql+psycopg2://" + url[len("postgresql://"):]

            try:
                candidate_engine = create_engine(
                    url,
                    pool_pre_ping=False,
                    pool_recycle=300,
                    pool_size=10,
                    max_overflow=10,
                    connect_args={"connect_timeout": 3},
                )
                with candidate_engine.connect() as test_conn:
                    test_conn.execute(text("SELECT 1;"))
                _engine = candidate_engine
                logger.info("Connected to PostgreSQL database.")
            except Exception as conn_err:
                logger.warning(
                    f"PostgreSQL connection failed ({conn_err}). "
                    f"Falling back to local SQLite database: {settings.sqlite_db_path}"
                )
                _engine = None

        if _engine is None:
            _engine = create_engine(
                f"sqlite:///{settings.sqlite_db_path}",
                connect_args={"check_same_thread": False},
            )
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    return _engine


def get_session():
    """Returns a new database session."""
    get_engine()
    return _SessionLocal()


def warmup_database_pool(n: int = 5) -> None:
    """Warms up the database connection pool by opening and returning connections."""
    engine = get_engine()
    conns = []
    try:
        for _ in range(n):
            c = engine.connect()
            c.execute(text("SELECT 1;"))
            conns.append(c)
        logger.info(f"Warmed up {len(conns)} database connections in pool.")
    except Exception as exc:
        logger.warning(f"Database pool warmup error: {exc}")
    finally:
        for c in conns:
            try:
                c.close()
            except Exception:
                pass


def ping_database_keepalive() -> bool:
    """Executes a quick query to keep connections fresh and recycled."""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1;"))
        return True
    except Exception as exc:
        logger.warning(f"Database keepalive ping failed: {exc}")
        return False


def init_db() -> None:
    """Initializes the database schema and creates all tables if they do not exist."""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)

    # Schema migrations: ensure newly added columns, unique constraints, and composite indexes exist
    with engine.connect() as conn:
        try:
            if engine.dialect.name == "postgresql":
                conn.execute(text("ALTER TABLE employees ADD COLUMN IF NOT EXISTS probation_end_date DATE;"))
                conn.execute(text("ALTER TABLE employees ADD COLUMN IF NOT EXISTS personal_email VARCHAR(128);"))
                conn.execute(text("ALTER TABLE employees ADD COLUMN IF NOT EXISTS last_working_date DATE;"))
                conn.execute(text("ALTER TABLE employees ADD COLUMN IF NOT EXISTS offboard_reason TEXT;"))
                conn.execute(text("ALTER TABLE auth_credentials ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 1;"))
                conn.execute(text("ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'ok';"))
                conn.execute(text("ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS error_code VARCHAR(64);"))
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_leave_balances_emp_type_year ON leave_balances (employee_id, leave_type, year);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_attendance_employee_date ON attendance (employee_id, date);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_leave_requests_employee_start_date ON leave_requests (employee_id, start_date);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_payroll_employee_year_month ON payroll (employee_id, year, month);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_sessions_user_last_msg ON chat_sessions (user_id, last_message_at);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_history_sess_created ON chat_history (session_id, created_at);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auth_credentials_employee_id ON auth_credentials (employee_id);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_employees_email ON employees (email);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_employees_manager_id ON employees (manager_id);"))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS policy_documents (
                        id SERIAL PRIMARY KEY,
                        source VARCHAR(255) UNIQUE NOT NULL,
                        original_filename VARCHAR(255) NOT NULL,
                        content_hash VARCHAR(64) NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        pages INTEGER NOT NULL DEFAULT 1,
                        chunks INTEGER NOT NULL,
                        uploaded_by VARCHAR(32),
                        uploaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        status VARCHAR(32) NOT NULL DEFAULT 'ok'
                    );
                """))
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_documents_source ON policy_documents (source);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_policy_documents_uploaded_by ON policy_documents (uploaded_by);"))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS policy_pages (
                        id SERIAL PRIMARY KEY,
                        source VARCHAR(255) NOT NULL,
                        page_number INTEGER NOT NULL,
                        text TEXT NOT NULL,
                        char_count INTEGER NOT NULL
                    );
                """))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_policy_pages_source_page ON policy_pages (source, page_number);"))
                conn.commit()
            elif engine.dialect.name == "sqlite":
                res = conn.execute(text("PRAGMA table_info(employees);")).fetchall()
                col_names = [r[1] for r in res]
                if "probation_end_date" not in col_names:
                    conn.execute(text("ALTER TABLE employees ADD COLUMN probation_end_date DATE;"))
                if "personal_email" not in col_names:
                    conn.execute(text("ALTER TABLE employees ADD COLUMN personal_email VARCHAR(128);"))
                if "last_working_date" not in col_names:
                    conn.execute(text("ALTER TABLE employees ADD COLUMN last_working_date DATE;"))
                if "offboard_reason" not in col_names:
                    conn.execute(text("ALTER TABLE employees ADD COLUMN offboard_reason TEXT;"))
                res_auth = conn.execute(text("PRAGMA table_info(auth_credentials);")).fetchall()
                col_names_auth = [r[1] for r in res_auth]
                if "token_version" not in col_names_auth:
                    conn.execute(text("ALTER TABLE auth_credentials ADD COLUMN token_version INTEGER NOT NULL DEFAULT 1;"))
                res_hist = conn.execute(text("PRAGMA table_info(chat_history);")).fetchall()
                col_names_hist = [r[1] for r in res_hist]
                if "status" not in col_names_hist:
                    conn.execute(text("ALTER TABLE chat_history ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'ok';"))
                if "error_code" not in col_names_hist:
                    conn.execute(text("ALTER TABLE chat_history ADD COLUMN error_code VARCHAR(64);"))
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_leave_balances_emp_type_year ON leave_balances (employee_id, leave_type, year);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_attendance_employee_date ON attendance (employee_id, date);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_leave_requests_employee_start_date ON leave_requests (employee_id, start_date);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_payroll_employee_year_month ON payroll (employee_id, year, month);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_sessions_user_last_msg ON chat_sessions (user_id, last_message_at);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_history_sess_created ON chat_history (session_id, created_at);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auth_credentials_employee_id ON auth_credentials (employee_id);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_employees_email ON employees (email);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_employees_manager_id ON employees (manager_id);"))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS policy_documents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source VARCHAR(255) UNIQUE NOT NULL,
                        original_filename VARCHAR(255) NOT NULL,
                        content_hash VARCHAR(64) NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        pages INTEGER NOT NULL DEFAULT 1,
                        chunks INTEGER NOT NULL,
                        uploaded_by VARCHAR(32),
                        uploaded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        status VARCHAR(32) NOT NULL DEFAULT 'ok'
                    );
                """))
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_documents_source ON policy_documents (source);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_policy_documents_uploaded_by ON policy_documents (uploaded_by);"))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS policy_pages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source VARCHAR(255) NOT NULL,
                        page_number INTEGER NOT NULL,
                        text TEXT NOT NULL,
                        char_count INTEGER NOT NULL
                    );
                """))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_policy_pages_source_page ON policy_pages (source, page_number);"))
                conn.commit()
        except Exception:
            pass

    session = get_session()
    try:
        if session.query(Employee).count() == 0:
            logger.info("Database has 0 employees; auto-seeding sample dataset...")
            from app.seed import seed_database
            seed_database(reset=False)
    except Exception as e:
        logger.warning(f"Auto-seed check: {e}")
    finally:
        session.close()

    try:
        backfill_sample_docs()
    except Exception as e:
        logger.warning(f"Backfill sample docs check: {e}")


def get_table_counts() -> Dict[str, int]:
    """Returns a dictionary with row counts for all tables in the SQLite database."""
    session = get_session()
    try:
        tables = [
            ("employees", Employee),
            ("leave_balances", LeaveBalance),
            ("leave_requests", LeaveRequest),
            ("attendance", Attendance),
            ("payroll", Payroll),
            ("performance_reviews", PerformanceReview),
            ("holidays", Holiday),
            ("chat_sessions", ChatSession),
            ("chat_history", ChatHistory),
            ("auth_credentials", AuthCredentials),
            ("invites", Invite),
            ("policy_documents", PolicyDocument),
            ("policy_pages", PolicyPage),
            ("admin_audit", AdminAudit),
        ]
        counts = {}
        for name, model in tables:
            key_col = model.session_id if hasattr(model, "session_id") else (model.id if hasattr(model, "id") else model.employee_id)
            counts[name] = session.query(func.count(key_col)).scalar() or 0
        return counts
    finally:
        session.close()


def get_session_owner(session_id: str) -> Optional[str]:
    """Returns the user_id of the session owner from chat_sessions or first message, or None if new."""
    session = get_session()
    try:
        sess = session.query(ChatSession).filter(ChatSession.session_id == session_id).first()
        if sess and sess.user_id:
            return sess.user_id
        first_msg = (
            session.query(ChatHistory)
            .filter(ChatHistory.session_id == session_id)
            .order_by(ChatHistory.id.asc())
            .first()
        )
        if first_msg:
            return first_msg.user_id
        return None
    finally:
        session.close()


def upsert_chat_session(session_id: str, user_id: str, message: str) -> None:
    """Creates a chat session row or updates its last_message_at timestamp."""
    session = get_session()
    try:
        sess = session.query(ChatSession).filter(ChatSession.session_id == session_id).first()
        now = get_current_datetime()
        if not sess:
            clean_msg = (message or "").strip().replace("\n", " ")
            title = clean_msg[:60] if clean_msg else "New Conversation"
            sess = ChatSession(
                session_id=session_id,
                user_id=user_id,
                title=title,
                created_at=now,
                last_message_at=now,
            )
            session.add(sess)
        else:
            sess.last_message_at = now
        session.commit()
    finally:
        session.close()


def get_user_chat_sessions(user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Returns the caller's chat sessions only, newest first."""
    session = get_session()
    try:
        results = (
            session.query(ChatSession)
            .filter(ChatSession.user_id == user_id)
            .order_by(ChatSession.last_message_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "session_id": s.session_id,
                "user_id": s.user_id,
                "title": s.title,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "last_message_at": s.last_message_at.isoformat() if s.last_message_at else None,
            }
            for s in results
        ]
    finally:
        session.close()


def delete_chat_session(session_id: str) -> bool:
    """Deletes a chat session and its corresponding messages from chat_history."""
    session = get_session()
    try:
        session.query(ChatHistory).filter(ChatHistory.session_id == session_id).delete()
        deleted = session.query(ChatSession).filter(ChatSession.session_id == session_id).delete()
        session.commit()
        return bool(deleted)
    finally:
        session.close()


def remove_failed_assistant_turn(session_id: str) -> None:
    """Removes the last assistant message if marked as failed, to prepare for retry."""
    session = get_session()
    try:
        last_msg = (
            session.query(ChatHistory)
            .filter(ChatHistory.session_id == session_id)
            .order_by(ChatHistory.id.desc())
            .first()
        )
        if last_msg and last_msg.role == "assistant" and (last_msg.status == "failed" or "(failed)" in (last_msg.content or "")):
            session.delete(last_msg)
            session.commit()
    finally:
        session.close()


def add_chat_message(
    session_id: str,
    role: str,
    content: str,
    user_id: Optional[str] = None,
    status: str = "ok",
    error_code: Optional[str] = None,
) -> int:
    """Inserts a new message into chat_history and returns the generated row id."""
    session = get_session()
    try:
        msg = ChatHistory(
            session_id=session_id,
            user_id=user_id,
            role=role,
            content=content,
            status=status,
            error_code=error_code,
        )
        session.add(msg)
        session.commit()
        session.refresh(msg)
        return msg.id
    finally:
        session.close()


def get_chat_history(
    session_id: str,
    limit: Optional[int] = 50,
    user_id: Optional[str] = None,
    exclude_failed: bool = True,
) -> List[Dict[str, Any]]:
    """Retrieves chronological chat history for a given session, optionally filtered by user_id and excluding failed turns."""
    session = get_session()
    try:
        query = session.query(ChatHistory).filter(ChatHistory.session_id == session_id)
        if user_id is not None:
            query = query.filter(ChatHistory.user_id == user_id)
        if exclude_failed:
            query = query.filter(ChatHistory.status != "failed").filter(~ChatHistory.content.like("%(failed)%"))

        if limit is not None:
            records = (
                query
                .order_by(desc(ChatHistory.id))
                .limit(limit)
                .all()
            )
            records = list(reversed(records))
        else:
            records = (
                query
                .order_by(ChatHistory.id.asc())
                .all()
            )

        return [
            {
                "id": r.id,
                "session_id": r.session_id,
                "user_id": r.user_id,
                "role": r.role,
                "content": r.content,
                "status": getattr(r, "status", "ok"),
                "error_code": getattr(r, "error_code", None),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ]
    finally:
        session.close()

