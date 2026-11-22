"""Resumable IVC publication, preserving IDs. No blind mutation retries."""
from __future__ import annotations
import hashlib
import http.client
import json
from pathlib import Path
import uuid
import wave

from elevenlabs_clones import multipart, references
from elevenlabs_speech import HOST, check_response, key_path, ElevenLabsClient
from key_store import load_key
from openai_speech import safe_error
from voice_pipeline import atomic_json, load, utc


class NeedsAttention(RuntimeError):
    pass


class API:
    def __init__(self):
        self.key = load_key(key_path())
        if not self.key:raise NeedsAttention('Нет сохранённого ключа ElevenLabs.')

    def request(self, method, path, body=None, content_type='application/json'):
        connection = http.client.HTTPSConnection(HOST, timeout=90)
        try:
            connection.request(method,path,body,{'xi-api-key':self.key,'Content-Type':content_type,'Accept':'application/json'})
            response=connection.getresponse()
            check_response(response,self.key)
            data=response.read(2*1024*1024+1)
            if len(data)>2*1024*1024:raise NeedsAttention('Слишком большой ответ API.')
            return json.loads(data)
        except Exception as exc:
            raise RuntimeError(safe_error(exc,self.key)) from None
        finally:connection.close()

    def voice(self, identifier):
        return self.request('GET','/v1/voices/'+identifier)

    def add(self, record, raw):
        boundary='GameVoiceAuto'+uuid.uuid4().hex
        body=multipart(record['voice_name'],[raw],record['marker'],boundary)
        body=body.replace(b'filename="reference-01.wav"',('filename="'+record['filename']+'"').encode())
        path='/v1/voices/'+record['voice_id']+'/edit' if record.get('voice_id') else '/v1/voices/add'
        return self.request('POST',path,body,'multipart/form-data; boundary='+boundary)

    def probe(self, identifier, target):
        client=ElevenLabsClient(self.key,identifier)
        try:pcm,_=client.synthesize('Хорошо. Давай разберёмся, что происходит.')
        finally:client.close()
        if len(pcm)<4800 or len(pcm)%2:raise RuntimeError('Не получен проверочный звук клона.')
        with wave.open(str(target),'wb') as output:
            output.setparams((1,2,24000,0,'NONE','not compressed'));output.writeframes(pcm)


