"""Russian HDMI subtitles -> local speech. Does not capture audio or desktop."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from difflib import SequenceMatcher
import io
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
import wave

from PIL import Image, ImageTk
import sounddevice as sd
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.media.speechsynthesis import SpeechSynthesizer
from winrt.windows.storage.streams import DataWriter, DataReader

from gamevoice import ffmpeg_path, HIDDEN
from speaker_voices import SpeakerVoices, VOICES, split_speaker, speaker_key, resolve_speaker_key, AcceptedSubtitle, is_interface_label
from subtitle_labels import coloured_word, separate_label, separate_captions
from device_settings import load_roi,save_setting,settings as device_settings,output_names
from device_settings import ROI_DEFAULTS, ROI_LABELS, selected_roi_profile, save_roi_profile, roi_ocr_options
from voice_samples import SubtitleSampleSink
from video_timing import FrameRate, DeadlineTicker
from service_health import ServiceHealth, COLOURS

WIDTH, HEIGHT = 1280, 720
CAPTURE_FPS = 30
OCR_FPS = 30
# Smaller font with backdrop: measured upper named row at y=551; keep
# padding above it for simultaneous and wrapped dialogue, exclude lower HUD.
DEFAULT_ROI = (.08, .74, .92, .918)


def normalize(text):
    return ' '.join(re.findall(r'[а-яёa-z0-9]+', text.casefold().replace('ё', 'е')))


def similar(a, b):
    return bool(a and b) and (a == b or SequenceMatcher(None, a, b).ratio() >= .91)


def fuzzy_fragment(short,long):
    """Edit distance to any substring, for an OCR-truncated subtitle line."""
    if len(short)>len(long): short,long=long,short
    if len(short)<12 or len(short)>.85*len(long): return False
    previous=[0]*(len(long)+1)
    for index,left in enumerate(short,1):
        current=[index]
        for j,right in enumerate(long,1):
            current.append(min(current[-1]+1,previous[j]+1,previous[j-1]+(left!=right)))
        previous=current
    return min(previous)<=max(2,round(len(short)*.18))


def same_caption(a, b):
    if similar(a, b): return True
    ac,bc = a.replace(' ',''),b.replace(' ','')
    if min(len(ac),len(bc)) >= 12:
        if ac in bc: return True
        match=SequenceMatcher(None,ac,bc)
        if match.ratio() >= .76: return True
        covered=sum(block.size for block in match.get_matching_blocks())
        if covered/min(len(ac),len(bc)) >= .88: return True
        if fuzzy_fragment(ac,bc): return True
    # During a subtitle transition OCR can first accept the new caption with
    # a leftover line attached, then return the clean caption. Do not narrate
    # that contained caption again. Direction matters: appended words in a
    # genuinely extended new caption must still be accepted below.
    if len(a) >= 20 and f' {a} ' in f' {b} ': return True
    # A moving background can corrupt several glyphs in an otherwise unchanged
    # long caption. Use a looser threshold only for suppression, not acceptance.
    return min(len(a), len(b)) >= 20 and SequenceMatcher(None, a, b).ratio() >= .82


class SubtitleGate:
    """Require stable observations; suppress OCR jitter, rolling overlap and repeats."""
    def __init__(self, names=()):
        self.candidate = ''
        self.count = 0
        self.first_seen = 0
        self.last_nonempty = 0
        self.spoken = ''
        self.spoken_speaker = ''
        self.candidate_speaker = ''
        self.speaker_names = {'джейсон', 'лусия', *names}
        self.recent = deque(maxlen=12)
        self.last_label_seen = -1000
        self.active_speaker = ''

    def update(self, text, now):
        text = ' '.join(text.split()).strip(' |')
        speaker, dialogue = split_speaker(text,self.speaker_names)
        accepted = self.update_dialogue(dialogue,speaker,now)
        if accepted:
            label = f'{speaker}: ' if speaker else ''
            return label+accepted.text, accepted.stamp

    def update_dialogue(self, dialogue, speaker, now):
        """Keep metadata separate: accepted text can never regain a label."""
        dialogue = ' '.join(dialogue.split()).strip(' |')
        who = resolve_speaker_key(speaker, self.speaker_names)
        if not who:
            who = self.active_speaker
        norm = normalize(dialogue)
        if len(re.findall('[а-яё]', dialogue.lower())) < 2 or len(norm) < 2:
            if now - self.last_nonempty > .65:
                # A pause menu can hide a caption for arbitrarily long. Reset
                # visual stability, but remember the last accepted dialogue
                # until a different caption advances it. A time limit here
                # would cause the same line to be spoken again after resume.
                self.candidate = ''; self.count = 0
            return None
        self.last_nonempty = now
        if who == self.candidate_speaker and similar(norm, normalize(self.candidate)):
            self.count += 1
            self.candidate = dialogue
        else:
            self.candidate = dialogue; self.candidate_speaker = who
            self.count = 1; self.first_seen = now
        # Short dialogue (including standalone expletives) is legitimate. Use
        # an extra stable observation instead of discarding the word outright.
        if self.count < (3 if len(norm) < 6 else 2) or now-self.first_seen < (.09 if len(norm)<6 else .14):
            return None
        # Compare the dialogue independently of a noisy name. Real short
        # replies ('Да', 'Нет') by different known speakers still remain valid.
        cross_speaker = len(norm.replace(' ','')) >= 12
        extension = bool(who == self.spoken_speaker and self.spoken and norm.startswith(self.spoken+' '))
        # OCR can momentarily lose just the coloured name. An unlabeled copy
        # of the same words is not another speaker repeating the line.
        if not extension and (cross_speaker or who == self.spoken_speaker or not who or not self.spoken_speaker) and same_caption(norm, self.spoken):
            return None
        if not extension and any((cross_speaker or who == old_who or not who or not old_who) and same_caption(norm, old) and now - when < 12
               for old, when, old_who in self.recent):
            return None
        output = dialogue
        # A subtitle may be extended in place: speak only the newly appended words.
        if who == self.spoken_speaker and self.spoken and norm.startswith(self.spoken + ' '):
            consumed = len(self.spoken.split())
            words = dialogue.split()
            output = ' '.join(words[consumed:])
            if len(normalize(output)) < 3:
                return None
        self.spoken = norm; self.spoken_speaker = who
        if speaker:
            self.active_speaker = who; self.last_label_seen = now
            self.speaker_names.add(who)
        self.recent.append((norm, now, who))
        return AcceptedSubtitle(output,who,self.first_seen)


class SubtitleTracker:
    """Independent stability for simultaneous speakers, shared dialogue dedup."""
    def __init__(self,names=(), resolver=None, require_named=False):
        self.speaker_names={'джейсон','лусия',*names}
        self.resolver = resolver
        self.gates={}
        self.recent=deque(maxlen=64)
        self.last_speaker=''
        self.require_named=require_named
        self.label_context={}

    def update_frame(self,groups,now):
        accepted=[];visible=set()
        for name,text in groups:
            known=(*self.speaker_names,*self.gates)
            if not name:
                recovered,body=split_speaker(text,known)
                if recovered: name,text=recovered,body
            labelled=bool(name)
            if self.require_named:
                if name:
                    key=speaker_key(name)
                    if sum(c.isalpha() for c in key)<2 or is_interface_label(name):
                        continue
                else:
                    # Only a fragment of a recently labelled caption can retain
                    # its voice. New menu/HUD text cannot start a speakerless cue.
                    norm=normalize(text)
                    matches=[who for who,(body,stamp) in self.label_context.items()
                             if now-stamp<=1.5 and norm and
                             (norm==body or len(norm)>=4 and f' {norm} ' in f' {body} ')]
                    if len(matches)!=1: continue
                    name=matches[0]
            who=(self.resolver(name) if self.resolver else resolve_speaker_key(name,known)) if name else self.last_speaker
            if name and normalize(text):
                # Do not refresh this evidence from an unlabelled fragment.
                if labelled:
                    self.label_context[who]=(normalize(text),now)
            visible.add(who)
            if who not in self.gates:
                gate=SubtitleGate(self.speaker_names)
                gate.speaker_names=self.speaker_names
                gate.recent=self.recent
                self.gates[who]=gate
            value=self.gates[who].update_dialogue(text,who,now)
            if value:
                accepted.append(value)
                if who:self.last_speaker=who
        if not groups:
            for gate in self.gates.values():gate.update_dialogue('','',now)
        if accepted:
            # A truly new dialogue advances a hidden speaker's history. Keep
            # visible old captions sticky, including both sides of an exchange.
            for who,gate in self.gates.items():
                if who not in visible:gate.spoken=''
        return accepted


class VideoCapture:
    def __init__(self, emit):
        self.emit = emit
        self.stop_event = threading.Event()
        self.proc = None
        self.latest = None
        self.counter = 0
        self.frame_rate = FrameRate()
        self.lock = threading.Lock()
        self.frame_ready = threading.Condition(self.lock)
        self.errors = deque(maxlen=4)
        self.thread = None

    def start(self):
        self.proc = subprocess.Popen([ffmpeg_path(), '-hide_banner', '-loglevel', 'warning',
            '-rtbufsize', '16M', '-f', 'dshow', '-video_size', f'{WIDTH}x{HEIGHT}',
            '-framerate', str(CAPTURE_FPS), '-vcodec', 'mjpeg', '-i', 'video=USB3.0 Video',
            '-an', '-c:v', 'copy', '-f', 'image2pipe', 'pipe:1'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=HIDDEN, bufsize=0)
        threading.Thread(target=self.read_errors, daemon=True).start()
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read_errors(self):
        for line in self.proc.stderr:
            self.errors.append(line.decode('utf8', 'replace').strip())

    def read(self):
        # Keep MJPEG compressed across the Windows pipe (rather than moving
        # 83 MB/s of raw RGB through its small buffers). Decode each complete
        # JPEG locally; resolution and camera frames remain unchanged.
        data = bytearray()
        while not self.stop_event.is_set():
            chunk = self.proc.stdout.read(65536)
            if not chunk:
                if not self.stop_event.is_set():
                    self.emit('error', 'Видео остановилось. ' + ' '.join(self.errors)[-400:])
                return
            data.extend(chunk)
            while True:
                start=data.find(b'\xff\xd8')
                if start<0:
                    data[:]=data[-1:];break
                end=data.find(b'\xff\xd9',start+2)
                if end<0:
                    if start:del data[:start]
                    if len(data)>10*1024*1024:
                        self.emit('error','Карта вернула повреждённый MJPEG-кадр.');return
                    break
                jpeg=bytes(data[start:end+2]);del data[:end+2]
                try:
                    with Image.open(io.BytesIO(jpeg)) as encoded:
                        frame=encoded.convert('RGB')
                    if frame.size!=(WIDTH,HEIGHT):
                        raise ValueError('Неверный размер кадра с карты.')
                except (OSError,ValueError) as exc:
                    self.errors.append(str(exc));continue
                with self.frame_ready:
                    self.counter += 1
                    stamp = time.monotonic()
                    self.latest = self.counter, stamp, frame
                    self.frame_rate.record(time.perf_counter())
                    self.frame_ready.notify_all()

    def snapshot(self):
        with self.lock:
            return self.latest

    def measured_fps(self, now):
        with self.lock:
            return self.frame_rate.value(now)

    def wait_for_frame(self, after):
        with self.frame_ready:
            self.frame_ready.wait_for(lambda:self.stop_event.is_set() or
                self.latest is not None and self.latest[0]!=after,timeout=.1)
            return self.latest

    def close(self):
        self.stop_event.set()
        with self.frame_ready:self.frame_ready.notify_all()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try: self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill(); self.proc.wait(timeout=3)
        if self.thread: self.thread.join(timeout=3)
        if self.proc:
            self.proc.stdout.close(); self.proc.stderr.close()


class RussianOCR:
    def __init__(self):
        self.engine = OcrEngine.try_create_from_language(Language('ru'))
        if not self.engine:
            raise RuntimeError('В Windows отсутствует русское распознавание текста.')

    async def recognize(self, frame, roi, scale=1, min_line_height=17, unrestricted=False):
        left, top, right, bottom = [round(v * d) for v, d in zip(roi, (WIDTH, HEIGHT, WIDTH, HEIGHT))]
        crop = frame.crop((left, top, right, bottom)).convert('RGBA')
        if scale != 1:
            crop = crop.resize((round(crop.width*scale), round(crop.height*scale)), Image.Resampling.BICUBIC)
        writer = DataWriter()
        writer.write_bytes(crop.tobytes('raw', 'BGRA'))
        bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(),
            BitmapPixelFormat.BGRA8, crop.width, crop.height, BitmapAlphaMode.IGNORE)
        started = time.monotonic()
        try:
            result = await self.engine.recognize_async(bitmap)
            lines = []
            for line in result.lines:
                words = list(line.words)
                if not words: continue
                x1 = min(w.bounding_rect.x for w in words)/scale + left
                x2 = max(w.bounding_rect.x + w.bounding_rect.width for w in words)/scale + left
                y1 = min(w.bounding_rect.y for w in words)/scale + top
                y2 = max(w.bounding_rect.y + w.bounding_rect.height for w in words)/scale + top
                if y2 - y1 < min_line_height: continue
                word_data=[]
                scan_label = True
                seen_colour = False
                for index,word in enumerate(words):
                    rect=word.bounding_rect
                    box=[rect.x/scale+left,rect.y/scale+top,(rect.x+rect.width)/scale+left,(rect.y+rect.height)/scale+top]
                    colour = not unrestricted and scan_label and index < 7 and coloured_word(frame,box)
                    word_data.append({'text':word.text,'box':box,'coloured':colour})
                    if colour: seen_colour=True
                    elif seen_colour or sum(char.isalpha() for char in word.text)>2: scan_label=False
                # An OCR line containing only the coloured name can sit well
                # left of its centered dialogue. Retain it as metadata.
                label_only = word_data[0]['coloured'] and any(mark in line.text for mark in (':','：'))
                if not unrestricted and abs((x1 + x2) / 2 - WIDTH / 2) > WIDTH * .3 and not label_only: continue
                lines.append({'text': line.text, 'box': [x1, y1, x2, y2], 'words':word_data})
            return ' '.join(l['text'] for l in lines), lines, (time.monotonic() - started) * 1000
        finally:
            bitmap.close(); writer.close()


class SpeechWorker:
    def __init__(self, emit, voice_name='Microsoft Pavel'):
        self.emit = emit
        self.voice_name = voice_name
        self.enabled = threading.Event()
        self.stopping = threading.Event()
        self.queue = queue.Queue(maxsize=12)
        self.rate = 1.25
        self.volume = .9
        self.epoch = 0
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def set_enabled(self, value):
        with self.lock:
            self.epoch += 1
            if value: self.enabled.set()
            else: self.enabled.clear()
        while True:
            try: self.queue.get_nowait()
            except queue.Empty: break

    def submit(self, text, first_seen):
        if not self.enabled.is_set(): return
        item = text, first_seen, self.epoch
        try: self.queue.put_nowait(item)
        except queue.Full:
            self.emit('notice', 'Очередь заполнена: новая реплика не принята. Начатые фразы будут дочитаны.')

    async def synthesize(self, synth, text):
        stream = await synth.synthesize_text_to_stream_async(text)
        try:
            reader = DataReader(stream.get_input_stream_at(0))
            try:
                await reader.load_async(stream.size)
                data = bytearray(stream.size)
                reader.read_bytes(data)
                return data
            finally: reader.close()
        finally: stream.close()

    def valid(self, epoch):
        return self.enabled.is_set() and not self.stopping.is_set() and self.epoch == epoch

    def run(self):
        loop = asyncio.new_event_loop()
        synth = None
        try:
            synth = SpeechSynthesizer()
            voices = [v for v in SpeechSynthesizer.all_voices if v.language.startswith('ru')]
            if not voices: raise RuntimeError('Не установлен русский голос Windows.')
            synth.voice = next((v for v in voices if v.display_name == self.voice_name), voices[0])
            synth.options.speaking_rate = self.rate
            while not self.stopping.is_set():
                try: text, first_seen, epoch = self.queue.get(timeout=.1)
                except queue.Empty: continue
                if not self.valid(epoch):
                    continue
                synth.options.speaking_rate = self.rate
                data = loop.run_until_complete(self.synthesize(synth, text))
                if not self.valid(epoch): continue
                with wave.open(io.BytesIO(data), 'rb') as wav:
                    if wav.getsampwidth() != 2: raise RuntimeError('Голос вернул неподдерживаемый формат.')
                    import audioop
                    with sd.RawOutputStream(samplerate=wav.getframerate(), channels=wav.getnchannels(),
                                            dtype='int16', latency='low') as output:
                        first = True
                        while self.valid(epoch):
                            chunk = wav.readframes(max(1, wav.getframerate() // 50))
                            if not chunk: break
                            output.write(audioop.mul(chunk, 2, self.volume))
                            if first:
                                self.emit('latency', round((time.monotonic() - first_seen) * 1000))
                                first = False
                        if not self.valid(epoch): output.abort()
        except Exception as exc:
            self.emit('error', f'Ошибка местного голоса: {exc}')
            self.enabled.clear()
        finally:
            if synth: synth.close()
            loop.close()

    def close(self):
        self.set_enabled(False)
        self.stopping.set()
        self.thread.join(timeout=5)


class Observer:
    def __init__(self, emit, roi=None):
        self.emit, self.roi = emit, load_roi(DEFAULT_ROI) if roi is None else roi
        self.roi_profile = selected_roi_profile()
        self.capture = VideoCapture(self.event)
        self.stop_event = threading.Event()
        self.thread = None
        self.failure = None
        self.gate = SubtitleTracker(require_named=True)
        self.tv_enabled=False;self.tv_blocked=True;self.tv_captions=None

    def event(self, kind, value):
        if kind == 'error':
            self.failure = value; self.stop_event.set()
        self.emit(kind, value)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        loop = asyncio.new_event_loop()
        samples = SubtitleSampleSink()
        try:
            ocr = RussianOCR()
            self.capture.start()
            self.emit('status', 'Читаю русские субтитры · голос выключен')
            last = 0
            readings=deque()
            reported=time.monotonic()
            menu_checked=-1000;on_menu=True
            # Wake directly on a captured frame. Timed polling on Windows can
            # add a scheduler tick after every recognition, even for 1 ms waits.
            while not self.stop_event.is_set():
                item = self.capture.wait_for_frame(last)
                if not item or item[0] == last: continue
                last, stamp, frame = item
                scale, min_height = roi_ocr_options(self.roi_profile)
                text, boxes, ms = loop.run_until_complete(ocr.recognize(
                    frame, self.roi, scale=scale, min_line_height=min_height))
                current=time.monotonic(); readings.append(current)
                while readings and readings[0]<current-2:readings.popleft()
                if current-reported>=1 and len(readings)>1:
                    self.emit('read_rate',(len(readings)-1)/(readings[-1]-readings[0]))
                    reported=current
                self.emit('ocr', {'text': text, 'lines': boxes, 'ms': round(ms), 'frame': last})
                if self.tv_enabled:
                    from tv_voice import character_label
                    groups=separate_captions(text,boxes,self.gate.speaker_names,label_policy=character_label)
                else:groups=separate_captions(text,boxes,self.gate.speaker_names)
                # The vehicle/boat radio hotkey can sit inside the subtitle ROI.
                # A persistent HUD label must not permanently pause radio.
                groups=[group for group in groups if group[0] or normalize(group[1]) != 'радио']
                # White TV text can contain colons and even a character's name.
                # Never feed it back to the main tracker's name recovery.
                named=[(n,b) for n,b in groups if n and not is_interface_label(n)]
                accepted_cues = self.gate.update_frame(named if self.tv_enabled else groups,stamp)
                if self.tv_enabled:
                    from tv_voice import TVCaptions,menu_visible
                    if time.monotonic()-menu_checked>=.5:
                        menu_text,_,_=loop.run_until_complete(ocr.recognize(
                            frame,(0,0,1,.76),min_line_height=10,unrestricted=True))
                        on_menu=menu_visible(menu_text);menu_checked=time.monotonic()
                    tv=self.tv_captions
                    if tv is None:
                        tv=self.tv_captions=TVCaptions(SubtitleGate())
                    tv_cue,self.tv_blocked=tv.update(groups,self.gate.speaker_names,stamp,on_menu,accepted_cues)
                    self.emit('dialogue_visible',bool(named or accepted_cues))
                    # TV lines/crops never become a character training sidecar.
                    samples.observe(stamp,last,frame,self.roi,' '.join(b for _,b in named),named,accepted_cues,save_image=False)
                    if tv_cue and self.tv_enabled:self.emit('tv_subtitle',tv_cue)
                else:
                    self.tv_captions=None;self.tv_blocked=True;menu_checked=-1000
                    self.emit('dialogue_visible', any(name or len(normalize(body).split())>=3 for name,body in groups))
                    samples.observe(stamp,last,frame,self.roi,text,groups,accepted_cues)
                for accepted in accepted_cues:
                    self.emit('subtitle', accepted)
        except Exception as exc:
            self.event('error', str(exc))
        finally:
            samples.close()
            self.capture.close()
            loop.close()
            self.emit('done', None)

    def close(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=6)


class App:
    def __init__(self, root, cloud=False, provider='openai'):
        self.root = root
        self.events = queue.Queue()
        self.closed = False
        self.pipeline_checked = 0
        self.pipeline_launched = False
        self.recent_voice_routes = deque(maxlen=32)
        from voice_route_log import VoiceRouteLog
        from voice_samples import active_session, CONTROL
        self.route_log = VoiceRouteLog(CONTROL.parent/'voice-routing', active_session, provider,
                                      lambda value: self.events.put(('notice', value)))
        self.observer = Observer(lambda kind, value: self.events.put(
            ('capture_error' if kind == 'error' else kind, value)))
        self.cloud = cloud
        self.provider = provider
        self.provider_name = 'ElevenLabs' if provider=='elevenlabs' else 'OpenAI'
        self.model_id = None
        self.health = ServiceHealth(self.provider_name)
        self.voice_catalog = {}
        self.voice_options = VOICES
        self.radio = None
        self.audio_mixer = None
        self.demo = None
        self.tv = None
        if cloud:
            from openai_speech import OpenAISpeechWorker
            if provider=='elevenlabs':
                from elevenlabs_speech import ElevenLabsSpeechWorker, registry, voice_catalog, selected_model
                self.model_id = selected_model()
                from elevenlabs_speech import MODEL_LABELS
                self.health.provider = 'ElevenLabs '+MODEL_LABELS[self.model_id].split(' — ')[0]
                self.speakers = registry()
                self.voice_catalog = voice_catalog()
                self.voice_options = tuple(self.voice_catalog)
                worker_type = lambda emit,voice: ElevenLabsSpeechWorker(emit,voice,self.model_id)
            else:
                self.speakers = SpeakerVoices()
                worker_type = OpenAISpeechWorker
            self.observer.gate = self.new_gate()
            self.speech = worker_type(self.speech_event, self.speakers.default_voice)
            self.speech.prepare_voices(self.speakers.voices())
            self.speech.speed = self.speakers.speed
            self.speech.catch_up = self.speakers.catch_up
            self.speech.output_device_name = device_settings().get('output','')
            from radio_mixer import AudioMixer
            from radio_translation import RadioController
            self.audio_mixer = AudioMixer(self.speech.output_device_name)
            self.speech.audio_mixer = self.audio_mixer
            self.radio = RadioController(self.audio_mixer, lambda *e: self.events.put(e))
            self.radio_dialog = None
            self.speaker_dialog = None
            from demo_recording import DemoRecording
            self.demo=DemoRecording(self.observer.capture,self.audio_mixer,lambda *e:self.events.put(e))
            from tv_voice import TVSpeech,TV_VOICE
            self.tv=TVSpeech(worker_type,self.audio_mixer,TV_VOICE if provider=='elevenlabs' else 'echo',
                             lambda message,level:self.events.put(('tv_status',(message,level))))
        else: self.speech = SpeechWorker(lambda *e: self.events.put(e))
        root.title(f'GameVoice — субтитры и голос {self.provider_name}' if cloud else 'GameVoice — русские субтитры')
        width=min(850,root.winfo_screenwidth()-40)
        height=min(850 if cloud else 690,root.winfo_screenheight()-60)
        root.geometry(f'{width}x{height}'); root.minsize(680, 520)
        root.configure(bg='#101827')
        style = ttk.Style(); style.theme_use('clam')
        for kind in ('TFrame','TLabel','TCheckbutton'):
            style.configure(kind, background='#101827', foreground='#e5edf7', font=('Segoe UI',11))
        style.configure('TButton', font=('Segoe UI',11), padding=8)
        frame = ttk.Frame(root, padding=16); frame.pack(fill='both',expand=True)
        ttk.Label(frame, text='Русские субтитры → голос', font=('Segoe UI',20,'bold')).pack(anchor='w')
        description = (f'Голос создан ИИ {self.provider_name} · отправляется текст субтитров · API платный' if cloud else
                       'Местный голос Windows · без интернета · аудио игры не захватывается')
        ttk.Label(frame, text=description,wraplength=width-40).pack(anchor='w',pady=6)
        if cloud:
            provider_row=ttk.Frame(frame); provider_row.pack(fill='x')
            ttk.Label(provider_row,text='Озвучка:').pack(side='left')
            self.provider_choice=tk.StringVar(value=self.provider_name)
            provider_picker=ttk.Combobox(provider_row,textvariable=self.provider_choice,
                values=('OpenAI','ElevenLabs'),state='readonly',width=12)
            provider_picker.pack(side='left',padx=8)
            provider_picker.bind('<<ComboboxSelected>>',self.switch_provider)
            if provider=='elevenlabs':
                from elevenlabs_speech import MODEL_LABELS, MODEL_HINTS
                ttk.Label(provider_row,text='Режим:').pack(side='left',padx=(8,0))
                self.model_choice=tk.StringVar(value=MODEL_LABELS[self.model_id])
                model_picker=ttk.Combobox(provider_row,textvariable=self.model_choice,
                    values=tuple(MODEL_LABELS.values()),state='readonly',width=25)
                model_picker.pack(side='left',padx=8)
                model_picker.bind('<<ComboboxSelected>>',self.switch_model)
                self.model_hint=tk.StringVar(value=MODEL_HINTS[self.model_id])
                ttk.Label(frame,textvariable=self.model_hint,wraplength=width-40).pack(anchor='w')
        self.status = tk.StringVar(value='Запуск изображения…')
        ttk.Label(frame,textvariable=self.status,wraplength=790).pack(anchor='w')
        self.health_text = tk.StringVar()
        self.health_label = ttk.Label(frame, textvariable=self.health_text,
            wraplength=width-40, font=('Segoe UI', 11, 'bold'))
        if cloud: self.health_label.pack(anchor='w', fill='x', pady=(4,0))
        self.canvas = tk.Canvas(frame,bg='#070b12',highlightthickness=0,height=190 if cloud else 250)
        self.canvas.pack(fill='both',expand=True,pady=10)
        self.canvas.bind('<ButtonPress-1>',self.begin_roi)
        self.canvas.bind('<B1-Motion>',self.drag_roi)
        self.canvas.bind('<ButtonRelease-1>',self.end_roi)
        self.drag_start = None
        self.rect = None
        self.preview_size = (1,1)
        roi_row=ttk.Frame(frame); roi_row.pack(fill='x')
        ttk.Label(roi_row,text='Размер субтитров:').pack(side='left')
        self.roi_profile=tk.StringVar(value=ROI_LABELS[self.observer.roi_profile])
        roi_picker=ttk.Combobox(roi_row,textvariable=self.roi_profile,
            values=tuple(ROI_LABELS.values()),state='readonly',width=12)
        roi_picker.pack(side='left',padx=8)
        roi_picker.bind('<<ComboboxSelected>>',self.change_roi_profile)
        ttk.Label(roi_row,text='Зелёную рамку можно выделить мышью.').pack(side='left')
        row = ttk.Frame(frame); row.pack(fill='x',pady=10)
        self.voice = tk.BooleanVar(value=False)
        ttk.Checkbutton(row,text='Озвучивать субтитры',variable=self.voice,command=self.toggle).pack(side='left')
        self.voice_name = tk.StringVar(value=self.voice_label(self.speakers.default_voice) if cloud else 'cedar')
        if cloud:
            self.voice_picker = ttk.Combobox(row,textvariable=self.voice_name,values=self.voice_options,width=8,state='normal' if provider=='elevenlabs' else 'readonly')
            self.voice_picker.pack(side='left',padx=8)
            self.voice_picker.bind('<<ComboboxSelected>>', self.update_default_voice)
            self.voice_picker.bind('<Return>', self.update_default_voice)
            ttk.Button(row,text='Голоса персонажей…',command=self.show_speakers).pack(side='left')
        ttk.Button(row,text='Область по умолчанию',command=self.reset_roi).pack(side='right')
        if cloud:
            tempo_row = ttk.Frame(frame); tempo_row.pack(fill='x',pady=(0,6))
            ttk.Label(tempo_row,text='Скорость речи:').pack(side='left')
            self.speed = tk.StringVar(value=f'{self.speakers.speed:g}')
            picker = ttk.Combobox(tempo_row,textvariable=self.speed,
                values=('1.0','1.15','1.25','1.35','1.5'),width=5,state='readonly')
            picker.pack(side='left',padx=8)
            picker.bind('<<ComboboxSelected>>',self.update_tempo)
            self.catch_up = tk.BooleanVar(value=self.speakers.catch_up)
            ttk.Checkbutton(tempo_row,text='Догонять очередь (до 1,5×)',variable=self.catch_up,
                            command=self.update_tempo).pack(side='left',padx=8)
            output_row=ttk.Frame(frame);output_row.pack(fill='x',pady=(0,6))
            ttk.Label(output_row,text='Вывод звука:').pack(side='left')
            self.output_name=tk.StringVar(value=self.speech.output_device_name or 'Как в Windows')
            try:outputs=output_names()
            except Exception:outputs=[]
            output_picker=ttk.Combobox(output_row,textvariable=self.output_name,
                values=('Как в Windows',*outputs),state='readonly',width=44)
            output_picker.pack(side='left',padx=8)
            output_picker.bind('<<ComboboxSelected>>',self.update_output)
            radio_row = ttk.Frame(frame); radio_row.pack(fill='x', pady=(0,4))
            self.radio_on = tk.BooleanVar(value=False)
            ttk.Checkbutton(radio_row, text='Радио EN → RU · OpenAI', variable=self.radio_on,
                            command=self.toggle_radio).pack(side='left')
            self.tv_on=tk.BooleanVar(value=False)
            ttk.Checkbutton(radio_row,text='ТВ · субтитры без имён',variable=self.tv_on,
                            command=self.toggle_tv).pack(side='left',padx=12)
            ttk.Button(radio_row, text='История эфира…', command=self.show_radio_history).pack(side='right')
            volumes = ttk.Frame(frame); volumes.pack(fill='x', pady=(0,4))
            ttk.Label(volumes, text='Радио:').pack(side='left')
            self.radio_volume = tk.StringVar(value=str(round(float(device_settings().get('radio_volume', .85))*100)))
            rv = ttk.Combobox(volumes, textvariable=self.radio_volume, values=('0','25','50','75','85','100'), width=4, state='readonly')
            rv.pack(side='left', padx=5); rv.bind('<<ComboboxSelected>>', self.update_radio_volumes)
            ttk.Label(volumes, text='%  ·  Оригинал игры:').pack(side='left')
            self.original_volume = tk.StringVar(value=str(round(float(device_settings().get('radio_original_volume', 0))*100)))
            ov = ttk.Combobox(volumes, textvariable=self.original_volume, values=('0','10','25','50','75','100'), width=4, state='readonly')
            ov.pack(side='left', padx=5); ov.bind('<<ComboboxSelected>>', self.update_radio_volumes)
            ttk.Label(volumes, text='%  ·  приглушается под голосом', font=('Segoe UI',10)).pack(side='left')
            self.radio_status = tk.StringVar(value='Радио выключено · включайте на разговорной передаче; на песнях выключайте')
            ttk.Label(frame, textvariable=self.radio_status, wraplength=width-40, font=('Segoe UI',10)).pack(anchor='w')
            self.tv_status=tk.StringVar(value='ТВ: выключено · отдельный голос, без сбора образцов')
            self.tv_label=ttk.Label(frame,textvariable=self.tv_status,wraplength=width-40,font=('Segoe UI',10))
            self.tv_label.pack(anchor='w')
        self.metrics = tk.StringVar(value='Жду кадр с карты…')
        if cloud:
            video_row=ttk.Frame(frame);video_row.pack(fill='x',pady=4)
            ttk.Button(video_row,text='Запись видео…',command=self.start_demo).pack(side='left')
            ttk.Button(video_row,text='Остановить видео',command=lambda:self.demo.finish()).pack(side='left',padx=6)
            ttk.Label(video_row,text='Оригинал в видео, %:').pack(side='left')
            self.demo_gain=tk.StringVar(value='30')
            ttk.Combobox(video_row,textvariable=self.demo_gain,values=('10','20','30','40','50'),width=4,state='readonly').pack(side='left')
            self.demo_status=tk.StringVar(value='Видео не записывается · MP4, 720p30 · без микрофона')
            ttk.Label(frame,textvariable=self.demo_status,wraplength=width-40).pack(anchor='w')
        ttk.Label(frame,textvariable=self.metrics,wraplength=width-40,font=('Segoe UI',10)).pack(anchor='w')
        self.pipeline_status=tk.StringVar(value='')
        if cloud and provider=='elevenlabs':
            ttk.Label(frame,textvariable=self.pipeline_status,wraplength=width-40,font=('Segoe UI',10)).pack(anchor='w')
        self.text = tk.Text(frame,height=3,font=('Segoe UI',12),bg='#182438',fg='white',wrap='word',state='disabled')
        self.text.pack(fill='x',pady=8)
        ttk.Label(frame,text='Начатые фразы дочитываются целиком, следующие ждут в очереди.',font=('Segoe UI',10),wraplength=width-40).pack(anchor='w')
        root.protocol('WM_DELETE_WINDOW',self.close)
        self.ocr_ms = 0; self.read_rate=None; self.latency_ms = None; self.latest_id = 0; self.backlog = 0; self.active_tempo = 1.25
        self.preview_image = None
        self.preview_rate = FrameRate()
        self.preview_render_ms = 0
        self.metrics_updated = 0
        # Poll twice per camera interval to pick up newly arriving frames.
        # Deadlines compensate for render/event time; never redraw duplicates.
        self.ui_clock = DeadlineTicker(2 * CAPTURE_FPS, time.perf_counter())
        self.observer.start(); self.poll()

    def voice_label(self, voice):
        return next((name for name,identifier in self.voice_catalog.items() if identifier==voice),voice)

    def start_demo(self):
        from tkinter import filedialog
        if self.demo.busy:
            self.demo_status.set('Запись ещё идёт или сохраняется.');return
        if not self.voice.get():
            self.demo_status.set('Сначала включите «Озвучивать субтитры», чтобы перевод попал в видео.');return
        folder=filedialog.askdirectory(title='Куда сохранить видео GameVoice?')
        if not folder:return
        try:self.demo.start(folder,float(self.demo_gain.get())/100)
        except Exception as exc:self.demo_status.set('Не удалось начать запись: '+str(exc))

    def switch_model(self,event=None):
        from elevenlabs_speech import MODEL_LABELS, MODEL_HINTS
        model=next(key for key,label in MODEL_LABELS.items() if label==self.model_choice.get())
        if self.closed or model==self.model_id:return
        # No capture/player restart: a phrase already queued keeps its original model.
        try:save_setting('elevenlabs_model',model)
        except OSError as exc:
            self.model_choice.set(MODEL_LABELS[self.model_id])
            self.status.set('Не удалось сохранить режим: '+str(exc));return
        self.speech.set_model(model);self.model_id=model
        self.model_hint.set(MODEL_HINTS[model]+' Новые реплики используют этот режим.')

    def switch_provider(self,event=None):
        provider='elevenlabs' if self.provider_choice.get()=='ElevenLabs' else 'openai'
        if provider==self.provider or self.closed:return
        # Release the exclusive HDMI capture before opening the replacement.
        self.closed=True; self.voice.set(False); self.speech.set_enabled(False)
        if self.tv:self.tv.worker.set_enabled(False)
        self.status.set('Переключаю озвучку…')
        for child in self.root.winfo_children():
            for widget in child.winfo_children():
                try:widget.configure(state='disabled')
                except tk.TclError:pass
        def cleanup():
            if self.demo:self.demo.close()
            if self.tv:self.tv.close()
            if self.radio: self.radio.close()
            self.observer.close(); self.speech.close()
            self.route_log.close()
            if self.audio_mixer: self.audio_mixer.close()
        task=threading.Thread(target=cleanup,daemon=True);task.start()
        def finish():
            if task.is_alive():self.root.after(100,finish);return
            for child in self.root.winfo_children():child.destroy()
            App(self.root,cloud=True,provider=provider)
        self.root.after(100,finish)

    def selected_voice(self, label):
        return self.voice_catalog.get(label,label)

    def update_tempo(self, event=None):
        self.speech.speed = float(self.speed.get())
        self.speech.catch_up = self.catch_up.get()
        self.speakers.speed = self.speech.speed
        self.speakers.catch_up = self.speech.catch_up
        self.save_speakers()

    def update_output(self,event=None):
        value=self.output_name.get()
        self.speech.output_device_name='' if value=='Как в Windows' else value
        if self.audio_mixer: self.audio_mixer.output_device_name = self.speech.output_device_name
        try:save_setting('output',self.speech.output_device_name)
        except OSError as exc:self.status.set('Не удалось сохранить колонку: '+str(exc))

    def update_radio_volumes(self, event=None):
        self.audio_mixer.radio_gain = max(0, min(1, float(self.radio_volume.get()) / 100))
        original = max(0, min(1, float(self.original_volume.get()) / 100))
        self.audio_mixer.original_gain = original if self.radio_on.get() else 0
        try:
            save_setting('radio_volume', self.audio_mixer.radio_gain)
            save_setting('radio_original_volume', original)
        except OSError as exc:
            self.radio_status.set('Не удалось сохранить громкость: ' + str(exc))

    def toggle_radio(self):
        if self.radio_on.get() and self.tv_on.get():
            self.tv_on.set(False);self.toggle_tv()
        self.radio.set_enabled(self.radio_on.get())
        self.update_radio_volumes()

    def toggle_tv(self):
        if self.tv_on.get() and self.radio_on.get():
            self.radio_on.set(False);self.toggle_radio()
        self.observer.tv_enabled=self.tv_on.get()
        self.observer.tv_captions=None;self.observer.tv_blocked=True
        self.tv.tick(self.tv_on.get() and self.voice.get(),True,self.speech.output_device_name,
                     self.speech.speed,self.speech.catch_up,self.model_id)
        self.tv_status.set('ТВ: проверяю экран · меню и диалоги имеют приоритет' if self.tv_on.get() else 'ТВ: выключено')

    def show_radio_history(self):
        if self.radio_dialog and self.radio_dialog.winfo_exists():
            self.radio_dialog.lift(); return
        dialog = tk.Toplevel(self.root)
        self.radio_dialog = dialog
        dialog.title('Радио — оригинал и перевод')
        dialog.geometry('760x490'); dialog.configure(bg='#101827')
        self.radio_texts = {}
        recent = self.radio.history.read()
        for language, title in (('en','Оригинальная речь'), ('ru','Русский перевод')):
            ttk.Label(dialog, text=title).pack(anchor='w', padx=12, pady=5)
            box = tk.Text(dialog, height=9, wrap='word', bg='#182438', fg='white', font=('Segoe UI',11))
            box.pack(fill='both', expand=True, padx=12, pady=(0,5))
            box.insert('end', recent[language]); box.see('end'); box.configure(state='disabled')
            self.radio_texts[language] = box
        ttk.Label(dialog, text='Сохраняется только полученный текст эфира. Во время диалогов радиоперевод на паузе.', wraplength=730).pack(padx=12, pady=8)

    def append_radio_history(self, value):
        if not self.radio_dialog or not self.radio_dialog.winfo_exists(): return
        language, text = value
        box = self.radio_texts.get(language)
        if box:
            box.configure(state='normal'); box.insert('end', text)
            count = int(box.count('1.0','end-1c','chars')[0])
            if count > 20000: box.delete('1.0', f'1.0+{count-16000}c')
            box.see('end'); box.configure(state='disabled')

    def save_roi(self):
        try:save_roi_profile(self.observer.roi_profile,self.observer.roi)
        except OSError as exc:self.status.set('Не удалось сохранить область чтения: '+str(exc))

    def save_speakers(self):
        try: self.speakers.save()
        except OSError as exc: self.status.set('Не удалось сохранить голоса: ' + str(exc))

    def update_default_voice(self, event=None):
        voice = self.selected_voice(self.voice_name.get())
        if not self.speakers.valid_voice(voice):
            self.status.set('Выберите голос из списка или введите voice_id.'); return False
        self.speakers.default_voice = voice
        self.speech.prepare_voices(self.speakers.voices(), self.speakers.default_voice)
        self.save_speakers()
        return True

    def show_speakers(self):
        if self.speaker_dialog and self.speaker_dialog.winfo_exists():
            self.speaker_dialog.lift(); return
        dialog = self.speaker_dialog = tk.Toplevel(self.root)
        dialog.title('Голоса персонажей'); dialog.configure(bg='#101827')
        dialog.geometry('570x440'); dialog.transient(self.root)
        body = ttk.Frame(dialog,padding=14); body.pack(fill='both',expand=True)
        ttk.Label(body,text='Имя из субтитров → постоянный голос',font=('Segoe UI',13,'bold')).pack(anchor='w')
        ttk.Label(body,text='Новые имена добавляются автоматически. Имя вслух не читается.\nБез имени используется голос в основном окне.',wraplength=530).pack(anchor='w',pady=8)
        table = ttk.Treeview(body,columns=('name','voice','bank'),show='headings',height=7,selectmode='browse')
        table.heading('name',text='Персонаж'); table.heading('voice',text=f'Голос {self.provider_name}')
        table.heading('bank',text='Чистая речь')
        table.column('name',width=240); table.column('voice',width=170); table.column('bank',width=90); table.pack(fill='both',expand=True)
        row = ttk.Frame(body); row.pack(fill='x',pady=10)
        name = tk.StringVar(); voice = tk.StringVar(value=self.voice_label(self.speakers.default_voice))
        ttk.Entry(row,textvariable=name,width=23).pack(side='left')
        ttk.Combobox(row,textvariable=voice,values=self.voice_options,width=15,state='normal' if self.provider=='elevenlabs' else 'readonly').pack(side='left',padx=8)
        def refresh():
            if not dialog.winfo_exists(): return
            counters={speaker_key(item['character']):item for item in getattr(self,'pipeline_snapshot',{}).get('characters',[])}
            for key, entry in self.speakers.effective_entries().items():
                sample=counters.get(key,{})
                duration=sample.get('seconds',0)
                progress=(f'{duration:.1f}/60 с' if sample else 'копится')
                if sample.get('install') and sample['install']['state']=='complete':progress='60 с ✓'
                values = (entry['name'],self.voice_label(entry['voice']),progress)
                if table.exists(key): table.item(key,values=values)
                else: table.insert('', 'end',iid=key,values=values)
            dialog.after(500,refresh)
        def select(event=None):
            selected = table.selection()
            if selected:
                entry = self.speakers.effective_entries()[selected[0]]
                name.set(entry['name']); voice.set(self.voice_label(entry['voice']))
        def apply():
            try:
                self.speakers.assign(name.get(),self.selected_voice(voice.get()))
                self.observer.gate.speaker_names.add(speaker_key(name.get()))
                self.speech.prepare_voices(self.speakers.voices())
                self.status.set(f'Голос сохранён: {name.get()} → {voice.get()} · со следующей реплики')
            except (ValueError,OSError) as exc: self.status.set(str(exc))
        table.bind('<<TreeviewSelect>>',select)
        ttk.Button(row,text='Назначить',command=apply).pack(side='left')
        help_text = ('Фоновый сборщик подключает клоны после 60 с чистой речи.\nРучное назначение другого голоса имеет приоритет.' if self.provider=='elevenlabs' else
            'Для нового персонажа выбирается свободный голос. После 10 голосов возможны совпадения.')
        ttk.Label(body,text=help_text+'\nНазначения сохраняются после закрытия программы.',wraplength=530,font=('Segoe UI',10)).pack(anchor='w')
        refresh()

    def toggle(self):
        if self.cloud:
            if not self.update_default_voice():
                self.voice.set(False); return
        self.speech.set_enabled(self.voice.get())
        self.health.event('speech_enabled', self.voice.get())
        self.observer.gate = self.new_gate()
        self.observer.tv_captions=None
        self.status.set('Читаю и озвучиваю русские субтитры' if self.voice.get() else 'Читаю русские субтитры · голос выключен')

    def reset_roi(self):
        self.observer.roi = ROI_DEFAULTS[self.observer.roi_profile]
        self.observer.gate = self.new_gate()
        self.save_roi()

    def change_roi_profile(self,event=None):
        profile=next(key for key,label in ROI_LABELS.items() if label==self.roi_profile.get())
        try:
            roi=save_roi_profile(profile)
        except OSError as exc:
            self.roi_profile.set(ROI_LABELS[self.observer.roi_profile])
            self.status.set('Не удалось сменить размер субтитров: '+str(exc))
            return
        self.observer.roi=roi
        self.observer.roi_profile=profile
        self.observer.gate=self.new_gate()
        self.status.set('Область чтения: '+ROI_LABELS[profile].lower())

    def speech_event(self, kind, value):
        if kind in ('voice_route', 'voice_audit'):
            self.route_log.append(value, self.speakers.automatic_revision)
            if self.demo:self.demo.timeline_event(value)
            if kind == 'voice_audit':
                return
        self.events.put(('speech_error' if kind == 'error' else kind, value))

    def new_gate(self):
        return SubtitleTracker(self.speakers.effective_entries() if self.cloud else (),
                               resolver=self.speakers.resolve if self.cloud else None,
                               require_named=True)

    def begin_roi(self,e):
        self.drag_start = e.x,e.y

    def drag_roi(self,e):
        if self.drag_start:
            self.canvas.delete('roi')
            self.canvas.create_rectangle(*self.drag_start,e.x,e.y,outline='#ffd264',width=2,tags='roi')

    def end_roi(self,e):
        if not self.drag_start: return
        x,y = self.drag_start; self.drag_start = None
        w,h = self.preview_size
        left,right = sorted((max(0,min(x,w))/w,max(0,min(e.x,w))/w))
        top,bottom = sorted((max(0,min(y,h))/h,max(0,min(e.y,h))/h))
        if right-left>.12 and bottom-top>.025:
            self.observer.roi = left,top,right,bottom
            self.save_roi()
            self.observer.gate = self.new_gate()

    def poll(self):
        if self.closed:return
        if self.cloud and self.provider=='elevenlabs' and time.monotonic()-self.pipeline_checked>=1:
            self.pipeline_checked=time.monotonic()
            from elevenlabs_speech import folder
            from voice_pipeline import atomic_json,utc
            from voice_pipeline import load
            pipeline=load(folder()/'pipeline-status.json',{})
            self.pipeline_snapshot=pipeline
            names=pipeline.get('characters',[])
            ready=sum(bool(n.get('install') and n['install']['state']=='complete') for n in names)
            jobs=pipeline.get('jobs',{})
            state={'processing':'обработка','publishing':'подключение голоса','waiting':'жду часть 45 мин','paused':'пауза','stopped':'остановлен'}.get(pipeline.get('state'),'запуск')
            stale=time.time()-pipeline.get('updated_unix',0)>45
            if stale:state='нет связи с обработчиком'
            error=next(iter(pipeline.get('errors',{}).values()),'')
            self.pipeline_status.set(f'Автоголоса: {state} · частей готово {jobs.get("done",0)} · голосов от 60 с: {ready}'+(' · есть отложенные образцы' if error else ''))
            if not self.pipeline_launched or stale and time.monotonic()-getattr(self,'pipeline_launch_checked',0)>60:
                self.pipeline_launched=True;self.pipeline_launch_checked=time.monotonic()
                config=load(folder()/'pipeline-config.json',{})
                launcher=config.get('launcher');python=config.get('pythonw')
                if config.get('enabled') and launcher and python and Path(launcher).is_file() and Path(python).is_file():
                    try:subprocess.Popen([python,launcher],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=0x08000000)
                    except OSError:pass
            changed=self.speakers.reload_automatic(folder()/'automatic-voices.json')
            if changed:
                self.observer.gate.speaker_names.update(self.speakers.effective_entries())
                self.speech.prepare_voices(self.speakers.voices())
                self.voice_catalog.update({('GameVoice — '+v['name']):v['voice_id']
                                           for v in self.speakers.automatic.values()})
            try:
                if changed or not hasattr(self,'pipeline_heartbeat') or time.monotonic()-self.pipeline_heartbeat>10:
                    self.pipeline_heartbeat=time.monotonic()
                    atomic_json(folder()/'pipeline-app-status.json',{'pid':__import__('os').getpid(),
                        'utc':utc(),'automatic_revision':self.speakers.automatic_revision,
                        'recent_voice_routes':list(self.recent_voice_routes),
                        'audio_output':self.audio_mixer.diagnostics(),
                        'tv':dict(self.tv.diagnostics(),captions=self.observer.tv_captions.diagnostics()
                                  if self.observer.tv_captions else {}),
                        'assignments':{k:self.speakers.voice_for(v['name']) for k,v in self.speakers.automatic.items()}})
            except OSError:pass
        for _ in range(100):
            try: kind,value = self.events.get_nowait()
            except queue.Empty: break
            if kind=='voice_route' and self.provider=='elevenlabs' and value.get('model_id'):
                from elevenlabs_speech import MODEL_LABELS
                self.health.provider='ElevenLabs '+MODEL_LABELS[value['model_id']].split(' — ')[0]
            self.health.event(kind, value)
            if kind in ('error','notice','status'): self.status.set(value)
            elif kind in ('capture_error', 'speech_error', 'output_error', 'connection_error'):
                self.status.set(value)
            elif kind=='ocr': self.ocr_ms = value['ms']
            elif kind=='read_rate': self.read_rate=value
            elif kind=='dialogue_visible':
                if self.audio_mixer: self.audio_mixer.set_dialogue(value)
            elif kind=='radio_status': self.radio_status.set('Радио: ' + value)
            elif kind=='tv_subtitle':self.tv.offer(value)
            elif kind=='tv_status':
                self.tv_status.set(value[0]);self.tv_label.configure(foreground=COLOURS[value[1]])
            elif kind=='demo_status': self.demo_status.set(value)
            elif kind=='radio_error': self.radio_status.set('Радио: ' + value)
            elif kind=='radio_disabled': self.radio_on.set(False)
            elif kind=='radio_transcript': self.append_radio_history(value)
            elif kind=='latency': self.latency_ms = value
            elif kind=='backlog': self.backlog = value
            elif kind=='tempo': self.active_tempo = value
            elif kind=='voice_route': self.recent_voice_routes.append(value)
            elif kind=='subtitle':
                text,stamp=value.text,value.stamp
                speaker = self.speakers.display_name(value.speaker) if self.cloud and value.speaker else value.speaker.title()
                if self.demo:self.demo.timeline_event({'phase':'subtitle','text':text,'speaker':speaker,
                    'source_stamp':stamp,'at_monotonic':time.monotonic()})
                display = f'{speaker}: {text}' if speaker else text
                self.text.configure(state='normal'); self.text.insert('end',display+'\n'); self.text.see('end')
                if int(self.text.index('end-1c').split('.')[0])>80: self.text.delete('1.0','30.0')
                self.text.configure(state='disabled')
                dialogue = text
                if self.cloud:
                    try: voice = self.speakers.voice_for(speaker)
                    except OSError as exc:
                        self.status.set('Не удалось сохранить голос: ' + str(exc))
                        voice = self.speakers.entries.get(speaker_key(speaker), {}).get('voice', self.speakers.default_voice)
                    self.speech.prepare_voices(self.speakers.voices())
                    self.speech.submit(dialogue,stamp,voice,speaker)
                else: self.speech.submit(dialogue,stamp)
        if self.tv:
            self.tv.tick(self.tv_on.get() and self.voice.get(),self.observer.tv_blocked,
                         self.speech.output_device_name,self.speech.speed,self.speech.catch_up,self.model_id,
                         reason=self.observer.tv_captions.reason if self.observer.tv_captions else 'menu')
        item=self.observer.capture.snapshot()
        if item and item[0]!=self.latest_id and not self.drag_start:
            render_started = time.perf_counter()
            self.latest_id=item[0]
            w=max(1,self.canvas.winfo_width()); h=max(1,self.canvas.winfo_height())
            factor=min(w/WIDTH,h/HEIGHT)
            size=max(1,int(WIDTH*factor)),max(1,int(HEIGHT*factor))
            self.preview_size=size
            self.photo=ImageTk.PhotoImage(item[2].resize(size, Image.Resampling.BILINEAR))
            if self.preview_image is None:
                self.preview_image = self.canvas.create_image(0,0,image=self.photo,anchor='nw')
                self.canvas.tag_lower(self.preview_image)
            else:
                self.canvas.itemconfigure(self.preview_image, image=self.photo)
            a,b,c,d=self.observer.roi
            rect = (a*size[0],b*size[1],c*size[0],d*size[1])
            if self.canvas.find_withtag('roi'):
                self.canvas.coords('roi', *rect)
                self.canvas.itemconfigure('roi', outline='#54dfab')
            else:
                self.canvas.create_rectangle(*rect,outline='#54dfab',width=2,tags='roi')
            rendered = time.perf_counter()
            self.preview_rate.record(rendered)
            self.preview_render_ms = (rendered - render_started) * 1000
        now = time.perf_counter()
        if now - self.metrics_updated >= .25:
            self.metrics_updated = now
            if self.audio_mixer:
                audio_output=self.audio_mixer.diagnostics()
                self.health.event('audio_output',audio_output)
                if audio_output['underflows']>getattr(self,'audio_underflows_logged',0):
                    self.audio_underflows_logged=audio_output['underflows']
                    stamp=audio_output['last_underflow_monotonic']
                    self.route_log.append({'phase':'output_underflow',**audio_output,
                        'at_monotonic':stamp,'at_unix':time.time()-(time.monotonic()-stamp)},
                        self.speakers.automatic_revision)
            level, message = self.health.view()
            self.health_text.set(message)
            self.health_label.configure(foreground=COLOURS[level])
            if self.radio and self.radio_on.get():
                self.radio_status.set(f'Радио: {self.radio.session_status} · {self.radio.input_seconds/60:.1f} мин · буфер {self.audio_mixer.radio_buffer_ms:.0f} мс')
            delay=f'{self.latency_ms} мс' if self.latency_ms is not None else 'ещё не измерено'
            actual=f'{self.read_rate:.1f}' if self.read_rate is not None else '…'
            capture_fps = self.observer.capture.measured_fps(now)
            preview_fps = self.preview_rate.value(now)
            self.metrics.set(f'720p · карта {capture_fps:.1f} · окно {preview_fps:.1f} кадр/с · чтение {actual}/{OCR_FPS} в с · OCR {self.ocr_ms} мс · голос: {delay}' +
                             (f' · очередь {self.backlog} · {self.active_tempo:g}×' if self.cloud else '')+
                             (f' · буфер звука {audio_output["latency_ms"]} мс · сбоев {audio_output["underflows"]}'
                              if self.audio_mixer and audio_output['latency_ms'] is not None else ''))
        if not self.closed:
            self.root.after(self.ui_clock.next_delay_ms(time.perf_counter()), self.poll)

    def close(self):
        self.closed=True; self.speech.set_enabled(False); self.root.withdraw()
        if self.tv:self.tv.worker.set_enabled(False)
        def cleanup():
            if self.demo:self.demo.close()
            if self.tv:self.tv.close()
            if self.radio: self.radio.close()
            self.observer.close(); self.speech.close()
            self.route_log.close()
            if self.audio_mixer: self.audio_mixer.close()
            self.events.put(('closed',None))
        threading.Thread(target=cleanup,daemon=True).start()
        def wait():
            try:
                while True:
                    kind,_=self.events.get_nowait()
                    if kind=='closed': self.root.destroy(); return
            except queue.Empty: self.root.after(100,wait)
        wait()


def probe(seconds, folder):
    folder=Path(folder); folder.mkdir(parents=True,exist_ok=True)
    events=queue.Queue(); observer=Observer(lambda *e:events.put(e))
    observer.start(); until=time.monotonic()+seconds
    with (folder/'observations.jsonl').open('w',encoding='utf8') as log:
        while time.monotonic()<until:
            try: kind,value=events.get(timeout=.2)
            except queue.Empty: continue
            if kind=='ocr':
                log.write(json.dumps({'at':round(time.time(),3),**value},ensure_ascii=False)+'\n'); log.flush()
                item=observer.capture.snapshot()
                if item: item[2].save(folder/'latest.jpg')
            elif kind=='subtitle':
                print(json.dumps({'subtitle':value.text,'speaker':value.speaker},ensure_ascii=True),flush=True)
                item=observer.capture.snapshot()
                if item: item[2].save(folder/f'example-{item[0]}.jpg')
            elif kind=='error': print(value,flush=True); break
    observer.close()
    return int(bool(observer.failure))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--probe',type=float)
    parser.add_argument('--folder',default='work/subtitle-probe')
    parser.add_argument('--openai',action='store_true')
    parser.add_argument('--elevenlabs',action='store_true')
    args=parser.parse_args()
    if args.probe: return probe(args.probe,args.folder)
    root=tk.Tk(); App(root,cloud=args.openai or args.elevenlabs,
        provider='elevenlabs' if args.elevenlabs else 'openai'); root.mainloop(); return 0

if __name__=='__main__': sys.exit(main())
