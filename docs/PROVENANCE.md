# 제공 코드·직접 기여

원본 d1043ff, 원본 검증 e139001. doc/ 매뉴얼·예제 이미지는 제공 자료다.

## 직접 구현

- recovery.py: 스키마·정책·신선도·변화·연속 정상·복구·재발 판정
- recovery_store.py: 관측·판정·대상·실패 저장과 보고서
- recovery_nodes.py: 대상/명령 해석과 기능 노드
- monitoring.py, data/scenarios/: 샘플/대화별 고정 시나리오
- grounding.py: 근거 묶음·구조화 제안 스키마·출처/적용 가능성 검증·답변 구성
- server.py, graph.py, deps.py: 그래프·API·주입·요청 잠금/시간 제한
- web_chat.html, web_demo_server.py: 기능 버튼·다운로드
- tests/, eval/compare_recovery.py, eval/probe_grounding.py: 검사·전후 비교·일반 진단 재검증
- eval/probe_provided_api.py: 실제 제공 API의 제한된 웹 진단·복구·이력·보고서 호환 검사

## 재사용

기존 Supervisor/Multi-Agent, MCP Tool/Resource/Prompt, 모델 백엔드, Vue/SSE, 대화 저장, 의존성 주입.
RAG·LLM 심판·실제 로그 조회·운영 조치를 새 구현으로 기재하지 않는다.
키·DB·과제 PDF·배포 ZIP은 커밋하지 않으며 제공 코드에 임의 라이선스를 부여하지 않는다.
