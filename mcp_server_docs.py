"""all_total 통합 실습 — 두 번째(미사용) MCP 서버.

agent는 이 서버의 어떤 capability도 직접 호출하지 않는다. 여러 MCP 서버가 동시에
연결돼 있을 때, agent가 필요한 resource를 찾기 위해 "어느 서버가 그 resource를
제공하는지" 먼저 조회(list_resource_templates)하고서야 요청해야 하는 상황을
시뮬레이션하기 위한 decoy 서버다 — mcp_server.py(runbook)와 이름이 겹치지 않는
내부 API 문서 resource만 제공한다.
"""
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP(name="capstone-all-total-docs")

DOC_SECTIONS = {
    "billing": "# 내부 API 문서: billing\n\n결제 관련 엔드포인트와 webhook 재시도 정책을 설명합니다.",
    "auth": "# 내부 API 문서: auth\n\n토큰 발급/갱신 플로우와 스코프 목록을 설명합니다.",
}


@mcp.resource("docs://api-reference/{section}")
def get_api_doc(section: str) -> str:
    """내부 API 문서(section별 정적 문서)를 반환합니다. (agent는 사용하지 않음)"""
    if section not in DOC_SECTIONS:
        return f"# Doc Not Found\n\nNo API reference for section: {section}"
    return DOC_SECTIONS[section]


if __name__ == "__main__":
    try:
        mcp.run(show_banner=False)
    except TypeError:   # 구버전 fastmcp 호환
        mcp.run()
