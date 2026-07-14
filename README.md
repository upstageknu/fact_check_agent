# 사실 판단 Agent (Fact-Check Agent) — Solar Pro 3 + Tool Use

버그바운티 리포트의 claim을 **Upstage Solar Pro 3가 도구를 하나씩 호출**하며 실제 코드베이스와 대조해
검증하는 Agent입니다. rule-base가 아니라 `사실판단_Agent_Gemini.ipynb`의 도구 호출 루프 패턴을
따릅니다 — LLM이 어떤 함수/헤더/커밋/호출을 검증할지 스스로 판단해 도구를 호출하고, **도구는
실제 코드베이스를 결정론적으로 조회**합니다.

> **결정론적 슬롯 채우기 (중요):** 최종 `fact_check` JSON의 **5개 검사 배열은 시스템이 도구 결과로
> 직접 채웁니다** — LLM이 최종 JSON에 값을 전사(轉寫)하지 않습니다. 이 때문에 LLM이 리포트에 없는
> 헤더/커밋/함수 호출을 지어내거나(할루시네이션) 프롬프트 예시를 베끼는 일이 원천 차단됩니다.
> LLM(fact_check_agent)의 실제 산출물은 **`summary` 하나**이며, 도구는 그 summary를 사실에
> 근거해 쓰기 위해 호출합니다. 자세한 내용은 아래 "[결정론적 슬롯 채우기](#결정론적-슬롯-채우기)" 참고.

## 도구 (LLM이 function calling으로 호출)

| 도구 | 검증 | 백엔드 |
|---|---|---|
| `symbol_lookup(name)` | 함수 존재 유무 | ctags/grep 심볼 인덱스 (+ git 이력) — 결정론 |
| `git_history_query(ref)` | 커밋 존재 유무 | `git cat-file` — 결정론 |
| `header_lookup(name)` | 헤더 파일 존재 유무 | 표준(C/C++)/시스템 헤더 화이트리스트 + 저장소 파일명 대조 — 결정론 |
| `function_call(call)` | 올바른 함수 사용법 | **별도 LLM 호출**(보고서 원문 맥락) → valid/reason/confidence |
| `poc_reproduce()` | PoC 실제 재현 | **Docker 샌드박스**에서 컴파일/실행 + reporter 주장 대조 (`poc_repro/` 패키지) |

`symbol_lookup / git_history_query / header_lookup`는 `fact_check_tools.py`(참고/엔진, 수정 금지)로
코드베이스를 결정론적으로 조회합니다. `function_call`은 **fact_check_agent와 분리된 새 LLM**이
판단하며(`function_call_checker.py`), **함수 호출 배열 전체를 한 번의 LLM 호출로** 미리 판단해 둡니다
(입력: 보고서 원문 + 함수 호출 배열 / 출력: 호출별 `valid · reason · confidence`). 에이전트가 부르는
`function_call` 도구는 그 사전 판단 결과를 조회만 하므로, `/invoke` 1건당 함수 사용법 LLM 호출은 1회입니다.
판단 결과의 `call` 문자열은 **리포터 원문을 그대로 보존**합니다(판단 LLM이 이스케이프 등을 정규화해 바꿔
써도, 입력 순서에 맞춰 원본 문자열로 되돌림).

### PoC 재현 (`poc_reproduce`)

리포트에 PoC 코드가 있으면(`poc_present`/`poc_code`), 이를 **Docker 네트워크 격리 샌드박스**에서
실제로 컴파일·실행하고 결과를 reporter의 주장과 대조합니다. 내부적으로 `poc_repro/` 패키지의
`run_pipeline`(후보 추출 → LLM harness 생성 → Docker build/run → LLM judge)을 사용하며, 결과를
compact dict로 요약합니다.

| 필드 | 의미 |
|---|---|
| `verdict` | `REPRO_LIKELY`(크래시/재현 유력) · `NONZERO_EXIT` · `RAN_CLEAN`(정상 종료) · `COMPILE_FAILED` · `TIMEOUT` · `NEEDS_MANUAL_REVIEW` · `OUT_OF_SCOPE_REJECT` · `NOT_EXECUTED` |
| `compilable` / `compile_error` | 컴파일 성공 여부 / 실패 시 stderr |
| `judgement` | reporter 주장 vs 실제 실행 대조: `confirmed` · `partially_supported` · `not_supported` · `inconclusive` 등 |

- compile/run/`verdict`는 Docker exit code + 크래시 패턴 매칭이라 **완전 결정론적**이고,
  `judgement`만 `poc_repro` 내부의 제약된 단일 judge LLM 호출 결과입니다.
