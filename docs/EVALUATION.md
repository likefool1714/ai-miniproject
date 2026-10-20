# 평가 설계

1. tests/test_recovery.py: 시계·데이터 주입, 실제 SQLite의 규칙/중복/신선도/순서/재개방
2. tests/test_recovery_api.py: 실제 stdio MCP와 FastAPI POST/SSE, 문맥·다운로드·실패
3. 브라우저: 버튼으로 5단계 변화·이력·보고서 다운로드
4. eval/compare_recovery.py: 원본 d1043ff와 현재 그래프의 고정 I/O 비교
5. tests/test_grounding.py: 근거·서비스·ID 검증과 모델 요청 계약(모의 HTTP)
6. eval/probe_grounding.py: 같은 로컬 모델의 작성 방식 비교, 실제 웹 일반 진단부터 복구·재발까지 재확인

## 전후 비교

같은 질문: payments-service의 복구 여부를 진단해 주세요.
같은 5개 관측과 고정 시계: 장애 → 부분 회복 → 정상 1회 → 정상 2회 → 재발.
버전별 이전 대화는 자기 응답을 저장한다. MCP는 고정 fixture를 주입한다.
실제 MCP 통신·브라우저·운영 서버 지연을 포함하는 측정이 아니다.

원본은 LLM 진단을 수행하고 새 복구 경로는 코드만 수행한다.
시간 차이는 LLM 호출 생략을 포함한 경로 변경의 효과이며 동일 추론을 가속한 것이 아니다.
원본에 구조화 판정이 없다는 이유만으로 자연어를 오답 처리하지 않는다. 원문을 별도 검토한다.

## 지표

기대 phase/recurrence 일치 수, 잘못된 복구 확정, 중복 처리, 조회 건수, 지연 중앙값.
fake 검사는 실제 모델 자연어 품질 검사가 아니다.
cost_usd는 가상 단가 추정이며 실제 청구액이 아니다.

일반 진단 재검증은 docs/GROUNDING_VALIDATION.md에 별도 기록한다. 모델 제안 검증 통과는 실제 장애 원인 정답률이 아니다.
API 모의 요청 검사는 유효 키를 사용한 제공 API 실호출 확인이 아니다.
