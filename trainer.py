""""
목소리 모델 자동 학습

  preprocess.py(녹음 정리) → Applio preprocess → extract → train(+index) → 봇에 연결

Applio 명령줄(core.py)은 실패해도 종료 코드가 0일 수 있어서,
출력에 "failed"가 있는지와 결과 파일이 실제로 생겼는지로 성공을 판단한다.
"""
import asyncio
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

import music

BOT_DIR = Path(__file__).resolve().parent
SAMPLE_RATE = "40000"
MIN_TRAIN_SECONDS = 180          # 3분 미만이면 학습 거부
EPOCH_RE = re.compile(r"epoch=(\d+)")

Progress = Callable[[str], Awaitable[None]]


class TrainError(Exception):
    """사용자에게 보여줄 수 있는 실패 사유."""


def model_name(uid: int | str) -> str:
    return f"voice_{uid}"


def applio_log_dir(uid: int | str) -> Path:
    return music.APPLIO_DIR / "logs" / model_name(uid)


def dataset_dir(uid: int | str) -> Path:
    return (music.DATA_DIR / "datasets" / str(uid)).resolve()


def dataset_seconds(uid: int | str) -> float:
    report = dataset_dir(uid) / "report.json"
    if not report.exists():
        return 0.0
    return float(json.loads(report.read_text("utf-8")).get("total_seconds", 0))


def suggested_epochs(seconds: float) -> int:
    minutes = seconds / 60
    if minutes < 5:
        return 120
    if minutes < 10:
        return 150
    return 200


async def stream(cmd: list[str], cwd: Path, on_line: Callable[[str], Awaitable[None]] | None = None,
                 env: dict | None = None) -> tuple[int, str]:
    """명령을 실행하며 출력을 한 줄씩 넘겨준다. (tqdm의 \\r 진행바도 줄로 취급)"""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    tail: list[str] = []
    buf = ""
    assert proc.stdout
    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk.decode(errors="ignore")
        *lines, buf = re.split(r"[\r\n]", buf)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            tail = (tail + [line])[-40:]
            if on_line:
                await on_line(line)
    if buf.strip():
        tail.append(buf.strip())
    return await proc.wait(), "\n".join(tail)


async def applio(step: str, args: list[str], on_line=None) -> str:
    py = music.APPLIO_DIR / ".venv" / "bin" / "python"
    if not py.exists():
        raise TrainError(f"Applio를 찾지 못했어요. `.env`의 APPLIO_DIR을 확인해 주세요. ({music.APPLIO_DIR})")
    env = {**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    rc, tail = await stream([str(py), "core.py", *args], music.APPLIO_DIR, on_line, env)
    if rc != 0 or re.search(r"\bfailed\b", tail, re.IGNORECASE):
        raise TrainError(f"{step} 단계에서 실패했어요.\n```\n{music.summarize_log(tail, 600)}\n```")
    return tail


async def train_voice(uid: int | str, epochs: int | None, progress: Progress) -> tuple[Path, float, int]:
    """전체 학습. (연결된 모델 경로, 사용한 데이터 초, 에폭) 반환."""
    name = model_name(uid)

    # 1) 녹음 정리 (새 녹음만 추가 처리)
    await progress("🧹 1/5 녹음 정리 중... (목소리 분리 포함, 몇 분 걸릴 수 있어요)")
    rc, tail = await stream([sys.executable, "preprocess.py", "--user", str(uid)], BOT_DIR)
    if rc != 0:
        raise TrainError(f"녹음 정리에 실패했어요.\n```\n{tail[-500:]}\n```")
    seconds = dataset_seconds(uid)
    if seconds < MIN_TRAIN_SECONDS:
        raise TrainError(
            f"정리된 녹음이 {seconds / 60:.1f}분뿐이에요. 최소 {MIN_TRAIN_SECONDS // 60}분(권장 10분)을 모아 주세요."
        )
    epochs = epochs or suggested_epochs(seconds)

    # 이전 학습 흔적은 지우고 새로 시작 (연결된 기존 모델은 새 모델이 완성될 때까지 유지)
    shutil.rmtree(applio_log_dir(uid), ignore_errors=True)

    # 2) Applio 전처리
    await progress(f"✂️ 2/5 학습용으로 자르는 중... (데이터 {seconds / 60:.1f}분)")
    await applio("Applio 전처리", [
        "preprocess", "--model-name", name, "--dataset-path", str(dataset_dir(uid)),
        "--sample-rate", SAMPLE_RATE, "--cut-preprocess", "Automatic",
    ])

    # 3) 특징 추출 (음높이 + 음색)
    await progress("🔍 3/5 음높이·음색 특징 추출 중...")
    await applio("특징 추출", [
        "extract", "--model-name", name, "--f0-method", "rmvpe",
        "--sample-rate", SAMPLE_RATE, "--embedder-model", "contentvec",
    ])

    # 4) 학습 (끝나면 Applio가 인덱스까지 생성)
    started = time.monotonic()
    last_report = 0.0

    async def on_train_line(line: str) -> None:
        nonlocal last_report
        m = EPOCH_RE.search(line)
        if not m or time.monotonic() - last_report < 20:
            return
        last_report = time.monotonic()
        ep = int(m.group(1))
        elapsed = time.monotonic() - started
        eta = elapsed / max(ep, 1) * (epochs - ep)
        bar = "█" * int(ep / epochs * 10) + "░" * (10 - int(ep / epochs * 10))
        await progress(f"🏋️ 4/5 학습 중 {bar} {ep}/{epochs} 에폭 · 남은 시간 약 {eta / 60:.0f}분")

    await progress(f"🏋️ 4/5 학습 시작... (총 {epochs} 에폭, 처음엔 준비에 시간이 걸려요)")
    await applio("학습", [
        "train", "--model-name", name, "--total-epoch", str(epochs),
        "--save-every-epoch", "25", "--save-only-latest",
        "--sample-rate", SAMPLE_RATE, "--batch-size", "8",
    ], on_train_line)

    # 5) 봇에 연결
    await progress("🔗 5/5 봇에 연결 중...")
    try:
        pth, _ = music.link_applio_model(uid, name)
    except music.SongError as e:
        raise TrainError(str(e))
    return pth, seconds, epochs