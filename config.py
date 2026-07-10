"""환경설정 — Grok(LLM) + 오케스트레이터 DB + 결정론적 검증 대상 저장소(REPO_PATH).

- LLM(Grok)은 결정론적 검증 결과를 사람이 읽기 쉬운 요약(summary)으로 만드는 데만 사용한다.
  (사실 판정 자체는 fact_check_tools.py의 ctags/git 조회로 결정론적으로 수행된다.)
- API 키/DB 주소/저장소 경로는 .env 또는 환경변수로만 주입한다(코드에 하드코딩 금지).
"""

import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ── LLM (Grok, OpenAI 호환 엔드포인트) ──────────────────────────────
LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_BASE_URL = "https://api.x.ai/v1"
LLM_MODEL = "grok-4"

# ── 오케스트레이터 DB ───────────────────────────────────────────────
# /invoke가 워크플로우 JSON을 조회하고, 결과/이벤트를 등록할 기본 주소.
# .env의 DATABASE_URL을 기본값으로 사용한다(없으면 아래 fallback).
ORCHESTRATOR_BASE_URL = (
    os.getenv("ORCHESTRATOR_BASE_URL")
    or os.getenv("DATABASE_URL")
    or "http://127.0.0.1:8000"
)

# ── 결정론적 검증 대상 저장소 ───────────────────────────────────────
# 리포트의 함수/커밋/헤더/파일 인용을 대조할 실제 소스 저장소의 로컬 경로.
REPO_PATH = os.getenv("REPO_PATH", "/repo")
# REPO_PATH가 비어 있으면 tools.ensure_repo가 기동 시 이 URL을 clone 한다(하드코딩).
REPO_URL = "https://github.com/curl/curl"

_client = None


def get_client() -> OpenAI:
    """OpenAI 호환 Grok 클라이언트를 지연 생성한다(키가 없으면 명확한 에러)."""
    global _client
    if _client is None:
        if not LLM_API_KEY:
            raise RuntimeError(
                "LLM_API_KEY가 설정되지 않았습니다. .env 파일이나 환경변수로 설정하세요."
            )
        _client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    return _client
