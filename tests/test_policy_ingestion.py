"""Tests for Part 2: Policy document ingestion, management, and error handling."""
import io
from datetime import datetime, timedelta
from typing import List, Optional
from unittest.mock import MagicMock, patch
import jwt
import pypdf
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
import pytest
from fastapi.testclient import TestClient
from qdrant_client.http import models
from qdrant_client.http.exceptions import ResponseHandlingException

from app.auth import get_jwt_secret
from app.config import get_settings
from app.db import (
    AuthCredentials,
    Employee,
    PolicyDocument,
    get_admin_audit_logs,
    get_policy_documents,
    get_session,
    init_db,
)
from app.ingest import (
    EmptyDocument,
    PdfCorrupt,
    PdfEncrypted,
    PdfNoText,
    extract_text_and_pages,
    process_and_ingest_document,
)
from app.main import app
from app.vectorstore import QdrantTimeout, get_qdrant_client, upsert_chunks


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    init_db()


@pytest.fixture
def client():
    return TestClient(app)


def get_cookie_for(emp_code: str):
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.emp_code == emp_code).first()
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == emp.id).first()
        token_ver = creds.token_version if creds else 1
        secret = get_jwt_secret()
        exp = datetime.utcnow() + timedelta(hours=8)
        token = jwt.encode(
            {"employee_id": emp.id, "emp_code": emp.emp_code, "role": emp.role, "token_version": token_ver, "exp": exp},
            secret,
            algorithm="HS256",
        )
        return {"access_token": token}
    finally:
        session.close()


def make_pdf(pages_text: List[str], password: Optional[str] = None) -> bytes:
    """Creates a PDF byte buffer with specified text on each page using pypdf."""
    writer = pypdf.PdfWriter()
    f1 = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    resources = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): f1
        })
    })

    for text in pages_text:
        page = writer.add_blank_page(width=400, height=400)
        if text:
            safe_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 50 200 Td ({safe_text}) Tj ET".encode("latin-1"))
            page[NameObject("/Contents")] = stream
            page[NameObject("/Resources")] = resources

    if password is not None:
        writer.encrypt(password)

    bio = io.BytesIO()
    writer.write(bio)
    return bio.getvalue()


# ==============================================================================
# TESTS
# ==============================================================================

