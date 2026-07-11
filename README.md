# 사실 판단 Agent (Fact-Check Agent) — Grok + Tool Use

버그바운티 리포트의 claim을 **LLM(Grok)이 도구를 하나씩 호출**하며 실제 코드베이스와 대조해
검증하는 Agent입니다. rule-base가 아니라 `사실판단_Agent_Gemini.ipynb`의 도구 호출 루프 패턴을
따릅니다 — LLM이 어떤 함수/헤더/커밋/호출을 검증할지 스스로 판단해 도구를 호출하고, **도구는
실제 코드베이스를 결정론적으로 조회**합니다.

## 도구 (LLM이 function calling으로 호출)

| 도구 | 검증 | 백엔드 |
|---|---|---|
| `symbol_lookup(name)` | 함수 존재 유무 | ctags/grep 심볼 인덱스 (+ git 이력) — 결정론 |
| `git_history_query(ref)` | 커밋 존재 유무 | `git cat-file` — 결정론 |
| `header_lookup(name)` | 헤더 파일 존재 유무 | 표준/시스템 헤더 화이트리스트 + 저장소 파일명 대조 — 결정론 |
| `function_call(call)` | 올바른 함수 사용법 | **별도 LLM 호출**(보고서 원문 맥락) → valid/reason/confidence |

`symbol_lookup / git_history_query / header_lookup`는 `fact_check_tools.py`(참고/엔진, 수정 금지)로
코드베이스를 결정론적으로 조회합니다. `function_call`은 **fact_check_agent와 분리된 새 LLM**이
판단하며(`function_call_checker.py`), **함수 호출 배열 전체를 한 번의 LLM 호출로** 미리 판단해 둡니다
(입력: 보고서 원문 + 함수 호출 배열 / 출력: 호출별 `valid · reason · confidence`). 에이전트가 부르는
`function_call` 도구는 그 사전 판단 결과를 조회만 하므로, `/invoke` 1건당 함수 사용법 LLM 호출은 1회입니다.

### 헤더 검증 분류 (`header_lookup`)

헤더는 존재 여부만이 아니라 **출처(kind)**까지 4가지로 분류합니다(모두 결정론). 표준/시스템 헤더는
대상 저장소에 파일이 없어도 실존하는 헤더로 인정하므로, `#include <stdio.h>` 같은 인용이 오탐(없음)으로
잘못 판정되지 않습니다.

| 예 | exists | kind |
|---|---|---|
| `stdio.h`, `string.h`, `stdint.h` | true | `standard_library` (C 표준 헤더 화이트리스트) |
| `unistd.h`, `<sys/socket.h>`, `netinet/in.h`, `winsock2.h` | true | `system` (POSIX/시스템 헤더 화이트리스트) |
| `curl_printf.h`, `curl/curl.h` | true | `project` (대상 저장소에 존재, `location` 포함) |
| `made_up.h` | false | `not_found` |

`<...>` / `"..."` / 경로(`curl/curl.h`)가 붙어 와도 정규화해서 매칭합니다.

## 동작 흐름 (Active Pull)

```
[오케스트레이터] ── POST /invoke {report_id, trace_id, request_id} ──▶ [Fact-Check Agent]
       │                                                                    │
       ├── GET  db/workflows/{report_id}  (리포트 구조화 JSON 조회) ◀───────┤
       └── POST db/{report_id}/agents/fact_check/invocations       ◀────────┘ (최종 결과 등록)
```

1. `report_id`로 워크플로우 JSON을 조회 → `agent_results.parser` 확보
2. **함수 존재 검증 대상은 `cited_library_functions`(라이브러리 함수)만** 사용
   (`cited_user_defined_functions`는 존재 검증 대상이 아님)
3. LLM(Grok)이 도구를 하나씩 호출하며 검증 → 최종 `fact_check` JSON 생성
4. 최종 결과를 `invocations`로 등록 (best-effort)

## 파일 구조

```text
fact_check/
├── server.py                 # HTTP API (FastAPI) — GET /health, POST /invoke
├── runner.py                 # Agent 오케스트레이션 (build_claim + 도구 루프 실행)
├── agent.py                  # Agent 모델 + run(도구 호출 루프) + extract_json
├── tools.py                  # 도구 4종 (symbol_lookup/git_history_query/header_lookup/function_call) + 헤더 분류
├── function_call_checker.py  # function_call 판단용 별도 LLM 호출(배열 1회)
├── prompts.py                # Fact-check Agent 시스템 프롬프트
├── orchestrator.py           # 오케스트레이터 연동: fetch_workflow / post_invocation
├── config.py                 # 환경설정(LLM_*/ORCHESTRATOR_BASE_URL/REPO_PATH) + Grok 클라이언트
├── fact_check_tools.py       # 결정론 검증 엔진 (심볼/커밋/파일 인덱싱) — tools.py가 사용
├── main.py                   # CLI (python main.py <report_id>)
├── requirements.txt / Dockerfile / .dockerignore / .env.example / .gitignore
└── README.md

# 로컬 전용(참고용, git 업로드 제외 — .gitignore):
#   api.py           # fact_check_tools를 노출하는 예시 REST 서버(서비스는 server.py 사용)
#   fact_check.py    # (구) 오케스트레이션 — 현재는 runner.py로 이전됨
```

