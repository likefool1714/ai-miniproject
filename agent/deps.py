"""Wave 13 — 의존성 주입.

노드 함수가 closure의 지역 변수에 암묵적으로 기대지 않도록, supervisor가 쓰는 모든
의존성을 SupervisorDeps 하나에 명시적으로 담아 functools.partial로 각 노드에 주입한다.
파일을 어디로 옮겨도, 단위 테스트에서 mock deps로 갈아 끼워도 동작이 같다.
"""
from dataclasses import dataclass
from typing import Any


@dataclass
class SupervisorDeps:
    fast_llm: Any        # Wave 10a — 분류/계획 등 짧은 단계용 저비용 모델
    quality_llm: Any     # Wave 10a — 최종 답변 작성용 고품질 모델
    mcp: Any             # Wave 2  — MCP stdio 클라이언트 (Tool/Resource/Prompt)
    session_store: Any   # Wave 12 — SQLite 세션 저장소
    version: str         # Wave 14 — SUPERVISOR_VERSION (v1/v2)
