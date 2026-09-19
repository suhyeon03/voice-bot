"""
노래 생성 파이프라인

  ACE-Step(작곡) → Demucs(보컬/반주 분리) → Applio(멤버 목소리로 변환) → ffmpeg(믹싱)

가수를 고르지 않았거나 목소리 모델이 없으면 ACE-Step이 만든 원곡을 그대로 씁니다.

필요한 것
- ACE-Step API 서버: ~/ACE-Step-1.5 에서 ./start_api_server_macos.sh (기본 포트 8001)
- Applio: ~/Applio (학습·변환용, .venv 포함)
"""
import asyncio
import json
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

ACESTEP_URL = os.getenv("ACESTEP_URL", "http://127.0.0.1:8001").rstrip("/")
APPLIO_DIR = Path(os.path.expanduser(os.getenv("APPLIO_DIR", "~/Applio")))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
SONG_TIMEOUT = int(os.getenv("SONG_TIMEOUT_SECONDS", "1200"))

# 디스코드 선택지(한국어) → 모델에 넣을 스타일 태그(영어가 더 잘 먹힘)
GENRES = {
    "K-POP": "k-pop, catchy hook, polished production",
    "발라드": "korean ballad, piano, strings",
    "힙합": "hip hop, rap, 808 bass, trap drums",
    "락": "rock, electric guitar, live drums",
    "락발라드": "rock ballad, power chords, big chorus",
    "인디/어쿠스틱": "indie folk, acoustic guitar, warm",
    "R&B": "r&b, smooth, groovy bass",
    "시티팝": "city pop, 80s, funky bass, electric piano",
    "EDM": "edm, synth lead, four on the floor, drop",
    "로파이": "lo-fi hip hop, chill, vinyl crackle",
    "트로트": "trot, korean retro pop, brass, accordion",
    "재즈": "jazz, saxophone, swing, upright bass",
}
MOODS = {
    "신나는": "upbeat, energetic",
    "감성적인": "emotional, heartfelt",
    "잔잔한": "calm, gentle, soft",
    "슬픈": "sad, melancholic",
    "웅장한": "epic, cinematic, powerful",
    "몽환적인": "dreamy, atmospheric",
    "유쾌한": "funny, playful, bright",
}
VOICES = {"자동": "", "남성": "male vocal", "여성": "female vocal"}
LYRIC_MODES = ["직접 입력", "AI가 작성", "연주곡"]
GEN_MODES = ["새로 생성", "레퍼런스 참고"]
DURATIONS = [30, 60, 90, 120, 180]


class SongError(Exception):
    """사용자에게 그대로 보여줄 수 있는 실패 사유."""


Progress = Callable[[str], Awaitable[None]]


@dataclass
class SongRequest:
    requester_id: int
    genre: str
    mood: str
    lyric_mode: str
    voice: str = "자동"
    duration: int = 60
    lyrics: str = ""
    title: str = ""
    topic: str = ""
    extra: str = ""
    singer_key: str | None = None      # 멤버는 디스코드 ID, 별도 목소리는 "v_..." 키
    singer_label: str = ""
    pitch: int = 0
    gen_mode: str = "새로 생성"
    reference_url: str = ""            # 레퍼런스 음악 (디스코드 첨부 URL)
    reference_name: str = ""
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])

    def caption(self) -> str:
        parts = [GENRES.get(self.genre, self.genre), MOODS.get(self.mood, self.mood), VOICES.get(self.voice, "")]
        if self.lyric_mode == "연주곡":
            parts.append("instrumental, no vocals")
        if self.extra:
            parts.append(self.extra)
        return ", ".join(p for p in parts if p)


@dataclass
class SongResult:
    mp3: Path
    lyrics: str
    converted: bool
    note: str = ""


# ---------------- 공통 ----------------
async def run(*cmd: str, cwd: Path | None = None, env: dict | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd) if cwd else None, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


NOISE = re.compile(r"(\d+%\|)|(it/s\])|(s/it\])|resource_tracker|warnings\.warn|leaked semaphore")


