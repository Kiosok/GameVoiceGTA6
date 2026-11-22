import struct,sys,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from radio_mixer import AudioMixer,OUTPUT_LATENCY
from service_health import ServiceHealth

class OutputTests(unittest.TestCase):
    def test_underflow_is_counted_without_dropping_current_audio(self):
        mixer=AudioMixer();raw=struct.pack('<h',2000)*960
        packet={'data':raw,'offset':0,'started':threading.Event(),'done':threading.Event()}
        mixer.character=packet;out=bytearray(len(raw))
        with patch('radio_mixer.time.monotonic',return_value=12.5):
            mixer.callback(out,480,None,SimpleNamespace(output_underflow=True))
        self.assertEqual(out,raw);self.assertTrue(packet['done'].is_set())
        diag=mixer.diagnostics();self.assertEqual(diag['underflows'],1)
        self.assertEqual(diag['last_underflow_monotonic'],12.5)
        mixer.callback(bytearray(1920),480,None,None)
        self.assertEqual(mixer.diagnostics()['underflows'],1)

    def test_scheduling_gap_alone_is_not_reported_as_underflow(self):
        mixer=AudioMixer()
        with patch('radio_mixer.time.monotonic',side_effect=[10,10.032]):
            # Each callback also reads time in _priority only when earlier terms are false.
            mixer.character_pending=1
            for _ in range(2):mixer.callback(bytearray(1920),480,None,None)
        self.assertAlmostEqual(mixer.diagnostics()['callback_gap_max_ms'],32)
        self.assertEqual(mixer.diagnostics()['underflows'],0)

    def test_fixed_reserve_and_single_stream_for_all_phrases(self):
        class Stream:
            latency=.12;active=True
            def __init__(self,**kw):self.kw=kw;self.starts=0
            def start(self):self.starts+=1
            def abort(self):pass
            def close(self):pass
        with patch('radio_mixer.sd.RawOutputStream',Stream),patch('radio_mixer.output_index',return_value=5):
            mixer=AudioMixer('station');mixer.ensure_stream();original=mixer.stream
            for _ in range(5):mixer.ensure_stream()
            self.assertIs(mixer.stream,original);self.assertEqual(original.starts,1)
            self.assertEqual(original.kw['latency'],OUTPUT_LATENCY)
            self.assertEqual(original.kw['blocksize'],480)
            self.assertEqual(mixer.diagnostics()['latency_ms'],120);mixer.close()

    def test_yellow_warning_survives_success_and_expires_without_extra_requests(self):
        now=[10.0];health=ServiceHealth('ElevenLabs',lambda:now[0])
        health.event('speech_enabled',True);health.event('synthesis',{})
        health.event('audio_output',{'underflows':1})
        self.assertEqual(health.view()[0],'waiting')
        health.event('synthesis',{});self.assertEqual(health.view()[0],'waiting')
        now[0]+=11;health.event('audio_output',{'underflows':1})
        self.assertEqual(health.view()[0],'ok')
        health.event('audio_output',{'underflows':2});self.assertEqual(health.view()[0],'waiting')
        health.event('output_error','Колонка недоступна');self.assertEqual(health.view()[0],'error')

if __name__=='__main__':unittest.main()
