import base64
import os
import sys
import threading
import time
import unittest
from unittest import mock
import types


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

try:
    from websockets.sync.client import connect as _unused_connect  # noqa: F401
except ImportError:
    websockets = types.ModuleType("websockets")
    sync = types.ModuleType("websockets.sync")
    client = types.ModuleType("websockets.sync.client")
    client.connect = lambda *_args, **_kwargs: None
    sys.modules["websockets"] = websockets
    sys.modules["websockets.sync"] = sync
    sys.modules["websockets.sync.client"] = client

import live  # noqa: E402


class FakePipe:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        raw = bytes(data)
        self.data.extend(raw)
        return len(raw)

    def close(self):
        self.closed = True


class FakePlayer:
    def __init__(self):
        self.stdin = FakePipe()
        self.killed = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class FakeWebSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class LiveChatTests(unittest.TestCase):
    def make_chat(self, **env):
        return live.LiveChat({"GEMINI_API_KEY": "test", **env}, "persona")

    def test_default_vad_and_chunk_are_not_aggressive(self):
        chat = self.make_chat()
        setup = chat._setup_message()["setup"]
        vad = setup["realtimeInputConfig"]["automaticActivityDetection"]
        self.assertEqual(700, vad["silenceDurationMs"])
        self.assertEqual(100, chat.chunk_ms)
        self.assertEqual("off", chat.vp_mode)

    def test_transcript_gap_never_mutes_microphone(self):
        chat = self.make_chat()
        chat.phase = "listen"
        chat.in_text = "まだ話しています"
        chat.last_voice = time.time() - 5
        self.assertFalse(chat._input_muted())

    def test_audio_before_transcription_is_fully_played(self):
        chat = self.make_chat()
        player = FakePlayer()
        pcm = b"\x01\x02\x03\x04"
        content = {"modelTurn": {"parts": [{"inlineData": {
            "data": base64.b64encode(pcm).decode(),
        }}]}}
        with mock.patch.object(live.subprocess, "Popen", return_value=player):
            chat._process_server_content(content)
        self.assertEqual(pcm, bytes(player.stdin.data))
        self.assertEqual("play", chat.phase)
        chat._stop_player()

    def test_interrupted_message_still_processes_turn_complete(self):
        chat = self.make_chat()
        player = FakePlayer()
        chat._player = player
        chat._playing = True
        chat.phase = "play"
        chat.in_text = "おやすみ"
        chat.out_text = "おやすみなさい"
        chat._process_server_content({"interrupted": True, "turnComplete": True})
        self.assertTrue(player.killed)
        self.assertEqual("think", chat.phase)
        deadline = time.monotonic() + 1
        utterance = None
        while utterance is None and time.monotonic() < deadline:
            utterance = chat.pop_completed_turn()
            time.sleep(0.01)
        self.assertEqual("おやすみ", utterance)
        self.assertEqual("idle", chat.phase)

    def test_transcription_after_turn_complete_joins_same_turn(self):
        chat = self.make_chat()
        chat._process_server_content({"turnComplete": True})
        chat._process_server_content({
            "inputTranscription": {"text": "おやすみ"},
            "outputTranscription": {"text": "おやすみなさい"},
        })
        self.assertEqual("think", chat.phase)
        deadline = time.monotonic() + 1
        utterance = None
        while utterance is None and time.monotonic() < deadline:
            utterance = chat.pop_completed_turn()
            time.sleep(0.01)
        self.assertEqual("おやすみ", utterance)

    def test_late_transcript_rearms_grace_without_leaking_partial_text(self):
        chat = self.make_chat()
        chat._process_server_content({"turnComplete": True})
        time.sleep(live.TRANSCRIPT_GRACE_SEC * 0.8)
        chat._process_server_content({"inputTranscription": {"text": "おや"}})
        time.sleep(live.TRANSCRIPT_GRACE_SEC * 0.6)
        self.assertIsNone(chat.pop_completed_turn())
        chat._process_server_content({"inputTranscription": {"text": "すみ"}})
        deadline = time.monotonic() + 1
        utterance = None
        while utterance is None and time.monotonic() < deadline:
            utterance = chat.pop_completed_turn()
            time.sleep(0.01)
        self.assertEqual("おやすみ", utterance)

    def test_suspend_invalidates_running_finalize_timer(self):
        chat = self.make_chat()
        chat.in_text = "おやすみ"
        chat._process_server_content({"turnComplete": True})
        chat.suspend()
        time.sleep(live.TRANSCRIPT_GRACE_SEC + 0.1)
        self.assertIsNone(chat.pop_completed_turn())
        self.assertEqual("idle", chat.phase)

    def test_pending_turn_mutes_new_audio_until_finalized(self):
        chat = self.make_chat()
        chat._process_server_content({"turnComplete": True})
        self.assertTrue(chat._input_muted())
        chat._discard_turn()
        chat._clean_after = 0
        self.assertFalse(chat._input_muted())

    def test_explicit_wake_drains_queued_control_turn(self):
        chat = self.make_chat()
        chat._completed_turns.put("おやすみ")
        chat.cancel_pending_controls()
        self.assertIsNone(chat.pop_completed_turn())

    def test_wake_after_transcript_append_invalidates_old_control(self):
        chat = self.make_chat()
        real_speech = live._real_speech

        def wake_during_transcript(text):
            chat.cancel_pending_controls()
            return real_speech(text)

        with mock.patch.object(live, "_real_speech",
                               side_effect=wake_during_transcript):
            chat._process_server_content({
                "inputTranscription": {"text": "おやすみ"},
            })
        chat._process_server_content({"turnComplete": True})
        time.sleep(live.TRANSCRIPT_GRACE_SEC + 0.1)
        self.assertIsNone(chat.pop_completed_turn())

    def test_idle_wake_does_not_suppress_a_later_turn(self):
        chat = self.make_chat()
        chat.cancel_pending_controls()
        chat._process_server_content({
            "inputTranscription": {"text": "おやすみ"},
            "turnComplete": True,
        })
        deadline = time.monotonic() + 1
        utterance = None
        while utterance is None and time.monotonic() < deadline:
            utterance = chat.pop_completed_turn()
            time.sleep(0.01)
        self.assertEqual("おやすみ", utterance)

    def test_audio_stream_end_is_only_sent_after_long_pause(self):
        chat = self.make_chat()
        with mock.patch.object(chat, "_send", return_value=True) as send:
            chat._pause_input_stream()
            send.assert_not_called()
            chat._mute_started -= live.STREAM_END_AFTER_SEC + 0.01
            chat._pause_input_stream()
            send.assert_called_once_with({"realtimeInput": {"audioStreamEnd": True}})

    def test_stale_recorder_chunk_is_not_sent_after_reconnect(self):
        chat = self.make_chat()
        chat.connected = True

        class NewRecorder:
            terminated = False

            def terminate(self):
                self.terminated = True

        new_rec = NewRecorder()

        class OldPipe:
            @staticmethod
            def read(_size):
                # readが戻る前にsuspend/resume相当で録音プロセスが差し替わる。
                chat._rec = new_rec
                chat._stop.set()
                return b"OLD!"

        class OldRecorder:
            stdout = OldPipe()

            @staticmethod
            def poll():
                return None

        chat._rec = OldRecorder()
        with mock.patch.object(chat, "_send", return_value=True) as send:
            chat._sender_loop()
        send.assert_not_called()
        self.assertFalse(new_rec.terminated)

    def test_goaway_closes_only_after_turn_finalization(self):
        chat = self.make_chat()
        ws = FakeWebSocket()
        chat._ws = ws
        chat._goaway = True
        chat._process_server_content({"turnComplete": True})
        self.assertFalse(ws.closed)
        deadline = time.monotonic() + 1
        while not ws.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ws.closed)

    def test_suspend_can_kill_player_while_finish_waits(self):
        chat = self.make_chat()

        class BlockingPlayer:
            def __init__(self):
                self.stdin = FakePipe()
                self.killed = False
                self.wait_started = threading.Event()
                self.released = threading.Event()

            def wait(self, timeout=None):
                self.wait_started.set()
                if not self.released.wait(timeout):
                    raise live.subprocess.TimeoutExpired("aplay", timeout)
                return 0

            def kill(self):
                self.killed = True
                self.released.set()

        player = BlockingPlayer()
        chat._player = player
        chat._playing = True
        chat.phase = "play"
        finishing = threading.Thread(target=chat._finish_turn)
        finishing.start()
        self.assertTrue(player.wait_started.wait(1))
        chat.suspend()
        finishing.join(timeout=1)
        self.assertTrue(player.killed)
        self.assertFalse(finishing.is_alive())

    def test_configuration_is_clamped_to_safe_ranges(self):
        chat = self.make_chat(LIVE_CHUNK_MS="1", LIVE_SILENCE_MS="100")
        self.assertEqual(40, chat.chunk_ms)
        self.assertEqual(500, chat.silence_ms)


if __name__ == "__main__":
    unittest.main()