def make_reference(rows, target):
    """Stop at the first complete word taking unique lexical speech past 60 s."""
    speech=0.;chunks=[];sources=[]
    for row in rows:
        evidence=json.loads(row['evidence'])
        raw=Path(row['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=row['pcm_hash']:raise NeedsAttention('Образец изменился после проверки.')
        with wave.open(str(row['path']),'rb') as source:
            if source.getparams()[:3]!=(1,2,24000):raise NeedsAttention('Неверный формат банка.')
            pcm=source.readframes(source.getnframes())
        stop=0
        for lo,hi in evidence['clean_word_intervals']:
            speech+=(hi-lo)/24000
            stop=max(stop,hi)
            if speech>=60:break
        if not stop:continue
        if chunks:chunks.append(bytes(4800))
        chunks.append(pcm[:stop*2]);sources.append(row['id'])
        if speech>=60:break
    if speech<60:raise ValueError('Нужно 60 секунд чистой речи.')
    target.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(target),'wb') as output:
        output.setparams((1,2,24000,0,'NONE','not compressed'));output.writeframes(b''.join(chunks))
    raw=target.read_bytes()
    return {'clean_speech_seconds':speech,'sha256':hashlib.sha256(raw).hexdigest(),
            'sources':sources,'wave_seconds':sum(len(c) for c in chunks)/48000}


def make_improved_reference(rows,target):
    """Select a <=120s reference from a bank of >=160s unique lexical speech.

    Rank by anchored speaker evidence, diarization share and speech density.
    This is an automatic quality estimate, not an audible similarity guarantee.
    """
    if sum(r['seconds'] for r in rows)<160:raise ValueError('Для второго обновления нужно 160 секунд чистой речи.')
    def score(row):
        evidence=json.loads(row['evidence'])
        with wave.open(row['path'],'rb') as wav:duration=wav.getnframes()/wav.getframerate()
        return (bool(evidence.get('anchored')),evidence.get('word_share',0),row['seconds']/max(duration,.001),row['seconds'])
    speech_frames=0;chunks=[];sources=[];frames=0
    for row in sorted(rows,key=score,reverse=True):
        raw=Path(row['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=row['pcm_hash']:raise NeedsAttention('Образец изменился после проверки.')
        with wave.open(row['path'],'rb') as wav:
            if wav.getparams()[:3]!=(1,2,24000):raise NeedsAttention('Неверный формат банка.')
            pcm=wav.readframes(wav.getnframes())
        gap=2400 if chunks else 0
        available=120*24000-frames-gap
        if available<=0:break
        intervals=[(a,b) for a,b in json.loads(row['evidence'])['clean_word_intervals'] if b<=available]
        if not intervals:continue
        stop=intervals[-1][1]
        if chunks:chunks.append(bytes(gap*2))
        chunks.append(pcm[:stop*2]);frames+=gap+stop
        speech_frames+=sum(b-a for a,b in intervals);sources.append(row['id'])
    if speech_frames<60*24000:raise ValueError('Пока недостаточно плотной чистой речи для улучшенного образца.')
    target.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(target),'wb') as wav:
        wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(b''.join(chunks))
    return {'clean_speech_seconds':speech_frames/24000,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
            'sources':sources,'wave_seconds':frames/24000,'selection':'speaker evidence, diarization share, speech density'}


def ensure_overlay(record, overlay_path):
    overlay=load(overlay_path,{'characters':[]})
    entries={c['name']:c for c in overlay.get('characters',[])}
    name=record['name']
    item={'name':name,'voice_id':record['voice_id'],'expected_voice':record['expected_voice'],
          'state':'ready','clean_speech_seconds':record['clean_speech_seconds'],
          'reference_sha256':record['sha256'],'installed_milestone':record.get('milestone',60)}
    if all(entries.get(name,{}).get(k)==v for k,v in item.items()):return
    entries[name]={**item,'updated':utc()}
    atomic_json(overlay_path,{'revision':uuid.uuid4().hex,'characters':list(entries.values())})


def mirror_clone_journal(record,directory,overlay_path):
    """Keep the original manual workflow aware of automatic creates/replacements."""
    target=Path(overlay_path).parent/'clones'/(hashlib.sha256(record['name'].encode()).hexdigest()[:16]+'.json')
    previous=load(target,{})
    if previous.get('pipeline_reference_sha256')==record['sha256'] and previous.get('voice_id')==record['voice_id']:return
    reference=directory/record.get('reference_file','reference-60s.wav')
    raw=reference.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:raise NeedsAttention('Повреждён эталон перед обновлением журнала.')
    value={'character':record['name'],'voice_name':record['voice_name'],'voice_id':record['voice_id'],
        'state':'ready','requires_verification':False,'sample_count':1,'sample_ids':[record['new_sample_id']],
        'sample_hash':hashlib.sha256(hashlib.sha256(raw).digest()).hexdigest(),
        'reference_file':str(reference),'reference_seconds':record['wave_seconds'],
        'clean_speech_seconds':record['clean_speech_seconds'],'pipeline_reference_sha256':record['sha256'],
        'reference_revision':previous.get('reference_revision',1)+1 if previous else 1,
        'reviewed':False,'audibly_reviewed':False,'explicitly_authorized':True,'automatic_pipeline':True,
        'review_basis':'Stable named subtitles, lexical timestamps, event trimming and conservative speaker diarization.',
        'updated_utc':utc(),'previous_reference':previous or None}
    atomic_json(target,value)


def publish(name, rows, current, root, overlay_path, api=None, milestone=60):
    """Two durable promotions (60/160). In-flight phrases keep their ID."""
    if milestone not in (60,160):raise ValueError('Неизвестный этап клонирования.')
    if sum(r['seconds'] for r in rows)<milestone:raise ValueError(f'Нужно {milestone} секунд чистой речи.')
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    directory=root/hashlib.sha256(name.encode()).hexdigest()[:16]
    if milestone!=60:directory=directory/('revision-'+str(milestone))
    directory.mkdir(exist_ok=True)
    journal_path=directory/'installation.json'
    record=load(journal_path)
    if record is None:
        reference_file='reference-60s.wav' if milestone==60 else 'reference-selected.wav'
        package=(make_reference if milestone==60 else make_improved_reference)(rows,directory/reference_file)
        record={'name':name,'voice_name':'GameVoice — '+name,'state':'prepared',
            'created':utc(),'voice_id':None,'expected_voice':current.get('voice') if current else None,
            'milestone':milestone,'bank_clean_speech_seconds':sum(r['seconds'] for r in rows),'reference_file':reference_file,
            'marker':f'GameVoice automatic {milestone}s '+package['sha256'],
            'filename':f'gamevoice-{milestone}s-'+package['sha256'][:20]+'.wav',**package}
        atomic_json(journal_path,record)
    if record['state']=='complete':
        ensure_overlay(record,overlay_path)
        mirror_clone_journal(record,directory,overlay_path)
        return record
    api=api or API()
    if record['state']=='prepared':
        voices=api.request('GET','/v1/voices')['voices']
        expected=record['expected_voice']
        existing=next((v for v in voices if v['voice_id']==expected and v['name']==record['voice_name']
                       and v.get('category')=='cloned'),None)
        if current and current.get('manual') and not existing:
            raise NeedsAttention(name+': ручной голос не принадлежит GameVoice; сохранён без замены.')
        if existing:
            remote=api.voice(existing['voice_id'])
            record['voice_id']=existing['voice_id']
            record['before_remote']=remote
            record['old_sample_ids']=[s['sample_id'] for s in remote.get('samples',[])]
        else:
            duplicates=[v for v in voices if v['name']==record['voice_name']]
            if duplicates:raise NeedsAttention(name+': одноимённый голос требует сверки.')
            plan=api.request('GET','/v1/user/subscription')
            if not plan.get('can_use_instant_voice_cloning') or plan['voice_slots_used']>=plan['voice_limit']:
                raise NeedsAttention('Нет свободных мест для новых голосов. Образцы продолжают копиться.')
            record['old_sample_ids']=[]
        # Durable state BEFORE sending. A lost response can never cause another POST.
        record['state']='submitting';atomic_json(journal_path,record)
        raw=(directory/record.get('reference_file','reference-60s.wav')).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=record['sha256']:raise NeedsAttention('Повреждён подготовленный образец.')
        result=api.add(record,raw)
        if not record.get('voice_id'):
            identifier=result.get('voice_id','')
            if not identifier or result.get('requires_verification') is not False:
                record.update(state='verification_required',response=result);atomic_json(journal_path,record)
                raise NeedsAttention(name+': ElevenLabs требует подтверждение голоса.')
            record['voice_id']=identifier
        record['state']='submitted';atomic_json(journal_path,record)
    if record['state']=='verification_required':raise NeedsAttention(name+': нужно подтверждение ElevenLabs.')
    if record['state'] in ('submitting','submitted'):
        # Reconcile ambiguous mutations by unique filename/description, never reissue.
        if not record.get('voice_id'):
            voices=api.request('GET','/v1/voices')['voices']
            matches=[v for v in voices if v.get('description')==record['marker'] and v['name']==record['voice_name']]
            if len(matches)!=1:raise NeedsAttention(name+': ответ создания потерян; автоматический повтор запрещён.')
            record['voice_id']=matches[0]['voice_id']
        remote=api.voice(record['voice_id'])
        # ElevenLabs normalizes uploaded WAV names to .mp3 in its sample catalog.
        matches=[s for s in remote.get('samples',[]) if Path(s.get('file_name','')).stem==Path(record['filename']).stem]
        if len(matches)!=1 or abs(matches[0]['duration_secs']-record['wave_seconds'])>.15:
            raise NeedsAttention(name+': новый образец ещё не подтверждён; старые не тронуты.')
        if remote.get('requires_verification'):
            raise NeedsAttention(name+': голос требует подтверждения сервиса.')
        record.update(state='sample_verified',new_sample_id=matches[0]['sample_id'])
        atomic_json(journal_path,record)
    if record['state']=='sample_verified':
        api.probe(record['voice_id'],directory/'verification-ru.wav')
        record['state']='synthesis_verified';atomic_json(journal_path,record)
    if record['state']=='synthesis_verified':
        # Only known old samples of this same voice; retain new and any later manual additions.
        for identifier in record['old_sample_ids']:
            remote=api.voice(record['voice_id'])
            ids={s['sample_id'] for s in remote.get('samples',[])}
            if record['new_sample_id'] not in ids:raise NeedsAttention('Новый образец отсутствует; удаление отменено.')
            if identifier in ids:
                api.request('DELETE','/v1/voices/'+record['voice_id']+'/samples/'+identifier)
        remote=api.voice(record['voice_id'])
        if record['new_sample_id'] not in {s['sample_id'] for s in remote.get('samples',[])}:
            raise NeedsAttention('Не подтверждён итоговый набор образцов.')
        record['state']='remote_ready';atomic_json(journal_path,record)
    if record['state']=='remote_ready':
        ensure_overlay(record,overlay_path)
        record.update(state='complete',completed=utc());atomic_json(journal_path,record)
        mirror_clone_journal(record,directory,overlay_path)
    return record