- **Docker 데몬이 필요**하며, 재현 불가(Docker 미실행 등) 시 `verdict=NOT_EXECUTED`로 무죄추정 처리합니다.
- curl scope gate가 대상(`curl_cli`/`libcurl`)이 아닌 PoC는 실행 전 `OUT_OF_SCOPE_REJECT`로 반려합니다.
- 사용자 원문에 있는 정확한 `affected_version=X.Y.Z`와 로컬 `REPO_PATH`의
  `curl-X_Y_Z` tag가 일치하면 curl CLI/libcurl을 함께 빌드한 버전 이미지를 재사용합니다.
  범위·복수 버전·없는 tag는 임의 대체하지 않고 `ENVIRONMENT_UNAVAILABLE`로 보류합니다.
- 실행 직전 `curl --version`과 `curl-config --version`을 모두 확인하며 요청/실제 버전과
  `match_status`는 `poc_check.reproduction_environment`에 기록됩니다.
- PoC 컨테이너는 network 차단 외에도 capability 제거, `no-new-privileges`, read-only rootfs,
  PID/메모리/CPU 제한을 적용합니다.

### 헤더 검증 분류 (`header_lookup`)

헤더는 존재 여부만이 아니라 **출처(kind)**까지 5가지로 분류합니다(모두 결정론). 표준/시스템 헤더는
대상 저장소에 파일이 없어도 실존하는 헤더로 인정하므로, `#include <stdio.h>`나 C++ PoC의 `#include <iostream>`
같은 인용이 오탐(없음)으로 잘못 판정되지 않습니다.

| 예 | exists | kind |
|---|---|---|
| `stdio.h`, `string.h`, `stdint.h` | true | `standard_library` (C 표준 헤더 화이트리스트) |
| `iostream`, `thread`, `chrono`, `unordered_map`, `stdexcept` | true | `cpp_standard_library` (C++ 표준 헤더 화이트리스트, 확장자 없음) |
| `unistd.h`, `<sys/socket.h>`, `netinet/in.h`, `winsock2.h` | true | `system` (POSIX/시스템 헤더 화이트리스트) |
| `curl_printf.h`, `curl/curl.h` | true | `project` (대상 저장소에 존재, `location` 포함) |
| `made_up.h` | false | `not_found` |

`<...>` / `"..."` / 경로(`curl/curl.h`)가 붙어 와도 정규화해서 매칭합니다. C++ PoC가 인용하는
확장자 없는 표준 헤더(`<vector>`, `<memory>` 등)와 C 호환 헤더(`<cstdio>` 등)도 인식합니다
(curl은 C 저장소라 파일로는 없지만 실존하는 표준 헤더이므로 `not_found` 오탐을 방지).

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
3. Solar Pro 3가 도구를 하나씩 호출하며 검증하고 `summary`를 작성
4. **5개 검사 배열은 시스템이 도구 결과로 결정론적으로 채움**(아래 참고) → 최종 `fact_check` JSON 생성
5. **모든 과정을 `addLog`로 DB(events)에 실시간 기록**(설명가능 AI): 사실 판단 시작/완료,
   함수 사용법 사전 판단 시작/완료, 그리고 **각 도구 호출의 시작(`tool_start`)·종료(`tool_end`)**.
   최종 결과는 `invocations`로 등록. (로그·등록 모두 best-effort)

## 결정론적 슬롯 채우기

최종 `fact_check` JSON의 **모든 사실 판단 배열은 LLM 전사에 의존하지 않고 시스템이 직접 채웁니다.**
루프 종료 후 `runner.run_fact_check`가 각 슬롯을 도구 결과로 덮어씁니다. 이렇게 하면 LLM이 리포트에
없는 항목을 지어내거나 프롬프트 예시를 복사하는 할루시네이션이 구조적으로 불가능해집니다.

| 슬롯 | 채우는 주체 | 근거(parser 필드) |
|---|---|---|
| `library_function_check` | 시스템 (`symbol_lookup`) | `cited_library_functions` |
| `header_check` | 시스템 (`header_lookup`) | `cited_headers` |
| `commit_check` | 시스템 (`git_history_query`) | `cited_commits` |
| `function_call_check` | 시스템 (사전 판단 캐시, `call` 원문 보존) | `function_calls` |
| `poc_check` | 시스템 (`poc_reproduce` 결과) | `poc_present` / `poc_code` |
| **`summary`** | **LLM (fact_check_agent)** | 위 도구 결과 전체 |

