import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs/GameVoice'))
from PIL import Image
import voice_samples as samples
import subtitle_voice as game
from speaker_voices import AcceptedSubtitle


class VoiceSampleTests(unittest.TestCase):
    def test_continuous_rollover_preserves_every_sample_with_one_capture(self):
        # Scale a 45-minute boundary down to 0.1 s; feed three whole parts.
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / 'session'
            pcm = b''.join(bytes([i, 0, i + 1, 0]) * 4800 for i in (10, 30, 50))
            class Pipe(io.BytesIO):
                def read(self, n):
                    result = super().read(n)
                    if self.tell() == len(pcm):
                        (session / 'STOP').touch()
                    return result
            proc = Mock()
            proc.stdout = Pipe(pcm)
            with patch.object(samples, 'CONTROL', Path(tmp) / 'control.json'), \
                 patch.object(samples.shutil, 'disk_usage', return_value=Mock(free=10 * 1024**3)), \
                 patch.object(samples, 'ffmpeg_path', return_value='mock-ffmpeg'), \
                 patch.object(samples.subprocess, 'Popen', return_value=proc) as launch:
                samples.record_audio(session, minutes=1 / 600, continuous=True)
                self.assertEqual(launch.call_count, 1)
                self.assertFalse(json.loads(samples.CONTROL.read_text(encoding='utf8'))['active'])
            parts = sorted(session.glob('audio-*.wav'))
            self.assertEqual(len(parts), 3)
            combined = bytearray()
            for part in parts:
                with samples.wave.open(str(part), 'rb') as wav:
                    self.assertEqual(wav.getnframes(), 4800)
                    self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (2, 2, 48000))
                    combined.extend(wav.readframes(wav.getnframes()))
            self.assertEqual(combined, pcm)
            meta = json.loads((session / 'session.json').read_text(encoding='utf8'))
            self.assertTrue(meta['continuous'])
            self.assertIsNone(meta['limit_minutes'])
            self.assertEqual(json.loads((session / 'status.json').read_text(encoding='utf8'))['state'], 'finished')
            proc.terminate.assert_called_once()

    def test_no_recording_starts_without_disk_space(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(samples.shutil, 'disk_usage', return_value=Mock(free=1024**3)), \
             patch.object(samples.subprocess, 'Popen') as launch:
            with self.assertRaises(RuntimeError):
                samples.record_audio(Path(tmp), continuous=True)
            launch.assert_not_called()

    def test_inactive_session_keeps_no_caption_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(samples, 'CONTROL', Path(tmp) / 'control.json'):
            sink = samples.SubtitleSampleSink()
            sink.observe(time.monotonic(), 1, Image.new('RGB', (100, 100)),
                         (0, 0, 1, 1), 'secret', [('Джейсон', 'Привет')], [])
            sink.close()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_multiple_speakers_and_blank_transition_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / 'session'
            with patch.object(samples, 'CONTROL', Path(tmp) / 'control.json'):
                samples.write_json(samples.CONTROL, {'active': True, 'session': str(session),
                                                      'expires_unix': time.time() + 60})
                sink = samples.SubtitleSampleSink()
                stamp = time.monotonic()
                image = Image.new('RGB', (100, 100))
                sink.observe(stamp, 1, image, (.1, .7, .9, .9), 'raw',
                    [('Джейсон', 'Привет'), ('Брайан', 'Ага')],
                    [AcceptedSubtitle('Привет', 'джейсон', stamp)])
                sink.last_observation = 0
                sink.observe(stamp + 1, 2, image, (.1, .7, .9, .9), '', [], [])
                sink.close()
                rows = [json.loads(x) for x in (session / 'captions.jsonl').read_text(encoding='utf8').splitlines()]
                self.assertEqual(len(rows[0]['groups']), 2)
                self.assertEqual(rows[0]['accepted'][0]['speaker'], 'джейсон')
                self.assertEqual(rows[0]['attribution'], 'unverified_caption_visibility')
                self.assertEqual(rows[1]['groups'], [])
                with Image.open(session / rows[0]['subtitle_image']) as crop:
                    self.assertEqual(crop.size, (80, 20))

    def test_observer_records_without_changing_speech_events(self):
        events = []
        observer = game.Observer(lambda *args: events.append(args))
        observer.capture = Mock()
        image = Image.new('RGB', (1280, 720))
        frames = iter([(1, time.monotonic(), image)])
        def next_frame(last):
            value = next(frames, None)
            if value is None:
                observer.stop_event.set()
            return value
        observer.capture.wait_for_frame.side_effect = next_frame
        async def recognize(*args,**kwargs):
            return 'Джейсон: Привет', [], 1
        ocr = Mock()
        ocr.recognize = recognize
        cue = AcceptedSubtitle('Привет', 'джейсон', time.monotonic())
        observer.gate.update_frame = Mock(return_value=[cue])
        with patch.object(game, 'RussianOCR', return_value=ocr), patch.object(game, 'SubtitleSampleSink') as sink:
            observer.run()
            sink.return_value.observe.assert_called_once()
            sink.return_value.close.assert_called_once()
        self.assertIn(('subtitle', cue), events)
        self.assertIsNone(observer.failure)


if __name__ == '__main__':
    unittest.main()
