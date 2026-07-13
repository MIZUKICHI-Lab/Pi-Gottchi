#!/usr/bin/env python3
"""「もこ」—— Whisplay たまごっち風キャラクター.

- LCD にまるいキャラを常時アニメ表示（まばたき・ぷにぷに・気分で表情が変化）
- 揺らすと反応（加速度センサー自動検出。未配線でも他機能は動く）
- Gemini Liveのハンズフリー会話。切断中はボタン押下録音のREST経路へ切替
- 音に反応、夜や放置で眠る、なつき度はハートで表示して保存

実行: cd ~/whisplay-chara && python3 chara.py
"""
import json
import math
import os
import random
import signal
import subprocess
import sys
import threading
import time
import wave

import numpy as np
import requests
from PIL import ImageDraw, ImageFont

sys.path.insert(0, "/home/mizukichi/Whisplay/runtime")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import face  # noqa: E402
import voice  # noqa: E402
from imu import ShakeMonitor  # noqa: E402
from intents import detect_control_intent  # noqa: E402
from memory import MokoMemory  # noqa: E402
from servo import ServoAnimator  # noqa: E402
from whisplay import WhisplayBoard  # noqa: E402

try:  # Live会話（即答モード）。websockets が無ければ REST にフォールバック
    import live
except ImportError:
    live = None

# ---------- 定数 ----------
ALSA_DEV = "plughw:CARD=whisplaysound"
STATE_PATH = os.path.join(HERE, "state.json")
VOICES_DIR = os.path.join(HERE, "voices")
REC_WAV = "/tmp/moko_in.wav"
TTS_WAV = "/tmp/moko_out.wav"
MIC_WAV = "/tmp/moko_mic.wav"
FONT_PATHS = ("/usr/share/fonts/truetype/vlgothic/VL-PGothic-Regular.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

FRAME_SEC = 0.2              # 描画周期（Pi Zero で無理のない速度）
AWAKE_BACKLIGHT = 100        # 起床中のLCDバックライト（0〜100）
SLEEP_BACKLIGHT = 12         # PWM対応機は減光。ON/OFF機では点灯した睡眠表示になる
HOLD_SEC = 0.35              # これ以上の長押しで「話しかける」
AUTO_SLEEP_SEC = 20          # 会話・操作がないと自動で眠るまでの秒数
NIGHT_AUTO_SLEEP_SEC = 10    # 夜間の自動入眠までの秒数
STARTUP_AWAKE_SEC = 0        # 必要な場合だけ自動睡眠を遅らせる起動猶予
NIGHT = (22, 7)              # 夜時間帯 [開始, 終了)
AMBIENT_SEC = 6.0            # 環境音チェックの間隔
AMBIENT_RMS = 0.09           # 反応する音量
WAKE_CHECK_SEC = 3.0         # 睡眠中の聞き耳の間隔
WAKE_RMS = 0.06              # 睡眠中に目を覚ます音量
MOOD_FULL_SEC = 5400         # 満タン→ゼロまでの気分減衰
MAX_HISTORY = 8              # 会話履歴の保持ターン数

LED = {
    "idle": (60, 24, 32), "happy": (255, 80, 120), "excited": (255, 200, 0),
    "surprised": (255, 200, 0), "dizzy": (255, 40, 40), "listening": (0, 60, 255),
    "thinking": (255, 140, 0), "talking": (0, 220, 120), "sleeping": (4, 4, 24),
    "sad": (120, 40, 60), "hungry": (255, 70, 0), "eating": (255, 150, 40),
}

CHIMES = {
    "happy": "synth 0.22 sine 700-1400 vol 0.5",
    "wow": "synth 0.15 sine 500-1000 vol 0.5",
    "dizzy": "synth 0.5 sine 800-300 vol 0.5",
    "sad": "synth 0.35 sine 500-260 vol 0.45",
    "hi": "synth 0.12 sine 1000-1200 vol 0.45",
}

# ボイスクリップ未生成（オフライン）時の代替電子音
FALLBACK_CHIME = {
    "petted": "happy", "greet": "happy", "shake_light": "wow", "wake": "hi",
    "hi": "hi", "shake_hard": "dizzy", "no_hear": "sad", "no_net": "sad",
    "sleepy": "sad", "hungry": "sad", "batt_low": "sad", "eat": "happy",
}

CLICK_GAP = 0.4              # 連続クリックのまとめ判定時間
SHUTDOWN_HOLD = 10.0         # この秒数の長押しで安全シャットダウン
BATT_LOW = 20                # おなかすいた表示のしきい値[%]
BATT_CRITICAL = 5            # 保護シャットダウンのしきい値[%]
BATT_CHECK_SEC = 5           # 残量・充電器の確認間隔（挿したらすぐもぐもぐ）
HUNGRY_NAG_SEC = 300         # 「おなかすいた」を言う間隔


# ---------- 小物 ----------
def rgb565_bytes(img):
    arr = np.array(img, dtype=np.uint16)
    r = (arr[:, :, 0] >> 3) & 0x1F
    g = (arr[:, :, 1] >> 2) & 0x3F
    b = (arr[:, :, 2] >> 3) & 0x1F
    return ((r << 11) | (g << 5) | b).astype(">u2").tobytes()


def load_font(size):
    for p in FONT_PATHS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def wrap_jp(draw, text, font, max_w):
    lines, cur = [], ""
    for ch in text.replace("\n", ""):
        if draw.textlength(cur + ch, font=font) > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def draw_bubble(img, text, font):
    """画面上部に吹き出しでセリフを表示。"""
    draw = ImageDraw.Draw(img)
    lines = wrap_jp(draw, text, font, 196)[:4]
    h = 16 + 19 * len(lines)
    draw.rounded_rectangle([10, 6, 230, 6 + h], radius=13,
                           fill=(255, 255, 255), outline=(226, 200, 196), width=2)
    draw.polygon([(112, 6 + h), (132, 6 + h), (120, 16 + h)], fill=(255, 255, 255))
    for i, line in enumerate(lines):
        draw.text((20, 14 + i * 19), line, font=font, fill=(88, 70, 64))


def ensure_chimes():
    for name, synth in CHIMES.items():
        path = f"/tmp/moko_{name}.wav"
        if not os.path.exists(path):
            subprocess.run(["sox", "-n", path] + synth.split(), check=False)


def is_night():
    hour = time.localtime().tm_hour
    return hour >= NIGHT[0] or hour < NIGHT[1]


def env_int(env, name, default, low=0, high=100):
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError):
        value = default
        print(f"[config] {name} は整数でないため既定値 {default} を使います")
    return max(low, min(high, value))


