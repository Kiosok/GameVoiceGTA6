import http.client,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from elevenlabs_speech import ElevenLabsClient,check_response,DEFAULT_VOICES
from openai_speech import SpeechFailure,safe_error
from speaker_voices import SpeakerVoices

class Response:
    status=200
    def __init__(self,chunks,headers=None,status=200):
        self.chunks=iter(chunks);self.headers=headers or {'Content-Type':'audio/pcm'};self.status=status
    def getheader(self,key,default=None):return self.headers.get(key,default)
    def read1(self,n):
        item=next(self.chunks,b'')
        if isinstance(item,Exception):raise item
        return item
    def read(self,n=None):return self.read1(n)
class Connection:
    sock=None
    def __init__(self,responses):self.responses=iter(responses);self.requests=[];self.closed=False
    def request(self,*args,**kwargs):self.requests.append((args,kwargs))
    def getresponse(self):return next(self.responses)
    def close(self):self.closed=True

class ElevenTests(unittest.TestCase):
    def test_flash_russian_pcm_and_connection_reuse(self):
        connection=Connection([Response([b'abcd']),Response([b'efgh'])])
        client=ElevenLabsClient('fake-key',DEFAULT_VOICES['George'],lambda *a,**k:connection)
        self.assertEqual(client.synthesize('Блядь, иди сюда!')[0],b'abcd')
        self.assertEqual(client.synthesize('Вторая фраза.')[0],b'efgh')
        self.assertIs(client.ws,connection)
        args,kwargs=connection.requests[0]
        self.assertIn('pcm_24000',args[1])
        body=json.loads(kwargs['body'])
        self.assertEqual(body['text'],'Блядь, иди сюда!')
        self.assertEqual(body['model_id'],'eleven_flash_v2_5')
        self.assertEqual(body['language_code'],'ru')
    def test_partial_audio_is_not_returned(self):
        connection=Connection([Response([b'ab',http.client.IncompleteRead(b'cd',4)])])
        client=ElevenLabsClient('fake',DEFAULT_VOICES['George'],lambda *a,**k:connection)
        with self.assertRaises(http.client.IncompleteRead):client.synthesize('Фраза')
        self.assertTrue(connection.closed)
    def test_short_content_length_is_rejected(self):
        connection=Connection([Response([b'ab'],{'Content-Type':'audio/pcm','Content-Length':'4'})])
        client=ElevenLabsClient('fake',DEFAULT_VOICES['George'],lambda *a,**k:connection)
        with self.assertRaises(SpeechFailure):client.synthesize('Фраза')
    def test_redirect_is_reported_without_forwarding_key(self):
        connection=Connection([Response([],{'Location':'https://geo-restriction.elevenlabs.io/'},302)])
        client=ElevenLabsClient('fake',DEFAULT_VOICES['George'],lambda *a,**k:connection)
        with self.assertRaises(SpeechFailure) as caught:client.synthesize('Фраза')
        self.assertFalse(caught.exception.retryable)
        self.assertIn('регион',str(caught.exception))
        self.assertEqual(len(connection.requests),1)
    def test_status_retry_policy_and_redaction(self):
        for status,retry in ((401,False),(402,False),(403,False),(404,False),(429,True),(503,True)):
            with self.assertRaises(SpeechFailure) as caught:
                check_response(Response([b'{"detail":{"message":"bad fake-key"}}'],status=status),'fake-key')
            self.assertEqual(caught.exception.retryable,retry)
            self.assertNotIn('fake-key',str(caught.exception))
        self.assertNotIn('sk_secret123',safe_error(ValueError('sk_secret123')))
    def test_separate_registry_persists_clone_ids_and_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'elevenlabs.json'
            options={'voices':tuple(DEFAULT_VOICES.values()),'seeds':{},'allow_voice_ids':True}
            registry=SpeakerVoices(path,**options)
            clone='CustomCloneVoiceId12345'
            registry.assign('Брайан',clone)
            self.assertEqual(registry.voice_for("Бра'йаы"),clone)
            registry.save();again=SpeakerVoices(path,**options)
            self.assertEqual(again.voice_for('Брайан'),clone)
            self.assertFalse(again.valid_voice('../invalid'))

if __name__=='__main__':unittest.main()
