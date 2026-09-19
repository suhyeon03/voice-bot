# Discord Voice Music Bot

디스코드 채널 멤버의 목소리(동의 기반)로 AI 음악을 만드는 봇.

## 파이프라인
1. **수집** – 동의 + 음성 파일 업로드 (현재 단계)
2. **전처리** – 보컬 분리, 무음 제거, 구간 분할 (`preprocess.py`)
3. 목소리 모델 학습 – RVC
4. 곡 생성 – 가사(LLM) + 노래 생성 모델
5. 보컬 교체 – 분리 → 멤버 목소리로 변환
6. 믹싱 후 채널 업로드

## 개발 환경 (macOS, Apple Silicon)
```bash
brew install python@3.11 ffmpeg
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-ml.txt   # 전처리 이후 단계용
cp .env.example .env   # 토큰·서버 ID 입력
python bot.py
```
VS Code에서는 `F5` → "봇 실행" / "전처리".

## 전처리
```bash
python preprocess.py              # 동의한 전원, 새 클립만
python preprocess.py --no-separate
python preprocess.py --user <ID> --force
```
결과는 `data/datasets/<유저ID>/`에 1~12초 WAV 조각과 `report.json`으로 저장됩니다.

## 디스코드 설정
- Developer Portal → Bot → **Message Content Intent** 켜기
- 초대 스코프: `bot`, `applications.commands`

## 명령어
| 명령 | 설명 |
|---|---|
| `/동의` | 목소리 수집·복제 동의 |
| `/대본` | 녹음용 문장 |
| `/목소리등록` | 음성 파일 등록 |
| `/내목소리` | 수집 현황 |
| `/삭제` | 음성·모델·동의 기록 전부 삭제 |

## 데이터와 프라이버시
음성 데이터는 `data/`에 로컬 저장되며 `.gitignore`로 저장소에서 제외됩니다.
동의한 멤버의 데이터만 수집하며, `/삭제`로 언제든 전부 지울 수 있습니다.
