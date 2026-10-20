# AI Mini Project

**운영 장애 대응 에이전트의 상태 변화 비교·복구 확인 기능 개선**

- 선택 과제: **1번 — 제공 Base AI Agent 기능 개선/추가**
- 저장소: https://github.com/likefool1714/ai-miniproject (Public)
- 진행 계획 초안 제출: **2026-10-20 17:00 KST**
- 현재: 등록한 개선·추가 기능 구현, 단위·실제 MCP/API 검사 20개 통과. 원본/개선 각 15회 비교와 발표용 8장 PPT 작성 완료.
- 제공 API / LangSmith: 로컬 키가 없어 실제 연결은 미검증.

기존 에이전트는 현재 모니터링 수치를 조회하고 런북으로 대응 절차를 작성합니다. 이번 작업은 **이전 조사와 비교하고 복구 여부를 다시 확인하는 기능**을 추가합니다.
CPU가 95%에서 55%로 내려가도 메모리·알림이 남아 있거나 정상 관측이 한 번뿐이면 복구를 확정하지 않습니다.

> 샘플 JSON·합성 시나리오 기반입니다. 실제 운영 서버 조회·자동 조치·실제 복구 증명이 아닙니다.

## 등록한 범위와 구현

| 구분 | 등록 내용 | 구현 |
|---|---|---|
| 개선 | 이전·현재 상태 비교 | CPU/메모리 변화(%p), 상태·알림 함께 비교 |
| 개선 | 복구·부분 회복·장애 지속·판단 보류 | 설정 파일의 정책으로 코드에서 판정 |
| 개선 | 일시적 정상화 처리 | 최근 장애 이후 최소 5초 간격 정상 2회 요구 |
| 개선 | 후속 대화 대상 유지 | 같은 thread의 대상 복원, 여러 대상이면 확인 질문 |
| 개선 | 데이터 부족 안내 | 첫 관측/오래된 자료/잘못된 자료는 판정 제한 |
| 추가 | 조사 이력 저장·조회 | SQLite, 대화·서비스별 분리, 재시작 후 유지 |
| 추가 | 상태 변화 비교 | 변화량과 알림 발생·해소 표시 |
| 추가 | 복구 재점검 | 사용자 요청 시 MCP Tool로 새 관측 조회 |
| 추가 | 장애 재발 감지 | 최근 복구 확인 이후 이상이 재관측되면 재발 표시 |
| 추가 | Markdown 보고서 | 현재 판정·이력·정책·한계를 포함한 다운로드 |

같은 관측 ID는 정상 횟수에 다시 추가하지 않습니다. 같은 ID의 내용 충돌·최근 재점검 실패는 보고서에서도 판단 보류합니다.
이력과 보고서 조회는 새 모니터링을 실행하지 않습니다.

## 처리 흐름

```text
Vue → FastAPI → 대화별 요청 직렬화 → 요청/대상 확인
  ├─ 일반 대화·기존 진단 → 기존 Supervisor / MCP / LLM
  │                       → 관측 저장·복구 판정 → 진단 + 정책 판정
  ├─ 복구 재점검 → MCP 조회 → 관측 저장 → 변화 비교 → 복구·재발 판정
  ├─ 조사 이력 → SQLite 조회
  └─ 보고서 → 최신 기록 유효성 재평가 → Markdown 다운로드
```

기존 진단의 분류·계획·조언에는 LLM을 사용합니다.
**새 복구 재점검의 수치 비교·최종 판정은 코드가 수행**하고 LLM 호출을 생략합니다.
기준을 일정하게 유지하고 지연을 줄이기 위한 선택입니다.

## 복구 정책

`data/recovery_policy.json`에서 관리합니다.

| 항목 | 기본값 |
|---|---|
| 정상 조건 | healthy, CPU < 80%, 메모리 < 85%, 활성 알림 0건 |
| 복구 확인 | 최근 장애 이후 정상 2회, 관측 사이 최소 5초 |
| 자료 유효시간 | 300초 |
| 재발 연관 기간 | 이전 복구 확인 후 3600초 |
| 이전 장애 기록 없음 | 현재 정상이어도 복구로 단정하지 않음 |

CPU 95% → 65%는 **-30%p**입니다. 지표 방향이 다르면 “혼재”로 표시합니다.
임계값·정상 2회 기준은 교육용 정책이며 실제 SLO가 아닙니다.

## 실행

검증 환경: Python 3.12, Ollama `qwen3:0.6b` / `qwen3:8b`.

```bash
# 최초 환경 준비
bash sh/setup_venv.sh

# 터미널 1: 이미 실행 중이면 중복 실행하지 않습니다.
ollama serve

# 터미널 2: 기본 샘플 모니터링 + 로컬 모델
LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false \
LLM_BACKEND=ollama SUPERVISOR_VERSION=v2 .venv/bin/python server.py

# 터미널 3: 웹
.venv/bin/python -m uvicorn web_demo_server:app --host 127.0.0.1 --port 8600
```

웹: http://localhost:8600 / 상태: http://localhost:8100/health

### 복구·재발 데모

기본 난수 샘플은 회복 과정을 재현하지 않습니다. 고정 합성 시나리오는 다음처럼 백엔드를 실행합니다.

```bash
LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false \
LLM_BACKEND=ollama SUPERVISOR_VERSION=v2 \
MONITORING_SCENARIO=recovery-demo SESSION_DB_PATH=recovery-demo.db \
.venv/bin/python server.py
```

1. **새 조사**를 누르고 `payments-service 복구 재점검`을 입력합니다.
2. **복구 재점검**을 네 번 누릅니다.
3. `장애 지속 → 부분 회복 → 부분 회복(정상 1회) → 복구 확인 → 장애 재발`을 확인합니다.
4. **조사 이력**, **Markdown 보고서**로 기록과 다운로드를 확인합니다.

