"""
목소리 수집 봇
- /동의        : 목소리 수집·복제 동의 (버튼 확인)
- /대본        : 녹음할 때 읽을 문장 받기
- /녹음시작     : 봇이 내 음성 채널에 들어와 녹음 시작 (실험 기능)
- /녹음종료     : 녹음을 끝내고 사람별로 저장
- /목소리등록   : 음성 파일 업로드로 샘플 등록
- /내목소리     : 수집 현황 확인
- /삭제        : 내 데이터 + 동의 전부 삭제
- /노래만들기   : 장르·분위기·가사·가수를 골라 AI 노래 만들기
- /모델연결     : Applio에서 학습한 목소리 모델을 멤버에게 연결
- /가수목록     : 목소리 모델이 준비된 멤버 보기
- /모델학습     : 모은 녹음으로 목소리 모델 자동 학습 → 바로 가수로 사용 가능
- /목소리삭제   : 내가 등록한 별도 목소리 삭제
- /안내 /공지   : (관리자) 봇 소개 패널·공지를 봇 이름으로 게시
- /도움말       : 사용설명서 (나에게만 보임)
※ /목소리등록에서 '목소리이름'을 적으면 디스코드 멤버가 아닌 별도 목소리로 등록됩니다 (허락 확인 필수)
- 등록 채널(REGISTER_CHANNEL_ID)에 음성 메시지를 보내면 자동 등록

⚠️ 음성 채널 녹음은 py-cord 개발 브랜치(fix/voice-rec-2)에 의존하는 실험 기능입니다.
"""
import asyncio
import ctypes.util
import hashlib
import json
import logging
import os
import random
import shutil
import threading
import time
import urllib.request
import uuid
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import discord
from dotenv import load_dotenv

