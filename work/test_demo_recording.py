"""Real local encoder smoke test with synthetic pictures/audio; no HDMI or API."""
import audioop,json,math,sys,tempfile,time,unittest,wave
from array import array
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from PIL import Image
from demo_recording import DemoRecording,PCMTrack

class DemoTests(unittest.TestCase):
    def test_delivery_jitter_never_inserts_gaps_or_removes_audio(self):
        for rate in (24000,48000):
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp)/'jitter.wav';block=b'\x12\x03'*960
                with wave.open(str(p),'wb') as w:
                    w.setparams((2,2,rate,0,'NONE',''))
                    track=PCMTrack(w,1,rate,True)
                    for i in range(100):
                        stamp=1+i*480/rate+(0 if i==0 else (.013 if i%2 else -.004))
                        track.write(block,stamp)
                with wave.open(str(p),'rb') as w:
                    self.assertEqual(w.readframes(w.getnframes()),block*100)

    def test_offsets_preserve_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'voice.wav'
            with wave.open(str(p),'wb') as w:
                w.setparams((2,2,24000,0,'NONE',''))
                DemoRecording.put(w,b'\1\0'*480,1.5,1,24000)
            with wave.open(str(p),'rb') as w:
                self.assertEqual(w.getnframes(),12240)
                self.assertEqual(w.readframes(12000),b'\0'*48000)

    def test_real_mp4_encoder_and_stop(self):
        class Capture:
            def snapshot(self):return (1,time.monotonic(),Image.new('RGB',(1280,720),'navy'))
        class Source:
            def read(self,stop,size):
                stop.wait(.1)
                samples=array('h',(int(2500*math.sin(i*.1)) for i in range(4800)))
                return audioop.tostereo(samples.tobytes(),2,1,1)
            def close(self):pass
        class Mixer:recording_sink=None
        events=[]
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            r=DemoRecording(Capture(),Mixer(),lambda *e:events.append(e),Source)
            r.start(tmp)
            time.sleep(.8)
            r.timeline_event(dict(phase='subtitle',text='Пример',speaker='Кэт',
                                  source_stamp=time.monotonic(),at_monotonic=time.monotonic()))
            r.timeline_event(dict(phase='playback_started',text='Пример',speaker='Кэт',
                                  voice_id='test',audio_seconds=.2,at_monotonic=time.monotonic()))
            if r.mixer.recording_sink:r.mixer.recording_sink(b'\x80\x02'*9600)
            time.sleep(.8);r.close()
            self.assertIsNone(r.error,events)
            target=r.folder/'GameVoice.mp4'
            self.assertTrue(target.is_file(),events)
            self.assertGreater(target.stat().st_size,2000)
            self.assertIn('Кэт: Пример',(r.folder/'subtitles-voice.srt').read_text(encoding='utf-8-sig'))
            self.assertIn('Пример',(r.folder/'clips.csv').read_text(encoding='utf-8-sig'))
            self.assertIn('✓ Видео сохранено',events[-1][1])
            # Decode the final movie, not just a file-exists check.
            import subprocess
            from gamevoice import ffmpeg_path,HIDDEN
            p=subprocess.run([ffmpeg_path(),'-v','error','-i',str(target),'-f','null','-'],
                             capture_output=True,creationflags=HIDDEN,timeout=20)
            self.assertEqual(p.returncode,0,p.stderr)

if __name__=='__main__':unittest.main()
