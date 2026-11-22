import base64
import io
import json
from pathlib import Path
import queue
import struct
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/GameVoice"))
import gamevoice as g


class AudioTests(unittest.TestCase):
    def test_bounded_fifo_preserves_pcm_and_underflow(self):
        buf = g.PCMBuffer(16)
        self.assertTrue(buf.put(b"abcd" * 3))
        self.assertFalse(buf.put(b"efgh" * 2))
        self.assertEqual(buf.get(8), (b"abcdabcd", 8))
        self.assertEqual(buf.get(8), (b"abcd", 4))
        self.assertEqual(buf.size, 0)

    def test_mixer_gain_duck_and_no_feedback(self):
        engine = g.Engine()
        engine.original_gain = engine.translation_gain = 1
        original = struct.pack("<hh", 10000, 10000) * 10
        voice = struct.pack("<hh", 2000, 2000) * 10
        engine.original.put(original)
        engine.translated.put(voice)
        out = bytearray(40)
        engine.callback(out, 10, None, None)
        self.assertEqual(struct.unpack("<h", out[:2])[0], 5000)
        self.assertEqual(engine.capture_bytes, 0)
        engine.abort_event.set()
        engine.callback(out, 10, None, None)
        self.assertEqual(out, bytes(40))

    def test_translation_event_resamples_to_output(self):
        events = []
        engine = g.Engine(emit=lambda *event: events.append(event))
        pcm = struct.pack("<h", 1200) * 2400
        engine.handle_event({"type": "session.output_audio.delta", "delta": base64.b64encode(pcm).decode()})
        self.assertGreater(engine.translated.size, 19000)
        self.assertLessEqual(engine.translated.size, 19200)
        engine.handle_event({"type": "session.output_transcript.delta", "delta": "Привет"})
        self.assertIn(("ru", "Привет"), events)

    def test_audio_backlog_stops_instead_of_unbounded_delay(self):
        engine = g.Engine()
        engine.translated = g.PCMBuffer(4)
        pcm = bytes(2400)
        with self.assertRaisesRegex(RuntimeError, "8 секунд"):
            engine.handle_event({"type": "session.output_audio.delta", "delta": base64.b64encode(pcm).decode()})

    def test_api_errors_and_secret_redaction(self):
        engine = g.Engine(key="sk-private-test")
        with self.assertRaisesRegex(RuntimeError, "Недостаточно"):
            engine.handle_event({"type": "error", "error": {"code": "insufficient_quota"}})
        engine.fail("Failure sk-private-test")
        self.assertNotIn("sk-private-test", engine.failure)


class FakePipe:
    def read(self, size):
        time.sleep(.02)
        return bytes(size)
    def close(self):
        pass


class FakeProcess:
    def __init__(self, *args, **kwargs):
        self.stdout, self.stderr = FakePipe(), io.BytesIO()
        self.returncode = None
    def poll(self):
        return self.returncode
    def terminate(self):
        self.returncode = 0
    kill = terminate
    def wait(self, timeout=None):
        return self.returncode


class FakeStream:
    def __init__(self, **kwargs):
        pass
    def start(self):
        pass
    def abort(self):
        pass
    def close(self):
        pass


class FakeSocket:
    def __init__(self):
        self.incoming = queue.Queue()
        self.incoming.put({"type": "session.created"})
        self.sent = []
    def connect(self, url, **kwargs):
        self.url = url
    def settimeout(self, timeout):
        pass
    def send(self, value):
        event = json.loads(value)
        self.sent.append(event)
        if event["type"] == "session.update":
            self.incoming.put({"type": "session.updated"})
        elif event["type"] == "session.input_audio_buffer.append":
            self.incoming.put({"type": "session.output_transcript.delta", "delta": "Тест"})
        elif event["type"] == "session.close":
            self.incoming.put({"type": "session.closed"})
    def recv(self):
        try:
            return json.dumps(self.incoming.get(timeout=.1))
        except queue.Empty:
            raise g.websocket.WebSocketTimeoutException()
    def abort(self):
        self.incoming.put({"type": "session.closed"})
    def close(self, **kwargs):
        pass


class LifecycleTests(unittest.TestCase):
    def run_engine(self, translated):
        sock = FakeSocket()
        events = []
        engine = g.Engine(key="sk-test", translate=translated, emit=lambda *e: events.append(e))
        with patch.object(g.sd, "RawOutputStream", FakeStream), \
             patch.object(g.subprocess, "Popen", FakeProcess), \
             patch.object(g.websocket, "WebSocket", return_value=sock):
            thread = threading.Thread(target=engine.run)
            thread.start()
            deadline = time.monotonic() + 3
            while engine.capture_bytes == 0 and time.monotonic() < deadline:
                time.sleep(.01)
            time.sleep(.04)
            engine.stop()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertIsNone(engine.failure)
            self.assertGreater(engine.capture_bytes, 0)
            self.assertEqual(engine.proc.poll(), 0)
            self.assertTrue(all(not t.is_alive() for t in engine.workers))
            self.assertEqual(events[-1], ("done", None))
            self.assertEqual(engine.key, "")
        return sock, events

    def test_monitor_never_connects_or_sends(self):
        sock, events = self.run_engine(False)
        self.assertEqual(sock.sent, [])

    def test_translation_protocol_order_language_and_graceful_stop(self):
        sock, events = self.run_engine(True)
        self.assertEqual(sock.url, g.URL)
        self.assertEqual(sock.sent[0]["session"]["audio"]["output"]["language"], "ru")
        self.assertEqual(sock.sent[1]["type"], "session.input_audio_buffer.append")
        self.assertEqual(len(base64.b64decode(sock.sent[1]["audio"])), 4800)
        self.assertEqual(sock.sent[-1]["type"], "session.close")
        self.assertTrue(any(e[0] == "ru" for e in events))

    def test_missing_key_cleans_up_without_network(self):
        with patch.object(g.sd, "RawOutputStream", FakeStream), patch.object(g.websocket, "WebSocket") as ws:
            engine = g.Engine(translate=True)
            engine.run()
            ws.assert_not_called()
            self.assertIn("API-ключ", engine.failure)


if __name__ == "__main__":
    unittest.main(verbosity=2)
