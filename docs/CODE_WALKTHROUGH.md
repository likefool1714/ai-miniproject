# 코드 흐름 설명

## 먼저 이해할 기존 흐름

1. `web_chat.html`: 사용자가 입력합니다. `web_demo_server.py`가 정적 화면과 API 프록시를 제공합니다.
2. `server.py`: FastAPI의 lifespan에서 LLM/MCP/SQLite 의존성을 만들고 그래프를 한 번 생성합니다.
3. `/chat` 또는 `/chat/stream`: thread_id의 대화 이력을 읽고 그래프를 실행합니다.
4. `agent/graph.py`: route → plan → gate → research → replan_check → analysis → write → validate → finalize 순서입니다. 인사는 chat_answer로 바로 갑니다.
5. `agent/mcp_client.py`: stdio로 별도 MCP 프로세스에 연결합니다. `mcp_server.py`가 모니터링 Tool, 런북 Resource, 진단 절차 Prompt를 제공합니다.
6. `agent/deps.py`: 노드가 사용하는 의존성을 명시적으로 주입합니다. fake/Ollama/API 구현은 같은 인터페이스로 교체합니다.
7. `agent/session_store.py`: SQLite에 대화를 저장합니다. 서버가 이력을 LLM에 전달해야 모델이 문맥을 사용할 수 있습니다.
8. `write` 결과를 검증한 후 답변을 전달합니다. SSE는 노드 이벤트/답변 토큰을 웹에 보냅니다.

주의: 현재 모니터링은 `data/monitoring.json`의 **샘플**에 난수를 더한 것입니다. 실제 인프라에 연결한 모니터링 시스템이 아닙니다.

## 현재 코드에서 확인한 개선 위치

| 위치 | 기존 동작 | 변경 계획 |
|---|---|---|
| `agent/graph.py::_extract_system_name` | 명시 대상 없으면 auth-service | 기본값 제거, 공통 대상 판별 사용 |
| `agent/ollama_llm.py::_guess_target(s)` 및 plan 프롬프트 | 명시 대상 없으면 auth-service | 모델 출력/실행 대상 검증, 확인 분기와 일관되게 처리 |
| `agent/openai_llm.py` 동일 함수/프롬프트 | Ollama와 같은 기본값 | 백엔드마다 동작이 달라지지 않도록 공통 처리 |
| `agent/fake_llm.py` | 결정론 예제 | 새 분기를 확인할 수 있게 동작 정합성 점검 |
| `agent/graph.py::_classify_incident_type` | 알림 영어 키워드 순서 매칭, 실패 시 db-down | 검색 모듈로 교체 가능하게 분리 |
| `agent/graph.py::replan_check_node` | 선택한 런북 task 추가 | 검색 결과/실패 상태를 명시적으로 연결 |
| `server.py` POST/SSE | 각 경로에 실행 및 저장 흐름 존재 | 확인 상태/이벤트 일관성 점검 |

## 개선 후 설명할 흐름 (설계, 미구현)

입력 → 의도 분류 → 등록 서비스 검사 → 누락이면 확인 질문 및 대기 상태 저장 → 후속 입력으로 대상 확정 → MCP 모니터링 조회 → 증상/알림 임베딩 → 로컬 런북 검색 → MCP Resource로 본문 조회 → 근거를 포함한 답변 생성 → 검증 → 답변/출처 반환.

그냥 `payments-service`라고 답하는 확인 후속 입력은 일반 대화로 분류되기 전에 대기 상태에서 원래 요청을 복원해야 합니다. 무관한 후속 입력, 취소, 반복된 불명확 입력도 종료 가능한 동작으로 설계합니다.

## 5분 설명 연습

- 30초: 기존 코드에서 발견한 두 문제와 재현 입력.
- 1분: 화면 → API → 그래프 → MCP → 답변의 전체 흐름.
- 1분: 대상 검사와 확인 대화 상태를 어디에 둔 이유.
- 1분: 임베딩/유사도/본문 전달이 각각 하는 일.
- 1분: 동일 평가 입력의 실제 결과와 실패 사례.
- 30초: 한계와 다음 개선.

## 예상 질문

- 왜 무조건 모델에게 맡기지 않았나요? 등록 서비스라는 검증 가능한 조건은 코드에서 확인할 수 있기 때문입니다.
- RAG가 무엇인가요? 관련 문서를 검색하고 그 본문을 답변 생성의 근거로 전달하는 흐름입니다.
- 왜 별도 벡터 DB 서버가 없나요? 현재 6개 문서여서 로컬 인덱스 전체 비교로 충분한지 측정하고, 운영 복잡성을 줄이려는 선택입니다.
- 제공 기능도 본인 구현인가요? 기존 LangGraph/MCP/UI는 제공 코드이며, 직접 바꾼 두 기능과 평가 부분은 Git 차이로 보여드립니다.
- 확인 질문이 늘면 느려지지 않나요? 확인 전 불필요한 조회를 줄이는 대신 추가 사용자 턴이 필요합니다. 턴당 지연과 해결까지 걸린 턴 수를 함께 보여드립니다.
