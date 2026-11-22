import base64,json,sys,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from elevenlabs_v4 import V4Client
from openai_speech import SpeechFailure, OpenAISpeechWorker
VOICE='ZXTnBsrRUD3CsadzpNOR'
def audio(data):return {'audio':base64.b64encode(data).decode()}
class Socket:
    def __init__(self,events):self.events=iter(events);self.sent=[];self.closed=False
    def send(self,msg):self.sent.append(json.loads(msg))
    def recv(self):
        item=next(self.events,'')
        if isinstance(item,Exception):raise item
        return json.dumps(item) if isinstance(item,dict) else item
    def close(self):self.closed=True
    def abort(self):self.closed=True
class V4Tests(unittest.TestCase):
    def test_two_short_turns_reuse_socket_and_flush_without_speaking_labels(self):
        ws=Socket([audio(b'ab'),audio(b'cd'),{'is_final_audio_for_turn':True},
                   audio(b'ef'),{'is_final_audio_for_turn':True}]);calls=[]
        def connect(*args,**kwargs):calls.append((args,kwargs));return ws
        c=V4Client('fake',VOICE,connect);c.maintain();self.assertEqual(calls,[])
        self.assertEqual(c.synthesize('Привет!')[0],b'abcd')
        self.assertEqual(c.synthesize('Пока!')[0],b'ef')
        self.assertEqual(len(calls),1);self.assertEqual(ws.sent[0],{'voices':[VOICE]})
        self.assertEqual(ws.sent[1]['inputs'][0]['text'],'Привет!')
        self.assertTrue(ws.sent[1]['flush']);self.assertIn('model_id=eleven_v4_turbo',calls[0][0][0])
        self.assertEqual(calls[0][1]['redirect_limit'],0)
        c.last_send=0;c.maintain();self.assertEqual(ws.sent[-1],{'keep_alive':True})
    def test_partial_phrase_is_discarded_and_socket_closed(self):
        for events in ([audio(b'ab'),''],[audio(b'ab'),ConnectionError('lost')],
                       [audio(b'a'),{'is_final_audio_for_turn':True}],
                       [{'is_final_audio_for_turn':True}]):
            ws=Socket(events);c=V4Client('fake',VOICE,lambda *a,**k:ws)
            with self.assertRaises(Exception):c.synthesize('Фраза')
            self.assertTrue(ws.closed);self.assertIsNone(c.ws)
    def test_server_error_is_visible_and_credential_redacted(self):
        ws=Socket([{'type':'error','message':'denied fake-key'}])
        c=V4Client('fake-key',VOICE,lambda *a,**k:ws)
        with self.assertRaises(SpeechFailure) as e:c.synthesize('Фраза')
        self.assertFalse(e.exception.retryable);self.assertNotIn('fake-key',str(e.exception))
    def test_failed_phrase_can_be_retried_whole(self):
        sockets=[Socket([audio(b'ab'),'']),Socket([audio(b'cdef'),{'is_final_audio_for_turn':True}])]
        c=V4Client('fake',VOICE,lambda *a,**k:sockets.pop(0))
        with self.assertRaises(SpeechFailure):c.synthesize('Вся фраза')
        self.assertEqual(c.synthesize('Вся фраза')[0],b'cdef')

    def test_server_idle_timeout_is_retryable_and_discards_partial_audio(self):
        for event in ({'type':'error','message':'No message received within 20s.'},
                      {'error':'No message received within 20s.'}):
            with self.subTest(event=event):
                ws=Socket([audio(b'ab'),event])
                c=V4Client('fake',VOICE,lambda *a,**k:ws)
                with self.assertRaises(SpeechFailure) as caught:c.synthesize('Вся фраза')
                self.assertTrue(caught.exception.retryable)
                self.assertTrue(ws.closed);self.assertIsNone(c.ws)

    def test_idle_timeout_reconnects_same_turn_before_next_without_partial_playback(self):
        failed=Socket([audio(b'xx'),{'type':'error','message':'No message received within 20s.'}])
        recovered=Socket([audio(b'A'*960),{'is_final_audio_for_turn':True},
                          audio(b'B'*960),{'is_final_audio_for_turn':True}])
        sockets=iter([failed,recovered]);connections=[];played=[];events=[];finished=threading.Event()
        def connect(*a,**k):
            ws=next(sockets);connections.append(ws);return ws
        c=V4Client('fake',VOICE,connect)
        class Output:
            def __init__(self,**kw):pass
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def write(self,data):
                played.append(data)
                if len(played)==2:finished.set()
            def abort(self):pass
        with patch('openai_speech.sd.RawOutputStream',Output),\
             patch('openai_speech.change_tempo',side_effect=lambda pcm,*a:pcm):
            worker=OpenAISpeechWorker(lambda *e:events.append(e),VOICE,
                                     client_factory=lambda *a:c,key='fake');worker.volume=1
            try:
                worker.set_enabled(True)
                worker.submit('Первая фраза.',time.monotonic(),VOICE,'Джейсон')
                worker.submit('Вторая фраза.',time.monotonic(),VOICE,'Джейсон')
                self.assertTrue(finished.wait(5))
                self.assertEqual(connections,[failed,recovered])
                turns=[m['inputs'][0]['text'] for ws in connections for m in ws.sent if 'inputs' in m]
                self.assertEqual(turns,['Первая фраза','Первая фраза','Вторая фраза'])
                self.assertEqual(played,[b'A'*960,b'B'*960])
                self.assertFalse(any(e[0]=='error' for e in events))
            finally:worker.close()
if __name__=='__main__':unittest.main()
