"""Windows game audio translator with optional current-user DPAPI key storage."""
from __future__ import annotations

import argparse
import audioop
import base64
from collections import deque
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import uuid

import sounddevice as sd
import websocket
from key_store import load_key, save_key

RATE = 48000
CHUNK = 4800  # 100 ms stereo PCM16
DEVICE = "Интерфейс цифрового звука (USB3.0 Audio)"
URL = "wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate"
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ConnectionInterrupted(ConnectionError):
    pass


def ffmpeg_path():
    candidates = [shutil.which("ffmpeg"), str(Path(os.environ.get("LOCALAPPDATA", "")) /
                  "Microsoft/WinGet/Links/ffmpeg.exe")]
    for path in candidates:
        if path and Path(path).is_file():
            return path
    raise RuntimeError("Не найден FFmpeg. Установите FFmpeg и перезапустите программу.")


def input_devices():
    result = subprocess.run([ffmpeg_path(), "-hide_banner", "-list_devices", "true",
                             "-f", "dshow", "-i", "dummy"], capture_output=True,
                            creationflags=HIDDEN, timeout=10)
    text = result.stderr.decode("utf-8", "replace")
    return re.findall(r'"([^"\r\n]+)" \(audio\)', text)


class PCMBuffer:
    """Bounded PCM byte FIFO; callbacks never wait for network or capture."""
    def __init__(self, capacity):
        self.capacity = capacity
        self.chunks = deque()
        self.size = 0
        self.lock = threading.Lock()

    def put(self, data):
        if len(data) % 4:
            raise ValueError("Stereo PCM frame alignment")
        with self.lock:
            if self.size + len(data) > self.capacity:
                return False
            self.chunks.append(data)
            self.size += len(data)
            return True

    def get(self, size):
        with self.lock:
            parts, needed = [], size
            while needed and self.chunks:
                chunk = self.chunks.popleft()
                take = min(needed, len(chunk))
                parts.append(chunk[:take])
                if take < len(chunk):
                    self.chunks.appendleft(chunk[take:])
                self.size -= take
                needed -= take
            return b"".join(parts), size - needed


