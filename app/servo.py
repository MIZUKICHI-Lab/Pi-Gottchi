"""ハードウェアPWMを使った、安全側のサーボ制御と非同期リアクション。

BCM12（物理32番）を channel 0、BCM13（物理33番）を channel 1 として使う。
必要な ``pwm-2chan`` overlay と電源・配線は WIRING.md を参照すること。
"""
import math
import os
import threading
import time


PWMCHIP = "/sys/class/pwm/pwmchip0"
PERIOD_NS = 20_000_000          # 20ms (50Hz)
MIN_NS = 500_000                # 実機校正の開始値 (0.5ms)
MAX_NS = 2_400_000              # 実機で端当たりしなかった上限 (2.4ms)


def _clamp(value, low, high):
    return max(low, min(high, value))


def _env_bool(env, name, default=False):
    value = env.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _env_number(env, name, default, cast=float):
    try:
        value = cast(env.get(name, default))
        if not math.isfinite(value):
            raise ValueError("non-finite value")
        return value
    except (TypeError, ValueError, OverflowError):
        print(f"[servo] {name}={env.get(name)!r} は不正なため既定値 {default} を使います")
        return cast(default)


class Servo:
    """Linux PWM sysfs の小さなラッパー。

    ``release()`` 後の ``angle()`` ではPWMを自動的に再有効化する。既存PWMが
    有効なまま残っていても、period変更前に一度無効化するため再起動に強い。
    """

    def __init__(self, channel=0, pwmchip=PWMCHIP, min_ns=MIN_NS, max_ns=MAX_NS):
        self.channel = int(channel)
        self.pwmchip = pwmchip
        self.path = os.path.join(pwmchip, f"pwm{self.channel}")
        self.min_ns = int(min_ns)
        self.max_ns = int(max_ns)
        self._enabled = False
        self._closed = False
        self._lock = threading.Lock()

        if not 0 < self.min_ns < self.max_ns < PERIOD_NS:
            raise ValueError("サーボのパルス幅は 0 < min < max < period で指定してください")
        if not os.path.isdir(pwmchip):
            raise RuntimeError(
                "pwmchip0 がありません。config.txt に pwm-2chan overlay を設定してください。")
        if not os.path.isdir(self.path):
            self._write(os.path.join(pwmchip, "export"), str(self.channel))
            deadline = time.monotonic() + 1.0
            while not os.path.isdir(self.path) and time.monotonic() < deadline:
                time.sleep(0.02)
            if not os.path.isdir(self.path):
                raise RuntimeError(f"PWM channel {self.channel} のexportが完了しません")

        # 前回の異常終了でenable=1でも、periodを安全に再設定できるようにする。
        self._write(os.path.join(self.path, "enable"), "0")
        self._write(os.path.join(self.path, "period"), str(PERIOD_NS))

    @staticmethod
    def _write(path, value):
        with open(path, "w", encoding="ascii") as pwm_file:
            pwm_file.write(value)

    def angle(self, degrees):
        """論理角0〜180度を校正済みパルス幅へ変換して動かす。"""
        degrees = float(degrees)
        if not math.isfinite(degrees):
            raise ValueError("サーボ角度は有限値で指定してください")
        degrees = _clamp(degrees, 0.0, 180.0)
        duty = int(self.min_ns + (self.max_ns - self.min_ns) * degrees / 180.0)
        with self._lock:
            if self._closed:
                raise RuntimeError("close済みのServoは再利用できません")
            self._write(os.path.join(self.path, "duty_cycle"), str(duty))
            if not self._enabled:
                self._write(os.path.join(self.path, "enable"), "1")
                self._enabled = True

    def release(self):
        """PWM信号を止める。保持トルクも失われる点に注意。"""
        with self._lock:
            if self._closed or not self._enabled:
                return
            self._write(os.path.join(self.path, "enable"), "0")
            self._enabled = False

    def close(self):
        with self._lock:
            if self._closed:
                return
            if self._enabled:
                try:
                    self._write(os.path.join(self.path, "enable"), "0")
                except OSError:
                    pass
                self._enabled = False
            self._closed = True


