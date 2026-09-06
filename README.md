# RSS Summarizer

RSS를 수집해 **자체 호스팅 LLM**으로 요약하고, 매일 텔레그램으로 발송하는 서비스.
외부 AI API 비용 0원. 라즈베리파이에서 24시간 운영한다.

## 구조

```
[Raspberry Pi 5]                         [H255 미니PC]
  app (FastAPI + APScheduler)  --HTTP-->  koboldcpp (Qwen2.5-14B)
  db  (PostgreSQL)                        Tailscale 100.101.214.67:5001
        |
        +--> Telegram 발송
        +--> 웹 페이지(:8080)
```

두 장비는 **Tailscale**로 연결되어 있어 포트포워딩 없이 통신한다.

## 운영 설계

- **재시도**: H255가 꺼져 있으면 요약 실패 → `pending` 유지 → 다음 주기 자동 재시도(최대 3회)
- **중복 방지**: 기사 `link`에 UNIQUE 제약
- **자동 정리**: 30일 지난 데이터 매일 04시 삭제 (디스크 고갈 예방)
- **헬스체크**: `/health` (Docker HEALTHCHECK가 30초마다 확인, 실패 시 재시작)
- **관측**: `/stats` 로 pending/summarized/sent 건수 확인

## 실행

```bash
cp .env.example .env      # 값 채우기
docker compose up -d --build
```

- 웹: `http://<pi-ip>:8080`
- 상태: `curl http://<pi-ip>:8080/stats`
- 수동 실행: `POST /run/collect`, `/run/summarize`, `/run/digest`

## 백업

```bash
docker compose exec db pg_dump -U rss rss > backup_$(date +%F).sql
```

복구 훈련을 정기적으로 하고 소요 시간을 기록할 것 (RTO 측정).