class Engine:
    def __init__(self, device=DEVICE, output=None, key="", translate=False, emit=None):
        self.device, self.output, self.key = device, output, key
        self.translate = translate
        self.emit = emit or (lambda *args: None)
        self.stop_event = threading.Event()
        self.abort_event = threading.Event()
        self.original = PCMBuffer(RATE * 4)  # 1 second
        self.translated = PCMBuffer(RATE * 4 * 8)
        self.send_queue = queue.Queue(maxsize=20)
        self.original_gain = 0.0
        self.translation_gain = .9
        self.duck = True
        self.proc = None
        self.ws = None
        self.stream = None
        self.workers = []
        self.capture_bytes = 0
        self.output_bytes = 0
        self.peak = 0
        self.failure = None
        self.retryable = False
        self.failure_lock = threading.Lock()
        self.last_translation = 0.0
        self.send_rate_state = None
        self.receive_rate_state = None
        self.stderr_tail = deque(maxlen=8)

    def fail(self, message, retryable=False):
        with self.failure_lock:
            if self.failure is not None:
                return
            self.failure = str(message).replace(self.key, "[ключ]") if self.key else str(message)
            self.retryable = retryable
        self.emit("error", self.failure)
        self.abort_event.set()
        self.stop_event.set()
        if self.ws:
            try:
                self.ws.abort()
            except Exception:
                pass

    def stop(self, abort=False):
        self.stop_event.set()
        if abort:
            self.abort_event.set()
            if self.ws:
                try:
                    self.ws.abort()
                except Exception:
                    pass

    def callback(self, outdata, frames, timing, status):
        size = frames * 4
        if self.abort_event.is_set():
            outdata[:] = b"\0" * size
            return
        original, _ = self.original.get(size)
        translated, count = self.translated.get(size)
        now = time.monotonic()
        if count:
            self.last_translation = now
            self.output_bytes += count
        duck_gain = .3 if self.duck and now - self.last_translation < .25 else 1
        original = original.ljust(size, b"\0")
        translated = translated.ljust(size, b"\0")
        outdata[:] = audioop.add(audioop.mul(original, 2, self.original_gain * duck_gain),
                                audioop.mul(translated, 2, self.translation_gain), 2)

    def capture(self):
        try:
            while not self.stop_event.is_set():
                data = self.proc.stdout.read(CHUNK * 4)
                if not data:
                    if not self.stop_event.is_set():
                        self.fail("Захват звука остановился. Проверьте карту и выбранный вход. " +
                                  " ".join(self.stderr_tail)[-600:])
                    return
                if len(data) % 4:
                    self.fail("Карта передала неполный звуковой кадр.")
                    return
                self.capture_bytes += len(data)
                self.peak = max(self.peak, audioop.max(data, 2))
                level = audioop.rms(data, 2)
                self.emit("level", max(-80, 20 * math.log10(max(level, 1) / 32768)))
                if not self.original.put(data):
                    self.fail("Воспроизведение не успевает за игрой. Проверьте устройство вывода.")
                    return
                if self.translate:
                    mono = audioop.tomono(data, 2, .5, .5)
                    pcm, self.send_rate_state = audioop.ratecv(mono, 2, 1, RATE, 24000,
                                                               self.send_rate_state)
                    try:
                        self.send_queue.put_nowait(pcm)
                    except queue.Full:
                        self.fail("Передача отстаёт от игры. Проверьте интернет и запустите перевод заново.")
                        return
        except Exception as exc:
            if not self.stop_event.is_set():
                self.fail(f"Ошибка захвата: {exc}")

    def send(self):
        try:
            while not self.stop_event.is_set():
                try:
                    pcm = self.send_queue.get(timeout=.15)
                except queue.Empty:
                    continue
                if self.stop_event.is_set():
                    break
                self.ws.send(json.dumps({"type": "session.input_audio_buffer.append",
                                         "audio": base64.b64encode(pcm).decode("ascii")}))
            if not self.abort_event.is_set():
                self.ws.send(json.dumps({"type": "session.close"}))
        except Exception as exc:
            if not self.abort_event.is_set():
                self.fail(f"Передача звука прервана: {exc}",
                          isinstance(exc, (websocket.WebSocketException, OSError)))

    def handle_event(self, event):
        kind = event.get("type")
        if kind == "error":
            error = event.get("error", {})
            code = error.get("code", "unknown")
            descriptions = {"invalid_api_key": "Неверный API-ключ.",
                            "insufficient_quota": "Недостаточно средств или квоты API.",
                            "model_not_found": "Модель перевода недоступна этому API-проекту."}
            raise RuntimeError(descriptions.get(code, "Ошибка API: " + str(code)) +
                               " " + str(error.get("message", ""))[:400])
        if kind == "session.output_audio.delta":
            pcm = base64.b64decode(event["delta"], validate=True)
            if len(pcm) % 2:
                raise RuntimeError("Неверный формат звука перевода.")
            pcm, self.receive_rate_state = audioop.ratecv(pcm, 2, 1, 24000, RATE,
                                                         self.receive_rate_state)
            stereo = audioop.tostereo(pcm, 2, 1, 1)
            if not self.translated.put(stereo):
                raise RuntimeError("Перевод отстал более чем на 8 секунд. Остановлено: запустите заново.")
        elif kind in ("session.output_transcript.delta", "session.input_transcript.delta"):
            self.emit("ru" if kind.startswith("session.output") else "en", event.get("delta", ""))
        return kind

    def receive_until(self, wanted):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not self.stop_event.is_set():
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise ConnectionInterrupted("Сервис закрыл подключение.")
            if self.handle_event(json.loads(raw)) == wanted:
                return True
        if not self.stop_event.is_set():
            raise TimeoutError("Сервис перевода не подтвердил подключение за 15 секунд.")
        return False

    def stderr_reader(self):
        for line in self.proc.stderr:
            self.stderr_tail.append(line.decode("utf-8", "replace").strip())

    def worker(self, target):
        thread = threading.Thread(target=target, daemon=True)
        self.workers.append(thread)
        thread.start()

    def run(self):
        try:
            # Check local playback before opening a billed remote session.
            self.stream = sd.RawOutputStream(samplerate=RATE, channels=2, dtype="int16",
                device=self.output, blocksize=960, latency="low", callback=self.callback)
            self.stream.start()
            if self.translate:
                if not self.key:
                    raise RuntimeError("Введите API-ключ OpenAI в поле программы.")
                self.emit("status", "Подключение к переводу…")
                self.ws = websocket.WebSocket()
                safety_id = hashlib.sha256(str(uuid.getnode()).encode()).hexdigest()
                self.ws.connect(URL, header=[f"Authorization: Bearer {self.key}",
                                f"OpenAI-Safety-Identifier: {safety_id}"], timeout=15)
                self.ws.settimeout(1)
                if not self.receive_until("session.created"):
                    return
                self.ws.send(json.dumps({"type": "session.update", "session": {
                    "audio": {"output": {"language": "ru"}}}}))
                if not self.receive_until("session.updated"):
                    return
            if self.stop_event.is_set():
                return
            self.proc = subprocess.Popen([ffmpeg_path(), "-hide_banner", "-loglevel", "warning",
                "-rtbufsize", "16M", "-f", "dshow", "-audio_buffer_size", "50", "-i",
                f"audio={self.device}", "-vn", "-ac", "2", "-ar", str(RATE), "-f", "s16le",
                "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=HIDDEN, bufsize=0)
            self.worker(self.stderr_reader)
            self.worker(self.capture)
            self.emit("status", "Перевод включён • английский → русский" if self.translate
                      else "Проверка звука • только оригинал")
            if self.translate:
                self.worker(self.send)
                deadline = None
                while not self.abort_event.is_set():
                    if self.stop_event.is_set() and deadline is None:
                        deadline = time.monotonic() + 10
                        self.emit("status", "Завершение перевода…")
                    if deadline and time.monotonic() >= deadline:
                        self.emit("notice", "Ожидание последних реплик завершено по таймеру.")
                        break
                    try:
                        raw = self.ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        raise ConnectionInterrupted("Соединение с переводом закрыто.")
                    if self.handle_event(json.loads(raw)) == "session.closed":
                        if not self.stop_event.is_set():
                            self.emit("notice", "Сессия перевода завершена сервисом. Можно запустить заново.")
                        break
                until = time.monotonic() + 8
                while self.translated.size and not self.abort_event.is_set() and time.monotonic() < until:
                    time.sleep(.05)
            else:
                self.stop_event.wait()
        except websocket.WebSocketBadStatusException as exc:
            code = getattr(exc, "status_code", "")
            self.fail({401: "API-ключ не принят. Проверьте ключ OpenAI.",
                       403: "Доступ к сервису запрещён для этого подключения или проекта.",
                       429: "Достигнут лимит API или закончилась квота."}.get(code,
                    f"Не удалось подключить перевод (HTTP {code})."),
                    retryable=code in (408, 500, 502, 503, 504))
        except (websocket.WebSocketException, OSError, TimeoutError) as exc:
            if not self.abort_event.is_set():
                self.fail(f"Прервано соединение с переводом: {exc}", retryable=True)
        except Exception as exc:
            if not self.abort_event.is_set():
                self.fail(str(exc))
        finally:
            self.stop_event.set()
            self.abort_event.set()
            if self.proc:
                if self.proc.poll() is None:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait(timeout=3)
            if self.ws:
                try:
                    self.ws.close(timeout=1)
                except Exception:
                    pass
            for thread in self.workers:
                thread.join(timeout=2)
            if self.proc:
                self.proc.stdout.close()
                self.proc.stderr.close()
            if self.stream:
                self.stream.abort()
                self.stream.close()
            self.key = ""
            self.emit("done", None)


class ResilientEngine:
    """Retry transient transport failures; each attempt owns and cleans its resources."""
    def __init__(self, device=DEVICE, output=None, key="", translate=False, emit=None):
        self.device, self.output, self.key, self.translate = device, output, key, translate
        self.emit = emit or (lambda *args: None)
        self.original_gain, self.translation_gain, self.duck = 0.0, .9, True
        self.stop_event = threading.Event()
        self.child = None
        self.lock = threading.Lock()
        self.failure = None
        self.completed_bytes = 0

    @property
    def capture_bytes(self):
        return self.completed_bytes + (self.child.capture_bytes if self.child else 0)

    def stop(self, abort=False):
        self.stop_event.set()
        with self.lock:
            if self.child:
                self.child.stop(abort=abort)

    def gains(self):
        with self.lock:
            if self.child:
                self.child.original_gain = self.original_gain
                self.child.translation_gain = self.translation_gain
                self.child.duck = self.duck

    def run(self):
        retries = 0
        try:
            while not self.stop_event.is_set():
                def forward(kind, value):
                    if kind not in ("done", "error"):
                        self.emit(kind, value)
                with self.lock:
                    if self.stop_event.is_set():
                        break
                    self.child = Engine(self.device, self.output, self.key, self.translate, forward)
                    self.child.original_gain = self.original_gain
                    self.child.translation_gain = self.translation_gain
                    self.child.duck = self.duck
                    child = self.child
                began = time.monotonic()
                child.run()
                with self.lock:
                    self.completed_bytes += child.capture_bytes
                    self.child = None
                if self.stop_event.is_set():
                    break
                if not self.translate or not child.failure or not child.retryable:
                    self.failure = child.failure
                    break
                if time.monotonic() - began >= 30:
                    retries = 0
                if retries >= 5:
                    self.failure = "Не удалось восстановить связь после 5 попыток. Проверьте интернет и нажмите «Включить перевод»."
                    break
                delay = min(2 ** retries, 15)
                retries += 1
                self.emit("status", f"Связь прервалась. Переподключение через {delay} с · попытка {retries}/5")
                if self.stop_event.wait(delay):
                    break
        finally:
            self.key = ""
            if self.failure:
                self.emit("error", self.failure)
            self.emit("done", None)


class App:
    def __init__(self, root):
        self.root = root
        self.engine = None
        self.thread = None
        self.events = queue.Queue()
        self.closing = False
        self.started = None
        self.root.title("GameVoice — русская озвучка")
        self.root.geometry("850x720")
        self.root.minsize(740, 620)
        self.root.configure(bg="#101827")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#101827")
        style.configure("TLabel", background="#101827", foreground="#e5edf7", font=("Segoe UI", 11))
        style.configure("Hint.TLabel", foreground="#9aaec5", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 24, "bold"))
        style.configure("TButton", font=("Segoe UI", 11), padding=10)
        style.configure("TCheckbutton", background="#101827", foreground="#e5edf7", font=("Segoe UI", 10))
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="GameVoice", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Игра на английском. Озвучка на русском.", style="Hint.TLabel").pack(anchor="w", pady=(0, 16))
        self.saved_key = load_key()
        self.status = tk.StringVar(value="Ключ подставлен · готов к запуску перевода" if self.saved_key else "Готов к проверке звука")
        ttk.Label(frame, textvariable=self.status, wraplength=780).pack(anchor="w", pady=(0, 12))
        self.device = tk.StringVar(value=DEVICE)
        ttk.Label(frame, text="Источник игрового звука").pack(anchor="w")
        self.input_combo = ttk.Combobox(frame, textvariable=self.device, state="readonly", values=[DEVICE])
        self.input_combo.pack(fill="x", pady=(4, 10))
        ttk.Label(frame, text="Колонки / устройство вывода").pack(anchor="w")
        self.output_labels = {"Устройство Windows по умолчанию": None}
        try:
            hosts = sd.query_hostapis()
            for index, device in enumerate(sd.query_devices()):
                if device["max_output_channels"] >= 2 and hosts[device["hostapi"]]["name"] == "Windows WASAPI":
                    self.output_labels[f'{device["name"]} [WASAPI]'] = index
        except Exception:
            pass
        self.output = tk.StringVar(value="Устройство Windows по умолчанию")
        self.output_combo = ttk.Combobox(frame, textvariable=self.output, state="readonly", values=list(self.output_labels))
        self.output_combo.pack(fill="x", pady=(4, 10))
        ttk.Label(frame, text="API-ключ OpenAI · сохранение защищено Windows").pack(anchor="w")
        self.key = tk.StringVar(value=self.saved_key or os.environ.get("OPENAI_API_KEY", ""))
        key_row = ttk.Frame(frame)
        key_row.pack(fill="x", pady=(4, 4))
        self.key_entry = ttk.Entry(key_row, textvariable=self.key, show="•")
        self.key_entry.pack(side="left", fill="x", expand=True)
        self.save_key_button = ttk.Button(key_row, text="Запомнить ключ", command=self.remember_key)
        self.save_key_button.pack(side="left", padx=(8, 0))
        self.saved_key = ""
        ttk.Label(frame, text="При запуске перевода звук игры передаётся OpenAI. Использование API платное.",
                  style="Hint.TLabel").pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=14)
        self.monitor_button = ttk.Button(buttons, text="Проверить звук", command=lambda: self.start(False))
        self.monitor_button.pack(side="left")
        self.start_button = ttk.Button(buttons, text="Включить перевод", command=lambda: self.start(True))
        self.start_button.pack(side="left", padx=10)
        self.stop_button = ttk.Button(buttons, text="Остановить", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        volumes = ttk.Frame(frame)
        volumes.pack(fill="x")
        self.original_gain = tk.DoubleVar(value=0)
        self.translation_gain = tk.DoubleVar(value=90)
        for col, label, variable in [(0, "Оригинал", self.original_gain), (1, "Русская озвучка", self.translation_gain)]:
            volumes.columnconfigure(col, weight=1)
            box = ttk.Frame(volumes, padding=(0, 0, 16, 0))
            box.grid(row=0, column=col, sticky="ew")
            ttk.Label(box, text=label).pack(anchor="w")
            ttk.Scale(box, from_=0, to=100, variable=variable, command=self.gains).pack(fill="x")
        self.duck = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Приглушать оригинал во время перевода", variable=self.duck,
                        command=self.gains).pack(anchor="w", pady=(4, 10))
        self.level = tk.DoubleVar(value=0)
        ttk.Progressbar(frame, maximum=80, variable=self.level).pack(fill="x")
        self.metrics = tk.StringVar(value="Звук не захватывается • видео и микрофон выключены")
        ttk.Label(frame, textvariable=self.metrics, style="Hint.TLabel").pack(anchor="w", pady=(4, 12))
        notebook = ttk.Notebook(frame)
        notebook.pack(fill="both", expand=True)
        self.texts = {}
        for code, title in [("ru", "Русский перевод"), ("en", "Исходная речь")]:
            box = ttk.Frame(notebook)
            notebook.add(box, text=title)
            text = tk.Text(box, bg="#182438", fg="#e5edf7", insertbackground="white", wrap="word",
                           font=("Segoe UI", 12), relief="flat", padx=12, pady=10, height=6, state="disabled")
            scroll = ttk.Scrollbar(box, command=text.yview)
            text.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            text.pack(fill="both", expand=True)
            self.texts[code] = text
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.poll)
        threading.Thread(target=self.discover, daemon=True).start()

    def discover(self):
        try:
            self.events.put(("devices", input_devices()))
        except Exception as exc:
            self.events.put(("notice", str(exc)))

    def gains(self, *_):
        if self.engine:
            self.engine.original_gain = self.original_gain.get() / 100
            self.engine.translation_gain = self.translation_gain.get() / 100
            self.engine.duck = self.duck.get()
            if isinstance(self.engine, ResilientEngine):
                self.engine.gains()

    def remember_key(self):
        try:
            save_key(self.key.get())
        except (OSError, ValueError):
            self.status.set("Не удалось сохранить ключ. Проверьте, что он введён одной строкой.")
            return False
        self.status.set("Ключ сохранён для вашей учётной записи Windows")
        return True

    def start(self, translate):
        if self.thread and self.thread.is_alive():
            return
        key = self.key.get().strip()
        if translate and not key:
            self.status.set("Для перевода введите API-ключ OpenAI. Проверка звука доступна без ключа.")
            self.key_entry.focus_set()
            return
        if translate and not self.remember_key():
            return
        self.engine = ResilientEngine(self.device.get(), self.output_labels[self.output.get()], key,
                             translate, lambda *event: self.events.put(event))
        self.gains()
        self.monitor_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.input_combo.configure(state="disabled")
        self.output_combo.configure(state="disabled")
        self.key_entry.configure(state="disabled")
        self.save_key_button.configure(state="disabled")
        self.started = time.monotonic()
        self.status.set("Запуск…")
        self.thread = threading.Thread(target=self.engine.run, daemon=True)
        self.thread.start()

    def stop(self):
        if self.engine:
            self.engine.stop()
            self.stop_button.configure(state="disabled")
            self.status.set("Остановка…")

    def close(self):
        self.closing = True
        self.key.set("")
        if self.engine:
            self.engine.stop(abort=True)
        self.root.withdraw()
        self.wait_close()

    def wait_close(self):
        if self.thread and self.thread.is_alive():
            self.root.after(100, self.wait_close)
        else:
            self.root.destroy()

    def poll(self):
        for _ in range(400):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "devices":
                if value:
                    self.input_combo.configure(values=value)
                    if self.device.get() not in value:
                        self.status.set("Выберите аудиовход карты захвата в списке.")
            elif kind == "level":
                self.level.set(max(0, min(80, value + 80)))
            elif kind in self.texts:
                text = self.texts[kind]
                text.configure(state="normal")
                text.insert("end", value)
                if len(text.get("1.0", "end-1c")) > 20000:
                    text.delete("1.0", "end-10000c")
                text.see("end")
                text.configure(state="disabled")
            elif kind in ("status", "error", "notice"):
                self.status.set(value)
            elif kind == "done":
                self.monitor_button.configure(state="normal")
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.input_combo.configure(state="readonly")
                self.output_combo.configure(state="readonly")
                self.key_entry.configure(state="normal")
                self.save_key_button.configure(state="normal")
                self.level.set(0)
                if self.engine and self.engine.failure:
                    self.status.set(self.engine.failure)
                else:
                    self.status.set("Остановлено")
        if self.engine and self.started:
            seconds = self.engine.capture_bytes / (RATE * 4)
            self.metrics.set(f"Принято {seconds:.1f} с звука • видео и микрофон выключены")
        if not self.closing:
            self.root.after(100, self.poll)


def self_test(seconds):
    events = []
    engine = Engine(emit=lambda *event: events.append(event))
    engine.original_gain = .35
    thread = threading.Thread(target=engine.run)
    thread.start()
    time.sleep(seconds)
    engine.stop(abort=True)
    thread.join(timeout=10)
    result = {"capture_seconds": round(engine.capture_bytes / (RATE * 4), 2),
              "peak": engine.peak, "error": engine.failure,
              "worker_stopped": not thread.is_alive(),
              "capture_process_stopped": engine.proc is not None and engine.proc.poll() is not None}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["capture_seconds"] > .5 and not engine.failure and not thread.is_alive() else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", type=float, metavar="SECONDS")
    args = parser.parse_args()
    if args.self_test:
        return self_test(args.self_test)
    ctypes.windll.kernel32.CreateMutexW.restype = ctypes.c_void_p
    ctypes.windll.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\GameVoiceDesktop")
    if ctypes.windll.kernel32.GetLastError() == 183:
        ctypes.windll.user32.MessageBoxW(None, "GameVoice уже открыт.", "GameVoice", 0)
        return 0
    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    finally:
        ctypes.windll.kernel32.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    sys.exit(main())
