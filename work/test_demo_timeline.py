import csv,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from demo_timeline import DemoTimeline


class TimelineTests(unittest.TestCase):
    def test_source_and_actual_playback_are_separate_and_clipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors=[];t=DemoTimeline(tmp,100,lambda *e:errors.append(e))
            subtitle=dict(phase='subtitle',at_monotonic=101,source_stamp=100.5,speaker='Кэт',text='Удачи')
            t.append(subtitle);t.append(subtitle)
            t.append(dict(phase='request_failed',at_monotonic=102,text='Ошибка'))
            t.append(dict(phase='playback_started',at_monotonic=103,source_stamp=100.5,
                          speaker='Кэт',text='Удачи',voice_id='cat',model_id='example',audio_seconds=4))
            t.close(5)
            p=Path(tmp)
            self.assertEqual(errors,[])
            with (p/'clips.csv').open(encoding='utf-8-sig') as stream:
                rows=list(csv.DictReader(stream))
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['start_seconds'],'3')
            self.assertEqual(rows[0]['end_seconds_estimate'],'5')
            self.assertEqual(rows[0]['subtitle_seconds'],'0.5')
            self.assertEqual(rows[0]['voice_id'],'cat')
            self.assertIn('00:00:03,000 --> 00:00:05,000',(p/'subtitles-voice.srt').read_text(encoding='utf-8-sig'))
            game=(p/'subtitles-game.srt').read_text(encoding='utf-8-sig')
            self.assertEqual(game.count('Кэт: Удачи'),1)
            self.assertIn('00:00:00,500',game)
            events=[json.loads(line) for line in (p/'timeline.jsonl').read_text(encoding='utf8').splitlines()]
            self.assertEqual(events[-1]['duration_seconds'],5)
            self.assertTrue(any(e.get('phase')=='request_failed' for e in events))

    def test_sidecar_failure_is_reported_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked=Path(tmp)/'file';blocked.write_text('x')
            errors=[];t=DemoTimeline(blocked,0,lambda *e:errors.append(e));t.close(1)
            self.assertEqual(len(errors),1)

if __name__=='__main__':unittest.main()
