import audioop
import io
import json
from pathlib import Path
import queue
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs/GameVoice'))
import radio_source
from radio_source import RecordingTail
from radio_mixer import AudioMixer
from radio_translation import RadioController, RadioHistory


def pcm(value, frames):
    return struct.pack('<h', value) * frames


class RadioTests(unittest.TestCase):
    def write_wav(self, path, data):
        with wave.open(str(path), 'wb') as out:
            out.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
            out.writeframes(data)

    def test_live_tail_skips_history_and_follows_rollover_without_sample_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'audio-0000.wav'
            out = wave.open(str(path), 'wb')
            out.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
            out.writeframes(pcm(11, 200))
            out._file.flush()
            source = RecordingTail(root)
            source._open(0, True)
            fresh = pcm(22, 100)
            out.writeframes(fresh); out.close()
            next_part = pcm(33, 100)
            self.write_wav(root / 'audio-0001.wav', next_part)
            stop = threading.Event()
            try:
                self.assertEqual(source.read(stop), fresh)
                self.assertEqual(source.read(stop), next_part)
                self.assertEqual(source.index, 1)
            finally:
                source.close()

    def test_slow_reader_returns_to_live_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = wave.open(str(root / 'audio-0000.wav'), 'wb')
            out.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
            out.writeframes(pcm(1, 2))
            out._file.flush()
            source = RecordingTail(root); source._open(0, True)
            out.writeframes(pcm(7, 192000)); out.close()
            try:
                self.assertEqual(source.read(threading.Event()), pcm(7, 19200))
                self.assertGreater(source.skipped_bytes, 0)
            finally:
                source.close()

    def test_recording_is_reused_instead_of_second_hdmi_capture(self):
        with patch.object(radio_source, 'active_session', return_value=Path('mock-session')), \
             patch.object(radio_source, 'DirectHDMI') as direct:
            source = radio_source.live_source()
            self.assertIsInstance(source, RecordingTail)
            direct.assert_not_called()

    def test_continuous_radio_preserves_every_sample_across_packets(self):
        mixer = AudioMixer(); mixer.radio_gain = 1
        result, expected = bytearray(), bytearray()
        for value in (1000, 2000, 3000, 4000):
            packet = pcm(value, 4800)
            expected.extend(audioop.tostereo(packet, 2, 1, 1))
            # Deliberately split below network-packet boundaries.
            mixer.feed_radio(packet[:2000]); mixer.feed_radio(packet[2000:])
            for _ in range(10):
                data = bytearray(480 * 4)
                mixer.callback(data, 480, None, None)
                result.extend(data)
        self.assertEqual(result, expected)
        self.assertEqual(mixer.radio_played_bytes, len(expected)//2)
        self.assertEqual(mixer.radio_underflows, 0)
        mixer.close()

    def test_radio_yields_at_pause_then_character_plays_without_overlap(self):
        mixer = AudioMixer(); mixer.radio_gain = 1
        mixer.ensure_stream = lambda: None
        valid = lambda: True
        began = threading.Event()
        mixer.feed_radio(pcm(1000, 480 * 4) + pcm(0, 480 * 3) + pcm(3000, 480 * 5))
        mixer.callback(bytearray(480 * 4), 480, None, None)
        mixer.set_pending(1)
        character = threading.Thread(target=lambda: mixer.play_character(pcm(2000, 480 * 2), 1, valid, began.set))
        character.start()
        deadline = time.monotonic() + 1
        while mixer.character is None and time.monotonic() < deadline: time.sleep(.002)
        rendered = []
        for i in range(8):
            data = bytearray(480 * 4)
            mixer.callback(data, 480, None, None)
            rendered.append(struct.unpack('<h', data[:2])[0])
        character.join(1)
        self.assertEqual(rendered, [1000]*3 + [0]*3 + [2000]*2)
        self.assertTrue(began.is_set())
        self.assertFalse(character.is_alive())
        self.assertFalse(mixer.feed_radio(pcm(4000, 480)))
        self.assertEqual(mixer.radio_buffer_ms, 0)
        mixer.close()

    def test_underrun_rebuffers_instead_of_dropping_later_words(self):
        mixer = AudioMixer(); mixer.radio_gain = 1
        mixer.feed_radio(pcm(1000, 4800))
        for _ in range(11): mixer.callback(bytearray(1920), 480, None, None)
        self.assertEqual(mixer.radio_underflows, 1)
        mixer.feed_radio(pcm(3000, 4800))
        data = bytearray(1920); mixer.callback(data, 480, None, None)
        self.assertEqual(struct.unpack('<h', data[:2])[0], 3000)
        mixer.close()

    def test_overflow_is_explicit_and_never_silently_deletes_words(self):
        mixer = AudioMixer()
        mixer.feed_radio(pcm(1000, 24000*8))
        before = bytes(mixer.radio_stream)
        with self.assertRaises(RuntimeError): mixer.feed_radio(pcm(2000, 480))
        self.assertEqual(mixer.radio_stream, before)
        mixer.close()

    def test_cancelled_character_is_removed_from_the_mixer(self):
        mixer = AudioMixer(); mixer.ensure_stream = lambda: None
        self.assertFalse(mixer.play_character(pcm(1000, 480), 1, lambda: False, lambda: None))
        self.assertIsNone(mixer.character)
        mixer.close()

    def test_history_append_only_deltas_keep_spaces_and_rotate(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = RadioHistory(Path(tmp) / 'history.jsonl')
            history.append('ru', 'Добрый'); history.append('ru', ' вечер!')
            self.assertEqual(history.read()['ru'], 'Добрый вечер!')
            history.path.write_bytes(b' ' * 512001)
            history.append('ru', 'Новый эфир')
            self.assertTrue(history.path.with_suffix('.previous.jsonl').exists())
            self.assertEqual(history.read()['ru'], 'Новый эфир')

    def test_radio_does_not_open_a_paid_session_during_dialogue(self):
        mixer = AudioMixer(); mixer.set_dialogue(True)
        source = Mock(description='mock')
        def read(stop):
            stop.wait(.02)
            return pcm(1000, 4800 * 2)
        source.read.side_effect = read
        sockets = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            controller = RadioController(mixer, lambda *a: None,
                source_factory=lambda: source, socket_factory=sockets,
                key_loader=lambda: 'never-print-this', history=RadioHistory(Path(tmp) / 'history'))
            try:
                controller.set_enabled(True)
                time.sleep(.4)
                sockets.assert_not_called()
                self.assertEqual(controller.input_seconds, 0)
            finally:
                controller.close(); mixer.close()

    def test_unstarted_buffer_is_not_replayed_after_dialogue(self):
        mixer = AudioMixer()
        mixer.feed_radio(pcm(1000, 2400))
        mixer.set_dialogue(True)
        mixer.callback(bytearray(1920), 480, None, None)
        self.assertEqual(mixer.radio_buffer_ms, 0)
        self.assertEqual(mixer.radio_played_bytes, 0)
        mixer.close()

    def test_exhausted_balance_is_fatal_and_does_not_trigger_reconnects(self):
        controller = RadioController.__new__(RadioController)
        with self.assertRaises(RuntimeError) as failure:
            controller._handle({'type':'error','error':{'code':'credit_balance_exhausted'}}, 1, 'secret')
        self.assertFalse(failure.exception.retryable)
        self.assertIn('OpenAI', str(failure.exception))
        self.assertNotIn('secret', str(failure.exception))


if __name__ == '__main__': unittest.main()
