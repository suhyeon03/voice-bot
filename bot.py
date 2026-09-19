"""
목소리 수집 봇
- /동의        : 목소리 수집·복제 동의 (버튼 확인)
- /대본        : 녹음할 때 읽을 문장 받기
- /녹음시작     : 봇이 내 음성 채널에 들어와 녹음 시작 (실험 기능)
- /녹음종료     : 녹음을 끝내고 사람별로 저장
- /목소리등록   : 음성 파일 업로드로 샘플 등록
- /내목소리     : 수집 현황 확인
- /삭제        : 내 데이터 + 동의 전부 삭제
- 등록 채널(REGISTER_CHANNEL_ID)에 음성 메시지를 보내면 자동 등록

⚠️ 음성 채널 녹음은 py-cord 개발 브랜치(fix/voice-rec-2)에 의존하는 실험 기능입니다.
"""
import asyncio
import ctypes.util
import json
import logging
import os
import random
import shutil
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import discord
from dotenv import load_dotenv

load_dotenv()
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
            for d in (user_dir(uid), DATA_DIR / "datasets" / str(uid), DATA_DIR / "models" / str(uid)):
                shutil.rmtree(d, ignore_errors=True)

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


async def register_audio(user: discord.abc.User, att: discord.Attachment) -> tuple[float, float]:
    if not store.has_consent(user.id):
        raise ValueError("먼저 `/동의`를 진행해 주세요.")
    if not is_audio(att):
        raise ValueError("음성 파일만 등록할 수 있어요 (ogg, mp3, wav, m4a 등).")
    if att.size > MAX_BYTES:
        raise ValueError("파일이 너무 커요 (최대 25MB).")
    clip_id = uuid.uuid4().hex[:12]
    ext = Path(att.filename).suffix.lower() or ".ogg"
    raw_path = user_dir(user.id) / "raw" / f"{clip_id}{ext}"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(await att.read())
    return await ingest_raw(user.id, raw_path, clip_id, f"upload:{att.filename}")


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
    vc: discord.VoiceClient
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
bot = discord.Bot(intents=intents, debug_guilds=GUILD_IDS)


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
async def on_ready():
    opus_ok = ensure_opus()
    log.info("로그인: %s (등록 채널: %s, opus: %s, ffmpeg: %s)", bot.user, REGISTER_CHANNEL_ID or "미설정",
             "OK" if opus_ok else "없음 → brew install opus",
             "OK" if has_ffmpeg() else "없음 → brew install ffmpeg")
    print(f"로그인: {bot.user} (등록 채널: {REGISTER_CHANNEL_ID or '미설정'})")


@bot.slash_command(name="동의", description="목소리 수집·복제에 동의합니다")
async def consent_cmd(ctx: discord.ApplicationContext):
    if store.has_consent(ctx.author.id):
        return await ctx.respond("이미 동의하셨어요. `/대본`을 보고 녹음을 시작해 보세요!", ephemeral=True)

    async def grant(user):
        await store.grant(user)
        return "동의 완료! `/대본`으로 읽을 문장을 받고, 음성 채널에서 `/녹음시작`을 해보세요."

    view = ConfirmView(ctx.author.id, grant, "동의합니다", discord.ButtonStyle.success)
    await ctx.respond(CONSENT_TEXT, view=view, ephemeral=True)


@bot.slash_command(name="대본", description="녹음할 때 읽을 문장을 받아요")
async def script_cmd(ctx: discord.ApplicationContext):
    lines = random.sample(SCRIPTS, 5)
    body = "\n".join(f"{i}. {s}" for i, s in enumerate(lines, 1))
    tip = (
        "\n\n**녹음 팁**: 조용한 곳에서, 평소 말투로, 문장 사이 1초 쉬기. "
        "노래 한 소절을 흥얼거린 녹음도 섞어 주면 노래 품질이 좋아져요."
    )
    await ctx.respond(body + tip, ephemeral=True)


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


@bot.slash_command(name="목소리등록", description="음성 파일을 업로드해 샘플로 등록해요")
async def register_cmd(
    ctx: discord.ApplicationContext,
    file: discord.Option(discord.Attachment, "등록할 음성 파일", name="파일"),
):
    await ctx.defer(ephemeral=True)
    try:
        seconds, total = await register_audio(ctx.author, file)
    except ValueError as e:
        return await ctx.respond(f"⚠️ {e}", ephemeral=True)
    await ctx.respond(f"✅ {seconds:.1f}초 등록!\n{progress_text(total)}", ephemeral=True)


@bot.slash_command(name="내목소리", description="내 목소리 샘플 수집 현황")
async def status_cmd(ctx: discord.ApplicationContext):
    if not store.has_consent(ctx.author.id):
        return await ctx.respond("아직 동의하지 않았어요. `/동의`부터 시작해 주세요.", ephemeral=True)
    clips = store.meta(ctx.author.id)["clips"]
    total = sum(c["seconds"] for c in clips)
    vc_sec = sum(c["seconds"] for c in clips if c.get("source") == "voice-channel")
    await ctx.respond(
        f"클립 {len(clips)}개 (음성 채널 녹음 {vc_sec / 60:.1f}분 포함)\n{progress_text(total)}",
        ephemeral=True,
    )


@bot.slash_command(name="삭제", description="내 음성·모델·동의 기록을 전부 삭제해요")
async def delete_cmd(ctx: discord.ApplicationContext):
    async def wipe(user):
        await store.revoke_and_delete(user.id)
        return "🗑️ 음성 파일, 모델, 동의 기록을 모두 삭제했어요."

    view = ConfirmView(ctx.author.id, wipe, "전부 삭제", discord.ButtonStyle.danger)
    await ctx.respond("정말 전부 삭제할까요? 되돌릴 수 없어요.", view=view, ephemeral=True)


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
            _, total = await register_audio(message.author, att)
        except ValueError as e:
            await message.reply(f"⚠️ {e}", mention_author=False, delete_after=15)
    if total is not None:
        await message.add_reaction("✅")
        await message.reply(progress_text(total), mention_author=False, delete_after=10)


if __name__ == "__main__":
    bot.run(TOKEN)