def summarize_log(text: str, limit: int = 700) -> str:
    """외부 도구 출력에서 진행 막대·종료 경고를 걸러내고 진짜 에러 부분만 보여준다."""
    lines = [l.rstrip() for l in re.split(r"[\r\n]+", text) if l.strip() and not NOISE.search(l)]
    for i in range(len(lines) - 1, -1, -1):          # 마지막 Traceback부터
        if lines[i].startswith("Traceback"):
            return "\n".join(lines[i:])[-limit:]
    keys = [l for l in lines if re.search(r"error|exception|failed|not found|no such", l, re.I)]
    return "\n".join((keys or lines)[-8:])[-limit:] or "(출력 없음)"


def normalize_lyrics(text: str) -> str:
    """구조 태그가 하나도 없으면 [verse]를 붙여 모델이 가사로 인식하게 한다."""
    text = text.strip()
    if text and "[" not in text:
        text = "[verse]\n" + text
    return text


# ---------------- 1. 작곡 (ACE-Step) ----------------
async def acestep_alive() -> bool:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get(f"{ACESTEP_URL}/health") as r:
                return r.status == 200
    except Exception:
        return False


def build_payload(req: SongRequest) -> dict:
    payload = {
        "prompt": req.caption(),
        "audio_duration": req.duration,
        "vocal_language": "ko",
        "thinking": True,          # 작곡 설계 모델(LM)로 곡 구조를 먼저 잡음
        "batch_size": 1,
        "inference_steps": 8,
        "audio_format": "wav",     # 목소리 변환 품질을 위해 무손실로 받음
    }
    if req.lyric_mode == "연주곡":
        payload["lyrics"] = "[Instrumental]"
    elif req.lyric_mode == "AI가 작성":
        voice = VOICES.get(req.voice, "")
        query = f"A {MOODS.get(req.mood, '')} {GENRES.get(req.genre, '')} song sung in Korean"
        if voice:
            query += f", {voice}"
        if req.topic:
            query += f". Topic: {req.topic}"
        payload["sample_mode"] = True
        payload["sample_query"] = query
    else:
        payload["lyrics"] = normalize_lyrics(req.lyrics)
    return payload


async def download_reference(s: aiohttp.ClientSession, req: SongRequest, workdir: Path) -> Path:
    ext = Path(req.reference_name).suffix.lower() or ".mp3"
    out = workdir / f"reference{ext}"
    async with s.get(req.reference_url, timeout=aiohttp.ClientTimeout(total=120)) as r:
        if r.status != 200:
            raise SongError("레퍼런스 음악 파일을 내려받지 못했어요. 다시 첨부해 주세요.")
        out.write_bytes(await r.read())
    return out


async def compose(req: SongRequest, workdir: Path) -> tuple[Path, dict]:
    try:
        return await _compose(req, workdir)
    except aiohttp.ClientError as e:
        raise SongError(f"작곡 서버(ACE-Step)와 연결이 끊겼어요. 서버가 켜져 있는지 확인해 주세요. ({type(e).__name__})")


async def _compose(req: SongRequest, workdir: Path) -> tuple[Path, dict]:
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        payload = build_payload(req)
        if req.gen_mode == "레퍼런스 참고" and req.reference_url:
            ref = await download_reference(s, req, workdir)
            form = aiohttp.FormData()
            for k, v in payload.items():
                form.add_field(k, str(v).lower() if isinstance(v, bool) else str(v))
            form.add_field("reference_audio", ref.read_bytes(), filename=ref.name)
            post = s.post(f"{ACESTEP_URL}/release_task", data=form,
                          timeout=aiohttp.ClientTimeout(total=300))
        else:
            post = s.post(f"{ACESTEP_URL}/release_task", json=payload)
        async with post as r:
            body = await r.json(content_type=None)
        if body.get("code") != 200 or not body.get("data"):
            raise SongError(f"작곡 요청이 거절됐어요: {body.get('error')}")
        task_id = body["data"]["task_id"]

        started = time.monotonic()
        while True:
            await asyncio.sleep(3)
            if time.monotonic() - started > SONG_TIMEOUT:
                raise SongError("작곡이 너무 오래 걸려서 중단했어요.")
            async with s.post(f"{ACESTEP_URL}/query_result", json={"task_id_list": [task_id]}) as r:
                q = await r.json(content_type=None)
            items = q.get("data") or []
            if not items:
                continue
            item = items[0]
            status = item.get("status")
            if status == 2:
                raise SongError(f"작곡에 실패했어요: {str(item.get('result'))[:200]}")
            if status == 1:
                break

        result = item.get("result")
        if isinstance(result, str):
            result = json.loads(result)
        info = result[0] if isinstance(result, list) else result
        url = info["file"]
        if url.startswith("/"):
            url = ACESTEP_URL + url
        out = workdir / "original.wav"
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=300)) as r:
            if r.status != 200:
                raise SongError(f"완성된 곡을 받지 못했어요 (HTTP {r.status})")
            out.write_bytes(await r.read())
    return out, info


