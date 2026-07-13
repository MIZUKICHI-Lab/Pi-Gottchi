import os
import sys
import time
import types
import unittest
from unittest import mock


APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

# 実機専用Whisplayランタイムなしでも、経路ラッチの純粋な部分だけを検証する。
numpy = types.ModuleType("numpy")
sys.modules.setdefault("numpy", numpy)
face = types.ModuleType("face")
face.W = 240
face.H = 280
sys.modules.setdefault("face", face)
voice = types.ModuleType("voice")
voice.SYSTEM_PROMPT = ""
sys.modules.setdefault("voice", voice)
imu = types.ModuleType("imu")
imu.ShakeMonitor = object
sys.modules.setdefault("imu", imu)
memory = types.ModuleType("memory")
memory.MokoMemory = object
sys.modules.setdefault("memory", memory)
requests = types.ModuleType("requests")
requests.RequestException = RuntimeError
sys.modules.setdefault("requests", requests)
whisplay = types.ModuleType("whisplay")
whisplay.WhisplayBoard = object
sys.modules.setdefault("whisplay", whisplay)

import chara  # noqa: E402


class ButtonRoutingTests(unittest.TestCase):
    def test_simple_switch_sleep_default_keeps_display_visible(self):
        class Board:
            def __init__(self):
                self.values = []

            def set_backlight(self, value):
                self.values.append(value)

        class Motion:
            def __init__(self):
                self.actions = []

            def react(self, name):
                self.actions.append(name)

        moko = chara.Moko.__new__(chara.Moko)
        moko._control_lock = chara.threading.RLock()
        moko.manual_sleep = False
        moko.chat = None
        moko.was_sleeping = False
        moko.was_deep_sleeping = False
        moko.sleep_backlight = chara.SLEEP_BACKLIGHT
        moko.board = Board()
        moko.motion = Motion()
        with mock.patch.object(moko, "_is_sleeping", return_value=True):
            moko._handle_sleep_state()
        self.assertEqual([chara.SLEEP_BACKLIGHT], moko.board.values)
        self.assertGreater(moko.board.values[0], 0)
        self.assertEqual(["sleeping"], moko.motion.actions)

    def test_auto_sleep_keeps_live_listening_but_manual_sleep_suspends(self):
        class Chat:
            phase = "idle"

            def __init__(self):
                self.suspends = 0

            def suspend(self):
                self.suspends += 1

            @staticmethod
            def external_mute(_muted):
                return None

        class Board:
            @staticmethod
            def set_backlight(_value):
                return None

        class Motion:
            @staticmethod
            def react(_name):
                return True

        moko = chara.Moko.__new__(chara.Moko)
        moko._control_lock = chara.threading.RLock()
        moko.manual_sleep = False
        moko.chat = Chat()
        moko.was_sleeping = False
        moko.was_deep_sleeping = False
        moko.sleep_backlight = chara.SLEEP_BACKLIGHT
        moko.board = Board()
        moko.motion = Motion()
        with mock.patch.object(moko, "_is_sleeping", return_value=True):
            moko._handle_sleep_state()
        self.assertEqual(0, moko.chat.suspends)

        moko.manual_sleep = True
        with mock.patch.object(moko, "_is_sleeping", return_value=True):
            moko._handle_sleep_state()
        self.assertEqual(1, moko.chat.suspends)
        self.assertTrue(moko.was_deep_sleeping)

    def test_startup_grace_uses_monotonic_time_before_auto_sleep(self):
        class Conversation:
            @staticmethod
            def busy():
                return False

        moko = chara.Moko.__new__(chara.Moko)
        moko._control_lock = chara.threading.RLock()
        moko.manual_sleep = False
        moko._rest_fallback = False
        moko.press_t = None
        moko.rec_proc = None
        moko.audio_proc = None
        moko.convo = Conversation()
        moko.chat = None
        moko._boot_monotonic = 100
        moko.last_activity = 100
        moko.startup_awake_sec = 20
        moko.auto_sleep_sec = 30
        moko.night_auto_sleep_sec = 10

        with (mock.patch.object(chara.time, "monotonic", return_value=110),
              mock.patch.object(chara, "is_night", return_value=True)):
            self.assertFalse(moko._is_sleeping())
        with (mock.patch.object(chara.time, "monotonic", return_value=135),
              mock.patch.object(chara, "is_night", return_value=True)):
            self.assertTrue(moko._is_sleeping())

    def bare_moko(self, mode):
        moko = chara.Moko.__new__(chara.Moko)
        moko.press_t = time.time() - 1
        moko.press_mode = mode
        moko.actions = []
        moko.clicks = 0
        moko.last_click = 0
        moko._rest_release_pending = False
        return moko

    def test_rest_route_is_kept_until_release(self):
        moko = self.bare_moko("rest")
        moko._on_release()
        self.assertEqual(["talk"], moko.actions)
        self.assertEqual(0, moko.clicks)

    def test_live_route_remains_a_pet_action(self):
        moko = self.bare_moko("live")
        moko._on_release()
        self.assertEqual([], moko.actions)
        self.assertEqual(1, moko.clicks)

    def test_explicit_wake_cancels_pending_sleep(self):
        moko = chara.Moko.__new__(chara.Moko)
        moko._control_lock = chara.threading.RLock()
        moko.manual_sleep = False
        moko.pending_sleep = True
        moko._wake_generation = 0
        moko.chat = None
        moko.last_activity = 0
        moko._wake("button")
        self.assertFalse(moko.pending_sleep)
        self.assertGreater(moko.last_activity, 0)

    def test_stop_audio_waits_for_device_release(self):
        events = []

        class AudioProcess:
            def poll(self):
                return None

            def terminate(self):
                events.append("terminate")

            def wait(self, timeout=None):
                events.append(("wait", timeout))
                return 0

        moko = chara.Moko.__new__(chara.Moko)
        moko.audio_proc = AudioProcess()
        moko._stop_audio()
        self.assertEqual(["terminate", ("wait", 0.5)], events)
        self.assertIsNone(moko.audio_proc)

    def test_rest_sleep_result_before_latest_wake_is_ignored(self):
        class Conversation:
            @staticmethod
            def take_result():
                return "speak", "おやすみ", "おやすみ"

        class Motion:
            @staticmethod
            def react(_name):
                return True

        moko = chara.Moko.__new__(chara.Moko)
        moko._control_lock = chara.threading.RLock()
        moko.convo = Conversation()
        moko._rest_request_generation = 1
        moko._wake_generation = 2             # 結果待ち中に物理wake済み
        moko.audio_proc = None
        moko.state = {"bond_xp": 0, "mood": 50}
        moko.motion = Motion()
        moko._rest_fallback = False
        moko.pending_sleep = False
        moko.bubble = None
        moko.last_activity = 0
        with mock.patch.object(chara.os.path, "exists", return_value=False):
            moko._handle_convo_result()
        self.assertFalse(moko.pending_sleep)

    def test_rest_wake_during_playback_setup_cancels_sleep_result(self):
        class Conversation:
            @staticmethod
            def take_result():
                return "speak", "おやすみ", "おやすみ"

        class Motion:
            @staticmethod
            def react(_name):
                return True

        moko = chara.Moko.__new__(chara.Moko)
        moko._control_lock = chara.threading.RLock()
        moko.convo = Conversation()
        moko._rest_request_generation = 1
        moko._wake_generation = 1
        moko.manual_sleep = False
        moko.pending_sleep = False
        moko.chat = None
        moko.audio_proc = None
        moko.state = {"bond_xp": 0, "mood": 50}
        moko.motion = Motion()
        moko._rest_fallback = False
        moko.bubble = None
        moko.last_activity = 0

        def wake_while_stopping_audio():
            moko._wake("button")

        with (mock.patch.object(chara.os.path, "exists", return_value=False),
              mock.patch.object(moko, "_stop_audio",
                                side_effect=wake_while_stopping_audio)):
            moko._handle_convo_result()
        self.assertEqual(2, moko._wake_generation)
        self.assertFalse(moko.pending_sleep)

    def test_live_wake_between_pop_and_apply_cancels_sleep_result(self):
        class Chat:
            phase = "idle"
            out_text = ""
            last_voice = 0

            def __init__(self, owner):
                self.owner = owner
                self.sent = False

            @staticmethod
            def external_mute(_muted):
                return None

            @staticmethod
            def cancel_pending_controls():
                return None

            def pop_completed_turn(self):
                if self.sent:
                    return None
                self.sent = True
                self.owner._wake("button")
                return "おやすみ"

        class Motion:
            moving = chara.threading.Event()

            @staticmethod
            def react(_name):
                return True

        moko = chara.Moko.__new__(chara.Moko)
        moko._control_lock = chara.threading.RLock()
        moko._wake_generation = 1
        moko.manual_sleep = False
        moko.pending_sleep = False
        moko.last_activity = 0
        moko._last_live_phase = "idle"
        moko.audio_proc = None
        moko.motion = Motion()
        moko.state = {"mood": 50, "bond_xp": 0}
        moko.chat = Chat(moko)
        moko._handle_live()
        self.assertEqual(2, moko._wake_generation)
        self.assertFalse(moko.pending_sleep)

    def test_live_input_transcription_is_exposed_as_user_bubble(self):
        class Chat:
            phase = "listen"
            in_text = "リアルタイムで表示して"
            out_text = ""
            last_voice = 0

            @staticmethod
            def external_mute(_muted):
                return None

            @staticmethod
            def pop_completed_turn():
                return None

        class Motion:
            moving = chara.threading.Event()

            @staticmethod
            def react(_name):
                return True

        moko = chara.Moko.__new__(chara.Moko)
        moko._control_lock = chara.threading.RLock()
        moko._wake_generation = 0
        moko.last_activity = 0
        moko._last_live_phase = "idle"
        moko.audio_proc = None
        moko.motion = Motion()
        moko.state = {"mood": 50, "bond_xp": 0}
        moko.user_bubble = None
        moko.bubble = None
        moko.chat = Chat()
        moko._handle_live()
        self.assertEqual("あなた: リアルタイムで表示して", moko.user_bubble[0])

    def test_wake_during_pending_sleep_transition_wins(self):
        moko = chara.Moko.__new__(chara.Moko)
        moko._control_lock = chara.threading.RLock()
        moko._wake_generation = 1
        moko.manual_sleep = False
        moko.pending_sleep = True
        moko.chat = None
        moko.last_activity = 0
        attempting_wake = chara.threading.Event()
        wake_done = chara.threading.Event()
        worker = None

        def wake_from_gpio_thread():
            attempting_wake.set()
            moko._wake("button")
            wake_done.set()

        def start_wake_while_checking_audio():
            nonlocal worker
            worker = chara.threading.Thread(target=wake_from_gpio_thread)
            worker.start()
            self.assertTrue(attempting_wake.wait(1))
            return False

        with mock.patch.object(moko, "_speaking",
                               side_effect=start_wake_while_checking_audio):
            moko._handle_pending_sleep()
        self.assertTrue(wake_done.wait(1))
        worker.join(timeout=1)
        self.assertFalse(moko.manual_sleep)
        self.assertFalse(moko.pending_sleep)


if __name__ == "__main__":
    unittest.main()