class ServoAnimator:
    """メインループを止めず、最新リアクションだけを安全範囲で再生する。"""

    # (中心からの可動範囲に対する比率, その姿勢の保持秒数)
    PATTERNS = {
        "idle": ((0.0, 0.08),),
        "happy": ((0.45, 0.10), (-0.45, 0.10), (0.0, 0.08)),
        "excited": ((0.65, 0.10), (-0.65, 0.10), (0.0, 0.08)),
        "surprised": ((0.65, 0.16), (0.0, 0.10)),
        # 激しく揺らされた直後は追加で振らず、重心を中央へ戻す。
        "dizzy": ((0.0, 0.16),),
        "sad": ((-0.25, 0.20), (0.0, 0.08)),
        "hungry": ((-0.30, 0.20), (0.0, 0.08)),
        "eating": ((0.30, 0.12), (-0.20, 0.12), (0.30, 0.12), (0.0, 0.08)),
        "talking": ((0.22, 0.10), (-0.22, 0.10), (0.0, 0.08)),
        "wake": ((0.55, 0.14), (0.0, 0.08)),
        "sleeping": ((0.0, 0.12),),  # 実角は SERVO_SLEEP_ANGLE を使う
    }

    def __init__(self, servo=None, *, center=90.0, min_angle=70.0, max_angle=110.0,
                 sleep_angle=80.0, reversed_direction=False, hold=False,
                 max_step=4.0, step_interval=0.04):
        self._servo = servo
        self.enabled = servo is not None
        center = float(center)
        self.min_angle = float(min_angle)
        self.max_angle = float(max_angle)
        self.sleep_angle = float(sleep_angle)
        self.max_step = float(max_step)
        self.step_interval = float(step_interval)
        if not all(math.isfinite(value) for value in (
                center, self.min_angle, self.max_angle, self.sleep_angle,
                self.max_step, self.step_interval)):
            raise ValueError("サーボ設定は有限値で指定してください")
        if self.min_angle >= self.max_angle:
            raise ValueError("SERVO_MIN_ANGLE は SERVO_MAX_ANGLE より小さくしてください")
        self.center = _clamp(center, self.min_angle, self.max_angle)
        self.sleep_angle = _clamp(self.sleep_angle, self.min_angle, self.max_angle)
        self.direction = -1.0 if reversed_direction else 1.0
        self.hold = bool(hold)
        self.max_step = max(0.5, self.max_step)
        self.step_interval = max(0.01, self.step_interval)
        self.moving = threading.Event()
        self._cv = threading.Condition()
        self._pending = None
        self._generation = 0
        self._stop = False
        self._closed = False
        self._current = self.center
        self._thread = None

        if self.enabled:
            # 有効化は明示設定時だけ。まず狭い安全範囲の中央へ置く。
            self._servo.angle(self.center)
            time.sleep(0.2)
            if not self.hold:
                self._servo.release()
            self._thread = threading.Thread(target=self._worker, daemon=True,
                                            name="moko-servo")
            self._thread.start()

    @classmethod
    def from_env(cls, env):
        """環境変数から生成する。未設定・初期化失敗時は安全なno-opになる。"""
        if not _env_bool(env, "SERVO_ENABLED", False):
            return cls()
        servo = None
        try:
            min_angle = _env_number(env, "SERVO_MIN_ANGLE", 70.0)
            max_angle = _env_number(env, "SERVO_MAX_ANGLE", 110.0)
            servo = Servo(
                channel=_env_number(env, "SERVO_CHANNEL", 0, int),
                min_ns=int(_env_number(env, "SERVO_MIN_PULSE_US", 500.0) * 1000),
                max_ns=int(_env_number(env, "SERVO_MAX_PULSE_US", 2400.0) * 1000),
            )
            animator = cls(
                servo,
                center=_env_number(env, "SERVO_CENTER", 90.0),
                min_angle=min_angle,
                max_angle=max_angle,
                sleep_angle=_env_number(env, "SERVO_SLEEP_ANGLE", 80.0),
                reversed_direction=_env_bool(env, "SERVO_REVERSED", False),
                hold=_env_bool(env, "SERVO_HOLD", False),
            )
            print(f"[servo] 有効 channel={servo.channel} "
                  f"range={animator.min_angle:.0f}..{animator.max_angle:.0f} "
                  f"center={animator.center:.0f} hold={animator.hold}")
            return animator
        except (OSError, RuntimeError, ValueError, OverflowError) as exc:
            if servo is not None:
                try:
                    servo.release()
                except (OSError, RuntimeError):
                    pass
                servo.close()
            print(f"[servo] 初期化できないため無効化: {type(exc).__name__}: {exc}")
            return cls()

    def react(self, name):
        """名前付き動作を予約する。待機列は作らず常に最新要求を優先する。"""
        if not self.enabled or name not in self.PATTERNS or self._closed:
            return False
        with self._cv:
            self._generation += 1
            self._pending = (name, self._generation)
            self._cv.notify_all()
        return True

    def _interrupted_wait(self, seconds, generation):
        deadline = time.monotonic() + seconds
        with self._cv:
            while True:
                if self._stop or generation != self._generation:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return True
                self._cv.wait(remaining)

    def _move_to(self, target, generation):
        target = _clamp(float(target), self.min_angle, self.max_angle)
        steps = max(1, int(math.ceil(abs(target - self._current) / self.max_step)))
        start = self._current
        for index in range(1, steps + 1):
            if self._stop or generation != self._generation:
                return False
            angle = start + (target - start) * index / steps
            self._servo.angle(angle)
            self._current = angle
            if index < steps and not self._interrupted_wait(self.step_interval, generation):
                return False
        return True

    def _play(self, name, generation):
        amplitude = max(0.0, min(self.center - self.min_angle,
                                 self.max_angle - self.center))
        for offset, hold_sec in self.PATTERNS[name]:
            target = (self.sleep_angle if name == "sleeping"
                      else self.center + self.direction * amplitude * offset)
            if not self._move_to(target, generation):
                return
            if not self._interrupted_wait(hold_sec, generation):
                return
        if not self.hold:
            self._servo.release()

    def _worker(self):
        while True:
            with self._cv:
                while self._pending is None and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                name, generation = self._pending
                self._pending = None
            self.moving.set()
            try:
                self._play(name, generation)
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"[servo] 動作エラーのため停止: {type(exc).__name__}: {exc}")
                try:
                    self._servo.release()
                except (OSError, RuntimeError, ValueError):
                    pass
                self._servo.close()
                self.enabled = False
                with self._cv:
                    self._stop = True
            finally:
                self.moving.clear()

    def close(self, park_center=True):
        """ワーカーを止め、通常終了時は中央へ戻してPWMを解放する。"""
        if self._closed:
            return
        self._closed = True
        with self._cv:
            self._stop = True
            self._generation += 1
            self._cv.notify_all()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._servo:
            try:
                if park_center:
                    self._servo.angle(self.center)
                    time.sleep(0.15)
                self._servo.release()
            except (OSError, RuntimeError, ValueError):
                pass
            self._servo.close()


if __name__ == "__main__":
    servo = Servo(0)
    # 実機で端当たりしないことを確認済みの保守的な範囲だけを動かす。
    for angle in (90, 75, 105, 90):
        print(f"angle {angle}")
        servo.angle(angle)
        time.sleep(0.8)
    servo.close()
