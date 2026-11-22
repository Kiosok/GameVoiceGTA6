"""Opt-in nameless TV captions: transient only, always below named dialogue."""
import re
import time
from collections import Counter
from speaker_voices import speaker_key,is_interface_label,ROLE_KEYS

TV_VOICE='onwK4e9ZLuTAKqWW03F9'  # Daniel, stock broadcaster; never a character clone.
TV_SPEAKER='Телевизор'

def character_label(name,rows,known):
    """In TV mode a white colon/mention is speech, not a game speaker label."""
    if speaker_key(name) in {'тема','поверьте','ведущий','ведущая','объявление'}:return False
    if not rows:return False
    words=rows[0].get('words',[])
    if not any(w.get('coloured') for w in words[:4]):return False
    # New characters still work with a coloured Name:. A missing delimiter
    # requires an exact known identity, not a fuzzy match against a TV word.
    return bool(re.search('[:：]',rows[0]['text']) or speaker_key(name) in {*known,*ROLE_KEYS})

def menu_visible(text):
    text=' '.join(text.casefold().replace('ё','е').split())
    tabs=('карта','игра','статистика','настройки','галерея','помощь','сохранение')
    return sum(bool(re.search(r'\b'+tab+r'\b',text)) for tab in tabs)>=2

def tv_sentence(text):
    if len(re.findall('[а-яё]',text.casefold()))<4:return False
    if is_interface_label(text):return False
    return not re.search(r'(?i)нажмите|удерживайте|выберите|вернуться в меню|загрузка|'
                         r'точка маршрута|поставить метку|продать за|купить за|\d+\s*[$₽€]',text)

class TVCaptions:
    def __init__(self,gate):
        self.gate=gate;self.last_named=-1000
        self.reason='';self.counts=Counter()
    def update(self,groups,names,now,blocked=False,named_accepted=()):
        self.counts['frames']+=1
        clean=[];has_named=False
        for name,text in groups:
            if name:
                if not is_interface_label(name):has_named=True
            elif tv_sentence(text):clean.append(text)
        if has_named or named_accepted:self.last_named=now
        self.reason=('menu' if blocked else 'named' if has_named or named_accepted else
                     'dialogue_hold' if now-self.last_named<1.5 else '')
        blocked=bool(self.reason)
        self.counts[self.reason or ('text' if clean else 'empty')]+=1
        if blocked or not clean:
            self.gate.update_dialogue('','',now)
            return None,blocked
        cue=self.gate.update_dialogue(' '.join(clean),TV_SPEAKER,now)
        if cue:self.counts['accepted']+=1
        return cue,False

    def diagnostics(self):return dict(reason=self.reason,counts=dict(self.counts))

class BackgroundMixer:
    def __init__(self,mixer):self.mixer=mixer
    @property
    def output_device_name(self):return self.mixer.output_device_name
    @output_device_name.setter
    def output_device_name(self,value):self.mixer.output_device_name=value
    def set_pending(self,count):pass  # TV must not acquire named-dialogue priority.
    def play_character(self,pcm,volume,valid,started):
        return self.mixer.play_character(pcm,volume,
            lambda:valid() and not self.mixer.priority(),started,background=True)

class TVSpeech:
    def __init__(self,worker_factory,mixer,voice,emit):
        self.emit=emit;self.mixer=mixer;self.voice=voice
        self.latest=None;self.fault=False;self.running=False;self.active=False
        self.requests=0;self.received=0;self.played=0;self.last_model=None;self.last_latency_ms=None
        self.counts=Counter();self.block_reason=None
        self.worker=worker_factory(self.event,voice)
        self.worker.audio_mixer=BackgroundMixer(mixer)
    def event(self,kind,value):
        # No route log, character registry, transcript archive or PCM cache for TV.
        if kind in ('voice_route','voice_audit'):
            phase=value.get('phase')
            if phase=='request':self.requests+=1;self.last_model=value.get('model_id')
            elif phase=='audio_received':self.received+=1
            elif phase=='playback_started':self.played+=1;self.last_latency_ms=value.get('latency_ms')
            elif phase=='request_failed':self.counts['request_failed']+=1
            return
        if kind=='error':
            self.fault=True;self.emit('ТВ: '+str(value),'error')
        elif kind in ('connection_error','output_error'):
            if kind=='output_error':self.fault=True
            self.emit('ТВ: '+str(value),'error' if kind=='output_error' else 'waiting')
        elif kind=='synthesis':self.emit('ТВ: голос получен','ok')
        elif kind=='latency':self.emit('ТВ: озвучиваю передачу','ok')
    def offer(self,cue):
        if not self.fault:
            if self.latest:self.counts['replaced_waiting']+=1
            self.latest=cue;self.counts['offered']+=1
    def tick(self,active,blocked,output,speed,catch_up,model=None,reason='blocked'):
        if active and not self.active:self.fault=False
        self.active=bool(active)
        block_reason=('off' if not self.active else 'error' if self.fault else
                      reason if blocked else 'dialogue_audio' if self.mixer.priority() else '')
        allowed=not block_reason
        if not allowed:
            if self.latest:self.counts['discarded_waiting']+=1
            self.latest=None
        if allowed!=self.running:
            if not allowed and self.worker.pending_count:
                self.counts['cancelled_'+block_reason]+=self.worker.pending_count
            self.running=allowed;self.worker.set_enabled(allowed)
        if block_reason!=self.block_reason:
            self.block_reason=block_reason;self.counts['state_changes']+=1
            labels={'off':'выключено','menu':'пауза · меню','named':'пауза · диалог персонажей',
                    'dialogue_hold':'пауза после диалога','dialogue_audio':'жду завершения диалога',
                    'blocked':'пауза · меню или диалог'}
            if not self.fault:self.emit('ТВ: '+labels.get(block_reason,'жду субтитры'),
                                        'waiting' if active else 'off')
        self.worker.output_device_name=output
        self.worker.speed=speed;self.worker.catch_up=catch_up
        if model and hasattr(self.worker,'set_model') and self.worker.model_id!=model:self.worker.set_model(model)
        # Prefetch the next complete phrase during playback; waiting until the
        # previous phrase ends adds a full network roundtrip to every caption.
        if allowed and self.latest and self.worker.pending_count<2:
            cue,self.latest=self.latest,None
            # Keep only the newest pending line; never build an old TV backlog.
            if time.monotonic()-cue.stamp<4:
                self.worker.submit(cue.text,cue.stamp,self.voice,TV_SPEAKER,strip_label=False)
                self.counts['submitted']+=1
            else:self.counts['stale_waiting']+=1
    def close(self):self.worker.close()
    def diagnostics(self):
        # Aggregate counters only; never retain the caption text.
        return dict(active=self.active,running=self.running,fault=self.fault,voice_id=self.voice,
                    requests=self.requests,received=self.received,played=self.played,
                    last_model=self.last_model,last_latency_ms=self.last_latency_ms,
                    block_reason=self.block_reason,counts=dict(self.counts),pending=self.worker.pending_count)
