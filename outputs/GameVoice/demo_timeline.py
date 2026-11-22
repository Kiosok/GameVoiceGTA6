"""Bounded, asynchronous editing sidecars; never write on the audio thread."""
import csv
import json
from pathlib import Path
import queue
import threading


def srt_time(seconds):
    ms=max(0,round(seconds*1000))
    hours,ms=divmod(ms,3600000);minutes,ms=divmod(ms,60000);seconds,ms=divmod(ms,1000)
    return f'{hours:02}:{minutes:02}:{seconds:02},{ms:03}'


class DemoTimeline:
    def __init__(self, folder, started, emit):
        self.folder=Path(folder);self.started=started;self.emit=emit
        self.items=queue.Queue(maxsize=2048);self.stop=threading.Event()
        self.failed=False;self.duration=0
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def warn(self):
        if not self.failed:
            self.failed=True
            self.emit('demo_status','Не удалось сохранить все таймкоды видео; запись видео продолжается.')

    def append(self, record):
        if self.stop.is_set():return
        try:self.items.put_nowait(dict(record))
        except queue.Full:self.warn()

    def close(self,duration):
        self.duration=duration;self.stop.set();self.thread.join()

    def run(self):
        events=[]
        try:
            with (self.folder/'timeline.jsonl').open('w',encoding='utf8') as stream:
                stream.write(json.dumps({'schema':1,'event':'recording_start','fps':30,
                    'timebase':'seconds from first video frame',
                    'playback_end':'estimated from PCM duration; not proof of completion',
                    'source_end':'estimated from next subtitle, capped at 6 seconds'},ensure_ascii=False)+'\n')
                while not self.stop.is_set() or not self.items.empty():
                    try:record=self.items.get(timeout=.1)
                    except queue.Empty:continue
                    record['video_seconds']=record.get('at_monotonic',self.started)-self.started
                    if isinstance(record.get('source_stamp'),(int,float)):
                        record['subtitle_seconds']=record['source_stamp']-self.started
                    events.append(record)
                    stream.write(json.dumps(record,ensure_ascii=False)+'\n');stream.flush()
                stream.write(json.dumps({'event':'recording_end','duration_seconds':self.duration})+'\n')
            self.export(events)
        except (OSError,ValueError,TypeError):self.warn()

    def export(self,events):
        source=[];played=[];seen=set()
        for e in events:
            if e.get('phase')=='subtitle':
                key=(e.get('source_stamp'),e.get('speaker'),e.get('text'))
                if key not in seen:
                    seen.add(key);source.append(e)
            if e.get('phase')=='playback_started':played.append(e)
        source.sort(key=lambda e:e.get('subtitle_seconds',e['video_seconds']))
        with (self.folder/'clips.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=['start_seconds','end_seconds_estimate',
                'cut_start_seconds','cut_end_seconds','subtitle_seconds','speaker','voice_id','model_id','text'])
            writer.writeheader()
            for e in played:
                start=max(0,e['video_seconds']);end=min(self.duration,e['video_seconds']+e.get('audio_seconds',0))
                if end<=start:continue
                writer.writerow(dict(start_seconds=round(start,3),end_seconds_estimate=round(end,3),
                    cut_start_seconds=round(max(0,start-.5),3),cut_end_seconds=round(min(self.duration,end+.75),3),
                    subtitle_seconds=round(e.get('subtitle_seconds',start),3),
                    **{key:e.get(key,'') for key in ('speaker','voice_id','model_id','text')}))
        for filename,entries,original in [('subtitles-game.srt',source,True),('subtitles-voice.srt',played,False)]:
            with (self.folder/filename).open('w',encoding='utf-8-sig') as stream:
                count=0
                for i,e in enumerate(entries):
                    start=e.get('subtitle_seconds',e['video_seconds']) if original else e['video_seconds']
                    if original:
                        following=entries[i+1].get('subtitle_seconds',entries[i+1]['video_seconds']) if i+1<len(entries) else self.duration
                        end=min(start+6,following)
                    else:end=start+max(0,e.get('audio_seconds',0))
                    start=max(0,start);end=min(self.duration,end)
                    if end<=start:continue
                    count+=1
                    text=' '.join(str(e.get('text','')).split())
                    speaker=e.get('speaker','')
                    stream.write(f'{count}\n{srt_time(start)} --> {srt_time(end)}\n'+
                                 (f'{speaker}: ' if speaker else '')+text+'\n\n')
