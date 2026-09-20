#!/bin/bash
# 작곡 서버(ACE-Step) + 디스코드 봇을 한 번에 켜기
# 사용법: ./start.sh   (끌 때는 Ctrl + C 한 번 → 둘 다 꺼짐)

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
ACE_DIR="${ACE_DIR:-$HOME/ACE-Step-1.5}"
CONDA_ENV="${CONDA_ENV:-voicebot}"
# conda 설치 위치 자동 탐지 (anaconda / miniconda / miniforge 어디든)
CONDA_BASE="$(conda info --base 2>/dev/null)"
for d in "$CONDA_BASE" "$HOME/anaconda3" "$HOME/Documents/anaconda3" "$HOME/miniconda3" "$HOME/miniforge3" "/opt/homebrew/Caskroom/miniforge/base"; do
  [ -n "$d" ] && [ -f "$d/etc/profile.d/conda.sh" ] && CONDA_SH="$d/etc/profile.d/conda.sh" && break
done

cleanup() {
  echo ""
  echo "종료 중..."
  pkill -f "acestep-api" 2>/dev/null
  pkill -f "acestep/api_server.py" 2>/dev/null
}
trap cleanup EXIT

echo "🎼 작곡 서버 켜는 중... (로그: $BOT_DIR/acestep.log)"
(cd "$ACE_DIR" && ./start_api_server_macos.sh > "$BOT_DIR/acestep.log" 2>&1) &

for i in $(seq 1 100); do
  curl -s http://127.0.0.1:8001/health > /dev/null && break
  sleep 3
done
if ! curl -s http://127.0.0.1:8001/health > /dev/null; then
  echo "⚠️ 작곡 서버가 5분 안에 켜지지 않았어요. acestep.log를 확인해 주세요."
  exit 1
fi
echo "✅ 작곡 서버 준비 완료"

echo "🤖 봇 켜는 중..."
if [ -n "$CONDA_SH" ]; then
  source "$CONDA_SH"
  conda activate "$CONDA_ENV"
elif [ -f "$BOT_DIR/.venv/bin/activate" ]; then
  source "$BOT_DIR/.venv/bin/activate"   # conda 대신 venv를 쓰는 경우
else
  echo "⚠️ conda나 .venv를 찾지 못했어요. 현재 파이썬으로 실행합니다."
fi
cd "$BOT_DIR"
# caffeinate: 봇이 켜져 있는 동안 맥이 잠들지 않게 함
caffeinate -i python bot.py