"""Closed-part processing child. Heavy separation is isolated from live OCR/TTS."""
from __future__ import annotations
import argparse
import audioop
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import wave

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'outputs/GameVoice'))
from voice_pipeline import Bank,InstanceLock,atomic_json,load,text_key,utc,recording_identity
from speaker_voices import speaker_key,resolve_speaker_key
from process_recorded_voices import NAMES,prepare,sha,canonical_caption

ROOT=Path('D:/GameVoice/automatic-voices')
LOCAL=Path(os.environ['LOCALAPPDATA'])/'GameVoice'
GENERIC=re.compile(r'мужчин|женщин|жител|турист|сотрудни|полици|незнаком|менеджер|инспектор|контакт|диспетчер|клиент|продав|водител|громил|охран|радио|телевиз|телеведущ|чаптер|зимород|делец|операционист|себя|я тут')


def known_names(rows):
    known={speaker_key(n):n for n in NAMES}
    labels=Counter();texts=defaultdict(set)
    for raw_row in rows:
        row=canonical_caption(raw_row)
        if len(row.get('groups',[]))!=1:continue
        name,text=row['groups'][0]
        if not name:continue
        canonical=resolve_speaker_key(name,known)
        if canonical in known:continue
        # New proper names need repeated independent dialogue, not an NPC role or OCR junk.
        if not re.fullmatch(r'[А-ЯЁ][а-яё]{2,15}(?: [А-ЯЁ][а-яё]{2,15})?',name.strip()):continue
        if GENERIC.search(name.casefold()):continue
        key=speaker_key(name);labels[name]+=1
        for cue in row.get('accepted',[]):
            if speaker_key(cue.get('speaker',''))==key and len(text_key(cue.get('text','')))>=10:
                texts[name].add(text_key(cue['text']))
    for name,count in labels.items():
        if count>=8 and len(texts[name])>=3:known[speaker_key(name)]=name
    return known


