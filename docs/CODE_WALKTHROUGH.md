# 코드 흐름과 변경 계획

현재 제품 코드는 제공 원본이며, 아래 개선 후 흐름은 설계 단계이다.

## 기존 요청 한 건의 흐름

1. `web_chat.html`에서 질문을 입력한다. `web_demo_server.py`가 정적 화면과 API 프록시를 제공한다.
2. `server.py`의 lifespan에서 LLM/MCP/SQLite 의존성과 그래프를 생성한다.
3. `/chat` 또는 `/chat/stream`이 thread별 이력을 읽고 그래프를 실행한다.
4. `agent/graph.py`의 route → plan → gate → research → replan_check → analysis → write → validate → finalize 순서를 따른다. 인사는 chat_answer로 바로 간다.
5. `agent/mcp_client.py`가 stdio MCP 프로세스에 연결한다. `mcp_server.py`는 모니터링 Tool, 런북 Resource, 진단 절차 Prompt를 제공한다.
6. `agent/deps.py`의 의존성 번들로 fake/Ollama/API 구현을 교체할 수 있다.
7. 답변과 대화를 SQLite에 저장한다. SSE에서는 단계 이벤트/토큰/최종 답변을 웹에 전달한다.

모니터링은 `data/monitoring.json` 샘플과 난수이다. 실제 인프라 조회나 운영 조치를 수행하지 않는다.

## 변경 위치

| 위치 | 현재 약점 | 개선 계획 |
|---|---|---|
| `graph.py::_extract_system_name` 및 백엔드 `_guess_target(s)` | 누락/미등록 대상의 기본값 대체 | 공통 대상 판별, 확인 분기 |
| `ollama_llm.py` / `openai_llm.py`의 plan 정규화 | 작업이 일부만 있으면 누락 대상 미보충 | 명시 대상과 계획 대상의 집합 대조 |
| `server.py` 및 `session_store.py` | 장애 계획에 대화 문맥 미전달 | 확인 대기 상태/원래 증상/대상 보존 |
| `graph.py::write_node`, 백엔드 writer | 관측과 권고 혼재, 미수집 출처 주장 | 관측/추정/조치/추가 확인의 구조화 |
| `graph.py::validate_node` | 길이·번호만 검사, 3회째 강제 통과 | 수치/출처 검사, 제한된 재작성과 실패 상태 |
| 모델 호출 / API / 웹 SSE | 전체 요청 예산·중단 안내 부족 | 생성량/시간 예산, 오류/중단 이벤트 |

## 개선 후 흐름 (미구현)

입력 → 확인 대기 상태와 문맥 확인 → 대상 검증 → 불명확하면 질문하고 대기 → 대상 확정 → 조사 계획 대상 대조 → MCP 조회 → 관측 사실 정리 → 분석/권고 생성 → 수치/출처 검증 → 제한된 재작성 또는 실패/부분 답변 → 근거와 상태를 포함해 반환.

`payments-service`처럼 서비스명만 답한 확인 후속 입력은 일반 대화로 분류하기 전에 원래 증상을 복원한다. “그 서비스” 참조는 thread의 명시된 조사 대상을 사용하되, 모호하면 다시 확인한다.

## 5분 설명 연습

- 30초: 원본에서 재현한 대상/근거 문제.
- 1분: 웹 → API → 그래프 → MCP → 답변의 흐름.
- 1분: 대상 검사와 확인 상태를 코드에 둔 이유.
- 1분: 관측 사실과 모델 권고의 구분, 검증 범위.
- 1분: 같은 입력의 실측 결과와 실패 사례.
- 30초: 추가 확인 턴, 검증 한계, 후속 개선.

제공 LangGraph/MCP/UI를 직접 새로 만들었다고 설명하지 않는다. Git 차이와 실행 증거로 직접 바꾼 부분을 제시한다.
