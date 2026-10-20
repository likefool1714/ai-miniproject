#!/usr/bin/env bash
#
# all_total_vue — 이 프로젝트 전용 .venv를 만들고 requirements.txt를 설치하는 스크립트.
#
# 다른 sh/ 스크립트들(run_agent_ollma.sh 등)은 ".venv가 이미 있으면 쓴다"고만
# 가정할 뿐 직접 만들어주지는 않는다 — 이 스크립트가 그 "맨 처음 한 번"을 담당한다.
# 깨끗한 환경에서 이 폴더를 처음 받았다면(학생 배포 등) 가장 먼저 이걸 실행한다.
#
# ⚠️ 이 머신은 plain python3/pip가 alias로 엉뚱한 경로를 가리켜, venv를 만들어도
# 그 안에 설치되는 게 아니라 시스템 전역에 설치돼버리는 경우가 실제로 있었다.
# 그래서 설치 단계는 반드시 "activate" 대신 .venv 안의 python3 바이너리를
# 절대/상대 경로로 직접 호출한다(아래 ./.venv/bin/python3 -m pip ...).
#
# 실행:
#   bash sh/setup_venv.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

command -v python3 >/dev/null 2>&1 \
    || { echo "ERROR: python3가 설치되어 있지 않습니다."; exit 1; }

# ── 1) .venv 생성 (이미 있으면 건너뜀) ──────────────────────────────────
if [ -x ".venv/bin/python3" ]; then
    echo ".venv가 이미 있습니다 — 생성은 건너뛰고 설치만 다시 확인합니다."
else
    echo "python3 -m venv .venv 로 가상환경을 새로 만듭니다..."
    python3 -m venv .venv
fi

# ── 2) 의존성 설치 — activate 대신 .venv의 python3를 직접 호출 ─────────
#    (plain pip/python3가 alias로 다른 경로를 가리키는 머신에서도 항상
#     .venv 안에 정확히 설치되도록 보장한다)
echo "requirements.txt 설치 중..."
./.venv/bin/python3 -m pip install --upgrade pip --quiet
./.venv/bin/python3 -m pip install -r requirements.txt

echo
echo "설치 확인:"
./.venv/bin/python3 -c "import fastapi, uvicorn, langgraph, langsmith, fastmcp, mcp, httpx, dotenv; print('  필요 패키지 전부 import 성공')"

cat <<EOF

################################################################
 .venv 준비 완료
################################################################

이제 아래 중 하나로 에이전트를 띄울 수 있습니다:
  bash sh/run_agent_ollma.sh     (로컬 Ollama)
  bash sh/run_agent_openai.sh    (OpenAI API, 과금)

Ollama를 처음 쓰거나 모델을 다시 받아야 하면 먼저:
  bash sh/setup_ollama.sh
EOF
