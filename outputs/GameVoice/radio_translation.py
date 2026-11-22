"""Manual talk-radio translation with bounded live queues and dialogue priority."""
import audioop
import base64
from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import queue
import threading
import time

import websocket
from gamevoice import URL
from key_store import load_key
from openai_speech import safe_error
from radio_source import live_source

_log = logging.getLogger('gamevoice.speech')


class RadioHistory:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ['LOCALAPPDATA']) / 'GameVoice/radio-history.jsonl'
        self.lock = threading.Lock()

    def append(self, language, text):
        if not text:
            return
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > 512000:
                self.path.replace(self.path.with_suffix('.previous.jsonl'))
            with self.path.open('a', encoding='utf8') as handle:
                handle.write(json.dumps({'utc': datetime.now(timezone.utc).isoformat(),
                    'language': language, 'text': text}, ensure_ascii=False) + '\n')

    def read(self):
        result = {'en': '', 'ru': ''}
        try:
            for line in self.path.read_text(encoding='utf8').splitlines():
                try:
                    item = json.loads(line)
                    key = item['language']
                    if key in result:
                        result[key] += item['text']
                except (ValueError, KeyError, TypeError):
                    continue
        except OSError:
            pass
        return {key: value[-16000:] for key, value in result.items()}


class RadioController:
    def __init__(self, mixer, emit, source_factory=None, socket_factory=None, key_loader=None, history=None):
        self.mixer, self.emit = mixer, emit
        self.source_factory = source_factory or live_source
        self.socket_factory = socket_factory or websocket.WebSocket
        self.key_loader = key_loader or load_key
        self.history = history or RadioHistory()
        self.enabled = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.epoch = 0
        self.source = None
        self.ws = None
        self.input = queue.Queue(maxsize=4)
        self.input_seconds = 0.0
        self.translated_bytes = 0
        self.connection_count = 0
        self.last_audio_at = 0.0
        self.last_input_at = 0.0
        self.session_status = 'выключено'
        self.threads = [threading.Thread(target=method, daemon=True) for method in
                        (self.capture_loop, self.network_loop)]
        for thread in self.threads:
            thread.start()

    def status(self, value):
        self.session_status = value
        self.emit('radio_status', value)

    def valid(self, epoch):
        return self.enabled.is_set() and not self.stopped.is_set() and epoch == self.epoch

    @property
    def played_bytes(self):
        return self.mixer.radio_played_bytes

    @staticmethod
    def empty(waiting):
        while True:
            try:
                waiting.get_nowait()
            except queue.Empty:
                return

    def set_enabled(self, value):
        with self.lock:
            self.epoch += 1
            if value:
                self.enabled.set()
            else:
                self.enabled.clear()
            ws, source = self.ws, self.source
        self.empty(self.input)
        if ws:
            try: ws.abort()
            except OSError: pass
        if not value:
            self.mixer.cancel_radio()
            self.mixer.original_gain = 0.0
            if source:
                try: source.close()
                except OSError: pass
        self.status('подключаю радио…' if value else 'выключено')

    def capture_loop(self):
        while not self.stopped.is_set():
            if not self.enabled.wait(.1):
                continue
            epoch = self.epoch
            source = None
            try:
                source = self.source_factory()
                with self.lock:
                    self.source = source
                self.status('источник: ' + source.description)
                stereo_state, mono_state = None, None
                delay = deque()
                while self.valid(epoch):
                    data = source.read(self.stopped)
                    if not data:
                        break
                    stereo, stereo_state = audioop.ratecv(data, 2, 2, 48000, 24000, stereo_state)
                    self.mixer.feed_original(stereo)
                    mono = audioop.tomono(data, 2, .5, .5)
                    mono, mono_state = audioop.ratecv(mono, 2, 1, 48000, 24000, mono_state)
                    # Small look-behind gives OCR time to flag a new game line
                    # before its English audio reaches the radio translator.
                    delay.append((time.monotonic(), mono))
                    if self.mixer.priority():
                        delay.clear()
                        self.empty(self.input)
                    while delay and delay[0][0] <= time.monotonic() - .25:
                        _, pcm = delay.popleft()
                        if self.input.full():
                            try: self.input.get_nowait()
                            except queue.Empty: pass
                        self.input.put_nowait((epoch, time.monotonic(), pcm))
            except Exception as exc:
                if self.valid(epoch):
                    _log.warning('radio source failure %s', safe_error(exc))
                    self.status('нет аудио: ' + safe_error(exc))
                    self.emit('radio_error', safe_error(exc))
                    self.stopped.wait(2)
            finally:
                if source:
                    try:
                        source.close()
                    except OSError:
                        pass
                with self.lock:
                    if self.source is source:
                        self.source = None

    def _handle(self, event, epoch, key):
        kind = event.get('type')
        if kind == 'error':
            code = event.get('error', {}).get('code', 'unknown')
            # Do not include a server-provided message that might echo headers.
            message = ('Закончился API-баланс OpenAI. Радио выключено; голоса ElevenLabs работают отдельно.'
                       if code in ('credit_balance_exhausted', 'insufficient_quota') else
                       'Ошибка API радио: ' + str(code)[:80])
            exc = RuntimeError(message)
            exc.retryable = code not in ('invalid_api_key', 'credit_balance_exhausted', 'insufficient_quota',
                                         'model_not_found', 'unsupported_country_region_territory')
            raise exc
        if kind == 'session.output_audio.delta':
            pcm = base64.b64decode(event['delta'], validate=True)
            self.translated_bytes += len(pcm)
            self.last_audio_at = time.monotonic()
            if not self.mixer.priority():
                self.mixer.feed_radio(pcm)
        elif kind in ('session.input_transcript.delta', 'session.output_transcript.delta'):
            language = 'en' if kind.startswith('session.input') else 'ru'
            text = event.get('delta', '')
            try:
                self.history.append(language, text)
            except OSError:
                self.emit('radio_error', 'Не удалось сохранить историю радио.')
            self.emit('radio_transcript', (language, text))
        return kind

    def network_loop(self):
        failures = 0
        while not self.stopped.is_set():
            if not self.enabled.wait(.1):
                continue
            if self.mixer.priority():
                self.status('пауза: диалог персонажей') if self.session_status != 'пауза: диалог персонажей' else None
                self.stopped.wait(.1)
                continue
            epoch = self.epoch
            ws = None
            key = ''
            try:
                # Wait for actual source PCM before opening a billed session.
                item = self.input.get(timeout=.25)
                if item[0] != epoch or time.monotonic() - item[1] > .6:
                    continue
                key = self.key_loader()
                if not key:
                    exc = RuntimeError('Нет сохранённого ключа OpenAI для радио.')
                    exc.retryable = False
                    raise exc
                self.mixer.ensure_stream()
                ws = self.socket_factory()
                with self.lock:
                    self.ws = ws
                self.status('подключение OpenAI…')
                ws.connect(URL, header=['Authorization: Bearer ' + key], timeout=10)
                ws.settimeout(.1)
                ws.send(json.dumps({'type': 'session.update', 'session': {'audio': {
                    'input': {'transcription': {'model': 'gpt-realtime-whisper'}},
                    'output': {'language': 'ru'}}}}))
                self.connection_count += 1
                _log.info('radio connected count=%d', self.connection_count)
                self.empty(self.input)  # Handshake time must not become live lag.
                self.status('перевод EN → RU · разговорное радио')
                closing, close_deadline = False, 0
                began, last_sent, pending_pcm = time.monotonic(), time.monotonic(), bytearray()
                while self.valid(epoch):
                    priority = self.mixer.priority()
                    if priority and not closing:
                        self.empty(self.input)
                        ws.send(json.dumps({'type': 'session.close'}))
                        closing, close_deadline = True, time.monotonic() + 3
                        self.status('пауза: диалог персонажей')
                    if not closing:
                        while True:
                            try:
                                source_epoch, stamp, pcm = self.input.get_nowait()
                            except queue.Empty:
                                break
                            if source_epoch == epoch and time.monotonic() - stamp < .6:
                                pending_pcm.extend(pcm)
                        while len(pending_pcm) >= 9600:  # 200ms PCM16 mono24k
                            chunk = bytes(pending_pcm[:9600])
                            del pending_pcm[:9600]
                            ws.send(json.dumps({'type': 'session.input_audio_buffer.append',
                                'audio': base64.b64encode(chunk).decode('ascii')}))
                            self.input_seconds += .2
                            self.last_input_at = last_sent = time.monotonic()
                        if time.monotonic() - last_sent > 4:
                            raise RuntimeError('Пропал свежий HDMI-звук для перевода.')
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        raw = None
                    if raw == '':
                        raise ConnectionError('Сервис закрыл радио-соединение.')
                    if raw:
                        event = json.loads(raw)
                        if closing:
                            # Do not schedule delayed translated game dialogue.
                            if event.get('type') == 'session.closed':
                                break
                            if event.get('type', '').endswith('transcript.delta'):
                                self._handle(event, epoch, key)
                        elif self._handle(event, epoch, key) == 'session.closed':
                            break
                    if closing and time.monotonic() > close_deadline:
                        break
                    if time.monotonic() - began > 15:
                        failures = 0
            except queue.Empty:
                continue
            except Exception as exc:
                if self.valid(epoch):
                    _log.warning('radio network failure attempt=%d %s', failures + 1, safe_error(exc, key))
                    retryable = getattr(exc, 'retryable', True)
                    if isinstance(exc, websocket.WebSocketBadStatusException):
                        retryable = getattr(exc, 'status_code', 0) in (408, 429, 500, 502, 503, 504)
                    self.status('ошибка: ' + safe_error(exc, key))
                    if not retryable or failures >= 5:
                        self.set_enabled(False)
                        self.emit('radio_error', safe_error(exc, key))
                        self.emit('radio_disabled', None)
                    else:
                        failures += 1
                        self.status(f'восстанавливаю соединение ({failures}/5)…')
                        self.stopped.wait(min(2 ** failures, 8))
            finally:
                if ws:
                    try:
                        ws.close(timeout=.2)
                    except Exception:
                        pass
                with self.lock:
                    if self.ws is ws:
                        self.ws = None
                key = ''

    def close(self):
        self.stopped.set()
        self.set_enabled(False)
        for thread in self.threads:
            thread.join(timeout=2)
