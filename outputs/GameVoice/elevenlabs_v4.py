"""V4 Turbo dialogue WebSocket, one voice per persistent connection."""
import base64
import json
import re
import time
import websocket
from openai_speech import SpeechFailure, MAX_AUDIO_BYTES
from elevenlabs_speech import voice_id
MODEL='eleven_v4_turbo'
URI=('wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input'
     '?model_id='+MODEL+'&output_format=pcm_24000&language_code=ru')

class V4Client:
    deferred_connection=True
    def __init__(self,key,voice,connection_factory=None):
        self.key,self.voice=key,voice
        self.factory=connection_factory or websocket.create_connection
        self.ws=None;self.needs_reconnect=False;self.first_chunk_ms=None;self.last_send=0
    def send(self,data):
        self.ws.send(json.dumps(data,ensure_ascii=False));self.last_send=time.monotonic()
    def maintain(self):
        if not self.key:raise SpeechFailure('Не найден сохранённый ключ ElevenLabs.',False)
        if self.ws and time.monotonic()-self.last_send>=12:self.send({'keep_alive':True})
    def synthesize(self,text,number=0):
        started=time.monotonic();self.first_chunk_ms=None
        try:
            self.maintain()
            if self.ws is None or self.needs_reconnect:
                self.close()
                self.ws=self.factory(URI,header={'xi-api-key':self.key},timeout=15,redirect_limit=0)
                self.needs_reconnect=False
                self.send({'voices':[voice_id(self.voice)]})
            self.send({'inputs':[{'text':text,'voice_id':voice_id(self.voice),'new_turn':True}],'flush':True})
            pcm=bytearray()
            while time.monotonic()-started<45:
                raw=self.ws.recv()
                if not raw:raise SpeechFailure('V4 Turbo: соединение закрылось до окончания фразы.')
                msg=json.loads(raw)
                if msg.get('error') or msg.get('type')=='error':
                    detail=str(msg.get('message') or msg.get('error')).replace(self.key,'[ключ]')[:300]
                    # An idle socket can already contain the server's timeout
                    # when its character speaks again. Reconnect and retry the
                    # same complete turn; authentication/region errors stay fatal.
                    idle_timeout=bool(re.search(r'no message received within\s+\d+(?:\.\d+)?\s*s',detail,re.I))
                    raise SpeechFailure('V4 Turbo: '+detail,retryable=idle_timeout)
                if msg.get('audio'):
                    chunk=base64.b64decode(msg['audio'],validate=True)
                    if chunk and not pcm:self.first_chunk_ms=round((time.monotonic()-started)*1000)
                    pcm.extend(chunk)
                    if len(pcm)>MAX_AUDIO_BYTES:raise SpeechFailure('Слишком длинная фраза V4 Turbo.',False)
                if msg.get('is_final_audio_for_turn') or msg.get('is_final'):
                    if not pcm or len(pcm)%2:raise SpeechFailure('V4 Turbo вернул пустую или неполную PCM-фразу.')
                    if msg.get('is_final'):self.close()
                    return bytes(pcm),''
            raise SpeechFailure('Истекло время получения фразы V4 Turbo.')
        except Exception:
            self.close();raise
    def close(self):
        ws,self.ws=self.ws,None
        if ws:
            try:ws.close()
            except Exception:pass
    def abort(self):
        self.needs_reconnect=True
        if self.ws:
            try:self.ws.abort()
            except Exception:pass
        self.close()