시나리오는 대화·서비스별로 재생합니다. 마지막 스텝 재조회는 같은 ID이므로 횟수가 늘지 않습니다.
오래된 시나리오는 판단 보류하며 **새 조사**로 새 대화를 시작합니다.
추가 시나리오: `temporary-normal`, `stale-data`. 출처를 응답에 표시합니다.

## 검사·전후 비교

- **20개 검사 통과**: 규칙, 중복/순서/신선도, 재발, 재개방, 대화 분리·동시 요청, 실제 stdio MCP와 POST/SSE, 다운로드, 시간 초과·재시도.
- 실제 브라우저에서 5단계 변화·이력·보고서 다운로드 확인.
- 원본 `d1043ff`와 현재 그래프에 같은 질문·고정 관측·고정 시계를 주입해 비교.
- 그래프 비교의 MCP는 고정 fixture입니다. 실제 MCP/HTTP 검사는 별도입니다.
- 원본에 구조화 판정이 없다는 이유만으로 자연어 답변을 오답 처리하지 않습니다.
- 기존 원본 18회 조사는 문제 탐색 기록이며 새 복구 기능의 비교와 구분합니다.

| 같은 5단계 관측을 3회 반복한 결과 | 원본 | 개선 |
|---|---|---|
| 정상 1회에서 조기 복구 확정 | 3/3건 | 0/3건 |
| 복구 후 재발 명시 | 0/3건 | 3/3건 |
| 정책 판정과 기대 결과 일치 | 자연어 원문 별도 검토 | 15/15건 |
| 고정 MCP fixture 그래프 지연 중앙값 | 28.79초 | 2.9ms |

지연 차이는 **새 복구 경로에서 LLM 호출을 생략한 효과를 포함**합니다. 실제 웹 응답시간으로 해석하지 않습니다.
별도의 실제 웹 프록시·FastAPI·stdio MCP 검사에서는 **15/15건** 기대 판정과 일치했고 중앙값은 **약 7.9ms**였습니다. 이 경로도 합성 데이터와 코드 기반 복구 판정을 사용합니다.
수치는 이 고정 시나리오의 결과이며 일반 장애 진단 정확도를 의미하지 않습니다.

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python eval/compare_recovery.py --backend ollama --repetitions 3
```

[새 기능 검증](docs/RECOVERY_VALIDATION.md) · [원본 검증](docs/BASELINE_VALIDATION.md)

[발표용 PPT 다운로드](output/presentation/AI-Mini-Project_G028_최민석.pptx)

## 평가 항목 대응

| 항목 | 대응 |
|---|---|
| 기능 완성도 30 | 등록한 10개 개선/추가 항목의 API·웹·저장·다운로드 |
| 전후 실측 보너스 10 | 같은 질문·관측, 원문·판정·시간·측정 범위 공개 |
| 기술 요소 30 | 기존 Multi-Agent/MCP, LangGraph 분기, SQLite, 의존성 주입, 모듈화, 단위·통합 검사 |
| 한계 20 | 확인 지연, 보수적 보류, 저장 비용, 정책·샘플 의존성 |
| PPT·발표 20 | 문제 → 코드 → 실측 → 한계 순서의 8장 PPT 작성. 실제 발표는 대기 |

RAG·LLM-as-a-Judge를 새 구현으로 기재하지 않습니다. API·LangSmith도 실제 확인 전 완료로 표시하지 않습니다.

## 직접 기여

- 새 모듈: `agent/recovery.py`, `recovery_store.py`, `recovery_nodes.py`, `monitoring.py`
- 새 정책·데이터: `data/recovery_policy.json`, `data/scenarios/`
- 연결: 기존 그래프·FastAPI·MCP·Vue에 노드/경로/버튼 추가
- 검사: `tests/`, `eval/compare_recovery.py`

제공 Supervisor, MCP Tool/Resource/Prompt, 모델 백엔드, Vue/SSE, 대화 저장, 의존성 주입 구조를 재사용했습니다.

## 한계·트레이드오프

- 정상 2회·시간 간격 때문에 회복 판단이 늦을 수 있고 관측 사이 정상은 보장하지 않습니다.
- 사용자 결제 성공, 실제 원인 제거, 로그를 검증하지 않습니다.
- 규칙 기반 명령 해석은 지원 표현이 제한적이며 모호한 대상은 확인합니다.
- API는 최근 100건, 판정은 최대 500건을 읽습니다. 장기 보관·정리 정책은 미구현입니다.
- 로컬 데모로 인증·권한 통제, 운영 배포, 자동 조치, 주기적 감시는 범위 밖입니다.
- 기존 LLM 진단의 모든 사실 오류를 해결한 것은 아닙니다. 새 정책 판정과 원인 추정·권장 조언을 구분합니다.
- cost_usd는 가상 단가 추정이며 실제 청구액이 아닙니다.

## 문서

[등록 내용](docs/REGISTRATION_DRAFT.md) · [개발 계획](docs/PROJECT_PLAN.md) · [코드 흐름](docs/CODE_WALKTHROUGH.md) · [평가](docs/EVALUATION.md) · [한계](docs/TRADEOFFS.md) · [발표 구성](docs/PRESENTATION_OUTLINE.md) · [수업 연결](docs/REQUIREMENTS_CROSSCHECK.md) · [출처](docs/PROVENANCE.md)

교육용 제공 코드 기반입니다. 원본 커밋 `d1043ff`와 개선 전 증거를 보존합니다.
과제 PDF·배포 ZIP·키·세션 DB는 커밋하지 않습니다.
