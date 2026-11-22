"""User policy: at least 60 seconds of clean speech before new clone creation.

Audio length is a lower-bound guard; prepared references also require word-level
evidence so padding and silence cannot make a short reference eligible.
"""
import hashlib
import io
import wave

MINIMUM_CLEAN_SPEECH_SECONDS = 60

def require_minimum_audio(files):
    seen=set();seconds=0
    for raw in files:
        digest=hashlib.sha256(raw).digest()
        if digest in seen:continue
        seen.add(digest)
        with wave.open(io.BytesIO(raw),'rb') as wav:
            seconds+=wav.getnframes()/wav.getframerate()
    if seconds < MINIMUM_CLEAN_SPEECH_SECONDS:
        raise ValueError(f'Накопление: {seconds:.2f}/60 с образцов. Новый клон пока не создаётся.')
    return seconds

def speech_seconds(details, transcript):
    if details['input_sha256'] != transcript['input_sha256']:
        raise ValueError('Разметка относится к другому образцу.')
    rate=24000
    selected=[]
    for word in transcript['words']:
        if word['type']!='word' or word.get('speaker_id')!=details['dominant_speaker']:
            continue
        if word.get('text','').strip().startswith(('[','(')):continue
        for start,end in details['intervals']:
            lo=round(max(start,word['start'])*rate)
            hi=round(min(end,word['end'])*rate)
            if hi>lo:selected.append((lo,hi))
    merged=[]
    for lo,hi in sorted(selected):
        if merged and lo<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],hi)
        else:merged.append([lo,hi])
    return sum(hi-lo for lo,hi in merged)/rate

def require_clean_speech(details, transcript):
    seconds=speech_seconds(details,transcript)
    if seconds < MINIMUM_CLEAN_SPEECH_SECONDS:
        raise ValueError(f'Накопление: {seconds:.2f}/60 с чистой речи. Создание или обновление клона отложено.')
    return seconds