load_dotenv()
import music  # noqa: E402  (.env를 먼저 읽은 뒤 import)
import trainer  # noqa: E402
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("voice-bot")
logging.getLogger("discord.voice.receive.reader").setLevel(logging.WARNING)  # RTCP 안내 로그 숨김

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_IDS = [int(x) for x in os.getenv("GUILD_IDS", "").split(",") if x.strip()] or None
REGISTER_CHANNEL_ID = int(os.getenv("REGISTER_CHANNEL_ID", "0"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
MAX_RECORD_SECONDS = int(os.getenv("MAX_RECORD_MINUTES", "10")) * 60

TARGET_SECONDS = 10 * 60          # RVC 학습 목표: 약 10분
SAMPLE_RATE = 48000
MIN_CLIP_SECONDS = 1.0
MAX_BYTES = 25 * 1024 * 1024
AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac", ".webm", ".aac"}
NO_PING = discord.AllowedMentions.none()

SCRIPTS = [
    "오늘 날씨가 정말 좋아서 산책하기 딱 좋은 날이네요.",
    "빨간 사과와 노란 바나나를 바구니에 가득 담았어요.",
    "지하철 이호선은 출근 시간마다 사람들로 붐빕니다.",
    "혹시 내일 오후 세 시에 시간 괜찮으세요?",
    "와, 이거 진짜 대박이다! 어떻게 이런 생각을 했어?",
    "천천히, 아주 천천히 숨을 들이쉬고 내쉬어 보세요.",
    "편의점에서 컵라면이랑 삼각김밥을 사 왔어.",
    "창밖으로 떨어지는 빗소리가 오늘따라 유난히 크게 들린다.",
    "그건 좀 아닌 것 같은데, 다시 한번 생각해 볼래?",
    "칠월 칠일 칠석날에는 견우와 직녀가 만난대요.",
    "핸드폰 배터리가 오 퍼센트밖에 안 남았어!",
    "우리 다음 주말에 바닷가로 여행 가는 거 어때?",
    "쿵쿵 울리는 베이스 소리에 심장이 같이 뛰었다.",
    "정말 고마워. 네 덕분에 큰 힘이 됐어.",
    "아, 진짜? 나는 전혀 몰랐는데 언제부터 그랬어?",
    "푸른 하늘 아래 흰 구름이 천천히 흘러갑니다.",
]


# ---------------- 음성 수신 준비 (실험 기능) ----------------
# 개발 브랜치의 알려진 버그: UDP keepalive가 5000초마다 전송되어
# 몇 분 뒤 디스코드가 음성 전송을 끊음 → 5초로 우회 (pycord 이슈 #3388)
try:
    from discord.voice.receive.reader import UDPKeepAlive
    UDPKeepAlive.delay = 5
except ImportError:
    log.warning("음성 수신 모듈을 찾지 못했어요. requirements.txt의 개발 브랜치를 설치했는지 확인하세요.")


def ensure_opus() -> bool:
    """맥(Homebrew)에서는 libopus를 자동으로 못 찾는 경우가 있어 경로를 직접 시도."""
    if discord.opus.is_loaded():
        return True
    candidates = [
        ctypes.util.find_library("opus"),
        "/opt/homebrew/lib/libopus.dylib",
        "/opt/homebrew/opt/opus/lib/libopus.dylib",
        "/usr/local/lib/libopus.dylib",
        "libopus.so.0",
    ]
    for path in filter(None, candidates):
        try:
            discord.opus.load_opus(path)
            log.info("opus 로드: %s", path)
            return True
        except Exception:
            continue
    return False


def has_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def user_dir(uid: int) -> Path:
    return DATA_DIR / "voices" / str(uid)


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


# ---------------- 동의 / 메타데이터 ----------------
class Store:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.consent_path = DATA_DIR / "consent.json"
        self.consent = (
            json.loads(self.consent_path.read_text("utf-8"))
            if self.consent_path.exists() else {}
        )

    def has_consent(self, uid: int) -> bool:
        return str(uid) in self.consent

    def name(self, uid: int) -> str:
        return self.consent.get(str(uid), {}).get("name", str(uid))

    async def grant(self, user: discord.abc.User) -> None:
        async with self.lock:
            self.consent[str(user.id)] = {"name": str(user), "consented_at": now_iso()}
            atomic_write_json(self.consent_path, self.consent)

    async def revoke_and_delete(self, uid: int) -> None:
        async with self.lock:
            self.consent.pop(str(uid), None)
            atomic_write_json(self.consent_path, self.consent)
            for d in (user_dir(uid), DATA_DIR / "datasets" / str(uid), DATA_DIR / "models" / str(uid),
                      trainer.applio_log_dir(uid)):
                shutil.rmtree(d, ignore_errors=True)

    # ---- 별도 목소리 (디스코드 멤버가 아닌 목소리) ----
    @staticmethod
    def custom_key(name: str) -> str:
        return "v_" + hashlib.sha1(name.strip().lower().encode()).hexdigest()[:10]

    def is_custom(self, key) -> bool:
        return bool(self.consent.get(str(key), {}).get("custom"))

    def custom_voices(self) -> dict[str, dict]:
        return {k: v for k, v in self.consent.items() if v.get("custom")}

    def find_custom(self, name: str) -> str | None:
        key = self.custom_key(name)
        return key if self.is_custom(key) else None

    def label(self, key) -> str:
        entry = self.consent.get(str(key))
        return f"🎙️{entry['name']}" if entry and entry.get("custom") else f"<@{key}>"

    def can_manage(self, key, member) -> bool:
        is_admin = bool(getattr(getattr(member, "guild_permissions", None), "manage_guild", False))
        entry = self.consent.get(str(key), {})
        if entry.get("custom"):
            return entry.get("owner") == member.id or is_admin
        return str(member.id) == str(key) or is_admin

    async def create_custom(self, name: str, owner: discord.abc.User) -> str:
        key = self.custom_key(name)
        async with self.lock:
            self.consent[key] = {
                "name": name.strip(), "custom": True,
                "owner": owner.id, "owner_name": str(owner), "consented_at": now_iso(),
                "attestation": "등록자가 본인 목소리이거나 목소리 주인에게 허락받았다고 확인함",
            }
            atomic_write_json(self.consent_path, self.consent)
        return key

    def meta(self, uid: int) -> dict:
        p = user_dir(uid) / "meta.json"
        return json.loads(p.read_text("utf-8")) if p.exists() else {"clips": []}

    async def add_clip(self, uid: int, clip: dict) -> float:
        async with self.lock:
            m = self.meta(uid)
            m["clips"].append(clip)
            atomic_write_json(user_dir(uid) / "meta.json", m)
            return sum(c["seconds"] for c in m["clips"])


store = Store()


# ---------------- 오디오 처리 ----------------
async def run(*cmd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


async def to_wav(src: Path, dst: Path) -> None:
    rc, _, err = await run(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", str(dst),
    )
    if rc != 0:
        raise ValueError(f"오디오 변환 실패: {err.strip()[:200]}")


async def probe_seconds(path: Path) -> float:
    rc, out, _ = await run(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path),
    )
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def is_audio(att: discord.Attachment) -> bool:
    ext = Path(att.filename).suffix.lower()
    return (att.content_type or "").startswith("audio/") or ext in AUDIO_EXTS


async def ingest_raw(uid: int, raw_path: Path, clip_id: str, source: str) -> tuple[float, float]:
    """raw 파일 → 48kHz 모노 wav 변환 → 메타데이터 기록. (클립 길이, 누적 길이) 반환."""
    wav_path = user_dir(uid) / "wav" / f"{clip_id}.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await to_wav(raw_path, wav_path)
    except Exception:
        wav_path.unlink(missing_ok=True)  # 원본(raw)은 보존 → 나중에 다시 변환 가능
        raise
    seconds = await probe_seconds(wav_path)
    if seconds < MIN_CLIP_SECONDS:
        raw_path.unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)
        raise ValueError("녹음이 너무 짧아요 (1초 미만).")
    total = await store.add_clip(uid, {
        "id": clip_id,
        "raw": raw_path.name,
        "wav": wav_path.name,
        "seconds": round(seconds, 2),
        "source": source,
        "added_at": now_iso(),
    })
    return seconds, total


async def register_audio(key: int | str, att: discord.Attachment) -> tuple[float, float]:
    if not store.has_consent(key):
        raise ValueError("먼저 `/동의`를 진행해 주세요.")
    if not is_audio(att):
        raise ValueError("음성 파일만 등록할 수 있어요 (ogg, mp3, wav, m4a 등).")
    if att.size > MAX_BYTES:
        raise ValueError("파일이 너무 커요 (최대 25MB).")
    clip_id = uuid.uuid4().hex[:12]
    ext = Path(att.filename).suffix.lower() or ".ogg"
    raw_path = user_dir(key) / "raw" / f"{clip_id}{ext}"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(await att.read())
    return await ingest_raw(key, raw_path, clip_id, f"upload:{att.filename}")


async def ingest_pcm(uid: int, pcm: bytes) -> tuple[float, float]:
    """음성 채널에서 받은 PCM(48kHz, 스테레오, 16bit)을 저장."""
    clip_id = uuid.uuid4().hex[:12]
    raw_path = user_dir(uid) / "raw" / f"{clip_id}.wav"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(raw_path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(48000)
        f.writeframes(pcm)
    return await ingest_raw(uid, raw_path, clip_id, "voice-channel")


def progress_text(total: float) -> str:
    ratio = min(total / TARGET_SECONDS, 1.0)
    bar = "█" * int(ratio * 10) + "░" * (10 - int(ratio * 10))
    return f"{bar} {total / 60:.1f}분 / {TARGET_SECONDS // 60}분"


# ---------------- 음성 채널 녹음 ----------------
BYTES_PER_SEC = 48000 * 2 * 2  # 48kHz * 스테레오 * 16bit


class ConsentSink(discord.sinks.Sink):
    """동의한 사람의 목소리만 사람별로 모으는 Sink.

    말이 끊긴 구간은 패킷이 오지 않으므로, 0.3초 이상 쉬면 무음을 넣어서
    전처리 단계에서 문장 단위로 잘 잘리도록 한다.
    """
    GAP = 0.3

    def __init__(self, allowed: Callable[[int], bool]):
        super().__init__()
        self.allowed = allowed
        self._lock = threading.Lock()
        self._buffers: dict[int, bytearray] = {}
        self._last: dict[int, float] = {}
        self._silence = b"\x00" * (int(BYTES_PER_SEC * self.GAP) // 4 * 4)

    def write(self, data, user):  # 패킷 라우터 스레드에서 호출됨
        uid = getattr(user, "id", None)
        if uid is None or not self.allowed(uid):
            return
        pcm = getattr(data, "pcm", data)
        if not pcm:
            return
        now = time.monotonic()
        with self._lock:
            buf = self._buffers.setdefault(uid, bytearray())
            last = self._last.get(uid)
            if last is not None and now - last > self.GAP:
                buf.extend(self._silence)
            buf.extend(pcm)
            self._last[uid] = now

    def cleanup(self):
        self.finished = True  # 파일 포맷팅은 직접 하므로 기본 동작 생략

    def format_audio(self, audio):
        pass

    def pop_all(self) -> dict[int, bytes]:
        with self._lock:
            out = {uid: bytes(b) for uid, b in self._buffers.items()}
            self._buffers.clear()
            self._last.clear()
        return out


@dataclass
class Session:
    vc: "discord.VoiceClient"
    sink: ConsentSink
    text_channel: discord.abc.Messageable
    started: float = field(default_factory=time.monotonic)
    timer: asyncio.Task | None = None


sessions: dict[int, Session] = {}


def humans_in(channel) -> list[int]:
    """음성 채널에 있는 사람 ID (봇 자신 제외). members 권한 없이도 동작."""
    me = bot.user.id if bot.user else None
    return [uid for uid in channel.voice_states.keys() if uid != me]


async def stop_session(guild_id: int) -> list[tuple[int, float | None, float | str]] | None:
    s = sessions.pop(guild_id, None)
    if s is None:
        return None
    if s.timer and s.timer is not asyncio.current_task():
        s.timer.cancel()
    try:
        if s.vc.is_recording():
            s.vc.stop_recording()
    except Exception:
        log.exception("stop_recording 실패")
    await asyncio.sleep(0.5)  # 마지막 패킷 처리 대기
    buffers = s.sink.pop_all()
    try:
        await s.vc.disconnect(force=True)
    except Exception:
        log.exception("음성 채널 퇴장 실패")

    results = []
    for uid, pcm in buffers.items():
        if not store.has_consent(uid):  # 녹음 중 /삭제한 경우
            continue
        try:
            seconds, total = await ingest_pcm(uid, pcm)
            results.append((uid, seconds, total))
        except ValueError as e:
            results.append((uid, None, str(e)))
        except Exception as e:
            log.exception("녹음 저장 실패 (uid=%s)", uid)
            results.append((uid, None, f"저장 실패 ({type(e).__name__}) — 원본은 raw 폴더에 보존됨"))
    return results


def summary(results) -> str:
    if not results:
        return "저장된 목소리가 없어요. (동의한 분의 말소리가 들어오지 않았어요)"
    lines = []
    for uid, sec, total in results:
        if sec is None:
            lines.append(f"• <@{uid}>: ⚠️ {total}")
        else:
            lines.append(f"• <@{uid}>: +{sec:.1f}초  {progress_text(total)}")
    return "\n".join(lines)


async def auto_stop(guild_id: int) -> None:
    await asyncio.sleep(MAX_RECORD_SECONDS)
    s = sessions.get(guild_id)
    if s is None:
        return
    results = await stop_session(guild_id)
    await s.text_channel.send(
        f"⏹️ {MAX_RECORD_SECONDS // 60}분이 지나 녹음을 자동 종료했어요.\n{summary(results)}",
        allowed_mentions=NO_PING,
    )


# ---------------- 봇 ----------------
intents = discord.Intents.default()     # voice_states 포함
intents.message_content = True          # 등록 채널의 첨부파일 읽기
def joined_guild_ids(token: str) -> set[int] | None:
    """봇이 실제로 들어가 있는 서버 목록 (시작 전에 확인)."""
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v10/users/@me/guilds",
            headers={"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (voice-bot, 1.0)"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return {int(g["id"]) for g in json.load(r)}
    except Exception as e:
        log.warning("서버 목록 확인 실패 (%s) — GUILD_IDS를 그대로 사용해요", e)
        return None


if GUILD_IDS:
    _joined = joined_guild_ids(TOKEN)
    if _joined is not None:
        for gid in GUILD_IDS:
            if gid not in _joined:
                log.warning("⚠️ 봇이 들어가 있지 않은 서버 %s → 명령 등록을 건너뛰어요. "
                            "'bot'과 'applications.commands' 범위로 초대해 주세요.", gid)
        GUILD_IDS = [g for g in GUILD_IDS if g in _joined] or None

bot = discord.Bot(intents=intents, debug_guilds=GUILD_IDS)
_panel_registered = False


class ConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, on_confirm, confirm_label: str, style: discord.ButtonStyle):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.on_confirm = on_confirm
        self.children[0].label = confirm_label
        self.children[0].style = style

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="확인", style=discord.ButtonStyle.success)
    async def confirm(self, button, interaction: discord.Interaction):
        msg = await self.on_confirm(interaction.user)
        await interaction.response.edit_message(content=msg, view=None)
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, button, interaction: discord.Interaction):
        await interaction.response.edit_message(content="취소했어요.", view=None)
        self.stop()


CONSENT_TEXT = (
    "**목소리 수집·복제 동의**\n"
    "• 올린 음성, 그리고 `/녹음시작` 중 음성 채널에서 한 말로 내 목소리 AI 모델을 학습하고, "
    "채널 AI 음악의 보컬로 사용해요.\n"
    "• 결과물은 이 서버 안에서만 공유해요.\n"
    "• 언제든 `/삭제`로 음성 파일·모델·동의 기록을 전부 지울 수 있어요.\n\n"
    "동의하시나요?"
)


@bot.event
async def on_connect():
    try:
        await bot.sync_commands()
    except discord.Forbidden:
        log.error("슬래시 명령 등록 실패 (403 Missing Access). 봇을 초대할 때 'applications.commands' 범위를 "
                  "체크했는지 확인하고, 초대 URL을 새로 만들어 다시 초대해 주세요.")


@bot.event
async def on_ready():
    global _panel_registered
    if not _panel_registered:
        bot.add_view(HelpPanel())   # 예전에 올린 패널의 버튼도 계속 동작
        _panel_registered = True
    opus_ok = ensure_opus()
    log.info("로그인: %s (등록 채널: %s, opus: %s, ffmpeg: %s)", bot.user, REGISTER_CHANNEL_ID or "미설정",
             "OK" if opus_ok else "없음 → brew install opus",
             "OK" if has_ffmpeg() else "없음 → brew install ffmpeg")
    print(f"로그인: {bot.user} (등록 채널: {REGISTER_CHANNEL_ID or '미설정'})")


def consent_prompt(user) -> tuple[str, discord.ui.View | None]:
    if store.has_consent(user.id):
        return "이미 동의하셨어요. `/대본`을 보고 녹음을 시작해 보세요!", None

    async def grant(u):
        await store.grant(u)
        return "동의 완료! `/대본`으로 읽을 문장을 받고, 음성 채널에서 `/녹음시작`을 해보세요."

    return CONSENT_TEXT, ConfirmView(user.id, grant, "동의합니다", discord.ButtonStyle.success)


def script_text() -> str:
    lines = random.sample(SCRIPTS, 5)
    body = "\n".join(f"{i}. {s}" for i, s in enumerate(lines, 1))
    return body + (
        "\n\n**녹음 팁**: 조용한 곳에서, 평소 말투로, 문장 사이 1초 쉬기. "
        "노래 한 소절을 흥얼거린 녹음도 섞어 주면 노래 품질이 좋아져요."
    )


def singers_text() -> str:
    ready = [f"• {store.label(k)}" for k in store.consent if music.singer_model(k)]
    if not ready:
        return "아직 목소리 모델이 준비된 가수가 없어요. 녹음 → `/모델학습` 순서로 준비해 주세요."
    return "🎤 **부를 수 있는 가수**\n" + "\n".join(ready)


@bot.slash_command(name="동의", description="목소리 수집·복제에 동의합니다")
async def consent_cmd(ctx: discord.ApplicationContext):
    text, view = consent_prompt(ctx.author)
    await ctx.respond(text, view=view, ephemeral=True) if view else await ctx.respond(text, ephemeral=True)


@bot.slash_command(name="대본", description="녹음할 때 읽을 문장을 받아요")
async def script_cmd(ctx: discord.ApplicationContext):
    await ctx.respond(script_text(), ephemeral=True)


@bot.slash_command(name="녹음시작", description="봇이 내 음성 채널에 들어와 녹음을 시작해요 (실험 기능)")
async def rec_start(ctx: discord.ApplicationContext):
    voice = ctx.author.voice
    if not voice or not voice.channel:
        return await ctx.respond("먼저 음성 채널에 들어간 다음 다시 시도해 주세요.", ephemeral=True)
    if ctx.guild.id in sessions:
        return await ctx.respond("이미 녹음 중이에요. `/녹음종료`로 먼저 끝내 주세요.", ephemeral=True)
    if not ensure_opus():
        return await ctx.respond(
            "opus 라이브러리가 없어요. 터미널에서 `brew install opus` 후 봇을 재시작해 주세요.",
            ephemeral=True,
        )

    if not has_ffmpeg():
        return await ctx.respond(
            "ffmpeg가 없어서 녹음을 저장할 수 없어요. 터미널에서 `brew install ffmpeg` 후 봇을 재시작해 주세요.",
            ephemeral=True,
        )

    await ctx.defer()
    channel = voice.channel
    try:
        vc = ctx.guild.voice_client
        if vc and vc.channel != channel:
            await vc.move_to(channel)
        elif not vc:
            vc = await channel.connect(timeout=20)
        sink = ConsentSink(store.has_consent)
        vc.start_recording(sink)
    except Exception as e:
        log.exception("녹음 시작 실패")
        if ctx.guild.voice_client:
            await ctx.guild.voice_client.disconnect(force=True)
        return await ctx.respond(f"⚠️ 녹음을 시작하지 못했어요: `{type(e).__name__}: {e}`")

    session = Session(vc=vc, sink=sink, text_channel=ctx.channel)
    session.timer = asyncio.create_task(auto_stop(ctx.guild.id))
    sessions[ctx.guild.id] = session

    people = humans_in(channel)
    yes = [f"<@{u}>" for u in people if store.has_consent(u)]
    no = [f"<@{u}>" for u in people if not store.has_consent(u)]
    dave = "🔒 E2EE" if getattr(vc, "is_dave_connection", lambda: False)() else ""
    msg = (
        f"🔴 **녹음 시작** — {channel.mention} {dave}\n"
        f"최대 {MAX_RECORD_SECONDS // 60}분 뒤 자동 종료, 끝낼 땐 `/녹음종료`\n"
        f"저장 대상: {', '.join(yes) or '없음'}\n"
    )
    if no:
        msg += f"저장 안 함 (미동의): {', '.join(no)} — 원하면 `/동의` 후 참여할 수 있어요."
    await ctx.respond(msg, allowed_mentions=NO_PING)


@bot.slash_command(name="녹음종료", description="녹음을 끝내고 사람별로 저장해요")
async def rec_stop(ctx: discord.ApplicationContext):
    if ctx.guild.id not in sessions:
        return await ctx.respond("지금은 녹음 중이 아니에요.", ephemeral=True)
    await ctx.defer()
    results = await stop_session(ctx.guild.id)
    await ctx.respond(f"⏹️ **녹음 종료**\n{summary(results)}", allowed_mentions=NO_PING)


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    """녹음 중인 채널에 사람이 아무도 안 남으면 자동 종료."""
    s = sessions.get(member.guild.id)
    if s is None or not s.vc.channel or before.channel != s.vc.channel:
        return
    if humans_in(s.vc.channel):
        return
    results = await stop_session(member.guild.id)
    await s.text_channel.send(
        f"⏹️ 음성 채널이 비어서 녹음을 종료했어요.\n{summary(results)}",
        allowed_mentions=NO_PING,
    )


# ---------------- 목소리 선택 공용 ----------------
async def voice_autocomplete(ctx: discord.AutocompleteContext):
    q = (ctx.value or "").lower()
    return [v["name"] for v in store.custom_voices().values() if q in v["name"].lower()][:25]


def resolve_voice(author, member, custom: str) -> tuple[str, str]:
    """(목소리 키, 표시 이름). 멤버·별도 목소리 중 하나, 둘 다 없으면 나 자신."""
    custom = (custom or "").strip()
    if member and custom:
        raise ValueError("멤버와 목소리 중 하나만 골라 주세요.")
    if custom:
        key = store.find_custom(custom)
        if not key:
            raise ValueError(f"`{custom}` 목소리가 없어요. `/목소리등록`에서 목소리이름을 적어 먼저 만들어 주세요.")
    else:
        key = str((member or author).id)
    if not store.has_consent(key):
        raise ValueError("목소리 사용에 동의한 목소리만 쓸 수 있어요.")
    if not store.can_manage(key, author):
        raise ValueError("본인 목소리(또는 내가 등록한 목소리)거나 서버 관리 권한이 있어야 해요.")
    return key, store.label(key)


# ---------------- 노래 만들기 ----------------
gpu_lock = asyncio.Lock()           # 맥 한 대라 작곡·학습은 한 번에 하나씩
gpu_waiting = 0
training_users: set[str] = set()
_song_tasks: set[asyncio.Task] = set()


def song_header(req: music.SongRequest) -> str:
    title = req.title or req.topic or "무제"
    singer = req.singer_label or "AI 보컬"
    lyric = {"직접 입력": "직접 쓴 가사", "AI가 작성": "AI 가사", "연주곡": "연주곡"}[req.lyric_mode]
    ref = f"\n🎧 레퍼런스: `{req.reference_name}`" if req.gen_mode == "레퍼런스 참고" else ""
    return (f"🎵 **{title}** — {req.genre} · {req.mood} · {req.duration}초 · {lyric}\n"
            f"가수: {singer} | 요청: <@{req.requester_id}>{ref}")


async def run_song_job(req: music.SongRequest, channel) -> None:
    global gpu_waiting
    header = song_header(req)
    ahead = gpu_waiting + (1 if gpu_lock.locked() else 0)
    status = await channel.send(
        f"{header}\n⏳ " + (f"앞에 {ahead}곡이 대기 중이에요." if ahead else "곧 시작해요."),
        allowed_mentions=NO_PING,
    )

    async def progress(text: str) -> None:
        try:
            await status.edit(content=f"{header}\n{text}", allowed_mentions=NO_PING)
        except discord.HTTPException:
            pass

    gpu_waiting += 1
    async with gpu_lock:
        gpu_waiting -= 1
        try:
            result = await music.make_song(req, progress)
        except music.SongError as e:
            return await progress(f"⚠️ {e}")
        except Exception as e:
            log.exception("노래 생성 실패")
            return await progress(f"⚠️ 예상치 못한 오류: `{type(e).__name__}: {e}`")

    done = "✅ 완성! 멤버 목소리로 불렀어요." if result.converted else "✅ 완성!"
    if result.note:
        done += f"\nℹ️ {result.note}"
    lyrics = result.lyrics.strip()
    if lyrics and req.lyric_mode != "연주곡":
        preview = lyrics if len(lyrics) <= 1200 else lyrics[:1200] + "\n…"
        done += f"\n```\n{preview}\n```"
    await progress(done)
    safe_title = "".join(c for c in (req.title or req.topic or "song") if c.isalnum() or c in " -_")[:40] or "song"
    await channel.send(file=discord.File(result.mp3, filename=f"{safe_title.strip()}.mp3"))


def start_song_job(req: music.SongRequest, channel) -> None:
    task = asyncio.create_task(run_song_job(req, channel))
    _song_tasks.add(task)
    task.add_done_callback(_song_tasks.discard)


class LyricsModal(discord.ui.Modal):
    def __init__(self, req: music.SongRequest):
        super().__init__(
            discord.ui.InputText(label="제목", required=False, max_length=80, placeholder="시험 끝난 날"),
            discord.ui.InputText(
                label="가사 ([verse], [chorus] 같은 구조 태그를 쓰면 더 좋아요)",
                style=discord.InputTextStyle.long, max_length=3000,
                placeholder="[verse]\n시험 끝난 금요일 밤\n\n[chorus]\n우리끼리 신나게 놀자",
            ),
            title="🎤 가사 입력",
        )
        self.req = req

    async def callback(self, interaction: discord.Interaction):
        self.req.title = (self.children[0].value or "").strip()
        self.req.lyrics = self.children[1].value
        await interaction.response.send_message("🎵 접수했어요! 채널에 진행 상황을 올릴게요.", ephemeral=True)
        start_song_job(self.req, interaction.channel)


@bot.slash_command(name="노래만들기", description="장르, 분위기, 가사, 가수를 골라 AI 노래를 만들어요")
async def song_cmd(
    ctx: discord.ApplicationContext,
    genre: discord.Option(str, "장르", name="장르", choices=list(music.GENRES)),
    mood: discord.Option(str, "분위기", name="분위기", choices=list(music.MOODS)),
    lyric_mode: discord.Option(str, "가사를 어떻게 할까요?", name="가사", choices=music.LYRIC_MODES),
    singer: discord.Option(discord.Member, "이 멤버 목소리로 불러요 (목소리 모델이 있어야 해요)",
                           name="가수", required=False, default=None),
    voice: discord.Option(str, "원곡 보컬 성별 (가수와 맞추면 자연스러워요)", name="보컬",
                          choices=list(music.VOICES), required=False, default="자동"),
    duration: discord.Option(int, "길이(초)", name="길이", choices=music.DURATIONS,
                             required=False, default=60),
    topic: discord.Option(str, "주제 (AI 가사일 때 사용, 예: 시험 끝난 날)", name="주제",
                          required=False, default=""),
    extra: discord.Option(str, "추가 스타일 (예: 피아노, 90년대 느낌)", name="추가스타일",
                          required=False, default=""),
    pitch: discord.Option(int, "가수 목소리 키 조절 (여성곡→남성 목소리 -12, 반대 +12)", name="키조절",
                          min_value=-12, max_value=12, required=False, default=0),
    custom: discord.Option(str, "멤버 대신 등록해 둔 별도 목소리로 불러요", name="목소리",
                           required=False, default="", autocomplete=voice_autocomplete),
    gen_mode: discord.Option(str, "완전히 새로 만들지, 레퍼런스 음악을 참고할지", name="방식",
                             choices=music.GEN_MODES, required=False, default="새로 생성"),
    reference: discord.Option(discord.Attachment, "레퍼런스 음악 파일 (방식: 레퍼런스 참고)", name="레퍼런스",
                              required=False, default=None),
):
    if not await music.acestep_alive():
        return await ctx.respond(
            "🎼 작곡 서버(ACE-Step)가 꺼져 있어요. 맥에서 `~/ACE-Step-1.5/start_api_server_macos.sh`를 실행해 주세요.",
            ephemeral=True,
        )
    singer_key, singer_label = None, ""
    if singer and custom.strip():
        return await ctx.respond("가수(멤버)와 목소리 중 하나만 골라 주세요.", ephemeral=True)
    if singer:
        if not store.has_consent(singer.id):
            return await ctx.respond(f"{singer.mention}님은 목소리 사용에 동의하지 않았어요.",
                                     ephemeral=True, allowed_mentions=NO_PING)
        singer_key, singer_label = str(singer.id), singer.mention
    elif custom.strip():
        singer_key = store.find_custom(custom)
        if not singer_key:
            return await ctx.respond(f"`{custom}` 목소리가 없어요. `/가수목록`에서 이름을 확인해 주세요.", ephemeral=True)
        singer_label = store.label(singer_key)

    if reference and gen_mode == "새로 생성":
        gen_mode = "레퍼런스 참고"   # 파일을 첨부했으면 참고 모드로 간주
    if gen_mode == "레퍼런스 참고":
        if not reference:
            return await ctx.respond("🎧 `레퍼런스` 칸에 참고할 음악 파일을 첨부해 주세요.", ephemeral=True)
        if not is_audio(reference) or reference.size > MAX_BYTES:
            return await ctx.respond("레퍼런스는 25MB 이하의 음악 파일(mp3, wav, m4a 등)만 가능해요.", ephemeral=True)

    req = music.SongRequest(
        requester_id=ctx.author.id, genre=genre, mood=mood, lyric_mode=lyric_mode,
        voice=voice, duration=duration, topic=topic.strip(), extra=extra.strip(),
        singer_key=singer_key, singer_label=singer_label, pitch=pitch, gen_mode=gen_mode,
        reference_url=reference.url if reference else "",
        reference_name=reference.filename if reference else "",
    )
    if lyric_mode == "직접 입력":
        return await ctx.send_modal(LyricsModal(req))

    await ctx.respond("🎵 접수했어요! 채널에 진행 상황을 올릴게요.", ephemeral=True)
    start_song_job(req, ctx.channel)


@bot.slash_command(name="모델연결", description="Applio에서 학습한 목소리 모델을 멤버에게 연결해요")
async def link_model_cmd(
    ctx: discord.ApplicationContext,
    model_name: discord.Option(str, "Applio 학습 때 정한 모델 이름 (예: suhyeon)", name="모델이름"),
    member: discord.Option(discord.Member, "목소리 주인 (기본: 나)", name="멤버", required=False, default=None),
    custom: discord.Option(str, "멤버 대신 별도 목소리에 연결", name="목소리",
                           required=False, default="", autocomplete=voice_autocomplete),
):
    try:
        key, label = resolve_voice(ctx.author, member, custom)
        pth, idx = music.link_applio_model(key, model_name.strip())
    except (ValueError, music.SongError) as e:
        return await ctx.respond(f"⚠️ {e}", ephemeral=True)
    await ctx.respond(f"✅ {label} 목소리 모델 연결 완료 (`{pth.name}`)\n"
                      "이제 `/노래만들기`에서 가수로 고를 수 있어요.",
                      ephemeral=True, allowed_mentions=NO_PING)


@bot.slash_command(name="가수목록", description="목소리 모델이 준비된 멤버를 보여줘요")
async def singers_cmd(ctx: discord.ApplicationContext):
    await ctx.respond(singers_text(), ephemeral=True, allowed_mentions=NO_PING)



# ---------------- 목소리 모델 학습 ----------------
async def run_train_job(uid: str, label: str, epochs: int | None, channel) -> None:
    global gpu_waiting
    header = f"🧠 **목소리 모델 학습** — {label}"
    ahead = gpu_waiting + (1 if gpu_lock.locked() else 0)
    status = await channel.send(
        f"{header}\n⏳ " + (f"앞에 {ahead}개 작업이 대기 중이에요." if ahead else "곧 시작해요."),
        allowed_mentions=NO_PING,
    )

    async def progress(text: str) -> None:
        try:
            await status.edit(content=f"{header}\n{text}", allowed_mentions=NO_PING)
        except discord.HTTPException:
            pass

    gpu_waiting += 1
    try:
        async with gpu_lock:
            gpu_waiting -= 1
            started = time.monotonic()
            try:
                pth, seconds, used_epochs = await trainer.train_voice(uid, epochs, progress)
            except trainer.TrainError as e:
                return await progress(f"⚠️ {e}")
            except Exception as e:
                log.exception("학습 실패")
                return await progress(f"⚠️ 예상치 못한 오류: `{type(e).__name__}: {e}`")
    finally:
        training_users.discard(uid)

    took = (time.monotonic() - started) / 60
    await progress(
        f"✅ 완성! 녹음 {seconds / 60:.1f}분 · {used_epochs} 에폭 · {took:.0f}분 걸렸어요.\n"
        f"이제 `/노래만들기`에서 {label}(으)로 부르게 할 수 있어요."
    )


@bot.slash_command(name="모델학습", description="모은 녹음으로 목소리 모델을 학습해요 (시간이 오래 걸려요)")
async def train_cmd(
    ctx: discord.ApplicationContext,
    member: discord.Option(discord.Member, "학습할 사람 (기본: 나)", name="멤버", required=False, default=None),
    custom: discord.Option(str, "멤버 대신 별도 목소리를 학습", name="목소리",
                           required=False, default="", autocomplete=voice_autocomplete),
    epochs: discord.Option(int, "학습 횟수 (비워두면 녹음 길이에 맞춰 자동)", name="에폭",
                           choices=[100, 150, 200, 300], required=False, default=None),
):
    try:
        key, label = resolve_voice(ctx.author, member, custom)
    except ValueError as e:
        return await ctx.respond(f"⚠️ {e}", ephemeral=True)
    if key in training_users:
        return await ctx.respond("이미 학습이 예약되어 있거나 진행 중이에요.", ephemeral=True)
    if not store.meta(key)["clips"]:
        return await ctx.respond("아직 녹음이 없어요. 녹음이나 `/목소리등록`으로 먼저 모아 주세요.", ephemeral=True)

    training_users.add(key)
    await ctx.respond("🧠 학습을 예약했어요! 채널에 진행 상황을 올릴게요. (보통 수십 분 이상 걸려요)", ephemeral=True)
    task = asyncio.create_task(run_train_job(key, label, epochs, ctx.channel))
    _song_tasks.add(task)
    task.add_done_callback(_song_tasks.discard)

@bot.slash_command(name="목소리등록", description="음성 파일로 내 목소리 또는 별도 목소리 샘플을 등록해요")
async def register_cmd(
    ctx: discord.ApplicationContext,
    file: discord.Option(discord.Attachment, "등록할 음성 파일", name="파일"),
    name: discord.Option(str, "다른 목소리로 등록하려면 이름 (비우면 내 목소리)", name="목소리이름",
                         required=False, default="", autocomplete=voice_autocomplete),
    attest: discord.Option(bool, "본인 목소리이거나 목소리 주인에게 허락받았나요? (새 목소리를 만들 때 필수)",
                           name="허락확인", required=False, default=False),
):
    await ctx.defer(ephemeral=True)
    name = name.strip()
    if name:
        if len(name) > 30:
            return await ctx.respond("목소리 이름은 30자 이내로 적어 주세요.", ephemeral=True)
        key = store.find_custom(name)
        if key and not store.can_manage(key, ctx.author):
            return await ctx.respond("다른 사람이 만든 목소리라 샘플을 추가할 수 없어요.", ephemeral=True)
        if not key:
            if not attest:
                return await ctx.respond(
                    "새 목소리를 만들려면 `허락확인`을 **True**로 골라 주세요.\n"
                    "본인 목소리이거나, 목소리 주인에게 녹음 사용과 AI 학습을 허락받은 경우에만 등록할 수 있어요.",
                    ephemeral=True,
                )
            key = await store.create_custom(name, ctx.author)
        label = store.label(key)
    else:
        key, label = str(ctx.author.id), "내 목소리"
    try:
        seconds, total = await register_audio(key, file)
    except ValueError as e:
        return await ctx.respond(f"⚠️ {e}", ephemeral=True)
    await ctx.respond(f"✅ {label}에 {seconds:.1f}초 등록!\n{progress_text(total)}", ephemeral=True,
                      allowed_mentions=NO_PING)


@bot.slash_command(name="내목소리", description="내 목소리 샘플 수집 현황")
async def status_cmd(ctx: discord.ApplicationContext):
    await ctx.respond(status_text(ctx.author), ephemeral=True)


def status_text(user) -> str:
    lines = []
    if store.has_consent(user.id):
        clips = store.meta(user.id)["clips"]
        total = sum(c["seconds"] for c in clips)
        vc_sec = sum(c["seconds"] for c in clips if c.get("source") == "voice-channel")
        model = " · 모델 ✅" if music.singer_model(user.id) else ""
        lines.append(f"**내 목소리**: 클립 {len(clips)}개 (음성 채널 {vc_sec / 60:.1f}분){model}\n{progress_text(total)}")
    for key, v in store.custom_voices().items():
        if v.get("owner") != user.id:
            continue
        clips = store.meta(key)["clips"]
        total = sum(c["seconds"] for c in clips)
        model = " · 모델 ✅" if music.singer_model(key) else ""
        lines.append(f"**🎙️{v['name']}**: 클립 {len(clips)}개{model}\n{progress_text(total)}")
    if not lines:
        return "아직 등록된 목소리가 없어요. `/동의`부터 시작해 주세요."
    return "\n\n".join(lines)


@bot.slash_command(name="삭제", description="내 음성·모델·동의 기록을 전부 삭제해요")
async def delete_cmd(ctx: discord.ApplicationContext):
    owned = [k for k, v in store.custom_voices().items() if v.get("owner") == ctx.author.id]

    async def wipe(user):
        await store.revoke_and_delete(user.id)
        for key in owned:
            await store.revoke_and_delete(key)
        extra = f" 내가 등록한 별도 목소리 {len(owned)}개도 함께 삭제했어요." if owned else ""
        return "🗑️ 음성 파일, 모델, 동의 기록을 모두 삭제했어요." + extra

    note = f"\n내가 등록한 별도 목소리 {len(owned)}개도 함께 지워져요." if owned else ""
    view = ConfirmView(ctx.author.id, wipe, "전부 삭제", discord.ButtonStyle.danger)
    await ctx.respond("정말 전부 삭제할까요? 되돌릴 수 없어요." + note, view=view, ephemeral=True)


@bot.slash_command(name="목소리삭제", description="내가 등록한 별도 목소리의 녹음과 모델을 삭제해요")
async def delete_voice_cmd(
    ctx: discord.ApplicationContext,
    name: discord.Option(str, "삭제할 목소리 이름", name="목소리", autocomplete=voice_autocomplete),
):
    key = store.find_custom(name)
    if not key:
        return await ctx.respond(f"`{name}` 목소리가 없어요.", ephemeral=True)
    if not store.can_manage(key, ctx.author):
        return await ctx.respond("내가 등록한 목소리이거나 서버 관리 권한이 있어야 삭제할 수 있어요.", ephemeral=True)
    label = store.label(key)

    async def wipe(user):
        await store.revoke_and_delete(key)
        return f"🗑️ {label} 목소리의 녹음과 모델을 모두 삭제했어요."

    view = ConfirmView(ctx.author.id, wipe, "삭제", discord.ButtonStyle.danger)
    await ctx.respond(f"{label} 목소리를 삭제할까요? 되돌릴 수 없어요.", view=view, ephemeral=True)



# ---------------- 안내 패널 · 공지 · 도움말 ----------------
PANEL_COLOR = 0xF5A623


def section(title: str, body: str) -> str:
    return f"> **{title}**\n{body}\n"


def bot_avatar() -> str | None:
    return bot.user.display_avatar.url if bot.user else None


def panel_embed() -> discord.Embed:
    name = bot.user.display_name if bot.user else "노래봇"
    e = discord.Embed(
        title=f"{name} - AI 노래 채널",
        color=PANEL_COLOR,
        description="\n".join([
            section("🎤 내 목소리로 부르는 AI 노래",
                    "동의한 멤버의 목소리를 학습해서, 원하는 노래를 그 목소리로 불러줘요."),
            section("🎼 장르·분위기·가사 골라서 작곡",
                    "K-POP부터 트로트까지. 가사는 직접 쓰거나 AI에게 맡기거나, "
                    "좋아하는 곡을 레퍼런스로 넣을 수도 있어요."),
            section("🔒 동의한 사람만, 언제든 삭제",
                    "동의하지 않은 목소리는 저장되지 않고, `/삭제` 한 번이면 녹음·모델이 전부 지워져요."),
        ]),
    )
    if bot_avatar():
        e.set_thumbnail(url=bot_avatar())
    e.set_footer(text="아래 버튼을 눌러보세요 · 버튼 결과는 나에게만 보여요")
    return e


def guide_embed() -> discord.Embed:
    e = discord.Embed(title="📖 사용설명서", color=PANEL_COLOR, description="\n".join([
        section("1️⃣ 동의하기", "`/동의` — 목소리 사용에 동의해야 녹음·학습이 돼요."),
        section("2️⃣ 목소리 모으기",
                "음성 채널에서 `/녹음시작` → 5~10분 수다 떨기 → `/녹음종료`\n"
                "`/대본`의 문장을 읽거나, 폰에서 음성 메시지·`/목소리등록`으로 올려도 돼요."),
        section("3️⃣ 목소리 모델 만들기", "`/모델학습` — 녹음 3분 이상이면 가능 (10분 이상 권장, 수십 분 걸려요)"),
        section("4️⃣ 노래 만들기", "`/노래만들기` — 장르·분위기·가사를 고르고 **가수**에 나를 선택!"),
        section("📊 확인하기", "`/내목소리` 진행 상황 · `/가수목록` 부를 수 있는 가수"),
    ]))
    e.set_footer(text="작업은 한 번에 하나씩 처리돼요 · 봇은 운영자 맥이 켜져 있을 때 동작해요")
    return e


def song_guide_embed() -> discord.Embed:
    e = discord.Embed(title="🎵 노래 만들기", color=PANEL_COLOR, description="\n".join([
        section("필수", "**장르** · **분위기** · **가사** (직접 입력 / AI가 작성 / 연주곡)"),
        section("선택",
                "**가수** 멤버 목소리로 · **목소리** 별도 등록한 목소리로\n"
                "**보컬** 남성/여성 · **길이** 30~180초 · **주제** AI 가사 주제\n"
                "**추가스타일** 예: 피아노, 90년대 느낌 · **키조절** -12~12\n"
                "**방식** 새로 생성 / 레퍼런스 참고 · **레퍼런스** 참고할 음악 파일"),
        section("예시",
                "`/노래만들기 장르:K-POP 분위기:신나는 가사:AI가 작성 주제:시험 끝난 날`\n"
                "`/노래만들기 장르:발라드 분위기:감성적인 가사:직접 입력 가수:@나`"),
        section("팁", "가수가 남자면 **보컬:남성**을 같이 골라 주면 훨씬 자연스러워요."),
    ]))
    return e


def privacy_embed() -> discord.Embed:
    return discord.Embed(title="🔒 개인정보 · 삭제", color=PANEL_COLOR, description="\n".join([
        section("저장하는 것", "동의한 사람의 녹음, 학습된 목소리 모델, 만든 노래"),
        section("저장하지 않는 것", "동의하지 않은 사람의 목소리 (녹음 중에도 바로 버려요)"),
        section("공유 범위", "이 서버 안에서만 사용해요. 녹음할 땐 봇이 🔴 표시와 저장 대상을 알려줘요."),
        section("삭제", "`/삭제` — 녹음·모델·동의 기록을 전부 지워요. 내가 등록한 별도 목소리도 함께 지워져요."),
    ]))


class HelpPanel(discord.ui.View):
    """재시작 후에도 동작하는 안내 패널 버튼 (custom_id 고정, timeout 없음)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="사용법", emoji="📖", style=discord.ButtonStyle.primary, custom_id="panel:guide", row=0)
    async def guide(self, button, interaction: discord.Interaction):
        await interaction.response.send_message(embed=guide_embed(), ephemeral=True)

    @discord.ui.button(label="동의하기", emoji="✅", style=discord.ButtonStyle.success, custom_id="panel:consent", row=0)
    async def consent(self, button, interaction: discord.Interaction):
        text, view = consent_prompt(interaction.user)
        if view:
            await interaction.response.send_message(text, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="대본", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="panel:script", row=0)
    async def script(self, button, interaction: discord.Interaction):
        await interaction.response.send_message(script_text(), ephemeral=True)

    @discord.ui.button(label="내 목소리", emoji="🎤", style=discord.ButtonStyle.secondary, custom_id="panel:status", row=0)
    async def status(self, button, interaction: discord.Interaction):
        await interaction.response.send_message(status_text(interaction.user), ephemeral=True)

    @discord.ui.button(label="노래 만들기", emoji="🎵", style=discord.ButtonStyle.primary, custom_id="panel:song", row=1)
    async def song(self, button, interaction: discord.Interaction):
        await interaction.response.send_message(embed=song_guide_embed(), ephemeral=True)

    @discord.ui.button(label="가수 목록", emoji="🎶", style=discord.ButtonStyle.secondary, custom_id="panel:singers", row=1)
    async def singers(self, button, interaction: discord.Interaction):
        await interaction.response.send_message(singers_text(), ephemeral=True, allowed_mentions=NO_PING)

    @discord.ui.button(label="개인정보·삭제", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="panel:privacy", row=1)
    async def privacy(self, button, interaction: discord.Interaction):
        await interaction.response.send_message(embed=privacy_embed(), ephemeral=True)


@bot.slash_command(name="안내", description="(관리자) 이 채널에 봇 소개 패널을 올려요")
@discord.default_permissions(manage_guild=True)
async def panel_cmd(
    ctx: discord.ApplicationContext,
    pin: discord.Option(bool, "패널을 고정할까요? (봇에 메시지 관리 권한 필요)", name="고정",
                        required=False, default=False),
):
    try:
        msg = await ctx.channel.send(embed=panel_embed(), view=HelpPanel())
    except discord.Forbidden:
        return await ctx.respond("이 채널에 글을 올릴 권한이 없어요. 봇 역할에 **메시지 보내기·링크 첨부** 권한을 확인해 주세요.",
                                 ephemeral=True)
    note = ""
    if pin:
        try:
            await msg.pin()
        except discord.Forbidden:
            note = " (고정은 권한이 없어서 못 했어요)"
    await ctx.respond("✅ 안내 패널을 올렸어요." + note, ephemeral=True)


class AnnounceModal(discord.ui.Modal):
    def __init__(self, channel):
        super().__init__(
            discord.ui.InputText(label="제목", max_length=100, placeholder="🎉 업데이트 안내"),
            discord.ui.InputText(label="내용 (디스코드 마크다운 사용 가능)", style=discord.InputTextStyle.long,
                                 max_length=4000, placeholder="이번에 새로 추가된 기능은..."),
            title="📢 공지 작성",
        )
        self.channel = channel

    async def callback(self, interaction: discord.Interaction):
        e = discord.Embed(title=self.children[0].value, description=self.children[1].value, color=PANEL_COLOR,
                          timestamp=datetime.now(timezone.utc))
        if bot_avatar():
            e.set_thumbnail(url=bot_avatar())
        e.set_footer(text=f"공지 · {interaction.user.display_name}")
        try:
            await self.channel.send(embed=e)
        except discord.Forbidden:
            return await interaction.response.send_message("그 채널에 글을 올릴 권한이 없어요.", ephemeral=True)
        await interaction.response.send_message(f"✅ {self.channel.mention}에 공지를 올렸어요.", ephemeral=True)


@bot.slash_command(name="공지", description="(관리자) 봇 이름으로 공지를 올려요")
@discord.default_permissions(manage_guild=True)
async def announce_cmd(
    ctx: discord.ApplicationContext,
    channel: discord.Option(discord.TextChannel, "올릴 채널 (기본: 지금 채널)", name="채널",
                            required=False, default=None),
):
    await ctx.send_modal(AnnounceModal(channel or ctx.channel))


@bot.slash_command(name="도움말", description="사용설명서를 보여줘요 (나에게만 보여요)")
async def help_cmd(ctx: discord.ApplicationContext):
    await ctx.respond(embeds=[guide_embed(), song_guide_embed()], view=HelpPanel(), ephemeral=True)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not REGISTER_CHANNEL_ID or message.channel.id != REGISTER_CHANNEL_ID:
        return
    audio = [a for a in message.attachments if is_audio(a)]
    if not audio:
        return
    total = None
    for att in audio:
        try:
            _, total = await register_audio(message.author.id, att)
        except ValueError as e:
            await message.reply(f"⚠️ {e}", mention_author=False, delete_after=15)
    if total is not None:
        await message.add_reaction("✅")
        await message.reply(progress_text(total), mention_author=False, delete_after=10)


if __name__ == "__main__":
    bot.run(TOKEN)