def prepare_part(part,folder,bank):
    session=Path(part['session']);meta=load(session/'session.json')
    origin=meta['estimated_first_sample_monotonic'];rate=part['rate']
    start,end=part['start']/rate,part['end']/rate
    rows=[]
    caption_file=session/'captions.jsonl'
    caption_text=caption_file.read_text(encoding='utf8') if caption_file.exists() else ''
    for line in caption_text.splitlines():
        try:row=canonical_caption(json.loads(line))
        except ValueError:continue
        t=row['frame_monotonic']-origin
        if start-1<=t<=end+1:rows.append(row)
    rows.sort(key=lambda r:r['frame_monotonic'])
    # Prevent carry-over captions from a previous/next part entering this job.
    clipped=[r for r in rows if start<=r['frame_monotonic']-origin<end]
    if rows and clipped:
        clipped.append({'frame_monotonic':min(end,rows[-1]['frame_monotonic']-origin)+origin,'groups':[]})
    atomic_json(folder/'snapshot.json',{'source_session':str(session),'cutoff_seconds':end,
        'files':[{'file':part['file'],'start':part['start'],'end':part['end']}],
        'session_metadata':meta,'part_start_seconds':start,'created_utc':utc()})
    (folder/'captions-snapshot.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in clipped),encoding='utf8')
    known=known_names(clipped)
    def exclude(name,lo,hi,text):
        if sum(r['seconds'] for r in bank.rows(name))>=180:return True
        return bool(bank.db.execute('SELECT 1 FROM clips WHERE session=? AND start<? AND end>? LIMIT 1',
            (recording_identity(session),hi-.02,lo+.02)).fetchone())
    prepare(session,folder,known,exclude,max_seconds=180)


def lexical(word):
    return word.get('type')=='word' and not word.get('text','').strip().startswith(('[','('))


def anchor_path(name,bank):
    # Original English references only; NEVER feed a generated Russian preview back into training.
    journal=load(LOCAL/'clones'/(hashlib.sha256(name.encode()).hexdigest()[:16]+'.json'),{})
    candidates=[Path(journal.get('additional_reference_file','_missing_')),Path(journal.get('reference_file','_missing_'))]
    candidates.extend((BASE/'outputs/GameVoice/voice-samples').glob('session-*/voice-bank-review/'+name+'-reference-clean-v2.wav'))
    candidates.extend((BASE/'outputs/GameVoice/voice-samples').glob('session-*/voice-bank-review/'+name+'-review.wav'))
    for path in candidates:
        if path.is_absolute() and path.is_file():return path
    for row in sorted(bank.rows(name),key=lambda r:r['seconds'],reverse=True):
        if row['seconds']>=2:return Path(row['path'])
    return None


def anchored_entry(entry,folder,bank):
    anchor=anchor_path(entry['name'],bank)
    if anchor is None:return entry,0
    source=folder/entry['reference'];target=source.with_name('anchored-review.wav')
    if not target.exists():
        with wave.open(str(anchor),'rb') as wav:
            if wav.getparams()[:3]!=(1,2,24000):return entry,0
            pcm=wav.readframes(min(wav.getnframes(),24000*12))
        with wave.open(str(source),'rb') as wav:
            rest=wav.readframes(wav.getnframes())
        with wave.open(str(target),'wb') as wav:
            wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(pcm+bytes(24000)+rest)
        atomic_json(target.with_suffix('.json'),{'anchor':str(anchor),'anchor_sha256':sha(anchor),
            'anchor_seconds':len(pcm)/48000,'offset':len(pcm)/48000+.5,'source_sha256':sha(source)})
    data=load(target.with_suffix('.json'))
    if data['source_sha256']!=sha(source):raise ValueError('Изменён исходник якорной подборки.')
    return {**entry,'reference':str(target.relative_to(folder)),'seconds':entry['seconds']+data['offset']},data['offset']


def ingest(folder,entry,details,transcript,bank,offset=0):
    """Map each retained word to unique source time and to its cleaned PCM offset."""
    source=folder/entry['reference']
    if sha(source)!=details['input_sha256'] or transcript['input_sha256']!=details['input_sha256']:
        raise ValueError('Несовпадающие источник и разметка.')
    if details.get('dominant_word_duration_share',0)<.8:return {'accepted':0,'reason':'mixed_speakers'}
    audit=load(folder/'audit.json');snap=load(folder/'snapshot.json')
    session=recording_identity(snap['source_session'])
    clips=[c for c in audit['candidates'] if c['display_name']==entry['name']]
    with wave.open(str(source),'rb') as wav:
        if wav.getparams()[:3]!=(1,2,24000):raise ValueError('Неверный формат очищенного источника.')
        pcm=wav.readframes(wav.getnframes())
    accepted=0
    for clip in clips:
        if sum(r['seconds'] for r in bank.rows(entry['name']))>=180:break
        lo=clip['montage_start']+offset;hi=clip['montage_end']+offset
        intervals=[]
        for a,b in details['intervals']:
            a,b=max(lo,a),min(hi,b)
            if b-a>=.15:intervals.append((round(a*24000),round(b*24000)))
        selected=[w for w in transcript['words'] if lexical(w) and w.get('speaker_id')==details['dominant_speaker']
                  and w['start']>=lo-.01 and w['end']<=hi+.01]
        spoken=text_key(' '.join(w['text'] for w in selected))
        if len(selected)<3 or len(spoken)<8:continue
        if any(SequenceMatcher(None,spoken,r['text_key']).ratio()>.9 for r in bank.rows(entry['name'])):continue
        chunks=[];words=[];at=0
        for a,b in intervals:
            if chunks:chunks.append(bytes(4800));at+=2400
            chunks.append(pcm[a*2:b*2])
            for word in selected:
                first,last=max(a,round(word['start']*24000)),min(b,round(word['end']*24000))
                if last>first:words.append((at+first-a,at+last-a))
            at+=b-a
        merged=[]
        for a,b in sorted(words):
            if merged and a<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],b)
            else:merged.append([a,b])
        seconds=sum(b-a for a,b in merged)/24000
        result=b''.join(chunks)
        if seconds<.8 or not result or audioop.rms(result,2)<30:continue
        identifier=hashlib.sha256(f"{session}|{clip['start']:.4f}|{clip['end']:.4f}|{entry['name']}".encode()).hexdigest()
        directory=bank.root/'clips'/hashlib.sha256(entry['name'].encode()).hexdigest()[:16]
        directory.mkdir(parents=True,exist_ok=True);target=directory/(identifier+'.wav')
        with wave.open(str(target),'wb') as wav:
            wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(result)
        evidence={'clean_word_intervals':merged,'source_folder':str(folder),'source_sha256':clip['source_sha256'],
            'ocr_texts':clip['texts'],'english_text':' '.join(w['text'] for w in selected),
            'diarization_speaker':details['dominant_speaker'],'word_share':details['dominant_word_duration_share'],
            'anchored':offset>0,'audibly_reviewed':False,'source_intervals':intervals}
        item={'id':identifier,'character':entry['name'],'session':session,'start':clip['start'],'end':clip['end'],
            'text_key':spoken,'pcm_hash':sha(target),'seconds':seconds,'path':str(target),'evidence':evidence}
        if bank.add(item):accepted+=1
    return {'accepted':accepted,'seconds':sum(r['seconds'] for r in bank.rows(entry['name']))}