try:
    from smbus2 import SMBus
except ImportError:
    SMBus = None


def battery_status():
    """PiSugar 3 の (残量[%], 充電器接続) を返す（読めなければ (None, False)）。"""
    if SMBus is None:
        return None, False
    try:
        with SMBus(1) as bus:
            pct = bus.read_byte_data(0x57, 0x2A)
            ctr1 = bus.read_byte_data(0x57, 0x02)   # bit7 = 外部電源あり
        return (pct if 0 <= pct <= 100 else None), bool(ctr1 & 0x80)
    except OSError:
        return None, False


def mic_rms():
    """0.3秒録音して RMS を返す（マイク使用不可なら None）。"""
    try:
        subprocess.run(
            ["arecord", "-q", "-D", ALSA_DEV, "-f", "S16_LE", "-r", "8000",
             "-c", "1", "--samples", "2400", MIC_WAV],
            check=True, timeout=2, stderr=subprocess.DEVNULL)
        with wave.open(MIC_WAV) as w:
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        return float(np.sqrt(np.mean((data / 32768.0) ** 2))) if data.size else 0.0
    except Exception:
        return None


# ---------- 永続状態 ----------
def stop_splash():
    """起動スプラッシュ(たまご)を止めて画面を引き継ぐ（GPIO解放を待つ）。"""
    subprocess.run(["systemctl", "stop", "moko-splash.service"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        if subprocess.run(["pgrep", "-f", "splash.py"],
                          stdout=subprocess.DEVNULL).returncode != 0:
            return
        time.sleep(0.15)


def load_state():
    now = time.time()
    default = {"mood": 80.0, "bond_xp": 0, "born": now, "last": now}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return {**default, **json.load(f)}
    except (OSError, ValueError):
        return default


def save_state(st):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except OSError:
        pass


# ---------- 会話ワーカー ----------
class Convo:
    """録音済み wav を STT → Claude → TTS まで裏スレッドで進める。"""

    def __init__(self, env, memory=None):
        self.env = env
        self.memory = memory
        self.model = env.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
        self.history = []
        self.lock = threading.Lock()
        self.stage = None            # None/stt/think/speak/err_no_hear/err_net
        self.reply = ""
        self.user_text = ""

    def busy(self):
        with self.lock:
            return self.stage in ("stt", "think")

    def start(self, wav):
        with self.lock:
            self.stage = "stt"
            self.reply = ""
            self.user_text = ""
        threading.Thread(target=self._run, args=(wav,), daemon=True).start()

    def _set(self, stage, reply="", user_text=None):
        with self.lock:
            self.stage, self.reply = stage, reply
            if user_text is not None:
                self.user_text = user_text

    def take_result(self):
        """完了時に (stage, reply, user_text) を返して待機状態へ戻す。"""
        with self.lock:
            if self.stage in ("speak", "err_no_hear", "err_net"):
                out = (self.stage, self.reply, self.user_text)
                self.stage = None
                self.reply = ""
                self.user_text = ""
                return out
        return None

    def _run(self, wav):
        user_text = ""
        try:
            t0 = time.time()
            trimmed = self.history[-MAX_HISTORY * 2:]
            extra = self.memory.prompt_text() if self.memory else ""
            if self.env["_provider"] == "gemini":
                # 聞き取り+返事を1コールで（無料枠にやさしい）
                user_text, reply = voice.gemini_converse(wav, trimmed, self.env, extra)
            else:
                user_text = voice.transcribe(wav, self.env["OPENAI_API_KEY"])
                reply = ""
                if user_text:
                    self._set("think")
                    reply = voice.ask_claude(
                        trimmed + [{"role": "user", "content": user_text}],
                        self.env["ANTHROPIC_API_KEY"], self.model, extra)
            if not user_text or not reply:
                print(f"[convo] 聞き取れず (user={user_text!r} reply={reply!r})")
                self._set("err_no_hear", user_text=user_text)
                return
            print(f"[you] {user_text}")
            print(f"[moko] {reply}")
            self._set("think")
            self.history += [{"role": "user", "content": user_text},
                             {"role": "assistant", "content": reply}]
            if self.memory:
                self.memory.add_turn(user_text, reply)
            try:
                os.remove(TTS_WAV)
            except OSError:
                pass
            t1 = time.time()
            try:
                voice.synthesize(reply, self.env, TTS_WAV)
            except (requests.RequestException, ValueError, KeyError) as exc:
                print(f"[!] TTSエラー（字幕のみで返答）: {exc}")
            print(f"[convo] 生成{t1 - t0:.1f}s + TTS{time.time() - t1:.1f}s")
            self._set("speak", reply, user_text=user_text)
        except (requests.RequestException, ValueError, KeyError,
                IndexError, TypeError, OSError) as exc:
            print(f"[!] 会話エラー: {type(exc).__name__}: {exc}")
            self._set("err_net", user_text=user_text)


# ---------- 本体 ----------
class Moko:
    def __init__(self):
        self._boot_monotonic = time.monotonic()
        self.env = voice.load_env(os.path.join(HERE, ".env"),
                                  "/home/mizukichi/whisplay-assistant/.env")
        self.online = self.env["_voice_ok"]
        self.provider = self.env["_provider"]
        if self.online:
            print(f"[*] 会話プロバイダ: {self.provider}")
        else:
            print("[!] APIキー未設定のためオフラインモード（会話・ボイスなし）")
            print(f"    {os.path.join(HERE, '.env')} に GEMINI_API_KEY（無料枠可）"
                  "を記入すると全機能が使えます")
        stop_splash()
        self.awake_backlight = env_int(
            self.env, "AWAKE_BACKLIGHT", AWAKE_BACKLIGHT)
        self.sleep_backlight = env_int(
            self.env, "SLEEP_BACKLIGHT", SLEEP_BACKLIGHT)
        self.auto_sleep_sec = env_int(
            self.env, "AUTO_SLEEP_SEC", AUTO_SLEEP_SEC, 5, 86400)
        self.night_auto_sleep_sec = env_int(
            self.env, "NIGHT_AUTO_SLEEP_SEC", NIGHT_AUTO_SLEEP_SEC, 5, 86400)
        self.startup_awake_sec = env_int(
            self.env, "STARTUP_AWAKE_SEC", STARTUP_AWAKE_SEC, 0, 1800)
        self.board = WhisplayBoard()
        self.board.set_backlight(self.awake_backlight)
        self.font = load_font(16)
        self.state = load_state()
        self.clips = voice.ClipBank(VOICES_DIR, ALSA_DEV)
        self.memory = MokoMemory(os.path.join(HERE, "memory.json"))
        self.convo = Convo(self.env, self.memory)
        self.shaker = ShakeMonitor()
        self.motion = ServoAnimator.from_env(self.env)
        self.chat = None             # 連続会話（ChatGPT音声モード風）
        self.was_sleeping = False
        self.was_deep_sleeping = False
        self.was_asleep = False      # 記憶整理トリガー用（寝入りの瞬間を検出）
        self._control_lock = threading.RLock()
        self.manual_sleep = False    # 音声コマンドで入る、明示的な睡眠ラッチ
        self.pending_sleep = False   # 返答再生後にmanual_sleepへ移す
        self._last_live_phase = "idle"
        self._rest_fallback = False  # ボタン録音中はLive再接続を止める
        self._rest_release_pending = False
        self._wake_generation = 0
        self._rest_request_generation = None
        self._stop_requested = False
        if live is not None and self.online and self.provider == "gemini":
            self.env["ALSA_DEV"] = ALSA_DEV
            self.chat = live.LiveChat(self.env, voice.SYSTEM_PROMPT, self.memory)
            self.chat.start()

        self.press_t = None          # ボタン押下時刻（押下中のみ）
        self.press_mode = None       # 押下開始時の経路をlive/restで固定する
        self.actions = []            # ボタン由来のアクション
        self.clicks = 0              # 短押しの連続回数
        self.last_click = 0.0
        self.rec_proc = None
        self.audio_proc = None
        self.react_expr = None
        self.react_until = 0.0
        self.hearts_start = None
        self.bubble = None           # (text, until)
        self.last_activity = time.monotonic()
        self.last_ambient = 0.0
        self.next_blink = time.time() + 3
        self.blink_now = False
        self.frame = 0
        self.batt = None             # バッテリー残量[%]
        self.plugged = False         # 充電器が挿さっているか
        self.last_batt_check = 0.0
        self.last_hungry = 0.0
        self._shutting = False

        self.board.on_button_press(self._on_press)
        self.board.on_button_release(self._on_release)

    # ---- ボタン ----
    def _on_press(self):
        """連続会話モードでは会話はハンズフリー。ボタンはなでる/起こす/電源用。"""
        self._wake("button")
        self.press_t = time.time()
        self.press_mode = "live"
        if self.chat is None or not self.chat.connected:
            # RESTモード（Live切断中の自動フォールバック含む）: 押下中録音→話す
            self.press_mode = "rest"         # 解放時に再判定せず、この経路を維持
            with self._control_lock:
                self._rest_fallback = True
                self._rest_release_pending = False
                if self.chat:
                    self.chat.suspend()       # 録音・REST処理中のALSA競合を防ぐ
            self._start_recording()
            if self.online:
                threading.Thread(target=voice.prewarm, args=(self.env,),
                                 daemon=True).start()

    def _on_release(self):
        if self.press_t is None:
            return
        dur = time.time() - self.press_t
        self.press_t = None
        mode, self.press_mode = self.press_mode, None
        if dur >= HOLD_SEC:
            if mode == "rest":
                self.actions.append("talk")  # 押下中にLiveが再接続しても録音を捨てない
            else:
                self.clicks += 1             # 連続会話モード中の長押し=なでる扱い
                self.last_click = time.time()
        else:                                # 短押しは回数をまとめて判定
            self.clicks += 1
            self.last_click = time.time()
            if mode == "rest":
                with self._control_lock:
                    self._rest_release_pending = True

    # ---- 録音 ----
    def _start_recording(self):
        self._stop_audio()
        try:
            os.remove(REC_WAV)               # 古い録音を確実に消す
        except OSError:
            pass
        self.rec_proc = subprocess.Popen(
            ["arecord", "-q", "-D", ALSA_DEV, "-f", "S16_LE", "-r", "16000",
             "-c", "1", REC_WAV], stderr=subprocess.DEVNULL)

    def _stop_recording(self):
        """arecord を止める。ALSA が固まっても本体を巻き込まない。"""
        if self.rec_proc:
            self.rec_proc.terminate()
            try:
                self.rec_proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.rec_proc.kill()
                try:
                    self.rec_proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            self.rec_proc = None

    # ---- 音声再生 ----
    def _stop_audio(self):
        proc, self.audio_proc = self.audio_proc, None
        if not proc or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass

    def _play_clip(self, prefix):
        if self.audio_proc and self.audio_proc.poll() is None:
            return
        self.audio_proc = self.clips.play(prefix, random.randrange(8), self.provider)
        if self.audio_proc is None and prefix in FALLBACK_CHIME:
            self._play_chime(FALLBACK_CHIME[prefix])   # ボイス未生成でも電子音で鳴く

    def _play_chime(self, name):
        self.audio_proc = subprocess.Popen(
            ["aplay", "-q", "-D", ALSA_DEV, f"/tmp/moko_{name}.wav"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _speaking(self):
        return self.audio_proc is not None and self.audio_proc.poll() is None

    # ---- 反応 ----
    def _wake(self, reason):
        """すべての起床経路で明示睡眠ラッチと活動時刻を同時に戻す。"""
        with self._control_lock:
            was_sleeping = self.manual_sleep
            self.manual_sleep = False
            self.pending_sleep = False
            self._wake_generation += 1
            chat = getattr(self, "chat", None)
            if chat:
                # 完了済みイベントも同じ起床操作で破棄し、後から寝直さない。
                chat.cancel_pending_controls()
            self.last_activity = time.monotonic()
            generation = self._wake_generation
        if was_sleeping:
            print(f"[*] 起床要求: {reason}")
        return generation

    def _request_sleep(self, reason):
        """誤認識でも戻せるアプリ内スリープだけを要求する。"""
        with self._control_lock:
            was_sleeping = self.manual_sleep
            self.pending_sleep = False
            self.manual_sleep = True
        if not was_sleeping:
            print(f"[*] 音声コマンドでおやすみ ({reason})")

    def _react(self, expr, secs, clip=None, mood=0.0, xp=0):
        now = time.time()
        self._wake(f"reaction:{expr}")
        self.react_expr, self.react_until = expr, now + secs
        self.state["mood"] = max(0.0, min(100.0, self.state["mood"] + mood))
        self.state["bond_xp"] += xp
        if expr == "happy":
            self.hearts_start = now
        self.motion.react("wake" if clip == "wake" else expr)
        if clip:
            self._play_clip(clip)

    def _handle_shake(self):
        ev = self.shaker.pop_event()
        if ev is None:
            return
        sleeping = self._is_sleeping()
        if ev == "hard":
            self._react("dizzy", 3.5, "shake_hard", mood=-6, xp=1)
        elif sleeping:
            self._react("surprised", 2.0, "wake", mood=2, xp=1)
        else:
            self._react("surprised", 2.0, "shake_light", mood=5, xp=1)

    def _handle_clicks(self):
        """短押しの回数で分岐（1=なでる / 2=揺れ軽 / 3+=揺れ強のシミュレート）。"""
        if not self.clicks or self.press_t is not None:
            return
        if time.time() - self.last_click < CLICK_GAP:
            return
        n, self.clicks = self.clicks, 0
        self._stop_recording()
        if n == 1:
            print("[btn] なでなで")
            self._react("happy", 1.8, "petted", mood=15, xp=1)
        elif n == 2:
            print("[btn] デバッグ: 軽い揺れをシミュレート")
            self.shaker.events.append("light")
        else:
            print("[btn] デバッグ: 激しい揺れをシミュレート")
            self.shaker.events.append("hard")

    def _handle_actions(self):
        while self.actions:
            act = self.actions.pop(0)
            request_generation = self._wake("button-talk")
            self._stop_recording()
            if act == "talk":
                size = os.path.getsize(REC_WAV) if os.path.exists(REC_WAV) else 0
                ok = size > 12000
                print(f"[btn] おはなし (録音={size}B ok={ok} busy={self.convo.busy()})")
                if not self.online:
                    self._react("sad", 2.5, "no_net")
                    with self._control_lock:
                        self._rest_release_pending = True
                elif ok and not self.convo.busy():
                    with self._control_lock:
                        self._rest_request_generation = request_generation
                    self.convo.start(REC_WAV)
                elif not ok:                 # 録音が短い/マイク競合
                    self._react("sad", 2.5, "no_hear")
                    with self._control_lock:
                        self._rest_release_pending = True
                else:
                    with self._control_lock:
                        self._rest_release_pending = True

    def _handle_live(self):
        """連続会話の表示・活動・確定発話イベントを本体へ反映する。"""
        if self.chat is None:
            return
        now = time.time()
        if self.chat.phase != self._last_live_phase:
            if self.chat.phase == "play":
                self.motion.react("talking")
            self._last_live_phase = self.chat.phase
        # 反応音声とサーボ機械音はGeminiへ戻さない。
        self.chat.external_mute(self._speaking() or self.motion.moving.is_set())
        if self.chat.phase == "play" and self.chat.out_text:
            self.bubble = (self.chat.out_text, now + 2.0)
            self.state["mood"] = min(100.0, self.state["mood"] + 0.05)
        self.last_activity = max(self.last_activity, self.chat.last_voice)
        while True:
            with self._control_lock:
                event_generation = self._wake_generation
            utterance = self.chat.pop_completed_turn()
            if utterance is None:
                break
            if detect_control_intent(utterance) == "sleep":
                with self._control_lock:
                    # popから反映までに物理操作があれば、古い音声指示を捨てる。
                    if event_generation == self._wake_generation:
                        self.pending_sleep = True
            self.state["bond_xp"] += 2
            self.state["mood"] = min(100.0, max(30.0, self.state["mood"] + 8))

    def _handle_convo_result(self):
        res = self.convo.take_result()
        if res is None:
            return
        stage, reply, user_text = res
        with self._control_lock:
            request_generation = self._rest_request_generation
            self._rest_request_generation = None
        if stage == "speak":
            self._stop_audio()
            if os.path.exists(TTS_WAV):      # TTS失敗時は字幕のみ
                self.audio_proc = subprocess.Popen(
                    ["aplay", "-q", "-D", ALSA_DEV, TTS_WAV],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.bubble = (reply, time.time() + max(5.0, len(reply) * 0.28))
            self.state["bond_xp"] += 2
            self.state["mood"] = min(100.0, max(30.0, self.state["mood"] + 8))
            self.motion.react("talking")
        elif stage == "err_no_hear":
            self._react("sad", 2.5, "no_hear")
        elif stage == "err_net":
            self._react("sad", 3.0, "no_net")
        self.last_activity = time.monotonic()
        if stage == "speak" and detect_control_intent(user_text) == "sleep":
            with self._control_lock:
                # 再生準備中のボタン起床も尊重し、古い結果で寝直さない。
                if request_generation == self._wake_generation:
                    self.pending_sleep = True
        with self._control_lock:
            if self._rest_fallback:
                self._rest_release_pending = True

    def _handle_pending_sleep(self):
        with self._control_lock:
            if self.pending_sleep and not self._speaking():
                self._request_sleep("おやすみ/さよなら")

    def _handle_rest_fallback(self):
        """REST返答・エラー音声が終わってからLiveを再開する。"""
        with self._control_lock:
            if not self._rest_fallback or not self._rest_release_pending:
                return
            if self.rec_proc or self.convo.busy() or self._speaking():
                return
            self._rest_release_pending = False
            self._rest_fallback = False
            if self.chat and not self.manual_sleep:
                # 新しいGPIO押下は同じlockの後でsuspendするため、必ず後勝ちになる。
                self.chat.resume()

    def _handle_sleep_state(self):
        """自動睡眠は会話待機を保ち、明示睡眠だけLiveを停止する。"""
        sleeping = self._is_sleeping()
        if self.chat is not None and self.chat.phase != "idle":
            sleeping = False                  # 会話ターンの途中では寝ない
        with self._control_lock:
            deep_sleeping = sleeping and self.manual_sleep
            entering_deep_sleep = deep_sleeping and not self.was_deep_sleeping
            if entering_deep_sleep and self.chat:
                # 物理wakeはこのlockの後で実行されるため、起床後の再suspendを防ぐ。
                self.chat.suspend()
        if sleeping and not self.was_sleeping:
            if deep_sleeping:
                print("[*] おやすみ（明示睡眠: マイク停止）")
            else:
                print("[*] うとうと（自動睡眠: 会話待機は継続）")
            self.board.set_backlight(self.sleep_backlight)
            self.motion.react("sleeping")
        elif sleeping and deep_sleeping and not self.was_deep_sleeping:
            # 自動睡眠中に「おやすみ」が確定した場合も、深い睡眠へ移す。
            print("[*] おやすみ（明示睡眠: マイク停止）")
            self.motion.react("sleeping")
        elif not sleeping and self.was_sleeping:
            print("[*] おはよう（画面・会話を再開）")
            self.board.set_backlight(self.awake_backlight)
            self.motion.react("wake")
            if self.chat and not self._rest_fallback:
                self.chat.resume()
        self.was_sleeping = sleeping
        self.was_deep_sleeping = deep_sleeping

    # ---- 環境音 ----
    def _ambient_check(self):
        now = time.time()
        busy = (self.press_t is not None or self.rec_proc or self.convo.busy()
                or self._speaking() or now < self.react_until
                or self.motion.moving.is_set()
                or (self.chat is not None and self.chat.active()))
        sleeping = self._is_sleeping()
        interval = WAKE_CHECK_SEC if sleeping else AMBIENT_SEC
        if busy or now - self.last_ambient < interval:
            return
        self.last_ambient = now
        lvl = mic_rms()
        if lvl is None:
            return
        if sleeping and lvl > WAKE_RMS:      # 大きめの呼びかけで目を覚ます
            self._react("surprised", 2.0, "wake", mood=2)
        elif not sleeping and lvl > AMBIENT_RMS:
            self._react("excited", 1.8, "hi", mood=4)

    # ---- バッテリー / シャットダウン ----
    def _check_battery(self):
        now = time.time()
        if now - self.last_batt_check < BATT_CHECK_SEC:
            return
        self.last_batt_check = now
        pct, plugged = battery_status()
        if plugged and not self.plugged:         # 充電器が挿さった → ごはん!
            print(f"[batt] 充電開始 もぐもぐ（残量{pct}%）")
            self._react("eating", 3.5, "eat", mood=8, xp=1)
        elif self.plugged and not plugged:
            print(f"[batt] 充電おわり（残量{pct}%）")
        self.plugged = plugged
        if pct is None:
            return
        self.batt = pct
        if plugged:                              # ごはん中は機嫌が少しずつ回復
            self.state["mood"] = min(100.0, self.state["mood"] + 0.4)
            return                               # 空腹表示・保護シャットダウンなし
        if pct <= BATT_CRITICAL:
            self._safe_shutdown("batt_low", f"電池残量{pct}%のため保護シャットダウン")
        elif pct <= BATT_LOW and now - self.last_hungry > HUNGRY_NAG_SEC:
            self.last_hungry = now
            print(f"[batt] 残量{pct}% おなかすいたよ〜")
            self._react("hungry", 4.0, "hungry")

    def _safe_shutdown(self, clip, reason):
        """おやすみの声と顔を見せてから安全に電源を落とす。"""
        if self._shutting:
            return
        self._shutting = True
        print(f"[*] シャットダウン: {reason}")
        self._stop_recording()
        if self.chat:
            self.chat.close()
        self._stop_audio()
        self.motion.react("sleeping")
        proc = self.clips.play(clip, 0, self.provider)
        for _ in range(8):
            self._draw("sleeping")
            time.sleep(0.3)
        if proc and proc.poll() is None:
            proc.wait()
        self.motion.close(park_center=False)
        save_state(self.state)
        subprocess.run(["shutdown", "-h", "now"], check=False)

    # ---- 状態 ----
    def _is_sleeping(self):
        with self._control_lock:
            if self.manual_sleep:
                return True
        if (self._rest_fallback or self.press_t is not None or self.rec_proc
                or self.convo.busy() or self._speaking()
                or (self.chat is not None and self.chat.phase != "idle")):
            return False
        now = time.monotonic()
        if now - self._boot_monotonic < self.startup_awake_sec:
            return False
        idle = now - self.last_activity
        return (idle > self.auto_sleep_sec
                or (is_night() and idle > self.night_auto_sleep_sec))

    def _decay_mood(self):
        st = self.state
        dt = time.time() - st.get("last", time.time())
        st["last"] = time.time()
        if 0 < dt < 3600:
            st["mood"] = max(0.0, st["mood"] - dt * (100.0 / MOOD_FULL_SEC))

    def _current_expr(self):
        now = time.time()
        if self.chat is not None and self.chat.active():
            if self.chat.phase == "play":
                return "talking"
            if self.chat.phase == "listen":
                return "listening"
            if self.chat.phase == "think":
                return "thinking"
        if self.press_t is not None or self.rec_proc:
            return "listening"
        if self.convo.busy():
            return "thinking"
        if self.bubble and now < self.bubble[1]:
            return "talking"
        if self.react_expr and now < self.react_until:
            return self.react_expr
        if self._is_sleeping():
            return "sleeping"
        if self.plugged:
            return "eating"          # 充電中はもぐもぐ（会話・睡眠が優先）
        if self.batt is not None and self.batt <= BATT_LOW:
            return "hungry"
        if self.state["mood"] < 25:
            return "sad"
        return "idle"

    def _update_blink(self):
        now = time.time()
        self.blink_now = False
        if now >= self.next_blink:
            self.blink_now = True
            gap = 0.4 if random.random() < 0.2 else random.uniform(2.5, 5.0)
            self.next_blink = now + gap

    # ---- 描画 ----
    def _draw(self, expr):
        extras = {"blink": self.blink_now}
        # ハート残量ゲージ: 充電中はずっと / 低残量の警告時
        if self.batt is not None and (self.plugged or self.batt <= 25):
            extras["battery"] = self.batt
            extras["charging"] = self.plugged
        # 会話待機中は耳マーク=「聞いてるよ」サイン
        if (self.chat is not None and self.chat.active()
                and self.chat.phase == "idle"
                and expr in ("idle", "happy", "hungry", "eating")):
            extras["mic_ready"] = True
        if expr == "talking":
            live_play = self.chat is not None and self.chat.phase == "play"
            extras["mouth_open"] = (self._speaking() or live_play) and self.frame % 2 == 0
        if self.hearts_start is not None:
            t = (time.time() - self.hearts_start) / 1.6
            if t <= 1.0:
                extras["hearts_t"] = t
            else:
                self.hearts_start = None
        img = face.render(expr, self.frame, extras)
        if self.bubble and time.time() < self.bubble[1] and expr == "talking":
            draw_bubble(img, self.bubble[0], self.font)
        self.board.draw_image(0, 0, face.W, face.H, rgb565_bytes(img))
        self.board.set_rgb(*LED.get(expr, LED["idle"]))

    # ---- 起動時のボイス準備 ----
    def _prepare_clips(self):
        if not self.online:
            return
        miss = self.clips.missing(self.provider)
        if not miss:
            return
        print(f"[*] 反応ボイスを {len(miss)} 件生成します（初回のみ・無料枠だと数分）")
        try:
            def progress(i, n, name):
                print(f"    {i}/{n} {name}")
                self._draw("thinking")
            self.clips.generate_missing(self.env, progress)
        except (requests.RequestException, ValueError, KeyError) as exc:
            print(f"[!] ボイス生成を中断（次回起動時に続きから再開）: {exc}")

    # ---- メインループ ----
    def _handle_stop_signal(self, signum, _frame):
        print(f"[*] 終了シグナル {signum} を受信、安全停止します")
        self._stop_requested = True

    def run(self):
        signal.signal(signal.SIGTERM, self._handle_stop_signal)
        ensure_chimes()
        self.shaker.start()
        self._prepare_clips()
        print("[*] もこ 起動! 短押し=なでる / 長押し=おはなし / 揺らすと反応")
        self._react("happy", 2.5, "greet", xp=0)
        was_recording = False
        try:
            while not self._stop_requested:
                self._decay_mood()
                self._handle_shake()
                self._handle_clicks()
                self._handle_actions()
                self._handle_live()
                self._handle_convo_result()
                self._handle_pending_sleep()
                self._handle_rest_fallback()

                # 長押し中: 録音監視と10秒シャットダウン
                pressing = self.press_t is not None
                if pressing:
                    held = time.time() - self.press_t
                    if held >= SHUTDOWN_HOLD:    # 10秒長押し=電源オフ
                        self._safe_shutdown("sleepy", "ボタン10秒長押し")
                    if self.rec_proc and self.rec_proc.poll() is not None:
                        self._start_recording()
                else:
                    if not was_recording:
                        self._ambient_check()
                was_recording = self.rec_proc is not None
                self._check_battery()
                self._handle_sleep_state()

                sleeping = self._is_sleeping()
                if sleeping and not self.was_asleep:
                    self.memory.consolidate_async(self.env)  # 寝ている間に記憶を整理
                self.was_asleep = sleeping

                self._update_blink()
                expr = self._current_expr()
                self._draw(expr)
                if self.frame % 80 == 0:
                    save_state(self.state)
                if self.frame % 300 == 0:   # フリーズ切り分け用ハートビート
                    imu = self.shaker.name if self.shaker.present else "未接続"
                    print(f"[tick] frame={self.frame} expr={expr} "
                          f"mood={self.state['mood']:.0f} imu={imu}")
                self.frame += 1
                # 相手の発話ストリーミング中は描画を減速してCPUを通信に譲る
                live_wait = self.chat is not None and self.chat.phase == "listen"
                time.sleep(0.6 if (self.convo.busy() or live_wait) else FRAME_SEC)
        except KeyboardInterrupt:
            print("\n[*] おやすみ、もこ…")
        finally:
            save_state(self.state)
            self.shaker.stop()
            if self.chat:
                self.chat.close()
            self._stop_recording()
            self._stop_audio()
            self.motion.close()
            self.board.set_backlight(0)
            self.board.set_rgb(0, 0, 0)
            self.board.cleanup()


if __name__ == "__main__":
    Moko().run()
