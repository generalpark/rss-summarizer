"""테스트는 실제 DB·LLM·텔레그램에 붙지 않고 임시 SQLite로만 돈다."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

_db_file = Path(tempfile.gettempdir()) / "rss_test.sqlite"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_db_file}")
os.environ.setdefault("LLM_URL", "http://127.0.0.1:1/none")  # 실수로 호출되면 즉시 실패
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

import main  # noqa: E402
from models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    """각 테스트마다 빈 테이블에서 시작한다."""
    Base.metadata.drop_all(main.engine)
    Base.metadata.create_all(main.engine)
    yield
    Base.metadata.drop_all(main.engine)


@pytest.fixture
def app_module():
    return main
