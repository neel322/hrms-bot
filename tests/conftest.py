"""Global pytest configuration and cleanup fixtures."""
import os
import sys
import pytest

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import init_db
from scripts.cleanup_extra_employees import cleanup_extra_employees


@pytest.fixture(scope="session", autouse=True)
def cleanup_after_test_suite():
    """Ensures test database is initialized and cleaned of extra test employees before and after running the suite."""
    init_db()
    try:
        cleanup_extra_employees(max_base_id=32)
    except Exception:
        pass
    yield
    try:
        cleanup_extra_employees(max_base_id=32)
    except Exception:
        pass
