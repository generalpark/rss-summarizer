from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Article


def test_strip_html_제거와_엔티티_복원(app_module):
    out = app_module.strip_html("<p>안녕 &amp; 반가워<br/>두번째</p>")
    assert "<" not in out
    assert "&" in out and "&amp;" not in out


def test_load_feeds_형식(app_module):
    feeds = app_module.load_feeds()
    assert len(feeds) > 0
    for f in feeds:
        assert "name" in f and "url" in f
        assert f["url"].startswith("http")


def test_중복_링크는_한_번만_저장된다(app_module, monkeypatch):
    """같은 기사가 여러 번 수집돼도 link UNIQUE 제약으로 1건만 남아야 한다."""
    entry = {"link": "https://example.com/a", "title": "제목", "summary": "<b>본문</b>"}

    class FakeParsed:
        entries = [entry, entry]

    monkeypatch.setattr(app_module.feedparser, "parse", lambda url: FakeParsed())
    monkeypatch.setattr(app_module, "load_feeds", lambda: [{"name": "t", "url": "http://x"}])

    app_module.collect()
    app_module.collect()  # 두 번 돌려도 늘지 않아야 함

    with Session(app_module.engine) as s:
        rows = s.scalars(select(Article)).all()
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert "<b>" not in rows[0].raw_text


def test_요약_실패시_pending_유지하고_시도횟수만_증가(app_module, monkeypatch):
    """H255가 꺼져 있어도 데이터가 사라지지 않고 다음 주기에 재시도돼야 한다."""
    with Session(app_module.engine) as s:
        s.add(Article(feed="t", title="제목", link="https://example.com/b", raw_text="본문"))
        s.commit()

    def boom(title, body):
        raise RuntimeError("LLM 연결 실패")

    monkeypatch.setattr(app_module, "call_llm", boom)
    done = app_module.summarize_pending()

    assert done == 0
    with Session(app_module.engine) as s:
        a = s.scalars(select(Article)).one()
    assert a.status == "pending"
    assert a.attempts == 1
    assert a.summary is None


def test_요약_성공시_상태전이(app_module, monkeypatch):
    with Session(app_module.engine) as s:
        s.add(Article(feed="t", title="제목", link="https://example.com/c", raw_text="본문"))
        s.commit()

    monkeypatch.setattr(app_module, "call_llm", lambda t, b: "- 한 줄 요약")
    assert app_module.summarize_pending() == 1

    with Session(app_module.engine) as s:
        a = s.scalars(select(Article)).one()
    assert a.status == "summarized"
    assert a.summary.startswith("-")


def test_최대_시도횟수를_넘으면_더_시도하지_않는다(app_module, monkeypatch):
    with Session(app_module.engine) as s:
        s.add(Article(feed="t", title="제목", link="https://example.com/d",
                      raw_text="본문", attempts=app_module.MAX_ATTEMPTS))
        s.commit()

    called = []
    monkeypatch.setattr(app_module, "call_llm", lambda t, b: called.append(1) or "x")
    app_module.summarize_pending()
    assert called == []


def test_cleanup은_오래된_것만_지운다(app_module):
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=40)
    with Session(app_module.engine) as s:
        s.add(Article(feed="t", title="옛글", link="https://example.com/old", created_at=old))
        s.add(Article(feed="t", title="새글", link="https://example.com/new"))
        s.commit()

    assert app_module.cleanup(days=30) == 1
    with Session(app_module.engine) as s:
        rows = s.scalars(select(Article)).all()
    assert len(rows) == 1 and rows[0].title == "새글"


def test_텔레그램_미설정이면_발송을_건너뛴다(app_module):
    with Session(app_module.engine) as s:
        s.add(Article(feed="t", title="제목", link="https://example.com/e",
                      summary="- 요약", status="summarized"))
        s.commit()
    assert app_module.send_digest() == 0


@pytest.mark.parametrize("path", ["/health", "/stats"])
def test_엔드포인트_응답(app_module, path):
    client = TestClient(app_module.app)
    r = client.get(path)
    assert r.status_code == 200
    assert isinstance(r.json(), dict)