> 서비스 코어 오케스트레이션은 `runner.py`에 있습니다(server.py·main.py가 여기서 `run_fact_check`를
> import). `api.py`·`fact_check.py`는 업로드하지 않으며, 서비스는 이들을 import하지 않습니다.

## 요구사항

- Python 3.9+
- **git** + **universal-ctags** (심볼 인덱싱). 로컬 macOS 기본 `ctags`(BSD)는 `-R` 미지원 →
  `brew install universal-ctags` 권장(없으면 정규식 grep 폴백, 정확도↓). Docker엔 포함됨.
- 검증 대상 소스 저장소 (예: curl) — `REPO_PATH`
- **Grok(xAI) API 키** — https://console.x.ai (Agent가 도구 호출을 구동하므로 필수)

## 환경변수

`.env.example`를 복사해 `.env` 작성:

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `LLM_API_KEY` | ✅ | — | LLM 프로바이더 API 키 |
| `DATABASE_URL` | ✅ | - | 워크플로우 JSON을 조회할 오케스트레이터 DB 주소 |

### API

| 메서드 · 경로 | 설명 |
|---|---|
| `GET /health` | 헬스체크. `{"status":"up"}` / `{"status":"down"}` |
| `POST /invoke` | `{report_id, trace_id, request_id}` → 조회 → Agent 도구 검증 → DB 등록 |

**응답** (성공/에러 모두 `{status_code, message, output}` 봉투)

```json
{
  "status_code": 200,
  "message": "fact_check completed",
  "output": {
    "report_id": "RPT-CURL-0001",
    "trace_id": "",
    "request_id": "",
    "fact_check": {
      "function_check": [ { "name": "curl_mfprintf", "exists": true, "location": "lib/mprintf.c:123" } ],
      "file_check": [],
      "header_check": [ { "name": "curl_printf.h", "exists": true, "kind": "project", "location": "lib/curl_printf.h" } ],
      "commit_check": [],
      "function_call_check": [ { "call": "curl_mfprintf(stdout, user_input)", "valid": true, "reason": "..." } ],
      "poc_check": { "compilable": null, "compile_error": null },
      "reachability": { "verdict": "UNKNOWN", "reason": "..." },
      "summary": "..."
    }
  }
}
```

에러 시 `output`은 `null`, `status_code`는 HTTP 상태와 일치. 주요 코드:
조회 실패 `502`, `agent_results`/`parser` 없음 `422`, `LLM_API_KEY` 미설정 `500`,
LLM 호출 실패 `502`, 저장소 오류 `500`.

## Docker

```bash
docker build -t fact-check-agent .

docker run --rm -p 8000:8000 \
  -e LLM_API_KEY=YOUR_XAI_KEY \
  -e ORCHESTRATOR_BASE_URL=https://api.mingyo.kim \
  -v /host/path/curl:/repo -e REPO_PATH=/repo \
  fact-check-agent

# 또는 기동 시 clone
docker run --rm -p 8000:8000 \
  -e LLM_API_KEY=YOUR_XAI_KEY \
  -e REPO_URL=https://github.com/curl/curl -e REPO_PATH=/repo \
  fact-check-agent
```

이미지에 `git` + `universal-ctags` 포함. API 키/DB/저장소는 런타임에 `-e`/`-v`로 주입.
`.env`는 `.dockerignore`로 이미지에서 제외됩니다(`--env-file .env`로도 주입 가능).

## 입력 형식 (parser)

Agent가 검증하는 리포트 parser 필드:

```
parser["cited_library_functions"]  # 라이브러리 함수 → symbol_lookup (함수 존재 검증 대상)
parser["cited_headers"]            # 헤더 파일명 → header_lookup
parser["cited_commits"]            # 커밋 SHA/ref → git_history_query
parser["function_calls"]           # 함수 호출 문자열 → function_call (인자 개수 검증)
```

> `cited_user_defined_functions`(리포터 정의 함수)는 존재 검증 대상이 아닙니다.
> 파일 조회/PoC 컴파일/도달성 도구는 없으므로 `file_check=[]`, `poc_check=null`,
> `reachability=UNKNOWN`으로 둡니다.
