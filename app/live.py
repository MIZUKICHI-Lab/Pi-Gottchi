"""Gemini Live API 連続会話モード（ChatGPT音声モード風）。

起きている間はマイクを常時ストリーミングし、サーバー側VADが発話を自動検出して
音声で即応答する。ボタン操作は不要。応答再生中はマイク送信を止めて
自己エコー（スピーカー音の拾い込み）を防ぐ。

suspend()/resume() で睡眠時にマイクと通信を止められる。切断時は自動再接続。
"""
import base64
import json
import queue
import re
import subprocess
import threading
import time

_NOISE_RE = re.compile(r"<[^>]*>|[\s。、.,!?！？…・~〜ー]+")


def _real_speech(text):
    """タグ・記号を除いて2文字以上残れば「本物の発話」（「うん」もOK）。"""
    return len(_NOISE_RE.sub("", text)) >= 2

from websockets.sync.client import connect as ws_connect

try:    # 声紋ライト実験（numpy必須。無くても会話機能は動く）
    import voiceprint
except Exception:
    voiceprint = None

WS_URL = ("wss://generativelanguage.googleapis.com/ws/"
          "google.ai.generativelanguage.v1beta.GenerativeService."
          "BidiGenerateContent?key={key}")
DEFAULT_MODEL = "gemini-2.5-flash-native-audio-latest"
VOICE = "Leda"
IN_RATE = 16000
DEFAULT_CHUNK_MS = 100       # 公式例に近い短い塊。Pi Zero向けに環境変数で調整可能
DEFAULT_SILENCE_MS = 700     # 自然な文中の間を切らず、応答遅延も増やしすぎない値
DEFAULT_ECHO_GUARD_MS = 350  # チャンク短縮後も筐体の残響待ちは十分に残す
TRANSCRIPT_GRACE_SEC = 0.4    # turnComplete後に独立配送の字幕を回収する猶予
TRANSCRIPT_MAX_WAIT_SEC = 1.2 # 遅着が続いても次ターンを無期限に止めない
STREAM_END_AFTER_SEC = 1.0   # 公式推奨どおり、長いマイク停止だけをflushする
UNMUTE_DELAY = 0.15         # 再生終了からマイク再開までの猶予（残響対策）
RECV_TIMEOUT = 60           # 受信待ちの区切り。無音は正常なのでpingで生存確認して継続
RECONNECT_MIN = 3.0         # 再接続の初回待ち。切断中は耳が聞こえないため短く
RECONNECT_MAX = 30.0        # 連続失敗時の上限（無料枠のレート制限を焼かない）
VP_THRESH = 0.75            # 声紋: これ未満は「知らない声」（logモードの実測で調整）
VP_KEEP_SEC = 4.0           # 声紋判定に使う直近音声（チャンク設定に依存させない）


def _env_int(env, name, default, low, high):
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError):
        value = default
        print(f"[live] {name} は整数でないため既定値 {default} を使います")
    return max(low, min(high, value))


