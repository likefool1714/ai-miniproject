#!/usr/bin/env bash
#
# all_total_vue — 로컬 Ollama 환경을 구성하는 스크립트.
#
# 이 실습에 필요한 모델(기본 qwen3:0.6b / qwen3:8b)이 이미 받아져 있으면 그대로
# 재사용하고(삭제·재다운로드 없음), 없는 모델만 새로 받는다.
#
# "모델이 이상하게 응답한다", "처음부터 깨끗하게 다시 세팅하고 싶다" 할 때는
# OLLAMA_FORCE_REFRESH=1을 주면 기존 모델을 내리고/지운 뒤 새로 받는다.
#
# ⚠️ 새로 받는 모델은 네트워크/디스크/시간이 든다(qwen3:8b는 수 GB).
# ⚠️ 삭제(OLLAMA_FORCE_REFRESH=1일 때)는 이 스크립트가 쓰는 두 모델에만 한정된다
#    — 다른 모델은 건드리지 않는다.
#
# 실행:
#   bash sh/setup_ollama.sh                                   # 있는 모델은 재사용, 없는 모델만 받기
#   OLLAMA_FORCE_REFRESH=1 bash sh/setup_ollama.sh             # 기존 모델 삭제 후 깨끗하게 재구성
#   OLLAMA_FAST_MODEL=qwen3:1.7b OLLAMA_QUALITY_MODEL=qwen3:14b bash sh/setup_ollama.sh   # 모델 바꿔서
#
set -euo pipefail
cd "$(dirname "$0")/.."

FAST_MODEL="${OLLAMA_FAST_MODEL:-qwen3:0.6b}"
QUALITY_MODEL="${OLLAMA_QUALITY_MODEL:-qwen3:8b}"
FORCE_REFRESH="${OLLAMA_FORCE_REFRESH:-0}"

command -v ollama >/dev/null 2>&1 \
    || { echo "ERROR: ollama가 설치되어 있지 않습니다. https://ollama.com 에서 설치 후 재실행하세요."; exit 1; }

echo "대상 모델: fast=$FAST_MODEL / quality=$QUALITY_MODEL"

# ── 1) 기존 ollama serve 내리기 ─────────────────────────────────────────
if curl -s --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "기존 ollama serve가 떠 있어 종료합니다..."
    lsof -ti:11434 | xargs kill -9 2>/dev/null || true
    for _ in $(seq 1 20); do
        curl -s --max-time 1 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 || break
        sleep 0.5
    done
fi

# ── 2) 모델 확인/구성을 위해 ollama serve 임시 기동 ─────────────────────
echo "모델 확인을 위해 ollama serve를 임시로 띄웁니다..."
nohup ollama serve > /tmp/all_total_vue_ollama_setup.log 2>&1 &

cleanup() {
    echo
    echo "임시로 띄운 ollama serve를 종료합니다..."
    lsof -ti:11434 | xargs kill -9 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 30); do
    curl -s --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
    sleep 0.5
done
curl -s --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 \
    || { echo "ERROR: ollama serve 기동 실패"; exit 1; }

# ── 3) 기존 모델 삭제 (OLLAMA_FORCE_REFRESH=1일 때만) ────────────────────
if [ "$FORCE_REFRESH" = "1" ]; then
    for m in "$FAST_MODEL" "$QUALITY_MODEL"; do
        if ollama list | grep "^${m}" >/dev/null; then
            echo "기존 모델 삭제(OLLAMA_FORCE_REFRESH=1): $m"
            ollama rm "$m"
        fi
    done
fi

# ── 4) 없는 모델만 pull (이미 있으면 재사용하고 건너뜀) ───────────────────
for m in "$FAST_MODEL" "$QUALITY_MODEL"; do
    if ollama list | grep "^${m}" >/dev/null; then
        echo "이미 받아져 있어 재사용: $m"
        continue
    fi
    echo
    echo "모델 받는 중: $m (크기에 따라 수 분 소요될 수 있습니다)"
    ollama pull "$m"
done

echo
cat <<EOF
################################################################
 Ollama 환경 준비 완료: $FAST_MODEL / $QUALITY_MODEL
################################################################

이제 ./sh/run_agent_ollma.sh 로 에이전트를 띄워 테스트할 수 있습니다.
EOF
