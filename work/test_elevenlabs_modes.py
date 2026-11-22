import json,sys,time,threading,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from elevenlabs_speech import (MODEL,DEFAULT_MODEL,MODEL_LABELS,ElevenLabsSpeechWorker,
                              V4QualityClient,create_client,selected_model)
from test_elevenlabs import Connection,Response
from openai_speech import SpeechFailure
VOICE='ZXTnBsrRUD3CsadzpNOR'

class ModesTests(unittest.TestCase):
    def test_switch_during_request_preserves_retry_model_and_playback_order(self):
        calls=[];played=[];events=[]
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        class Client:
            key='fake';first_chunk_ms=1;ws=None;needs_reconnect=False
            def __init__(self,model):self.model=model
            def maintain(self):pass
            def close(self):self.ws=None
            def abort(self):pass
            def synthesize(self,text,number):
                calls.append((self.model,number))
                if len(calls)==1:
                    entered.set()
                    if not release.wait(2):raise RuntimeError('test synchronization timed out')
                    raise ConnectionError('disconnect')
                return (b'A' if number==1 else b'B')*960,''
        class Output:
            def __init__(self,**kwargs):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def write(self,data):
                played.append(data)
                if len(played)==2:finished.set()
            def abort(self):pass
        with patch('elevenlabs_speech.create_client',side_effect=lambda model,*a:Client(model)),\
             patch('elevenlabs_speech.load_key',return_value='fake'),\
             patch('openai_speech.sd.RawOutputStream',Output),\
             patch('openai_speech.change_tempo',side_effect=lambda pcm,*a:pcm):
            worker=ElevenLabsSpeechWorker(lambda *e:events.append(e),VOICE);worker.volume=1
            try:
                worker.set_enabled(True);worker.submit('Первая реплика.',time.monotonic(),VOICE,'Джейсон')
                self.assertTrue(entered.wait(2));epoch=worker.epoch
                worker.set_model('eleven_v4');worker.submit('Вторая реплика.',time.monotonic(),VOICE,'Джейсон')
                self.assertEqual(worker.epoch,epoch);release.set()
                self.assertTrue(finished.wait(5))
                self.assertEqual(calls,[(DEFAULT_MODEL,1),(DEFAULT_MODEL,1),('eleven_v4',2)])
                self.assertEqual(played,[b'A'*960,b'B'*960])
                routes=[e[1] for e in events if e[0] in ('voice_route','voice_audit')]
                for row in routes:self.assertEqual(row['model_id'],DEFAULT_MODEL if row['cue']==1 else 'eleven_v4')
            finally:release.set();worker.close()

    def test_quality_uses_dialogue_endpoint_body_only_and_same_clone(self):
        connection=Connection([Response([b'abcd']),Response([b'efgh'])])
        client=V4QualityClient('fake',VOICE,lambda *a,**k:connection)
        for text in ('Привет!','Блядь, иди сюда!'):
            self.assertTrue(client.synthesize(text)[0])
            args,kwargs=connection.requests[-1]
            self.assertEqual(args[1],'/v1/text-to-dialogue/stream?output_format=pcm_24000')
            self.assertEqual(json.loads(kwargs['body']),{
                'inputs':[{'text':text,'voice_id':VOICE}], 'model_id':'eleven_v4','language_code':'ru'})
        self.assertEqual(len(connection.requests),2)

    def test_quality_never_returns_incomplete_audio_or_follows_redirect(self):
        for response in (Response([b'a']),Response([b'ab'],{'Content-Type':'audio/pcm','Content-Length':'4'}),
                         Response([],{'Location':'https://geo-restriction.elevenlabs.io/'},302)):
            connection=Connection([response]);client=V4QualityClient('fake',VOICE,lambda *a,**k:connection)
            with self.assertRaises(SpeechFailure):client.synthesize('Фраза')
            self.assertTrue(connection.closed);self.assertEqual(len(connection.requests),1)

    def test_switch_keeps_queued_phrase_model_character_and_retry_client(self):
        events=[]
        with patch('openai_speech.threading.Thread'),patch('elevenlabs_speech.load_key',return_value='fake'):
            worker=ElevenLabsSpeechWorker(lambda *e:events.append(e),VOICE)
        worker.enabled.set();now=time.monotonic()
        worker.submit('Джейсон: Первая фраза.',now,VOICE,'Джейсон')
        worker.set_model('eleven_v4')
        worker.submit('Вторая фраза.',now+.2,VOICE,'Джейсон')
        first,second=worker.pending.get_nowait(),worker.pending.get_nowait()
        self.assertEqual((first.model_id,second.model_id),(DEFAULT_MODEL,'eleven_v4'))
        self.assertEqual((first.voice,second.voice),(VOICE,VOICE))
        self.assertEqual(first.text,'Первая фраза')
        c1=worker.client_for_cue(first);c2=worker.client_for_cue(second)
        self.assertIsNot(c1,c2);self.assertIs(worker.client_for_cue(first),c1)
        for cue in (first,second):worker.route_event(cue,'request')
        routes=[e[1] for e in events if e[0]=='voice_route']
        self.assertEqual([r['model_id'] for r in routes],[DEFAULT_MODEL,'eleven_v4'])
        worker.set_model(MODEL);worker.submit('Третья фраза.',now+.4,VOICE,'Джейсон')
        third=worker.pending.get_nowait();self.assertEqual(third.model_id,MODEL)
        self.assertEqual(worker.client_for_cue(third).__class__.__name__,'ElevenLabsClient')

    def test_saved_mode_validation_and_no_speculative_requests(self):
        for value,expected in (('eleven_v4','eleven_v4'),(MODEL,MODEL),('unknown',DEFAULT_MODEL),([],DEFAULT_MODEL)):
            with patch('device_settings.settings',return_value={'elevenlabs_model':value}):
                self.assertEqual(selected_model(),expected)
        for model in MODEL_LABELS:
            client=create_client(model,'fake',VOICE)
            self.assertIsNone(client.ws);client.close()
        with self.assertRaises(ValueError):create_client('bad','fake',VOICE)

if __name__=='__main__':unittest.main()
