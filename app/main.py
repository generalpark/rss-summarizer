import html
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import feedparser
import httpx
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from models import Article, Base

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("summarizer")

DATABASE_URL = os.environ["DATABASE_URL"]
LLM_URL = os.environ.get("LLM_URL", "http://100.101.214.67:5001/v1/chat/completions")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "300"))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "8"))
COLLECT_INTERVAL_MIN = int(os.environ.get("COLLECT_INTERVAL_MIN", "60"))
MAX_ATTEMPTS = 3

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
scheduler = BackgroundScheduler(timezone="Asia/Seoul")


def load_feeds() -> list[dict]:
    with open("feeds.yml", encoding="utf-8") as f:
        return yaml.safe_load(f).get("feeds", [])


def strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def collect() -> int:
    """RSS를 읽어 새 글만 DB에 저장. 중복은 link 유니크 제약으로 건너뛴다."""
    added = 0
    with Session(engine) as s:
        for feed in load_feeds():
            try:
                parsed = feedparser.parse(feed["url"])
            except Exception as e:
                log.warning("feed fetch failed %s: %s", feed.get("name"), e)
                continue
            for entry in parsed.entries[:20]:
                link = entry.get("link")
                if not link:
                    continue
                if s.scalar(select(Article).where(Article.link == link)):
                    continue
                body = strip_html(entry.get("summary", ""))[:4000]
                s.add(Article(
                    feed=feed.get("name", "unknown"),
                    title=entry.get("title", "(제목 없음)")[:500],
                    link=link[:1000],
                    raw_text=body,
                ))
                added += 1
        s.commit()
    log.info("collect done: %d new", added)
    return added


def call_llm(title: str, body: str) -> str:
    prompt = (
        "다음 기사를 한국어 3줄로 요약해줘. 과장 없이 사실만, 각 줄은 '- '로 시작.\n\n"
        f"제목: {title}\n내용: {body[:2500]}"
    )
    r = httpx.post(
        LLM_URL,
        json={"messages": [{"role": "user", "content": prompt}],
              "max_tokens": 400, "temperature": 0.3},
        timeout=LLM_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def summarize_pending(limit: int = 10) -> int:
    """H255 LLM으로 요약. H255가 꺼져 있으면 pending으로 남아 다음 주기에 재시도된다."""
    done = 0
    with Session(engine) as s:
        rows = s.scalars(
            select(Article)
            .where(Article.status == "pending", Article.attempts < MAX_ATTEMPTS)
            .order_by(Article.id)
            .limit(limit)
        ).all()
        for a in rows:
            a.attempts += 1
            try:
                a.summary = call_llm(a.title, a.raw_text)
                a.status = "summarized"
                done += 1
            except Exception as e:
                log.warning("summarize failed id=%s attempt=%s: %s", a.id, a.attempts, e)
            s.commit()
    log.info("summarize done: %d", done)
    return done


def send_digest() -> int:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("telegram not configured, skip digest")
        return 0
    with Session(engine) as s:
        rows = s.scalars(
            select(Article).where(Article.status == "summarized").order_by(Article.id).limit(15)
        ).all()
        if not rows:
            return 0
        parts = [f"📰 {datetime.now():%Y-%m-%d} 요약 ({len(rows)}건)\n"]
        for a in rows:
            parts.append(f"<b>{html.escape(a.title)}</b>\n{html.escape(a.summary or '')}\n{a.link}\n")
        text = "\n".join(parts)[:4000]
        try:
            httpx.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=30,
            ).raise_for_status()
        except Exception as e:
            log.error("telegram send failed: %s", e)
            return 0
        for a in rows:
            a.status = "sent"
        s.commit()
    log.info("digest sent: %d", len(rows))
    return len(rows)


def cleanup(days: int = 30) -> int:
    """오래된 데이터 정리 — 디스크가 차서 죽는 흔한 장애 예방."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    with Session(engine) as s:
        rows = s.scalars(select(Article).where(Article.created_at < cutoff)).all()
        for a in rows:
            s.delete(a)
        s.commit()
    return len(rows)


def job_cycle():
    collect()
    summarize_pending()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    scheduler.add_job(job_cycle, "interval", minutes=COLLECT_INTERVAL_MIN,
                      id="cycle", next_run_time=datetime.now())
    scheduler.add_job(send_digest, "cron", hour=DIGEST_HOUR, minute=0, id="digest")
    scheduler.add_job(cleanup, "cron", hour=4, minute=0, id="cleanup")
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="RSS Summarizer", lifespan=lifespan)


@app.get("/health")
def health():
    with Session(engine) as s:
        s.execute(select(1))
    return {"status": "ok"}


@app.get("/stats")
def stats():
    with Session(engine) as s:
        out = {}
        for st in ("pending", "summarized", "sent"):
            out[st] = len(s.scalars(select(Article).where(Article.status == st)).all())
        return out


@app.post("/run/collect")
def run_collect():
    return {"added": collect()}


@app.post("/run/summarize")
def run_summarize():
    return {"summarized": summarize_pending()}


@app.post("/run/digest")
def run_digest():
    return {"sent": send_digest()}


@app.get("/", response_class=HTMLResponse)
def index():
    with Session(engine) as s:
        rows = s.scalars(
            select(Article).where(Article.summary.isnot(None)).order_by(Article.id.desc()).limit(30)
        ).all()
    items = "".join(
        f"<article><h3><a href='{html.escape(a.link)}' target='_blank'>{html.escape(a.title)}</a></h3>"
        f"<small>{html.escape(a.feed)}</small><pre>{html.escape(a.summary or '')}</pre></article>"
        for a in rows
    )
    return (
        "<!doctype html><meta charset='utf-8'><title>요약</title>"
        "<style>body{font-family:sans-serif;max-width:760px;margin:24px auto;padding:0 12px;"
        "background:#111;color:#eee}a{color:#8ab4f8}pre{white-space:pre-wrap;background:#1c1c1c;"
        "padding:10px;border-radius:6px}article{border-bottom:1px solid #333;padding:8px 0}</style>"
        f"<h1>기사 요약</h1>{items or '<p>아직 요약된 글이 없습니다.</p>'}"
    )
