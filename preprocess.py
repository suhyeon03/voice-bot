"""
2단계: 수집된 음성 → RVC 학습용 데이터셋

처리 순서 (클립마다):
  1. Demucs로 목소리만 분리 (배경 소음·음악 제거, Apple Silicon은 MPS 사용)
  2. 70Hz 하이패스 (웅웅거리는 저역 잡음 제거)
  3. 무음 구간 제거 → 말소리 구간만 추출
  4. 음량 정규화 (-20 LUFS, 피크 -1dBFS)
  5. 1~12초 세그먼트로 잘라 저장

사용법:
  python preprocess.py                 # 동의한 모든 멤버, 새 클립만
  python preprocess.py --user 1234     # 특정 멤버만
  python preprocess.py --no-separate   # Demucs 생략 (조용한 곳 녹음이면 빠름)
  python preprocess.py --force         # 처음부터 다시

결과: data/datasets/{유저ID}/*.wav + report.json
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import librosa
import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from dotenv import load_dotenv
from scipy.signal import butter, sosfiltfilt

load_dotenv()
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))

SR = 48000
TARGET_LUFS = -20.0
PEAK_DB = -1.0
TOP_DB = 40          # 최대 음량보다 40dB 이상 작으면 무음으로 판단
MERGE_GAP = 0.3      # 0.3초 미만의 쉼은 한 구간으로 합침
PAD = 0.1            # 구간 앞뒤 여유
MIN_SEG = 1.0
MAX_SEG = 12.0


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def load_consent() -> dict:
    p = DATA_DIR / "consent.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def pick_device() -> str:
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"


def to_mono_sr(y: np.ndarray, sr: int) -> np.ndarray:
    if y.ndim == 2:
        y = y.mean(axis=1)
    if sr != SR:
        y = librosa.resample(y, orig_sr=sr, target_sr=SR)
    return y.astype(np.float32)


# ---------------- 1. 목소리 분리 ----------------
def separate_vocals(src: Path, workdir: Path, device: str) -> np.ndarray:
    env = {**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    for dev in dict.fromkeys([device, "cpu"]):  # MPS 실패 시 CPU로 재시도
        r = subprocess.run(
            [sys.executable, "-m", "demucs", "--two-stems", "vocals",
             "-n", "htdemucs", "-d", dev, "-o", str(workdir), str(src)],
            capture_output=True, text=True, env=env,
        )
        out = workdir / "htdemucs" / src.stem / "vocals.wav"
        if r.returncode == 0 and out.exists():
            y, sr = sf.read(out, always_2d=True)
            return to_mono_sr(y, sr)
    raise RuntimeError(f"Demucs 실패: {r.stderr.strip()[-300:]}")


# ---------------- 2~4. 필터·구간·음량 ----------------
def highpass(y: np.ndarray) -> np.ndarray:
    sos = butter(4, 70, btype="highpass", fs=SR, output="sos")
    return sosfiltfilt(sos, y).astype(np.float32)


def voiced_intervals(y: np.ndarray) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for s, e in librosa.effects.split(y, top_db=TOP_DB, frame_length=2048, hop_length=512):
        if merged and s - merged[-1][1] < MERGE_GAP * SR:
            merged[-1][1] = e
        else:
            merged.append([int(s), int(e)])
    pad = int(PAD * SR)
    return [(max(0, s - pad), min(len(y), e + pad)) for s, e in merged]


def split_long(s: int, e: int) -> list[tuple[int, int]]:
    n = max(1, int(np.ceil((e - s) / (MAX_SEG * SR))))
    edges = np.linspace(s, e, n + 1).astype(int)
    return list(zip(edges[:-1], edges[1:]))


def loudness_gain(voiced: np.ndarray) -> tuple[float, float]:
    """말소리 구간만으로 음량을 재서 목표 LUFS까지의 배율을 계산."""
    if len(voiced) < int(0.5 * SR):
        return 1.0, float("nan")
    loud = pyln.Meter(SR).integrated_loudness(voiced)
    if not np.isfinite(loud):
        return 1.0, loud
    return float(10 ** ((TARGET_LUFS - loud) / 20)), loud


def peak_limit(seg: np.ndarray) -> np.ndarray:
    limit = 10 ** (PEAK_DB / 20)
    peak = float(np.max(np.abs(seg))) if len(seg) else 0.0
    return seg * (limit / peak) if peak > limit else seg


# ---------------- 클립 처리 ----------------
def process_clip(wav: Path, out_dir: Path, clip_id: str,
                 separate: bool, device: str, tmp: Path) -> dict:
    raw, sr = sf.read(wav, always_2d=True)
    raw = to_mono_sr(raw, sr)
    warnings = []

    clip_ratio = float(np.mean(np.abs(raw) >= 0.99))
    if clip_ratio > 0.001:
        warnings.append(f"클리핑 {clip_ratio * 100:.2f}% (마이크에 너무 가까움)")

    y = separate_vocals(wav, tmp, device) if separate else raw
    y = highpass(y)

    ivs = voiced_intervals(y)
    if not ivs:
        return {"segments": 0, "seconds": 0.0, "warnings": warnings + ["음성 구간 없음"]}

    gain, loud = loudness_gain(np.concatenate([y[s:e] for s, e in ivs]))
    if np.isfinite(loud) and loud < -45:
        warnings.append(f"소리가 매우 작음 ({loud:.0f} LUFS)")
    y = y * gain

    count, seconds = 0, 0.0
    for s, e in ivs:
        for a, b in split_long(s, e):
            if (b - a) / SR < MIN_SEG:
                continue
            seg = peak_limit(y[a:b])
            sf.write(out_dir / f"{clip_id}_{count:03d}.wav", seg, SR, subtype="PCM_16")
            count += 1
            seconds += (b - a) / SR

    return {"segments": count, "seconds": round(seconds, 2), "warnings": warnings}


def readiness(total: float) -> str:
    m = total / 60
    if m < 3:
        return f"⚠️ {m:.1f}분 — 학습하기엔 부족해요 (최소 5분 권장)"
    if m < 10:
        return f"🟡 {m:.1f}분 — 학습은 가능, 10분 이상이면 더 좋아요"
    return f"✅ {m:.1f}분 — 학습 준비 완료"


def main() -> None:
    ap = argparse.ArgumentParser(description="음성 샘플 → RVC 학습 데이터셋")
    ap.add_argument("--user", help="특정 유저 ID만 처리")
    ap.add_argument("--no-separate", action="store_true", help="Demucs 목소리 분리 생략")
    ap.add_argument("--force", action="store_true", help="기존 결과를 지우고 처음부터")
    args = ap.parse_args()

    consent = load_consent()
    users = [args.user] if args.user else sorted(consent)
    separate = not args.no_separate
    device = pick_device() if separate else "cpu"
    print(f"목소리 분리: {'켜짐 (' + device + ')' if separate else '꺼짐'}")

    for uid in users:
        if uid not in consent:
            print(f"- {uid}: 동의 기록 없음, 건너뜀")
            continue
        meta_path = DATA_DIR / "voices" / uid / "meta.json"
        if not meta_path.exists():
            print(f"- {consent[uid]['name']}: 등록된 음성 없음")
            continue

        clips = json.loads(meta_path.read_text("utf-8"))["clips"]
        out_dir = DATA_DIR / "datasets" / uid
        if args.force:
            shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / "report.json"
        report = json.loads(report_path.read_text("utf-8")) if report_path.exists() else {"clips": {}}

        todo = [c for c in clips if c["id"] not in report["clips"]]
        print(f"\n▶ {consent[uid]['name']}: 새 클립 {len(todo)}개 / 전체 {len(clips)}개")

        with tempfile.TemporaryDirectory() as tmp:
            for i, c in enumerate(todo, 1):
                wav = DATA_DIR / "voices" / uid / "wav" / c["wav"]
                if not wav.exists():
                    continue
                try:
                    r = process_clip(wav, out_dir, c["id"], separate, device, Path(tmp))
                except Exception as e:  # 한 클립이 실패해도 나머지는 계속
                    print(f"  [{i}/{len(todo)}] {c['id']} 실패: {e}")
                    continue
                report["clips"][c["id"]] = r
                atomic_write_json(report_path, report)
                warn = f"  ⚠️ {' / '.join(r['warnings'])}" if r["warnings"] else ""
                print(f"  [{i}/{len(todo)}] {c['id']} → {r['segments']}조각, {r['seconds']:.1f}초{warn}")

        total = sum(r["seconds"] for r in report["clips"].values())
        report["total_seconds"] = round(total, 1)
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(report_path, report)
        print(f"  {readiness(total)}")


if __name__ == "__main__":
    main()