class LiveChat:
    """常時リスニングの会話セッション。

    公開状態:
      phase: "idle"(待機) / "listen"(相手が話し中) / "think" / "play"(返答再生中)
      in_text / out_text: 現在ターンの文字起こし
      last_voice: 最後に会話が動いた時刻（睡眠判定に使う）
    """

    def __init__(self, env, persona, memory=None):
        self.env = env
        self.persona = persona
        self.memory = memory         # MokoMemory（無くても動く）
        self.model = env.get("GEMINI_LIVE_MODEL", DEFAULT_MODEL)
        self.chunk_ms = _env_int(env, "LIVE_CHUNK_MS", DEFAULT_CHUNK_MS, 40, 500)
        self.chunk_bytes = max(2, IN_RATE * 2 * self.chunk_ms // 1000)
        self.chunk_bytes -= self.chunk_bytes % 2
        self.chunk_sec = self.chunk_bytes / (IN_RATE * 2)
        self.silence_ms = _env_int(
            env, "LIVE_SILENCE_MS", DEFAULT_SILENCE_MS, 500, 2000)
        self.echo_guard_sec = _env_int(
            env, "LIVE_ECHO_GUARD_MS", DEFAULT_ECHO_GUARD_MS, 100, 1000) / 1000.0
        self._vp_keep_chunks = max(1, int(round(VP_KEEP_SEC / self.chunk_sec)))
        self.phase = "idle"
        self.in_text = ""
        self.out_text = ""
        self.last_voice = time.monotonic()
        self.connected = False
        self._ws = None
        self._send_lock = threading.Lock()
        self._suspended = threading.Event()
        self._stop = threading.Event()
        self._playing = False        # 再生中フラグ（receiverスレッドのみが書く）
        self._goaway = False         # サーバーの切断予告を受けたら立てる
        self._ext = False            # 反応ボイス等の外部再生中
        self._clean_after = 0.0      # この時刻まで再生をまたいだ録音を破棄（エコー対策）
        self._audio_stream_paused = False
        self._mute_started = None
        self._completed_turns = queue.SimpleQueue()
        self._turn_lock = threading.RLock()
        self._turn_generation = 0
        self._turn_pending = False
        self._turn_complete_at = 0.0
        self._finalize_timer = None
        self._finalize_token = 0
        self._control_generation = 0
        self._turn_control_generation = None
        self._first_input_at = None
        # 声紋ライト実験: off=無効 / log=一致度をログに出すだけ / gate=知らない声を聞き流す
        self.vp_mode = env.get("VOICEPRINT", "off") if voiceprint else "off"
        if self.vp_mode not in ("off", "log", "gate"):
            print(f"[voice] VOICEPRINT={self.vp_mode!r} は不正なためoffにします")
            self.vp_mode = "off"
        self._vp_ref = None          # 起こした人の声（suspend=おやすみでリセット）
        self._vp_ok = None           # このターンの声判定（None=未判定）
        self._vp_lock = threading.Lock()
        self._vp_epoch = 0
        self._turn_audio = []        # 判定用の直近送信音声
        self._rec = None
        self._player = None
        self._player_lock = threading.RLock()
        self._threads = []

    # ---------- 制御 ----------
    def start(self):
        if self.vp_mode != "off":
            print(f"[voice] 声紋ライト実験: {self.vp_mode}モード")
        for fn in (self._manager_loop, self._sender_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)

    def suspend(self):
        """睡眠: マイク停止+セッション切断（次の resume で張り直す）。"""
        self._suspended.set()
        self._discard_turn(clear_completed=True)  # timer/古いコマンドを先に無効化
        self._stop_rec()
        self._stop_player()
        self._close_ws()
        with self._vp_lock:
            self._vp_epoch += 1
            self._vp_ref = None      # おやすみで「起こした人の声」を忘れる

    def resume(self):
        if self._suspended.is_set():
            self._suspended.clear()
            self.last_voice = time.monotonic()

    def close(self):
        self._stop.set()
        self.suspend()

    def active(self):
        return self.connected and not self._suspended.is_set()

    def pop_completed_turn(self):
        """返答再生まで完了したユーザー発話を1件返す。なければ ``None``。"""
        try:
            return self._completed_turns.get_nowait()
        except queue.Empty:
            return None

    def cancel_pending_controls(self):
        """物理的な起床操作があったとき、古い音声コマンドだけを無効化する。"""
        with self._turn_lock:
            self._control_generation += 1
            while True:
                try:
                    self._completed_turns.get_nowait()
                except queue.Empty:
                    break

    def _mark_turn_started_locked(self):
        """現在ターンを、最初に観測した時点の制御世代へ結び付ける。"""
        if self._turn_control_generation is None:
            self._turn_control_generation = self._control_generation

    def external_mute(self, on):
        """反応ボイス等、Live外の音声再生中もマイクを止める（自己エコー防止）。"""
        on = bool(on)
        if self._ext and not on:             # 再生終了 → 境界チャンクも捨てる
            self._clean_after = time.time() + self.echo_guard_sec
        self._ext = on                       # ラッチせず毎回上書き（詰まらない）

    # ---------- 内部: 接続管理 ----------
    def _manager_loop(self):
        wait = RECONNECT_MIN
        while not self._stop.is_set():
            if self._suspended.is_set() or self.connected:
                time.sleep(0.5)
                continue
            try:
                self._connect()
                wait = RECONNECT_MIN      # 接続成功でバックオフを戻す
                self._receiver()          # 切断まで戻らない
            except Exception as exc:
                print(f"[live] 接続断: {type(exc).__name__}: {exc}")
            graceful_reconnect = self._goaway
            self.connected = False
            self._discard_turn()           # slowな停止処理より先にtimer/世代を無効化
            self._close_ws()
            self._stop_player()
            if graceful_reconnect:
                # 即時再接続の権利は1回だけ消費。次の接続失敗は通常backoffする。
                self._goaway = False
            if self._suspended.is_set() or graceful_reconnect:
                wait = RECONNECT_MIN
                self._stop.wait(0.1)
                continue
            self._stop.wait(wait)
            wait = min(wait * 2, RECONNECT_MAX)

    def _setup_message(self):
        """接続時のsetupメッセージを組み立てる（実機なしテストでも検証可能）。"""
        now = time.strftime("%Y年%m月%d日(%a) %H時%M分")
        gen_cfg = {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": VOICE}}},
        }
        if "native-audio" in self.model:     # 思考OFFはネイティブ音声型のみ対応
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
        remembered = self.memory.prompt_text() if self.memory else ""
        return {"setup": {
            "model": f"models/{self.model}",
            "generationConfig": gen_cfg,
            "systemInstruction": {"parts": [{"text": (
                self.persona + remembered
                + f"\n\n現在の日時: {now}（日本時間）。"
                "\nユーザーの発話は音声で届きます。かならず日本語で答えてください。")}]},
            "realtimeInputConfig": {
                # 返答開始直前の新しい発話を古い返答の後へ持ち越さない。
                # 再生中は端末側でマイクを閉じるため、自己音声の割り込みは防ぐ。
                "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
                "automaticActivityDetection": {
                    # 500ms未満は自然な文中の間まで分割しやすいため使わない。
                    "silenceDurationMs": self.silence_ms,
                }
            },
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
        }}

    def _connect(self):
        ws = ws_connect(WS_URL.format(key=self.env["GEMINI_API_KEY"]),
                        open_timeout=15, close_timeout=5)
        ws.send(json.dumps(self._setup_message()))
        try:
            first = json.loads(ws.recv(timeout=15))
        except Exception as exc:
            first = {"recv_error": f"{type(exc).__name__}: {exc}"}
        if "setupComplete" not in first:
            ws.close()
            raise RuntimeError(f"setup失敗: {str(first)[:200]}")
        self._ws = ws
        self.connected = True
        self._goaway = False
        self._audio_stream_paused = False
        self._mute_started = None
        print(f"[live] 連続会話セッション接続 ({self.model}, "
              f"chunk={self.chunk_ms}ms, silence={self.silence_ms}ms)")

    def _close_ws(self):
        ws, self._ws = self._ws, None
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        self.connected = False

    def _send(self, obj):
        with self._send_lock:
            ws = self._ws
            if ws is None:
                return False
            try:
                ws.send(json.dumps(obj))
                return True
            except Exception as exc:
                # 送信側だけが壊れた状態を受信timeoutまで放置せず、即再接続させる。
                print(f"[live] 音声送信失敗: {type(exc).__name__}: {exc}")
                if self._ws is ws:
                    self._ws = None
                self.connected = False
                try:
                    ws.close()
                except Exception:
                    pass
                return False

    # ---------- 内部: マイク送信 ----------
    def _pause_input_stream(self):
        """1秒を超えてマイク送信が止まった場合だけVADをflushする。"""
        if self._audio_stream_paused:
            return
        now = time.monotonic()
        if self._mute_started is None:
            self._mute_started = now
            return
        if now - self._mute_started < STREAM_END_AFTER_SEC:
            return
        if self._send({"realtimeInput": {"audioStreamEnd": True}}):
            self._audio_stream_paused = True

    def _input_muted(self):
        """自己エコー対策で現在の録音チャンクを破棄すべきか返す。"""
        with self._turn_lock:
            turn_pending = self._turn_pending
        return (self._playing or self._ext or turn_pending
                or time.time() < self._clean_after)

    def _sender_loop(self):
        while not self._stop.is_set():
            if self._suspended.is_set() or not self.connected:
                self._stop_rec()
                time.sleep(0.3)
                continue
            rec = self._rec
            if rec is None or rec.poll() is not None:
                # -B 1秒: CPUが描画/TLSで詰まった瞬間のオーバーラン（録音欠落）を防ぐ
                rec = subprocess.Popen(
                    ["arecord", "-q", "-D", self.env.get("ALSA_DEV", "plughw:CARD=whisplaysound"),
                     "-f", "S16_LE", "-r", str(IN_RATE), "-c", "1",
                     "-B", "1000000", "-t", "raw", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self._rec = rec
            try:
                chunk = rec.stdout.read(self.chunk_bytes)
            except (OSError, ValueError):
                chunk = b""
            # suspend/再接続がread中にrecを差し替えた場合、旧ALSAバッファを
            # 新しいLiveセッションへ送らず、新しいrecも誤って停止しない。
            if self._rec is not rec:
                continue
            if not chunk:
                self._stop_rec()
                continue
            if self._suspended.is_set() or not self.connected:
                continue
            if self._input_muted():
                self._pause_input_stream()
                continue    # 再生・サーボ・残響中は破棄（半二重・キュー防止）
            sent = self._send({"realtimeInput": {"audio": {
                "mimeType": f"audio/pcm;rate={IN_RATE}",
                "data": base64.b64encode(chunk).decode()}}})
            if not sent:
                self._stop_rec()
                self._stop.wait(0.1)
                continue
            self._audio_stream_paused = False
            self._mute_started = None
            if sent and self.vp_mode != "off":
                with self._turn_lock:
                    self._turn_audio.append(chunk)
                    del self._turn_audio[:-self._vp_keep_chunks]

    def _stop_rec(self):
        rec, self._rec = self._rec, None
        if rec:
            rec.terminate()
            try:
                rec.wait(timeout=1)
            except subprocess.TimeoutExpired:
                rec.kill()
                try:
                    rec.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass

    def _compare_voice(self, audio, epoch, gate):
        """声特徴を比較する。logは応答後の別スレッド、gateだけ同期実行。"""
        vec = voiceprint.extract(audio)
        if vec is None:
            return True                    # 判定材料が足りないターンは通す
        with self._vp_lock:
            if epoch != self._vp_epoch:    # 睡眠をまたいだ古い計算結果は使わない
                return True
            if self._vp_ref is None:
                self._vp_ref = vec
                print("[voice] この声を覚えた（次のおやすみまで）")
                return True
            sim = voiceprint.similarity(self._vp_ref, vec)
        accepted = not gate or sim >= VP_THRESH
        print(f"[voice] 声の一致度 {sim:.2f}"
              + ("" if accepted else " → 知らない声なので聞き流す"))
        return accepted

    def _judge_voice(self):
        """gateモードで返答前に同期判定する（明示的な実験設定のみ）。"""
        if self._vp_ok is not None:
            return
        epoch = self._vp_epoch
        audio = b"".join(tuple(self._turn_audio))
        self._vp_ok = self._compare_voice(audio, epoch, gate=True)

    def _log_voice(self, audio, epoch):
        try:
            self._compare_voice(audio, epoch, gate=False)
        except Exception as exc:
            print(f"[voice] 声紋ログ失敗: {type(exc).__name__}: {exc}")

    # ---------- 内部: 受信・再生 ----------
    def _receiver(self):
        while not self._stop.is_set() and not self._suspended.is_set():
            ws = self._ws
            if ws is None:
                return
            try:
                msg = json.loads(ws.recv(timeout=RECV_TIMEOUT))
            except TimeoutError:
                # 無音中はサーバーから何も届かないのが正常。切断せずpingで生存確認
                if not ws.ping().wait(10):
                    raise RuntimeError("ping応答なし")
                continue
            if "goAway" in msg:      # サーバーのセッション期限予告
                print("[live] セッション期限予告 → 再生完了後に張り直します")
                self._goaway = True
            content = msg.get("serverContent", {})
            if content:
                self._process_server_content(content)

    def _process_server_content(self, content):
        """独立配送される字幕・音声・完了通知を順序に依存せず処理する。"""
        if any(content.get(name) for name in (
                "inputTranscription", "outputTranscription", "modelTurn",
                "interrupted", "generationComplete", "turnComplete")):
            with self._turn_lock:
                self._mark_turn_started_locked()
        text = content.get("inputTranscription", {}).get("text")
        if text:
            with self._turn_lock:
                self.in_text += text
                real_input = _real_speech(self.in_text)
                pending = self._turn_pending
                if pending:
                    self._arm_finalize_locked(self._turn_generation)
            if real_input:                       # 本物の発話のみ起きてる扱い
                self.last_voice = time.monotonic()
                with self._turn_lock:
                    if self._first_input_at is None:
                        self._first_input_at = time.monotonic()
                if self.phase == "idle" and not pending:
                    self.phase = "listen"
        text = content.get("outputTranscription", {}).get("text")
        if text:
            with self._turn_lock:
                self.out_text += text
                if self._turn_pending:
                    self._arm_finalize_locked(self._turn_generation)

        interrupted = bool(content.get("interrupted"))
        if interrupted:
            # 公式仕様どおり即座に再生を止める。ただし同じメッセージにある
            # turnComplete等はこの後も必ず処理し、状態を固めない。
            self._stop_player()

        if not interrupted:
            for part in content.get("modelTurn", {}).get("parts", []):
                data = part.get("inlineData", {}).get("data")
                if not data:
                    continue
                # 字幕は別配送で順序保証がないため、再生可否の条件にしない。
                if self._vp_ok is None and self.vp_mode == "gate":
                    self._judge_voice()          # gateだけは明示的に遅延を許容
                if self._vp_ok is False:
                    continue                     # 知らない声のターンは返事を再生しない
                if not self._ensure_player():
                    continue
                try:
                    self._write_player(base64.b64decode(data))
                except (BrokenPipeError, OSError, ValueError):
                    self._stop_player()

        if content.get("turnComplete"):
            self._finish_turn()

    def _ensure_player(self):
        """aplayを1つだけ生成し、suspend側から常に停止できる状態で公開する。"""
        with self._player_lock:
            if self._player is not None:
                return True
            if self._suspended.is_set() or self._stop.is_set():
                return False
            player = subprocess.Popen(
                ["aplay", "-q", "-D",
                 self.env.get("ALSA_DEV", "plughw:CARD=whisplaysound"),
                 "-t", "raw", "-f", "S16_LE", "-r", "24000", "-c", "1", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, bufsize=0)
            # Popen中にsuspendされた場合は、共有状態へ出さずここで止める。
            if self._suspended.is_set() or self._stop.is_set():
                pipe, player.stdin = player.stdin, None
                try:
                    player.kill()
                except Exception:
                    pass
                try:
                    if pipe:
                        pipe.close()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    player.wait(timeout=1)
                except Exception:
                    pass
                return False
            self._player = player
            self._playing = True
            self.phase = "play"
            with self._turn_lock:
                first_input_at = self._first_input_at
            if first_input_at is not None:
                print(f"[live] 応答音声開始 (入力字幕から"
                      f"{time.monotonic() - first_input_at:.2f}s)")
            return True

    def _write_player(self, audio):
        """unbuffered pipeへ全PCMを書き、短いwriteにも対応する。"""
        view = memoryview(audio)
        while view:
            with self._player_lock:
                player = self._player
                pipe = player.stdin if player is not None else None
                if pipe is None:
                    raise BrokenPipeError("aplay pipe is closed")
            written = pipe.write(view)
            if not written:
                raise BrokenPipeError("aplay accepted no audio")
            view = view[written:]

    def _finish_turn(self):
        """音声再生を閉じ、独立配送の字幕を待ってからターンを確定する。"""
        with self._turn_lock:
            generation = self._turn_generation
        with self._player_lock:
            player = self._player
            pipe = None
            if player:
                # 共有参照はwait完了まで残し、suspend側がkillできるようにする。
                pipe, player.stdin = player.stdin, None
        if player:
            try:
                if pipe:
                    pipe.close()
                player.wait(timeout=30)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                try:
                    player.kill()
                    player.wait(timeout=1)
                except Exception:
                    pass
            time.sleep(UNMUTE_DELAY)
            with self._player_lock:
                if self._player is player:
                    self._player = None
        guard = max(self.echo_guard_sec, TRANSCRIPT_GRACE_SEC)
        self._clean_after = max(self._clean_after, time.time() + guard)
        self._playing = False
        self._schedule_finalize_turn(generation)

    def _schedule_finalize_turn(self, generation):
        with self._turn_lock:
            if (generation != self._turn_generation or self._suspended.is_set()
                    or self._stop.is_set()):
                return False
            self._turn_pending = True
            self._turn_complete_at = time.monotonic()
            self.phase = "think"             # 字幕grace中も睡眠・次入力を開始しない
            self._arm_finalize_locked(generation)
            return True

    def _arm_finalize_locked(self, generation):
        if self._finalize_timer:
            self._finalize_timer.cancel()
        self._finalize_token += 1
        token = self._finalize_token
        elapsed = time.monotonic() - self._turn_complete_at
        remaining = max(0.0, TRANSCRIPT_MAX_WAIT_SEC - elapsed)
        delay = min(TRANSCRIPT_GRACE_SEC, remaining)
        timer = threading.Timer(delay, self._finalize_turn, args=(generation, token))
        timer.daemon = True
        self._finalize_timer = timer
        timer.start()

    def _finalize_turn(self, generation, token):
        """turnComplete後に遅着した字幕も含め、記憶とコマンドへ1回だけ渡す。"""
        with self._turn_lock:
            if (generation != self._turn_generation or token != self._finalize_token
                    or not self._turn_pending or self._suspended.is_set()
                    or self._stop.is_set()):
                return
            self._turn_pending = False
            self._finalize_timer = None
            in_text = self.in_text.strip()
            out_text = self.out_text.strip()
            turn_audio = b"".join(tuple(self._turn_audio))
            vp_epoch = self._vp_epoch
            vp_ok = self._vp_ok
            self.in_text = ""
            self.out_text = ""
            self._vp_ok = None               # 次のターンの声判定へ
            self._turn_audio = []
            self.phase = "idle"
            real = _real_speech(in_text)
            control_allowed = (
                self._turn_control_generation is not None
                and self._turn_control_generation == self._control_generation)
            self._turn_control_generation = None
            self._first_input_at = None
            if real and control_allowed:
                # suspend側は同じlock取得後にqueueをdrainできる。
                self._completed_turns.put(in_text)

        if real:
            self.last_voice = time.monotonic()  # 雑音ターンでは眠気を妨げない
            print(f"[you] {in_text}")
            print(f"[moko] {out_text}")
            if self.memory and vp_ok is not False:   # 聞き流したターンは覚えない
                self.memory.add_turn(in_text, out_text)
        if real and self.vp_mode == "log" and turn_audio:
            threading.Thread(target=self._log_voice, args=(turn_audio, vp_epoch),
                             daemon=True, name="moko-voiceprint-log").start()
        if self._goaway:
            # GoAway受信直後には切らず、確定ターンの字幕回収後に閉じる。
            ws = self._ws
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass

    def _discard_turn(self, clear_completed=False):
        """切断・睡眠時に未完了ターンと遅延確定タイマーを破棄する。"""
        with self._turn_lock:
            self._turn_generation += 1
            self._finalize_token += 1
            timer, self._finalize_timer = self._finalize_timer, None
            if timer:
                timer.cancel()
            self._turn_pending = False
            self.in_text = ""
            self.out_text = ""
            self.phase = "idle"
            self._vp_ok = None
            self._turn_audio = []
            self._turn_control_generation = None
            self._first_input_at = None
            if clear_completed:
                while True:
                    try:
                        self._completed_turns.get_nowait()
                    except queue.Empty:
                        break

    def _stop_player(self):
        with self._player_lock:
            player, self._player = self._player, None
            pipe = None
            if player:
                pipe, player.stdin = player.stdin, None
        if player:
            try:
                player.kill()
            except Exception:
                pass
            try:
                if pipe:
                    pipe.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                player.wait(timeout=1)
            except Exception:
                pass
            self._clean_after = time.time() + self.echo_guard_sec
        self._playing = False
        if self.phase == "play":
            self.phase = "idle"
