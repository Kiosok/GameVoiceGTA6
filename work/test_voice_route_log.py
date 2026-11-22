import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from voice_route_log import VoiceRouteLog
from openai_speech import Cue, OpenAISpeechWorker


class RouteLogTests(unittest.TestCase):
    def test_full_text_retries_phases_and_runs_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)/'session'
            errors = []
            for _ in range(2):
                log = VoiceRouteLog(Path(tmp)/'fallback', lambda: session, 'elevenlabs', errors.append)
                worker = OpenAISpeechWorker.__new__(OpenAISpeechWorker)
                worker.emit = lambda kind, value: log.append(value, 'revision160')
                cue = Cue('Длинная реплика. '*50, time.monotonic(), 3, 7, 'jason-id', 'Джейсон')
                for phase, attempt in [('request',1),('request_failed',1),('request',2),
                                       ('audio_received',2),('playback_started',2)]:
                    worker.route_event(cue, phase, attempt=attempt)
                log.close()
            rows = [json.loads(line) for line in (session/'voice-routing.jsonl').read_text(encoding='utf8').splitlines()]
            self.assertEqual(len(rows),10)
            self.assertEqual(len({row['run_id'] for row in rows}),2)
            self.assertTrue(all(row['text']==cue.text and row['voice_id']=='jason-id' for row in rows))
            self.assertEqual(rows[3]['phase'],'audio_received')
            self.assertAlmostEqual(rows[0]['source_unix'], time.time(), delta=2)
            self.assertEqual(errors,[])

    def test_slow_disk_does_not_block_producer_and_fallback_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            entered, release = threading.Event(), threading.Event()
            def session():
                entered.set(); release.wait(2); return None
            log = VoiceRouteLog(tmp, session, 'elevenlabs', self.fail)
            log.append({'at_unix':time.time(), 'text':'Привет'})
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            log.append({'at_unix':time.time(), 'text':'Пока'})
            self.assertLess(time.monotonic()-started,.1)
            release.set(); log.close()
            files = list(Path(tmp).rglob('*.jsonl'))
            self.assertEqual(len(files),1)
            self.assertEqual(len(files[0].read_text(encoding='utf8').splitlines()),2)

    def test_write_error_reported_without_crashing_speech(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp)/'file'; bad.write_text('not a directory')
            errors=[]
            log = VoiceRouteLog(bad, lambda: None, 'elevenlabs', errors.append)
            log.append({'at_unix':time.time()}); log.close()
            self.assertEqual(len(errors),1)


if __name__=='__main__': unittest.main()