def process(folder,bank):
    from clean_recorded_voice_refs import transcribe,clean
    manifest=load(folder/'manifest.json')
    reports=[]
    for original in manifest['characters']:
        entry,offset=anchored_entry(original,folder,bank)
        transcript=transcribe(folder/entry['reference'])
        dominant=None
        if offset:
            durations=Counter()
            for word in transcript.get('words',[]):
                if lexical(word) and word.get('speaker_id') and word['end']<=offset-.5:
                    durations[word['speaker_id']]+=max(0,word['end']-word['start'])
            if not durations or durations.most_common(1)[0][1]<1.5 or durations.most_common(1)[0][1]/sum(durations.values())<.8:
                reports.append({'name':entry['name'],'state':'anchor_uncertain'});continue
            dominant=durations.most_common(1)[0][0]
        candidate_seconds=Counter()
        for word in transcript.get('words',[]):
            if lexical(word) and word.get('speaker_id') and word['start']>=offset:
                candidate_seconds[word['speaker_id']]+=max(0,word['end']-word['start'])
        if not candidate_seconds:
            reports.append({'name':entry['name'],'state':'no_candidate_speech'});continue
        selected=dominant or candidate_seconds.most_common(1)[0][0]
        share=candidate_seconds[selected]/sum(candidate_seconds.values())
        if share < (.85 if offset else .9):
            reports.append({'name':entry['name'],'state':'speaker_mismatch_or_mixed','share':share});continue
        result=clean(entry,dominant,folder)
        detail_name=entry['name']+'-'+dominant+'-candidate.json' if dominant else 'reference-clean.json'
        details=load((folder/entry['reference']).parent/detail_name)
        if details is None:reports.append({'name':entry['name'],'state':'no_speech'});continue
        reports.append({'name':entry['name'],**ingest(folder,entry,details,transcript,bank,offset)})
        atomic_json(folder/'bank-result.json',{'characters':reports,'updated':utc()})
    atomic_json(folder/'bank-result.json',{'state':'done','characters':reports,'updated':utc()})


def seed(bank):
    if bank.db.execute("SELECT 1 FROM metadata WHERE key='seed-v1'").fetchone():return
    folder=Path('D:/GameVoice/voice-samples/session-20261120-173608/processed-20261120-2115')
    manifest=load(folder/'manifest.json',{'characters':[]})
    report=[]
    for entry in manifest['characters']:
        details=load((folder/entry['reference']).parent/'reference-clean.json')
        transcript=load((folder/entry['reference']).parent/'reference-transcript.json')
        if details and transcript:report.append({'name':entry['name'],**ingest(folder,entry,details,transcript,bank)})
    atomic_json(bank.root/'seed-result.json',report)
    with bank.db:bank.db.execute("INSERT INTO metadata VALUES('seed-v1',?)",(utc(),))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['seed','process','isolate'])
    parser.add_argument('--folder');parser.add_argument('--root',default=str(ROOT));args=parser.parse_args()
    if args.stage=='isolate':
        os.environ['GAMEVOICE_PROCESS_GPU']='1'
        from process_recorded_voices import isolate
        with InstanceLock(Path(args.root)/'isolation.lock'):isolate(Path(args.folder))
        return
    bank=Bank(args.root)
    try:
        with InstanceLock(bank.root/'processor.lock'):
            if args.stage=='seed':seed(bank);return
            folder=Path(args.folder)
            if not (folder/'audit.json').exists():prepare_part(load(folder/'part.json'),folder,bank)
            if not (folder/'manifest.json').exists() or load(folder/'manifest.json').get('state')!='isolated':
                env=dict(os.environ,OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',OPENBLAS_NUM_THREADS='4',GAMEVOICE_PROCESS_GPU='1')
                command=['D:/FalloutVoiceoverLocal/.venv-pc/Scripts/python.exe',str(Path(__file__).resolve()),'isolate','--folder',str(folder),'--root',str(bank.root)]
                subprocess.run(command,env=env,check=True,creationflags=0x08000000|0x00004000)
            process(folder,bank)
    finally:bank.close()


if __name__=='__main__':main()
