"""Persistent UI health, driven by real service results rather than idle text."""
import time
import re

COLOURS = {'ok': '#64df98', 'waiting': '#ffd264', 'error': '#ff7373', 'off': '#aeb9c8'}


def readable_error(value):
    return re.sub(r'^[A-Za-z_][A-Za-z_0-9.]*:\s*', '', str(value))


class ServiceHealth:
    def __init__(self, provider, clock=time.monotonic):
        self.provider, self.clock = provider, clock
        self.errors = {}
        self.enabled = False
        self.last_success = None
        self.warning = ''
        self.request_started = None
        self.audio_underflows = 0
        self.audio_underflow_at = None

    def event(self, kind, value):
        if kind == 'audio_output':
            count=value.get('underflows',0)
            if count > self.audio_underflows:
                self.audio_underflows=count
                self.audio_underflow_at=self.clock()
        elif kind in ('speech_error', 'output_error', 'capture_error'):
            self.errors[kind] = readable_error(value)
        elif kind == 'connection_error':
            self.warning = 'Нет связи · повтор запроса: ' + readable_error(value)
        elif kind == 'synthesis_started':
            self.request_started = self.clock()
        elif kind == 'synthesis':
            self.errors.pop('speech_error', None)
            self.last_success = self.clock()
            self.warning = ''
            self.request_started = None
        elif kind == 'latency':
            self.errors.pop('output_error', None)
        elif kind == 'ocr':
            self.errors.pop('capture_error', None)
        elif kind == 'speech_enabled':
            self.enabled = bool(value)
            self.request_started = None
            if not self.enabled: self.warning = ''

    def view(self):
        if self.errors:
            labels = {'speech_error': 'Озвучка',
                      'output_error': 'Вывод звука', 'capture_error': 'Изображение / OCR'}
            text = '\n'.join(labels[k] + ': ' + v for k, v in self.errors.items())
            return 'error', '● ' + text
        if not self.enabled:
            return 'off', '● Озвучка выключена'
        if self.warning:
            return 'waiting', '● ' + self.warning
        if self.audio_underflow_at is not None and self.clock()-self.audio_underflow_at < 10:
            return 'waiting', f'● Вывод звука: провал буфера · всего {self.audio_underflows} · возможен треск'
        if self.request_started is not None:
            seconds = self.clock() - self.request_started
            if seconds >= 2:
                return 'waiting', f'● {self.provider}: жду ответ {seconds:.0f} с · получаю голос'
        if self.last_success is not None:
            if self.clock() - self.last_success < 10:
                return 'ok', f'● {self.provider}: голос получен успешно'
            return 'ok', f'● {self.provider}: последний запрос успешен · жду следующую реплику'
        if self.request_started is not None:
            return 'waiting', f'● {self.provider}: получаю голос…'
        return 'waiting', '● Жду субтитры · связь определится по первой реплике'