# ---------------- 2. 보컬 분리 (Demucs) ----------------
async def separate(src: Path, workdir: Path) -> tuple[Path, Path]:
    env = {**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    err = ""
    for dev in ("mps", "cpu"):
        rc, _, err = await run(
            sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs",
            "-d", dev, "-o", str(workdir / "sep"), str(src), env=env,
        )
        base = workdir / "sep" / "htdemucs" / src.stem
        if rc == 0 and (base / "vocals.wav").exists():
            return base / "vocals.wav", base / "no_vocals.wav"
    raise SongError(f"보컬 분리에 실패했어요: {err.strip()[-200:]}")


# ---------------- 3. 목소리 변환 (Applio) ----------------
def model_dir(uid: int | str) -> Path:
    return DATA_DIR / "models" / str(uid)


def singer_model(uid: int | str) -> tuple[Path, Path] | None:
    d = model_dir(uid)
    pths = sorted(d.glob("*.pth"), key=lambda p: p.stat().st_mtime)
    idxs = sorted(d.glob("*.index"), key=lambda p: p.stat().st_mtime)
    return (pths[-1], idxs[-1]) if pths and idxs else None


def link_applio_model(uid: int | str, name: str) -> tuple[Path, Path]:
    """Applio logs/<name>/ 의 최신 모델(.pth)과 인덱스(.index)를 멤버에게 연결(복사)."""
    if not re.fullmatch(r"[\w\-. ]+", name) or ".." in name:
        raise SongError("모델 이름에는 영문, 숫자, -, _ 만 써주세요.")
    src = APPLIO_DIR / "logs" / name
    if not src.is_dir():
        raise SongError(f"Applio에서 `{name}` 모델 폴더를 찾지 못했어요. ({src})")
    # G_*.pth / D_*.pth 는 학습 중간 체크포인트라 제외
    pths = [p for p in src.glob("*.pth") if not p.name.startswith(("G_", "D_"))]
    idxs = list(src.glob("*.index"))
    if not pths:
        raise SongError("학습된 모델(.pth)이 없어요. 학습이 끝났는지 확인해 주세요.")
    if not idxs:
        raise SongError("인덱스(.index)가 없어요. Applio에서 Generate Index를 눌러 주세요.")
    pth = max(pths, key=lambda p: p.stat().st_mtime)
    idx = max(idxs, key=lambda p: p.stat().st_mtime)
    dst = model_dir(uid)
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    return Path(shutil.copy2(pth, dst)), Path(shutil.copy2(idx, dst))


SEGFAULT_CODES = {-11, 139}   # 맥에서 faiss·OpenMP 충돌 시 나는 segmentation fault


async def convert_voice(vocals: Path, pth: Path, index: Path, pitch: int, out: Path) -> str:
    """멤버 목소리로 변환. 반환값은 사용자에게 알릴 참고 문구(없으면 빈 문자열).

    1차: 인덱스 사용 (음색이 더 닮음)
    2차: 맥에서 인덱스 검색(faiss)이 segfault로 죽으면 인덱스 없이 재시도
    """
    py = APPLIO_DIR / ".venv" / "bin" / "python"
    if not py.exists():
        raise SongError(f"Applio를 찾지 못했어요. `.env`의 APPLIO_DIR을 확인해 주세요. ({APPLIO_DIR})")
    # PyTorch와 faiss가 서로 다른 OpenMP를 불러 충돌하는 것을 막음
    env = {**os.environ, "OMP_NUM_THREADS": "1", "KMP_DUPLICATE_LIB_OK": "TRUE"}
    logs = []
    for index_rate in ("0.5", "0"):
        out.unlink(missing_ok=True)
        rc, o, e = await run(
            str(py), "core.py", "infer",
            "--input-path", str(vocals.resolve()),
            "--output-path", str(out.resolve()),
            "--pth-path", str(pth.resolve()),
            "--index-path", str(index.resolve()),
            "--index-rate", index_rate,          # 0이면 Applio가 인덱스 검색을 건너뜀
            "--pitch", str(pitch),
            "--f0-method", "rmvpe",
            "--protect", "0.33",
            "--export-format", "WAV",
            cwd=APPLIO_DIR, env=env,
        )
        logs.append(f"$ index_rate={index_rate} exit={rc}\n--- stdout\n{o}\n--- stderr\n{e}")
        (out.parent / "convert.log").write_text("\n\n".join(logs), "utf-8")
        if rc == 0 and out.exists():
            return "" if index_rate != "0" else "인덱스 없이 변환했어요 (맥 호환성 문제로 자동 전환, 음색이 조금 덜 닮을 수 있어요)."
        if rc not in SEGFAULT_CODES:
            break   # segfault가 아닌 실패는 재시도해도 같으므로 중단
    crashed = "\n(segmentation fault: 프로그램이 C 라이브러리 단계에서 강제 종료됨)" if rc in SEGFAULT_CODES else ""
    raise SongError(f"목소리 변환에 실패했어요.{crashed}\n```\n{summarize_log(o + chr(10) + e)}\n```\n"
                    f"전체 로그: `{out.parent / 'convert.log'}`")


# ---------------- 4. 믹싱 / 인코딩 ----------------
async def mix(vocals: Path, inst: Path, out: Path) -> None:
    rc, _, e = await run(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(vocals), "-i", str(inst),
        "-filter_complex",
        "[0:a]aresample=48000[v];[1:a]aresample=48000[i];"
        "[v][i]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95",
        "-ac", "2", "-b:a", "192k", str(out),
    )
    if rc != 0:
        raise SongError(f"믹싱에 실패했어요: {e.strip()[-200:]}")


async def to_mp3(src: Path, out: Path) -> None:
    rc, _, e = await run("ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-b:a", "192k", str(out))
    if rc != 0:
        raise SongError(f"mp3 변환에 실패했어요: {e.strip()[-200:]}")


# ---------------- 전체 파이프라인 ----------------
async def make_song(req: SongRequest, progress: Progress) -> SongResult:
    workdir = (DATA_DIR / "songs" / req.id).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    await progress("🎼 작곡 중이에요... (보통 1~3분)")
    original, info = await compose(req, workdir)
    lyrics = info.get("lyrics") or req.lyrics
    final = workdir / "song.mp3"

    model = singer_model(req.singer_key) if req.singer_key else None
    converted, note = False, ""
    if req.singer_key and req.lyric_mode == "연주곡":
        note = "연주곡이라 가수 목소리 변환은 건너뛰었어요."
    elif req.singer_key and not model:
        note = "가수의 목소리 모델이 아직 없어서 AI 원래 목소리로 만들었어요."

    if model and req.lyric_mode != "연주곡":
        await progress("🎚️ 보컬과 반주를 나누는 중...")
        vocals, inst = await separate(original, workdir)
        await progress("🎤 목소리를 바꾸는 중...")
        converted_vocals = workdir / "vocals_converted.wav"
        convert_note = await convert_voice(vocals, *model, req.pitch, converted_vocals)
        if convert_note:
            note = (note + " " + convert_note).strip()
        await progress("🎛️ 믹싱 중...")
        await mix(converted_vocals, inst, final)
        converted = True
    else:
        await progress("💿 마무리 중...")
        await to_mp3(original, final)

    meta = {**asdict(req), "final_lyrics": lyrics, "converted": converted,
            "acestep": {k: info.get(k) for k in ("metas", "seed_value", "dit_model", "lm_model")}}
    (workdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    return SongResult(final, lyrics, converted, note)