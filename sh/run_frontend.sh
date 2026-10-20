#!/usr/bin/env bash
#
# all_total_vue — web_demo_server.py(브라우저 채팅 화면, 이 실습의 "프론트엔드" 역할)를
# 띄우는 스크립트.
#
# 이 실습엔 별도 npm/Next.js 프론트엔드가 없고, web_demo_server.py + web_chat.html
# 조합이 그 역할을 한다 — server.py(8100, 백엔드)는 건드리지 않고, web_chat.html을
# 같은 출처(origin)로 띄운 뒤 /chat, /chat/stream 요청을 8100으로 프록시해주는
# 아주 얇은 레이어다(브라우저 CORS 제약을 피하기 위함).
# web_chat.html 내부만 바닐라 JS 대신 Vue 3(CDN 글로벌 빌드, 빌드 단계 없음)로
# 되어 있다는 점이 all_total과 다르다.
#
# ⚠️ 먼저 run_agent_openai.sh / run_agent_ollma.sh / run.sh 등으로 백엔드(8100)를
# 띄워둔 상태여야 한다 — 백엔드 없이 이 스크립트만 띄우면 채팅 화면은 열리지만
# 메시지를 보내면 전부 에러가 난다.
#
# 종료하려면 이 터미널에서 Ctrl+C.
#
set -euo pipefail
cd "$(dirname "$0")/.."

# 이 머신은 plain python3/python이 alias로 다른 경로를 가리켜 venv를 무시하는
# 경우가 있어, 이 프로젝트 전용 .venv가 있으면 PATH 맨 앞에 명시적으로 꽂아둔다.
[ -x ".venv/bin/python3" ] && export PATH="$PWD/.venv/bin:$PATH"

# ── 0) 백엔드(8100)가 먼저 떠 있는지 확인 ───────────────────────────────
if ! curl -s --max-time 2 http://127.0.0.1:8100/health >/dev/null 2>&1; then
    echo "ERROR: 백엔드(http://127.0.0.1:8100)가 응답하지 않습니다."
    echo "  먼저 다른 터미널에서 아래 중 하나로 백엔드를 띄우세요:"
    echo "    ./sh/run_agent_openai.sh   (실제 OpenAI API, 과금)"
    echo "    ./sh/run_agent_ollma.sh    (로컬 Ollama)"
    echo "    ./sh/run.sh                (fake 모드, 통합 테스트)"
    exit 1
fi

# ── 1) 이전 실행 정리 ──────────────────────────────────────────────────
lsof -ti:8600 | xargs kill -9 2>/dev/null || true

cleanup() {
    echo
    echo "프론트엔드(web_demo_server.py)를 종료합니다..."
    lsof -ti:8600 | xargs kill -9 2>/dev/null || true
}
trap cleanup EXIT

# ── 2) web_demo_server.py 기동 (백그라운드로 띄운 뒤 준비될 때까지 대기) ─
python3 web_demo_server.py &
SERVER_PID=$!
for _ in $(seq 1 40); do
    curl -s http://127.0.0.1:8600/health >/dev/null 2>&1 && break
    sleep 0.5
done

cat <<EOF

################################################################
 프론트엔드가 떠 있습니다 → http://localhost:8600
 (web_chat.html을 띄우고 /chat·/chat/stream을 백엔드 8100으로 프록시)
################################################################

브라우저에서 http://localhost:8600 접속해 바로 채팅해볼 수 있습니다.
종료하려면 이 터미널에서 Ctrl+C.
EOF

wait "$SERVER_PID"