- 리포트에 해당 claim이 없으면(`cited_headers=[]` 등) 그 배열은 정확히 `[]`가 됩니다.
- LLM 최종 JSON이 파싱에 실패해도 5개 배열은 시스템 값으로 정상 채워지고, `summary`만 폴백 문자열이 됩니다.
- `poc_check`는 에이전트가 `poc_reproduce`를 이미 호출했으면 그 결과를 재사용하고, PoC가 있는데
  호출하지 않았으면 시스템이 한 번 실행합니다(이중 Docker 실행 방지).

## 파일 구조

```text
fact_check/
├── server.py                 # HTTP API (FastAPI) — GET /health, POST /invoke
├── runner.py                 # Agent 오케스트레이션 (build_claim + 도구 루프 + 5개 슬롯 결정론 채우기)
├── agent.py                  # Agent 모델 + run(도구 호출 루프) + extract_json
├── tools.py                  # 도구 (symbol_lookup/git_history_query/header_lookup/function_call) + 헤더 분류(C/C++/시스템)
├── function_call_checker.py  # function_call 판단용 별도 LLM 호출(배열 1회, call 원문 보존)
├── poc_tool.py               # poc_reproduce 도구 래퍼 (poc_repro 패키지 ↔ fact_check 연결)
├── poc_repro/                # PoC 재현 파이프라인 패키지 (후보 추출 → harness → Docker build/run → judge)
│   ├── pipeline.py, extract_candidates.py, llm_harness.py, llm_judge.py, curl_scope.py, ...
│   ├── docker/               # 재현용 Docker 컨텍스트 (Dockerfile + runner.py)
│   └── data/, testcases/
├── prompts.py                # Fact-check Agent 시스템 프롬프트 (summary만 산출, 배열은 시스템 채움)
├── orchestrator.py           # 오케스트레이터 연동: fetch_workflow / post_invocation / addLog
├── config.py                 # 환경설정(UPSTAGE_*/SOLAR_MODEL/ORCHESTRATOR_BASE_URL/REPO_PATH)
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
- **Docker 데몬** — `poc_reproduce`(PoC 재현)가 사용. 없으면 PoC 재현은 `NOT_EXECUTED`로 건너뛰고
  나머지 검증은 정상 동작합니다.
- 검증 대상 소스 저장소 (예: curl) — `REPO_PATH`
- **Upstage API 키** — https://console.upstage.ai (Solar Pro 3 도구 호출에 필수)

## 환경변수

`.env.example`를 복사해 `.env` 작성:

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `UPSTAGE_API_KEY` | ✅ | — | Upstage API 키 |
| `UPSTAGE_BASE_URL` |  | `https://api.upstage.ai/v1` | Upstage OpenAI 호환 API 주소 |
| `SOLAR_MODEL` |  | `solar-pro3` | 호출할 Solar 모델 |
| `ORCHESTRATOR_BASE_URL` | ✅ | — | 워크플로우 API 기본 주소 |
| `DATABASE_URL` |  | — | 기존 실행 환경용 API 주소 별칭 |

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
      "library_function_check": [ { "name": "curl_mfprintf", "exists": true, "location": "lib/mprintf.c:123" } ],
      "header_check": [ { "name": "curl_printf.h", "exists": true, "kind": "project", "location": "lib/curl_printf.h" } ],
      "commit_check": [ { "ref": "abc123", "exists": true } ],
      "function_call_check": [ { "call": "curl_mfprintf(stdout, user_input)", "valid": true, "reason": "...", "confidence": 0.8 } ],
      "poc_check": { "verdict": "RAN_CLEAN", "compilable": true, "compile_error": null, "reproduced": true, "judgement": "not_supported" },
      "summary": "..."
    }
  }
}
```

에러 시 `output`은 `null`, `status_code`는 HTTP 상태와 일치. 주요 코드:
조회 실패 `502`, `agent_results`/`parser` 없음 `422`, `UPSTAGE_API_KEY` 미설정 `500`,
LLM 호출 실패 `502`, 저장소 오류 `500`.

## Docker

```bash
docker build -t fact-check-agent .

docker run --rm -p 8000:8000 \
  -e UPSTAGE_API_KEY=YOUR_UPSTAGE_KEY \
  -e ORCHESTRATOR_BASE_URL=https://api.mingyo.kim \
  -v /host/path/curl:/repo -e REPO_PATH=/repo \
  fact-check-agent

# 또는 기동 시 clone
docker run --rm -p 8000:8000 \
  -e UPSTAGE_API_KEY=YOUR_UPSTAGE_KEY \
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
parser["poc_present"] / parser["poc_code"]  # PoC 코드 → poc_reproduce (Docker 재현)
```

> `cited_user_defined_functions`(리포터 정의 함수)는 존재 검증 대상이 아닙니다.
> 각 검사 배열은 위 parser 필드를 근거로 시스템이 결정론적으로 채웁니다(claim에 없으면 `[]`).
