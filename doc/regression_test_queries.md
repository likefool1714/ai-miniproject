# 회귀 테스트용 쿼리 목록 — 시스템명 하드코딩 수정 검증

| # | 쿼리 | 테스트 목적 |
|---|---|---|
| 1 | payments-service 결제 응답이 느려지고 CPU가 치솟았어요, 원인 확인해주세요 | 기존엔 auth-service로 조용히 되돌아갔던 4개 시스템 중 하나 — KNOWN_SYSTEMS 인식 확인 (cpu-spike runbook) |
| 2 | cache-service 메모리 사용률이 계속 올라가고 있어요 | 미인식 시스템 확인 2 (memory-leak runbook) |
| 3 | log-service 디스크 공간이 부족하다는 알림이 왔어요 | 미인식 시스템 확인 3 (disk-full runbook) |
| 4 | gateway-service에서 payments-service로 가는 요청이 타임아웃 나고 있어요 | 두 시스템 동시 언급 — system_name은 먼저 등장한 gateway-service 하나만 뽑히는지, 그런데도 research task는 두 시스템 다 조사하는지(비교 분석 영향 없음) 확인 |
| 5 | web-01 CPU 급증, auth-service 커넥션 풀도 같이 확인해줘 | 기본 2개 시스템 동시 언급 — 기존 동작(web-01, auth-service 둘 다 조사) 회귀 확인 |
| 6 | payments-service랑 cache-service 둘 다 저하됐는데 공통 원인이 있는지 분석해줘 | needs_analysis=true 유도 — gate(W6)·analysis_node 비교 분석이 여전히 동작하는지 확인 |
| 7 | 로그인 응답이 느려요, 원인 확인해주세요 | 시스템명 미언급 — auth-service 폴백 확인 |
| 8 | billing-service 응답이 느려요 | 목록에 없는 시스템명 — system_name 라벨은 auth-service 폴백, plan()의 target 클램프도 정상 동작하는지(KNOWN_SYSTEMS에 없는 임의 이름은 여전히 걸러져야 함) |
| 9 | 안녕, 너는 누구야? | route=chat 직행(Wave 5) — 토큰 스트리밍(AnswerToken)이 채팅 경로에서도 실시간으로 오는지 확인 |
| 10 | 제 이름은 민수입니다 → (다음 메시지로) 제 이름이 뭐였죠? | Wave 12 세션 기억 — 서버 재시작 후에도 유지되는지, 스트리밍과 충돌 없는지 확인 |