def test_three_page_text_pdf_succeeds_and_appears_in_policies_files(client):
    """A 3-page text PDF succeeds, returns chunks and pages, and appears in /policies/files."""
    pdf_bytes = make_pdf([
        "Page 1: Acme Annual Leave and Paid Vacation Guidelines.",
        "Page 2: Acme Sick Leave and Medical Certification Procedures.",
        "Page 3: Acme Compassionate Leave and Emergency Time Off Policy.",
    ])

    hr_cookie = get_cookie_for("ACM-002")
    resp = client.post(
        "/ingest",
        files={"file": ("acme_leave_rules.pdf", pdf_bytes, "application/pdf")},
        cookies=hr_cookie,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["chunks"] is not None and data["chunks"] >= 1
    assert data["pages"] == 3
    assert data["filename"] == "acme_leave_rules.pdf"

    # Verify listing via GET /policies/files
    list_resp = client.get("/policies/files", cookies=hr_cookie)
    assert list_resp.status_code == 200
    files = list_resp.json().get("files", [])
    matched = [f for f in files if "acme_leave_rules" in f["name"]]
    assert len(matched) >= 1
    doc_info = matched[0]
    assert doc_info["pages"] == 3
    assert doc_info["chunks"] >= 1
    assert doc_info["uploaded_by"] == "ACM-002"
    assert doc_info["status"] == "ok"


def test_blank_no_text_pdf_returns_422_pdf_no_text_and_creates_no_row(client):
    """A blank/no-text PDF returns 422 PDF_NO_TEXT and creates no row in policy_documents."""
    blank_pdf = make_pdf(["", "", ""])
    hr_cookie = get_cookie_for("ACM-002")

    resp = client.post(
        "/ingest",
        files={"file": ("empty_scanned_doc.pdf", blank_pdf, "application/pdf")},
        cookies=hr_cookie,
    )
    assert resp.status_code == 422
    data = resp.json()
    assert data.get("code") == "PDF_NO_TEXT"
    assert "No readable text found" in data.get("message", "")

    # Ensure no row was created in policy_documents
    session = get_session()
    try:
        found = session.query(PolicyDocument).filter(PolicyDocument.original_filename == "empty_scanned_doc.pdf").first()
        assert found is None
    finally:
        session.close()


def test_encrypted_pdf_handling(client):
    """An encrypted PDF returns 422 PDF_ENCRYPTED or succeeds if decrypted with empty password."""
    # 1. Encrypted with password
    locked_pdf = make_pdf(["Top secret executive compensation policy"], password="secret_password_123")
    hr_cookie = get_cookie_for("ACM-002")

    resp = client.post(
        "/ingest",
        files={"file": ("locked_policy.pdf", locked_pdf, "application/pdf")},
        cookies=hr_cookie,
    )
    assert resp.status_code == 422
    data = resp.json()
    assert data.get("code") == "PDF_ENCRYPTED"

    # 2. Encrypted with empty password "" succeeds because reader.decrypt("") unlocks it
    empty_pw_pdf = make_pdf(["Open policy with empty encryption password"], password="")
    text, pages = extract_text_and_pages(empty_pw_pdf, "open.pdf")
    assert "Open policy" in text
    assert pages == 1


def test_corrupt_bytes_returns_422_pdf_corrupt(client):
    """Corrupt bytes return 422 PDF_CORRUPT."""
    corrupt_bytes = b"%PDF-1.4\ncorrupted garbage binary stream that cannot be read by pypdf \x00\xff\xfe"
    hr_cookie = get_cookie_for("ACM-002")

    resp = client.post(
        "/ingest",
        files={"file": ("corrupt_document.pdf", corrupt_bytes, "application/pdf")},
        cookies=hr_cookie,
    )
    assert resp.status_code == 422
    data = resp.json()
    assert data.get("code") == "PDF_CORRUPT"


def test_sixty_page_pdf_ingests_in_batches_without_timeout(client):
    """A 60-page PDF ingests in batches of 64 without timing out and records pages=60."""
    pages_text = [
        f"Section {i}: Operations and compliance clause details for Acme employees. " * 5
        for i in range(1, 61)
    ]
    large_pdf = make_pdf(pages_text)
    hr_cookie = get_cookie_for("ACM-002")

    resp = client.post(
        "/ingest",
        files={"file": ("handbook_60_pages.pdf", large_pdf, "application/pdf")},
        cookies=hr_cookie,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pages"] == 60
    assert data["chunks"] > 0

    # Verify policy_documents table
    session = get_session()
    try:
        row = session.query(PolicyDocument).filter(PolicyDocument.original_filename == "handbook_60_pages.pdf").first()
        assert row is not None
        assert row.pages == 60
        assert row.chunks > 0
    finally:
        session.close()


def test_replace_true_with_failing_extraction_keeps_old_chunks(client):
    """With replace=true, if extraction fails the old chunks and db metadata are preserved."""
    good_pdf = make_pdf(["Initial working company policy content before update."])
    source_name = "test_replace_safety.pdf"
    hr_cookie = get_cookie_for("ACM-002")

    # Ingest good file initially
    resp1 = client.post(
        "/ingest?replace=true",
        files={"file": (source_name, good_pdf, "application/pdf")},
        cookies=hr_cookie,
    )
    assert resp1.status_code == 200

    qdrant = get_qdrant_client()
    settings = get_settings()
    filt = models.Filter(
        must=[models.FieldCondition(key="source", match=models.MatchValue(value=source_name))]
    )
    initial_chunks = qdrant.count(
        collection_name=settings.hr_policies_collection,
        count_filter=filt,
        exact=True,
    ).count
    assert initial_chunks > 0

    # Now attempt replace with corrupt bytes
    corrupt_bytes = b"%PDF-1.4\nBROKEN"
    resp2 = client.post(
        "/ingest?replace=true",
        files={"file": (source_name, corrupt_bytes, "application/pdf")},
        cookies=hr_cookie,
    )
    assert resp2.status_code == 422
    assert resp2.json().get("code") == "PDF_CORRUPT"

    # Verify that the old chunks in Qdrant are STILL PRESENT
    chunks_after_fail = qdrant.count(
        collection_name=settings.hr_policies_collection,
        count_filter=filt,
        exact=True,
    ).count
    assert chunks_after_fail == initial_chunks


def test_non_hr_gets_403_on_upload_and_delete(client):
    """Non-HR employee gets 403 Forbidden on both upload and delete endpoints."""
    emp_cookie = get_cookie_for("ACM-001")
    pdf_bytes = make_pdf(["Some employee policy draft."])

    # Upload attempt
    resp_up = client.post(
        "/ingest",
        files={"file": ("unauthorized.pdf", pdf_bytes, "application/pdf")},
        cookies=emp_cookie,
    )
    assert resp_up.status_code == 403

    # Delete attempt
    resp_del = client.delete(
        "/policies/files/sample_docs/leave_policy.md",
        cookies=emp_cookie,
    )
    assert resp_del.status_code == 403


def test_delete_removes_chunks_and_the_row(client):
    """DELETE /policies/files/{source} deletes Qdrant chunks and the policy_documents row."""
    pdf_bytes = make_pdf(["Policy to be deleted shortly after verification."])
    source_name = "doc_to_delete.pdf"
    hr_cookie = get_cookie_for("ACM-002")

    # Ingest file
    resp_up = client.post(
        "/ingest?replace=true",
        files={"file": (source_name, pdf_bytes, "application/pdf")},
        cookies=hr_cookie,
    )
    assert resp_up.status_code == 200

    qdrant = get_qdrant_client()
    settings = get_settings()
    filt = models.Filter(
        must=[models.FieldCondition(key="source", match=models.MatchValue(value=source_name))]
    )
    count_before = qdrant.count(
        collection_name=settings.hr_policies_collection,
        count_filter=filt,
        exact=True,
    ).count
    assert count_before > 0

    # Delete file
    resp_del = client.delete(
        f"/policies/files/{source_name}",
        cookies=hr_cookie,
    )
    assert resp_del.status_code == 200
    assert resp_del.json().get("status") == "deleted"

    # Verify Qdrant chunks are gone
    count_after = qdrant.count(
        collection_name=settings.hr_policies_collection,
        count_filter=filt,
        exact=True,
    ).count
    assert count_after == 0

    # Verify row in DB is gone
    session = get_session()
    try:
        row = session.query(PolicyDocument).filter(PolicyDocument.source == source_name).first()
        assert row is None
    finally:
        session.close()

    # Verify admin audit recorded policy_delete
    audits = get_admin_audit_logs(limit=10)
    del_audits = [a for a in audits if a.get("action") == "policy_delete"]
    assert len(del_audits) > 0


def test_qdrant_timeouts_and_retry_behavior():
    """Mock Qdrant client to test retries with backoff and timeout mapping."""
    mock_client = MagicMock()
    mock_client.upsert.side_effect = [
        ResponseHandlingException(RuntimeError("Connection timed out")),
        ResponseHandlingException(RuntimeError("Connection timed out")),
        ResponseHandlingException(RuntimeError("Connection timed out")),
    ]

    points = [MagicMock()]
    with patch("app.vectorstore.get_qdrant_client", return_value=mock_client), \
         patch("time.sleep") as mock_sleep:
        with pytest.raises(QdrantTimeout):
            upsert_chunks(
                collection_name="hr_policies",
                points=points,
            )
        assert mock_client.upsert.call_count == 3
        assert mock_sleep.call_count == 2
