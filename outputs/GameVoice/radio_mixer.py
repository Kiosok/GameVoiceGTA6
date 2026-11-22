"""One output device for character speech, radio and optional original HDMI."""
import audioop
from collections import deque
import threading
import time

import sounddevice as sd
from device_settings import output_index

RATE = 24000
OUTPUT_LATENCY = .12


class AudioMixer:
    def __init__(self, output_name=''):
        self.output_device_name = output_name
        self.opened_device = None
        self.stream = None
        self.io_lock = threading.Lock()
        self.condition = threading.Condition()
        self.stopped = threading.Event()
        self.original = bytearray()
        self.original_gain = 0.0
        self.radio_gain = .85
        self.duck = True
        self.character = None
        self.radio_stream = bytearray()
        self.radio_running = False
        self.radio_played_bytes = 0
        self.radio_underflows = 0
        self.radio_yield_at = None
        self.radio_quiet_bytes = 0
        self.character_pending = 0
        self.dialogue_visible = False
        self.hold_until = 0.0
        self.current_gain = 0.0
        self.output_failure = None
        self.recording_sink = None
        self.output_underflows = 0
        self.last_underflow_monotonic = None
        self.callback_count = 0
        self.last_callback_monotonic = None
        self.callback_gap_max_ms = 0.0
        self.output_latency_ms = None

    def diagnostics(self):
        # Read by the UI, not written to disk by the audio callback.
        return {'underflows':self.output_underflows,
                'last_underflow_monotonic':self.last_underflow_monotonic,
                'callback_count':self.callback_count,
                'callback_gap_max_ms':round(self.callback_gap_max_ms,2),
                'latency_ms':self.output_latency_ms}

    def _priority(self):
        return self.character_pending > 0 or self.dialogue_visible or time.monotonic() < self.hold_until

    def priority(self):
        with self.condition:
            return self._priority()

    def set_pending(self, count):
        with self.condition:
            self.character_pending = count
            self.condition.notify_all()

    def set_dialogue(self, visible):
        with self.condition:
            self.dialogue_visible = bool(visible)
            if visible:
                self.hold_until = time.monotonic() + .6
            self.condition.notify_all()

    def ensure_stream(self):
        with self.io_lock:
            if self.stopped.is_set():
                raise RuntimeError('Аудиовывод закрыт.')
            if self.stream and self.opened_device == self.output_device_name:
                if not self.stream.active:
                    raise RuntimeError('Аудиовывод остановился. Проверьте колонку.')
                return
            if self.stream:
                self.stream.abort()
                self.stream.close()
            stream = sd.RawOutputStream(device=output_index(self.output_device_name),
                samplerate=RATE, channels=2, dtype='int16', blocksize=480,
                latency=OUTPUT_LATENCY, callback=self.callback)
            self.last_callback_monotonic = None
            try:
                stream.start()
            except Exception:
                stream.close()
                raise
            self.stream = stream
            self.opened_device = self.output_device_name
            self.output_failure = None
            self.output_latency_ms = round(stream.latency*1000)

    def feed_original(self, pcm):
        if not self.original_gain:
            with self.condition:
                self.original.clear()
            return
        self.ensure_stream()
        with self.condition:
            self.original.extend(pcm)
            # Max 0.4 s; prefer fresh original audio rather than accumulating lag.
            excess = len(self.original) - 38400
            if excess > 0:
                del self.original[:(excess + 3) // 4 * 4]

    def play_character(self, pcm, volume, valid, started, background=False):
        if not valid():return False
        self.ensure_stream()
        packet = {'data': audioop.tostereo(audioop.mul(pcm, 2, volume), 2, 1, 1),
                  'offset': 0, 'started': threading.Event(), 'done': threading.Event()}
        with self.condition:
            if not valid() or background and (self._priority() or self.character is not None):
                return False
            self.character = packet
        try:
            notified = False
            while not packet['done'].wait(.02):
                if packet['started'].is_set() and not notified:
                    started()
                    notified = True
                if self.stopped.is_set() or not valid():
                    return False
                if self.output_failure:
                    raise RuntimeError(self.output_failure)
            if packet['started'].is_set() and not notified:
                started()
            return True
        finally:
            with self.condition:
                if self.character is packet:
                    self.character = None

    @property
    def radio_buffer_ms(self):
        with self.condition:
            return len(self.radio_stream) / 96

    def feed_radio(self, pcm):
        """Append PCM in order; packet boundaries never become playback gaps."""
        if len(pcm) % 2:
            raise RuntimeError('Неполный PCM-пакет радио.')
        stereo = audioop.tostereo(pcm, 2, 1, 1)
        with self.condition:
            if self._priority():
                return False
            if len(self.radio_stream) + len(stereo) > 8 * RATE * 4:
                raise RuntimeError('Радио отстало более чем на 8 секунд; проверьте вывод звука.')
            self.radio_stream.extend(stereo)
        return True

    def _radio_audio(self, size):
        priority = self._priority() or self.character is not None
        if priority and not self.radio_running:
            self.radio_stream.clear()
        if priority and self.radio_running and self.radio_yield_at is None:
            self.radio_yield_at = time.monotonic() + 1.0
            self.radio_quiet_bytes = 0
        if not priority:
            self.radio_yield_at = None
        # A 200ms jitter buffer absorbs network packet bursts without adding a
        # separate sentence queue. Rebuffer only after a true network underrun.
        if not self.radio_running and not priority and len(self.radio_stream) >= 19200:
            self.radio_running = True
        if not self.radio_running:
            return None
        data = bytes(self.radio_stream[:size])
        del self.radio_stream[:len(data)]
        self.radio_played_bytes += len(data) // 2  # original mono PCM byte count
        if audioop.rms(data, 2) < 180:
            self.radio_quiet_bytes += len(data)
        else:
            self.radio_quiet_bytes = 0
        yielding = priority and (self.radio_quiet_bytes >= 5760 or
                                  time.monotonic() >= self.radio_yield_at)
        if yielding:
            # Prefer a pause. On nonstop speech use a short fade so urgent game
            # dialogue cannot wait behind seconds of buffered radio.
            from array import array
            faded = array('h'); faded.frombytes(data)
            length = max(1, len(faded) // 2 - 1)
            for i in range(len(faded)):
                faded[i] = round(faded[i] * (1 - (i // 2) / length))
            data = faded.tobytes()
            self.radio_stream.clear()
            self.radio_running = False
            self.radio_yield_at = None
        elif len(data) < size:
            if not priority:
                self.radio_underflows += 1
            self.radio_running = False
            self.radio_yield_at = None
        return audioop.mul(data.ljust(size, b'\0'), 2, self.radio_gain)

    @staticmethod
    def _take(packet, size):
        start = packet['offset']
        data = packet['data'][start:start + size]
        packet['offset'] += len(data)
        if data:
            packet['started'].set()
        if packet['offset'] == len(packet['data']):
            packet['done'].set()
        return data.ljust(size, b'\0')

    def callback(self, outdata, frames, timing, status):
        size = frames * 4
        now=time.monotonic()
        self.callback_count += 1
        if self.last_callback_monotonic is not None:
            self.callback_gap_max_ms=max(self.callback_gap_max_ms,(now-self.last_callback_monotonic)*1000)
        self.last_callback_monotonic=now
        if status and getattr(status,'output_underflow',False):
            self.last_underflow_monotonic=now
            self.output_underflows += 1
        try:
            with self.condition:
                original = bytes(self.original[:size]).ljust(size, b'\0')
                del self.original[:size]
                voice = self._radio_audio(size)
                if voice is None and self.character is not None:
                    voice = self._take(self.character, size)
                    if self.character['done'].is_set():
                        self.character = None
                elif voice is None:
                    voice = b'\0' * size
                talking = audioop.rms(voice, 2) > 50
                sink=self.recording_sink
                if sink:sink(voice)
                target = self.original_gain * (.22 if self.duck and talking else 1.0)
                self.current_gain += (target - self.current_gain) * (.45 if target < self.current_gain else .08)
                outdata[:] = audioop.add(audioop.mul(original, 2, self.current_gain), voice, 2)
                self.condition.notify_all()
        except Exception:
            self.output_failure = 'Ошибка общего аудиовывода; выберите доступную колонку.'
            outdata[:] = b'\0' * size

    def cancel_radio(self):
        with self.condition:
            self.radio_stream.clear()
            self.radio_running = False
            self.radio_yield_at = None
            self.original.clear()

    def close(self):
        self.stopped.set()
        with self.io_lock:
            if self.stream:
                self.stream.abort()
                self.stream.close()
                self.stream = None
