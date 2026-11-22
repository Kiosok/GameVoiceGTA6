"""Selectable ElevenLabs models, complete PCM phrases, existing FIFO player."""
from __future__ import annotations
import http.client
import json
import os
from pathlib import Path
import re
import socket
import time
from urllib.parse import quote

from key_store import load_key
from openai_speech import OpenAISpeechWorker, SpeechFailure, MAX_AUDIO_BYTES
from speaker_voices import SpeakerVoices, FEMALE_ROLES, speaker_key

MODEL = 'eleven_flash_v2_5'
DEFAULT_MODEL = 'eleven_v4_turbo'
MODEL_LABELS = {
    MODEL: 'Flash v2.5 — быстрый',
    DEFAULT_MODEL: 'v4 Turbo — баланс',
    'eleven_v4': 'v4 — качество',
}
MODEL_HINTS = {
    MODEL: 'Прежний режим: быстрый и экономичный синтез.',
    DEFAULT_MODEL: 'Выразительная речь в реальном времени.',
    'eleven_v4': 'Приоритет качества и выразительности; ожидание может быть дольше.',
}

def selected_model():
    from device_settings import settings
    value=settings().get('elevenlabs_model',DEFAULT_MODEL)
    return value if isinstance(value,str) and value in MODEL_LABELS else DEFAULT_MODEL

def create_client(model,key,voice):
    if model == MODEL:return ElevenLabsClient(key,voice)
    if model == 'eleven_v4':return V4QualityClient(key,voice)
    if model == DEFAULT_MODEL:
        from elevenlabs_v4 import V4Client
        return V4Client(key,voice)
    raise ValueError('Неизвестная модель ElevenLabs')
HOST = 'api.elevenlabs.io'
# Bootstrap examples only; the account's catalog replaces these after refresh.
DEFAULT_VOICES = {
    'George': 'JBFqnCBsd6RMkjVDRZzb',
    'Rachel': '21m00Tcm4TlvDq8ikWAM',
    'Adam': 'pNInz6obpgDQGcFmaJgB',
    'Antoni': 'ErXwobaYiN019PkySvjV',
    'Josh': 'TxGEqnHWrfWFTfGW9XjX',
    'Elli': 'MF3mGyEYCl7XYWbV9V6O',
}

def folder():
    return Path(os.environ['LOCALAPPDATA']) / 'GameVoice'

def key_path():
    return folder() / 'elevenlabs-key.dpapi'

def voice_catalog():
    result = dict(DEFAULT_VOICES)
    try:
        data = json.loads((folder()/'elevenlabs-catalog.json').read_text(encoding='utf8'))
        if data: result = data
    except (OSError, ValueError): pass
    return result

def voice_id(voice):
    value = voice_catalog().get(voice, voice)
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,64}',value):
        raise SpeechFailure('Выберите голос ElevenLabs или укажите его voice_id.',False)
    return value

def registry():
    catalog = voice_catalog()
    george = catalog.get('George',next(iter(catalog.values())))
    rachel = catalog.get('Rachel',DEFAULT_VOICES['Rachel'])
    # Persist IDs, not display names. Renaming a voice cannot change a character.
    result = SpeakerVoices(folder()/'elevenlabs-voices.json',
        voices=tuple(catalog.values()), allow_voice_ids=True,
        fallback_voices=tuple(DEFAULT_VOICES.values()),
        seeds={'джейсон':('Джейсон',george),'лусия':('Лусия',rachel),
               **{speaker_key(name):(name,rachel) for name in FEMALE_ROLES}})
    # Older automatic fallbacks may already have assigned a male stock voice
    # to a female role. Keep explicit choices and automatic clones authoritative.
    for name in FEMALE_ROLES:
        entry=result.entries[speaker_key(name)]
        if not entry.get('manual'): entry['voice']=rachel
    # Reuse recognition aliases, tempo and existing identities without changing OpenAI.
    original = SpeakerVoices()
    if not result.path.exists():
        result.speed, result.catch_up = original.speed, original.catch_up
        for entry in original.entries.values(): result.voice_for(entry['name'])
        result.aliases.update(original.aliases)
        result.save()
    return result

def check_response(response, key):
    content_type = response.getheader('Content-Type','').lower()
    if response.status in (301,302,303,307,308):
        location=response.getheader('Location','')
        if 'geo-restriction.elevenlabs.io' in location:
            raise SpeechFailure('ElevenLabs ограничил доступ по региону текущего подключения.',False)
        raise SpeechFailure('ElevenLabs перенаправил запрос; озвучка не запущена.',False)
    if response.status != 200 or 'text/html' in content_type:
        raw=response.read(4096).decode('utf8',errors='replace').replace(key,'[ключ]')
        if 'geo-restriction' in raw or 'Access restricted' in raw:
            raise SpeechFailure('ElevenLabs ограничил доступ по региону текущего подключения.',False)
        try:
            detail=json.loads(raw).get('detail',raw)
            message=detail.get('message',str(detail)) if isinstance(detail,dict) else str(detail)
        except ValueError: message='Сервис не вернул ожидаемый ответ.'
        raise SpeechFailure(f'ElevenLabs HTTP {response.status}: {message[:250]}',
            response.status in (408,429) or response.status>=500)

