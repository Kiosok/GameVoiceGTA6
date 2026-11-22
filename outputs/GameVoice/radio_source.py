"""Live HDMI audio, reusing the active recording without interrupting it."""
import json
from pathlib import Path
import struct
import subprocess
import time

from gamevoice import DEVICE, HIDDEN, ffmpeg_path
from voice_samples import active_session

FRAME_BYTES = 4  # stereo PCM16 at 48 kHz


def wav_data_offset(handle):
    handle.seek(0)
    header = handle.read(12)
    if len(header) != 12 or header[:4] != b'RIFF' or header[8:] != b'WAVE':
        raise ValueError('Файл записи ещё не готов: нет заголовка WAV.')
    while True:
        chunk = handle.read(8)
        if len(chunk) != 8:
            raise ValueError('Неполный заголовок WAV.')
        name, size = struct.unpack('<4sI', chunk)
        if name == b'fmt ':
            fmt = handle.read(size)
            if len(fmt) < 16 or struct.unpack('<HHIIHH', fmt[:16]) != (1, 2, 48000, 192000, 4, 16):
                raise ValueError('Запись должна быть stereo PCM16/48 кГц.')
            if size & 1:
                handle.seek(1, 1)
        elif name == b'data':
            return handle.tell()
        else:
            handle.seek(size + (size & 1), 1)


class RecordingTail:
    """Follow newly appended PCM and 45-minute rollover; never replay history."""
    description = 'текущая HDMI-запись'

    def __init__(self, session):
        self.session = Path(session)
        self.handle = None
        self.index = None
        self.offset = 0
        self.last_data = time.monotonic()
        self.skipped_bytes = 0

    def _open(self, index, live_edge):
        handle = (self.session / f'audio-{index:04}.wav').open('rb', buffering=0)
        try:
            start = wav_data_offset(handle)
            handle.seek(0, 2)
            end = handle.tell()
            offset = end - (end - start) % FRAME_BYTES if live_edge else start
            handle.seek(offset)
        except Exception:
            handle.close()
            raise
        if self.handle:
            self.handle.close()
        self.handle, self.index, self.offset = handle, index, offset

    def read(self, stop, size=38400):
        while not stop.is_set():
            if self.handle is None:
                status = json.loads((self.session / 'status.json').read_text(encoding='utf8'))
                if status.get('state') != 'recording':
                    raise RuntimeError('Сборщик HDMI-звука остановлен.')
                self._open(max(0, int(status['segments']) - 1), True)
            self.handle.seek(0, 2)
            end = self.handle.tell()
            # Stay at the live edge after a slow consumer. No minutes-old audio.
            if end - self.offset > 192000:
                new_offset = end - 38400
                new_offset -= new_offset % FRAME_BYTES
                self.skipped_bytes += new_offset - self.offset
                self.offset = new_offset
            self.handle.seek(self.offset)
            data = self.handle.read(size)
            data = data[:len(data) // FRAME_BYTES * FRAME_BYTES]
            if data:
                self.offset += len(data)
                self.last_data = time.monotonic()
                return data
            next_path = self.session / f'audio-{self.index + 1:04}.wav'
            if next_path.exists():
                try:
                    self._open(self.index + 1, False)
                    continue
                except (ValueError, OSError):
                    pass  # The recorder may still be writing the next header.
            if time.monotonic() - self.last_data > 4:
                raise RuntimeError('Нет свежего HDMI-звука в записи более 4 секунд.')
            stop.wait(.02)
        return b''

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None


class DirectHDMI:
    """Fallback for use when no recording is active."""
    description = 'HDMI-захват'

    def __init__(self):
        self.proc = subprocess.Popen([
            ffmpeg_path(), '-hide_banner', '-loglevel', 'error', '-f', 'dshow',
            '-audio_buffer_size', '50', '-i', 'audio=' + DEVICE, '-vn',
            '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1',
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=HIDDEN, bufsize=0)
        self.pending = bytearray()

    def read(self, stop, size=38400):
        while not stop.is_set():
            data = self.proc.stdout.read(size)
            if not data:
                raise RuntimeError('HDMI-аудиопоток остановился.')
            self.pending.extend(data)
            aligned = len(self.pending) // FRAME_BYTES * FRAME_BYTES
            if aligned:
                result = bytes(self.pending[:aligned])
                del self.pending[:aligned]
                return result
        return b''

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        self.proc.stdout.close()


def live_source():
    session = active_session()
    return RecordingTail(session) if session else DirectHDMI()
