"""Explicit opt-in game-only video; original HDMI and played Russian PCM, no loopback."""
import audioop
from datetime import datetime
from pathlib import Path
import queue
import subprocess
import threading
import time
import wave
from gamevoice import ffmpeg_path,HIDDEN
from radio_source import live_source


class PCMTrack:
    """Anchor once, then use the audio sample clock, not packet delivery jitter."""
    def __init__(self,wav,start,rate,allow_reopen=False):
        self.wav,self.start,self.rate=wav,start,rate
        self.last=None;self.allow_reopen=allow_reopen

    def write(self,pcm,stamp):
        if self.last is None or self.allow_reopen and stamp-self.last>.5:
            DemoRecording.put(self.wav,pcm,stamp,self.start,self.rate)
        else:self.wav.writeframes(pcm)
        self.last=stamp


def soundtrack_command(video,original,voice,target,gain=.30):
    # Sidechain follows actual Russian playback, never OCR visibility or API requests.
    filters=(f'[1:a]volume={gain},apad[o];[2:a]apad,asplit=2[v][key];'
             f'[o][key]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=400[d];'
             '[d][v]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95:latency=1[a]')
    return [ffmpeg_path(),'-hide_banner','-loglevel','error','-n','-i',str(video),'-i',str(original),
            '-i',str(voice),'-filter_complex',filters,'-map','0:v:0','-map','[a]',
            '-c:v','copy','-c:a','aac','-b:a','192k','-ar','48000','-shortest',
            '-movflags','+faststart',str(target)]


class DemoRecording:
    def __init__(self,capture,mixer,emit,source_factory=live_source):
        self.capture,self.mixer,self.emit=capture,mixer,emit
        self.source_factory=source_factory
        self.thread=None
        self.stop=threading.Event()
        self.voice=queue.Queue(maxsize=2048)
        self.error=None
        self.source=None
        self.source_lock=threading.Lock()
        self.timeline=None

    def timeline_event(self,record):
        timeline=self.timeline
        if timeline:timeline.append(record)

    @property
    def busy(self):return self.thread is not None and self.thread.is_alive()

    def start(self,folder,gain=.30):
        if self.busy:raise RuntimeError('Запись уже идёт или сохраняется.')
        item=self.capture.snapshot()
        if not item or time.monotonic()-item[1]>2:raise RuntimeError('Нет свежего изображения с карты.')
        self.stop.clear();self.error=None;self.voice=queue.Queue(maxsize=2048)
        self.folder=Path(folder)/datetime.now().strftime('GameVoice-%Y%m%d-%H%M%S-%f')
        self.folder.mkdir(parents=True,exist_ok=False)
        self.gain=max(0,min(1,gain))
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def feed_voice(self,pcm):
        if self.stop.is_set():return
        try:self.voice.put_nowait((time.monotonic(),bytes(pcm)))
        except queue.Full:self.error='Запись звука не успевает за воспроизведением.';self.stop.set()

    def finish(self):self.stop.set()

    def close(self):
        self.finish()
        if self.thread:self.thread.join()  # called by the app's cleanup thread

    @staticmethod
    def put(wav,pcm,stamp,start,rate):
        position=max(0,round((stamp-start)*rate))
        current=wav.getnframes()
        if position>current:
            remaining=(position-current)*4
            while remaining:
                count=min(remaining,192000);wav.writeframes(b'\0'*count);remaining-=count
        elif position<current:
            pcm=pcm[min(len(pcm),(current-position)*4):]
        wav.writeframes(pcm)

    def audio(self,origin):
        try:
            track=PCMTrack(origin,self.started,48000)
            source=self.source_factory()
            with self.source_lock:self.source=source
            while not self.stop.is_set():
                pcm=source.read(self.stop,19200)
                if pcm:track.write(pcm,time.monotonic()-len(pcm)/192000)
        except Exception as exc:
            if not self.stop.is_set():self.error=str(exc);self.stop.set()
        finally:
            self.close_source()

    def close_source(self):
        with self.source_lock:
            source,self.source=self.source,None
        if source:source.close()

    def run(self):
        process=None;audio_thread=None;log=None;origin=None;voice=None
        self.started=time.monotonic()
        index=0
        try:
            from demo_timeline import DemoTimeline
            self.timeline=DemoTimeline(self.folder,self.started,self.emit)
            log=(self.folder/'encoding.log').open('wb')
            origin=wave.open(str(self.folder/'original.wav'),'wb');origin.setparams((2,2,48000,0,'NONE',''))
            voice=wave.open(str(self.folder/'russian.wav'),'wb');voice.setparams((2,2,24000,0,'NONE',''))
            self.voice_track=PCMTrack(voice,self.started,24000,allow_reopen=True)
            process=subprocess.Popen([ffmpeg_path(),'-hide_banner','-loglevel','error','-n',
                '-f','rawvideo','-pix_fmt','rgb24','-s','1280x720','-r','30','-i','pipe:0',
                '-an','-c:v','libx264','-preset','veryfast','-crf','20','-pix_fmt','yuv420p',
                str(self.folder/'video.mkv')],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,
                stderr=log,creationflags=HIDDEN,bufsize=0)
            audio_thread=threading.Thread(target=self.audio,args=(origin,),daemon=True);audio_thread.start()
            self.mixer.recording_sink=self.feed_voice
            self.emit('demo_status',f'● Записываю видео · оригинал {self.gain:.0%}, с приглушением под озвучкой')
            index=0
            while not self.stop.is_set():
                due=self.started+index/30
                if self.stop.wait(max(0,due-time.monotonic())):break
                if time.monotonic()-due>2:raise RuntimeError('Кодирование видео не успевает. Исходники сохранены.')
                item=self.capture.snapshot()
                if not item or time.monotonic()-item[1]>2:raise RuntimeError('Пропало изображение с карты.')
                process.stdin.write(item[2].convert('RGB').resize((1280,720)).tobytes());index+=1
                self.drain_voice(voice)
        except Exception as exc:self.error=str(exc)
        finally:
            self.stop.set();self.mixer.recording_sink=None
            self.close_source()
            if audio_thread:audio_thread.join(timeout=5)
            if voice:
                self.drain_voice(voice)
                if voice.getnframes()==0:voice.writeframes(b'\0'*960)
                voice.close()
            if origin:origin.close()
            if process:
                try:process.stdin.close();process.wait(timeout=20)
                except Exception:process.kill();process.wait()
            if log:log.close()
            timeline,self.timeline=self.timeline,None
            if timeline:timeline.close(index/30)
        try:
            if self.error:raise RuntimeError(self.error)
            if not process or process.returncode:raise RuntimeError('Ошибка кодирования видео; см. encoding.log.')
            self.emit('demo_status','Сохраняю MP4… можно подождать, не закрывая окно')
            target=self.folder/'GameVoice.mp4'
            with (self.folder/'mux.log').open('wb') as log:
                subprocess.run(soundtrack_command(self.folder/'video.mkv',self.folder/'original.wav',
                    self.folder/'russian.wav',target,self.gain),stdout=subprocess.DEVNULL,stderr=log,
                    creationflags=HIDDEN,check=True,timeout=600)
            self.emit('demo_status','✓ Видео сохранено: '+str(target))
        except Exception as exc:
            self.emit('demo_status','Ошибка записи: '+str(exc)+' · исходники: '+str(self.folder))

    def drain_voice(self,wav):
        while True:
            try:stamp,pcm=self.voice.get_nowait()
            except queue.Empty:return
            self.voice_track.write(pcm,stamp)
