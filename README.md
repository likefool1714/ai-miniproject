# Incident Runbook Agent

운영 장애 대응 에이전트의 **대상 서비스 확인**과 **런북 검색**을 개선하는 개인 미니프로젝트입니다.

> 현재 단계: 제공 스켈레톤 보존 및 개발 계획 정리. 아래 개선 기능과 평가 실행기는 아직 구현 전입니다. 원본 예제의 실행 화면은 이번 프로젝트의 실험 결과가 아닙니다.

## 선택안

- 과제 유형: **1번 - Base Agent 기능 개선/추가**
- 핵심 개선 A: 대상 서비스 누락/미등록 시 임의로 `auth-service`를 조사하는 동작 제거 (예시 7번)
- 핵심 개선 B: 영어 키워드 우선순위로 고르는 런북을 의미 기반 검색으로 개선 (예시 1번)
- 공통 산출물: 동일 입력의 Before/After 평가, 코드 흐름 설명, 한계 분석, 발표 자료

## 해결할 문제

1. `로그인 응답이 느려요`처럼 대상이 없는 요청에서 시스템을 임의로 확정합니다. 사용자에게 대상을 확인한 뒤 조사를 진행하도록 바꿉니다.
2. 현재 `_classify_incident_type()`는 모니터링 알림의 영어 키워드를 순서대로 검사합니다. 사용자 증상과 관측 알림을 함께 검색해 관련 런북의 본문과 출처를 답변 근거로 전달합니다.

검색이 항상 더 정확하거나 더 빠르다고 가정하지 않습니다. 동결한 평가셋에서 실제 결과와 실패 사례를 기록합니다.

## 제공 기능과 직접 개선할 기능

| 구분 | 내용 |
|---|---|
| 제공 스켈레톤 | FastAPI, Vue 채팅, SSE, LangGraph, MCP Tool/Resource/Prompt, SQLite, 재시도, 백엔드 전환, LangSmith 연동 |
| 직접 구현 예정 | 대상 확인 분기, 확인 대화 상태, 검색 모듈, 인덱스 생성, 검색 근거 노출, 평가셋과 비교 실행기 |
| 이번 범위에서 제외 | 운영 서버 실제 제어, 추가 멀티에이전트 구조, 승인/재개 시스템, 로그인/배포, 대규모 벡터 DB |

## 문서

- [선택 이유와 개발 일정](docs/PROJECT_PLAN.md)
- [등록용 진행 계획 초안](docs/REGISTRATION_DRAFT.md)
- [코드 흐름과 변경 위치](docs/CODE_WALKTHROUGH.md)
- [평가 방법과 기록 양식](docs/EVALUATION.md)
- [한계와 트레이드오프](docs/TRADEOFFS.md)
- [발표 7장 구성안](docs/PRESENTATION_OUTLINE.md)
- [제공 코드 보존과 공개 범위](docs/PROVENANCE.md)
- [제공 스켈레톤 매뉴얼](doc/README.md)

## 현재 실행 방법: 스켈레톤 기준

```bash
bash sh/setup_venv.sh
cp .env.example .env
# .env를 로컬에서 편집합니다. 키를 Git에 넣지 않습니다.
bash sh/setup_ollama.sh
bash sh/run_agent_ollma.sh
# 다른 터미널
bash sh/run_frontend.sh
```

접속: `http://localhost:8600`. API: `http://localhost:8100/health`.

스크립트는 환경을 변경하므로 내용을 확인하고 실행합니다. 특히 제공 `run_agent_*.sh`는 시작 시 `session.db`를 삭제하므로, 대화 보존 확인/평가 실행 시에는 DB를 보존하는 별도 실행 방법이 필요합니다.

개발은 fake로 분기 확인 → Ollama 실모델 검증 → 제공 API의 허용 모델로 최종 호환성 확인 순서로 진행합니다. API 키와 허용 모델은 로컬 설정에서 확인해야 합니다.

## 비교 기준

기준 커밋: `d1043ff` (`chore: import provided project skeleton`). 원본의 v1/v2는 이미 서로 다른 예제이므로, 이번 개선 비교는 **동일한 v2에서 선택 기능만 변경**하여 진행합니다.

실험 결과는 아직 없습니다. `results/`에 실제 측정 후 기록합니다.
