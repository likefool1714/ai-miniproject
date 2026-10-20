| 시나리오 | 완료 | 기대 대상 처리 일치 | 실제 조회 대상(회차순) | 중앙값(초) | 범위(초) |
|---|---:|---:|---|---:|---|
| 인사 | 3/3 | 3/3 | 조회 없음 / 조회 없음 / 조회 없음 | 1.273 | 1.23-8.902 |
| 결제 서비스 명시 | 3/3 | 3/3 | payments-service / payments-service / payments-service | 32.711 | 32.506-35.426 |
| 서비스명 누락 | 3/3 | 0/3 | auth-service / auth-service / auth-service | 32.816 | 31.814-44.766 |
| 미등록 billing-service | 3/3 | 0/3 | auth-service / auth-service / auth-service | 39.69 | 34.815-41.111 |
| 결제+캐시 복수 서비스 | 3/3 | 0/3 | payments-service / payments-service / payments-service | 54.015 | 50.25-60.16 |
| 게이트웨이 타임아웃 | 3/3 | 3/3 | gateway-service / gateway-service / gateway-service | 36.895 | 35.414-37.789 |
