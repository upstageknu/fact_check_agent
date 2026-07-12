"""환경설정 — LLM(provider 선택 가능) + 오케스트레이터 DB + 결정론적 검증 대상 저장소(REPO_PATH).

- LLM은 도구 호출을 조정하고 검증 결과를 사람이 읽기 쉬운 요약(summary)으로 만든다.
  (사실 판정 자체는 fact_check_tools.py의 ctags/git 조회로 결정론적으로 수행된다.)
- 사용할 LLM은 .env의 `LLM_PROVIDER`(upstage|grok|gemini)만 바꾸면 전환된다.
  base_url/model은 provider별 기본값이 자동 적용되며, 필요하면 `LLM_BASE_URL`/`LLM_MODEL`로 덮어쓴다.
- API 키/DB 주소/저장소 경로는 .env 또는 환경변수로만 주입한다(코드에 하드코딩 금지).

.env 예시:
    # Grok(xAI) 사용
    LLM_PROVIDER=grok
    LLM_API_KEY=xai-...
    # Upstage Solar 사용
    LLM_PROVIDER=upstage
    LLM_API_KEY=up_...
"""

import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ── LLM provider 선택 ───────────────────────────────────────────────
# 지원: upstage(Solar), grok(xAI), gemini(OpenAI 호환 엔드포인트). 모두 OpenAI 호환 API.
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "upstage").strip().lower()

# provider별 (기본 base_url, 기본 model)
_PROVIDER_DEFAULTS = {
    "upstage": ("https://api.upstage.ai/v1", "solar-pro3"),
    "grok": ("https://api.x.ai/v1", "grok-4-latest"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.5-flash"),
}
if LLM_PROVIDER not in _PROVIDER_DEFAULTS:
    raise RuntimeError(
        f"알 수 없는 LLM_PROVIDER={LLM_PROVIDER!r}. "
        f"지원 값: {', '.join(_PROVIDER_DEFAULTS)}"
    )
_DEFAULT_BASE_URL, _DEFAULT_MODEL = _PROVIDER_DEFAULTS[LLM_PROVIDER]

# 활성 LLM 설정. 명시 설정이 있으면 우선, 없으면 provider 기본값.
# (하위호환: 예전 UPSTAGE_* / SOLAR_MODEL 환경변수도 계속 인식한다.)
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("UPSTAGE_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or os.getenv("UPSTAGE_BASE_URL") or _DEFAULT_BASE_URL
LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("SOLAR_MODEL") or _DEFAULT_MODEL

# ── 하위호환 별칭 ───────────────────────────────────────────────────
# 기존 모듈들이 import 하는 이름을 유지한다(값은 활성 LLM 설정을 가리킨다).
SOLAR_MODEL = LLM_MODEL
UPSTAGE_API_KEY = LLM_API_KEY
UPSTAGE_BASE_URL = LLM_BASE_URL

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


def llm_ready() -> bool:
    """활성 LLM을 호출할 준비가 되었는지(키 존재 여부)."""
    return bool(LLM_API_KEY)


def get_client() -> OpenAI:
    """활성 provider의 OpenAI 호환 클라이언트를 지연 생성한다(키가 없으면 명확한 에러)."""
    global _client
    if _client is None:
        if not LLM_API_KEY:
            raise RuntimeError(
                f"LLM_API_KEY가 설정되지 않았습니다(LLM_PROVIDER={LLM_PROVIDER}). "
                ".env 파일이나 환경변수로 설정하세요."
            )
        _client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    return _client
