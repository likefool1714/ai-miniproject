# 코드 흐름

## 조립

server.py lifespan: 모델·MCP·대화 저장소·RecoveryStore를 만들어 SupervisorDeps로 주입한다.
같은 대화의 POST/SSE는 잠금으로 순서대로 실행하며 전체 요청에 시간 제한을 둔다.

## 요청 해석

recovery_nodes.request_context_node가 서비스와 명령을 해석하고 같은 thread의 대상을 복원한다.
누락·미등록·여러 대상 중 단수 참조는 확인 질문으로 종료한다.
확인 대기 요청을 저장해 서비스 이름만 답해도 원래 요청을 이어간다.

## 기존 진단

route → plan → gate → research → replan_check → analysis → record_observations → write → validate → finalize.
계획에서 명시한 대상이 누락/대체되면 보정한다.
기존 LLM Writer 검증을 전면 교체하지 않았고 정책 기반 상태 판정을 별도로 덧붙인다.

## 복구 재점검

request_context → recovery_collect → recovery_assess → recovery_answer.
MCP get_monitoring_status의 대상·스키마를 확인한 후 저장한다.
RecoveryStore.record는 최근 이력과 정책·시계를 순수 함수 assess에 전달한다.
변화량·신선도·정상 횟수·최소 간격·과거 장애·재발 기간을 검사한다.
새 복구 판정 경로는 LLM을 호출하지 않는다.

## 저장

(thread_id, system_name, sample_id)로 중복을 막고 관측과 당시 판정을 함께 저장한다.
정책 판정은 최근 최대 500건을 읽는다. 새 관측/실패는 보고서에도 반영된다.
SQLite 대상 상태와 이력은 재개방 후 유지된다.

## 이력·보고서

이력은 당시 판정을 표시한다. 보고서는 현재 시각으로 신선도를 재검사한다.
최근 재점검이 실패했으면 예전 복구 판정을 최신으로 제공하지 않는다.
이력/보고서는 모니터링을 추가 호출하지 않는다.

## 시나리오

기본은 샘플 JSON+난수다. MONITORING_SCENARIO는 대화·서비스별 고정 관측을 재생한다.
합성 시각은 최초 조회 이전에 배치하고 ID·출처를 표시한다.
마지막 관측은 같은 ID로 반환해 클릭 횟수가 정상 횟수로 바뀌지 않게 한다.
