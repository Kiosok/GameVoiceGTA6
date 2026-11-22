import base64, json, sys, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from openai_speech import SpeechClient, SpeechFailure, OpenAISpeechWorker, clean_spoken_text

class FakeSocket:
    def __init__(self,events):self.events=iter(events);self.sent=[]
    def recv(self):
        event=next(self.events)
        if isinstance(event,Exception):raise event
        return json.dumps(event)
    def send(self,event):self.sent.append(json.loads(event))
    def close(self,**kwargs):pass

class ProtocolTests(unittest.TestCase):
    def test_character_delivery_does_not_leak_to_another_speaker(self):
        events=[{'type':'session.created'},{'type':'session.updated'}]
        for number in (1,2):
            events += [{'type':'response.created','response':{'id':str(number)}},
                {'type':'response.output_audio.delta','response_id':str(number),'delta':base64.b64encode(b'ab').decode()},
                {'type':'response.done','response':{'id':str(number),'status':'completed'}}]
        socket=FakeSocket(events)
        client=SpeechClient('fake','ash',factory=lambda *a,**k:socket)
        client.set_character('Джейсон');client.synthesize('Хорошо. Идём.',1)
        client.set_character('Брайан');client.synthesize('Ладно.',2)
        requests=[event['response'] for event in socket.sent if event['type']=='response.create']
        self.assertIn('gravelly',requests[0]['instructions'])
        self.assertNotIn('gravelly',requests[1]['instructions'])
        self.assertEqual(requests[0]['input'][0]['content'][0]['text'],'Хорошо. Идём')

    def client(self,tail):
        sock=FakeSocket([{'type':'session.created'},{'type':'session.updated'},
                         {'type':'response.created','response':{'id':'r1'}},*tail])
        return SpeechClient('fake-key',factory=lambda *a,**kw:sock),sock
    def chunk(self,data):return {'type':'response.output_audio.delta','response_id':'r1',
                                'delta':base64.b64encode(data).decode()}
    def test_only_complete_phrase_is_returned_and_context_is_isolated(self):
        client,sock=self.client([self.chunk(b'ab'),self.chunk(b'cd'),
            {'type':'response.done','response':{'id':'r1','status':'completed'}}])
        self.assertEqual(client.synthesize('Привет.')[0],b'abcd')
        request=sock.sent[-1]['response']
        self.assertEqual(request['conversation'],'none')
        self.assertEqual(request['input'][0]['content'][0]['text'],'Привет')
        self.assertEqual(sock.sent[0]['session']['audio']['input']['turn_detection'],None)
    def test_disconnect_after_partial_audio_does_not_return_half_phrase(self):
        client,_=self.client([self.chunk(b'ab'),ConnectionError('lost')])
        with self.assertRaises(ConnectionError):client.synthesize('Привет')
    def test_incomplete_response_is_not_playable(self):
        client,_=self.client([self.chunk(b'ab'),
             {'type':'response.done','response':{'id':'r1','status':'incomplete'}}])
        with self.assertRaises(SpeechFailure):client.synthesize('Привет')
    def test_key_is_redacted_and_auth_is_not_retried(self):
        client,_=self.client([{'type':'error','error':{'code':'invalid_api_key','message':'fake-key rejected'}}])
        with self.assertRaises(SpeechFailure) as caught:client.synthesize('Привет')
        self.assertNotIn('fake-key',str(caught.exception));self.assertFalse(caught.exception.retryable)
    def test_punctuation_noise_is_cleaned_but_word_is_preserved(self):
        self.assertEqual(clean_spoken_text('| Стой . . . < Иди сюда ! |'),'Стой... Иди сюда')
        self.assertEqual(clean_spoken_text('Это наша точка.'),'Это наша точка')

    def test_profanity_is_sent_as_written(self):
        text='Блядь, что за хуйня? Да пошёл ты нахуй!'
        client,sock=self.client([self.chunk(b'ab'),
             {'type':'response.done','response':{'id':'r1','status':'completed'}}])
        client.synthesize(text)
        sent=sock.sent[-1]['response']['input'][0]['content'][0]['text']
        self.assertEqual(sent,text.rstrip('!'))

class RetryTests(unittest.TestCase):
    def test_same_phrase_retried_and_playback_order_is_preserved(self):
        calls=[];played=[];finished=threading.Event()
        class Client:
            key='fake';first_chunk_ms=1
            ws=object();needs_reconnect=False
            def maintain(self):pass
            def synthesize(self,text,number):
                calls.append(number)
                if len(calls)==1:raise ConnectionError('disconnect')
                return (b'A' if number==1 else b'B')*960,text
            def close(self):pass
            def abort(self):pass
        class Output:
            def __init__(self,**kw):pass
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def write(self,data):
                played.append(data)
                if len(played)==2:finished.set()
            def abort(self):pass
        with patch('openai_speech.SpeechClient',return_value=Client()),patch('openai_speech.sd.RawOutputStream',Output),patch('openai_speech.change_tempo',side_effect=lambda pcm,factor:pcm):
            worker=OpenAISpeechWorker(lambda *a:None);worker.volume=1
            try:
                worker.set_enabled(True);worker.submit('Первая фраза.',time.monotonic())
                epoch=worker.epoch
                worker.submit('Вторая фраза.',time.monotonic())
                self.assertEqual(worker.epoch,epoch)
                self.assertTrue(finished.wait(4))
                self.assertEqual(calls,[1,1,2]);self.assertEqual(played,[b'A'*960,b'B'*960])
            finally:worker.close()

    def test_connection_is_prepared_before_first_subtitle(self):
        prepared=threading.Event()
        class Client:
            key='fake';ws=None;needs_reconnect=False
            def maintain(self):self.ws=object();prepared.set()
            def abort(self):pass
            def close(self):pass
        with patch('openai_speech.SpeechClient',return_value=Client()):
            worker=OpenAISpeechWorker(lambda *a:None)
            try:
                worker.set_enabled(True)
                self.assertTrue(prepared.wait(1))
                self.assertEqual(worker.pending_count,0)
            finally:worker.close()

    def test_idle_poll_services_connection_without_reconnecting(self):
        import websocket
        class IdleSocket:
            def __init__(self):self.timeouts=[];self.reads=0
            def settimeout(self,value):self.timeouts.append(value)
            def recv(self):
                self.reads+=1
                raise websocket.WebSocketTimeoutException('idle')
        client=SpeechClient('fake-key',factory=lambda *a,**kw:self.fail('healthy session was replaced'))
        sock=IdleSocket();client.ws=sock;client.connected_at=time.monotonic()
        client.maintain()
        self.assertIs(client.ws,sock);self.assertEqual(sock.reads,1)
        self.assertEqual(sock.timeouts,[.05,15])

if __name__=='__main__':unittest.main()