def fetch_voices():
    key=load_key(key_path())
    if not key: raise SpeechFailure('Не найден сохранённый ключ ElevenLabs.',False)
    connection=http.client.HTTPSConnection(HOST,timeout=15)
    try:
        connection.request('GET','/v1/voices',headers={'xi-api-key':key,'Accept':'application/json'})
        response=connection.getresponse(); check_response(response,key)
        data=json.loads(response.read())
        catalog={}
        for item in data.get('voices',[]):
            label=item['name']
            if label in catalog: label += ' · '+item['voice_id'][-5:]
            catalog[label]=item['voice_id']
        if not catalog: raise SpeechFailure('В аккаунте ElevenLabs нет доступных голосов.',False)
        target=folder()/'elevenlabs-catalog.json'; target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_suffix('.pending')
        temporary.write_text(json.dumps(catalog,ensure_ascii=False,indent=2),encoding='utf8')
        temporary.replace(target)
        return catalog
    finally: connection.close()

class ElevenLabsClient:
    deferred_connection = True
    def __init__(self,key,voice,connection_factory=None):
        self.key,self.voice=key,voice
        self.factory=connection_factory or http.client.HTTPSConnection
        self.ws=None
        self.needs_reconnect=False
        self.first_chunk_ms=None

    def maintain(self):
        if not self.key: raise SpeechFailure('Не найден сохранённый ключ ElevenLabs.',False)

    def connect(self):
        self.maintain()
        if self.ws is None or self.needs_reconnect:
            self.close()
            self.ws=self.factory(HOST,timeout=15)
            self.needs_reconnect=False

    def request_for(self,text):
        identifier=voice_id(self.voice)
        return (f'/v1/text-to-speech/{quote(identifier,safe="")}/stream?output_format=pcm_24000',
                {'text':text,'model_id':MODEL,'language_code':'ru',
                 'voice_settings':{'stability':.4,'similarity_boost':.75,'use_speaker_boost':False}})

    def synthesize(self,text,number=0):
        self.connect(); connection=self.ws
        self.first_chunk_ms=None; started=time.monotonic()
        endpoint,payload=self.request_for(text)
        body=json.dumps(payload,ensure_ascii=False).encode('utf8')
        try:
            connection.request('POST',endpoint,
                body=body,headers={'xi-api-key':self.key,'Content-Type':'application/json','Accept':'audio/pcm'})
            response=connection.getresponse(); check_response(response,self.key)
            content_type=response.getheader('Content-Type','').lower()
            if not ('audio/' in content_type or 'application/octet-stream' in content_type):
                raise SpeechFailure('ElevenLabs вернул ответ без PCM-аудио.',False)
            expected=response.getheader('Content-Length')
            pcm=bytearray()
            while True:
                if time.monotonic()-started>45: raise SpeechFailure('Истекло время получения фразы ElevenLabs.')
                chunk=response.read1(4096)
                if not chunk: break
                if not pcm:self.first_chunk_ms=round((time.monotonic()-started)*1000)
                pcm.extend(chunk)
                if len(pcm)>MAX_AUDIO_BYTES:raise SpeechFailure('Слишком длинная фраза ElevenLabs.',False)
            if expected is not None and len(pcm)!=int(expected):
                raise SpeechFailure('Фраза ElevenLabs получена не полностью; повторяю эту же фразу.')
            if not pcm or len(pcm)%2:raise SpeechFailure('ElevenLabs вернул пустую или неполную PCM-запись.')
            # TTS receives only the body; it does not generate a text transcript.
            return bytes(pcm),''
        except Exception:
            self.close();raise

    def close(self):
        connection,self.ws=self.ws,None
        if connection:
            try: connection.close()
            except Exception:pass

    def abort(self):
        self.needs_reconnect=True
        connection=self.ws
        if connection and getattr(connection,'sock',None):
            try:connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        self.close()

class V4QualityClient(ElevenLabsClient):
    def request_for(self,text):
        return ('/v1/text-to-dialogue/stream?output_format=pcm_24000',
                {'inputs':[{'text':text,'voice_id':voice_id(self.voice)}],
                 'model_id':'eleven_v4','language_code':'ru'})

class ElevenLabsSpeechWorker(OpenAISpeechWorker):
    def __init__(self,emit,voice_name,model=DEFAULT_MODEL):
        if model not in MODEL_LABELS:raise ValueError('Неизвестная модель ElevenLabs')
        self.model_id=model
        super().__init__(emit,voice_name,client_factory=lambda key,voice:create_client(model,key,voice),
                         key=load_key(key_path()))

    def set_model(self,model):
        if model not in MODEL_LABELS:raise ValueError('Неизвестная модель ElevenLabs')
        with self.lock:self.model_id=model

    def submit(self,text,first_seen,voice=None,speaker='',*,strip_label=True):
        # Snapshot the choice on submission: queued/retried phrases keep their model.
        with self.lock:model=self.model_id
        super().submit(text,first_seen,voice,speaker,model_id=model,strip_label=strip_label)

    def client_for_cue(self,cue):
        return self.client_for(cue.voice,cue.model_id or self.model_id)

    def client_for(self,voice,model=None):
        model=model or self.model_id
        with self.lock:
            cache_key=(model,voice)
            if cache_key not in self.clients:
                self.clients[cache_key]=create_client(model,self.client.key,voice)
            client=self.clients[cache_key]
        client.last_used=time.monotonic()
        others=sorted((c for c in self.clients.values() if c is not client and c.ws),
                      key=lambda c:getattr(c,'last_used',0),reverse=True)
        for old in others[2:]:old.close()
        return client
