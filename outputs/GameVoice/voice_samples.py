"""Local HDMI voice-reference collection. Never uploads or creates voice clones."""
from __future__ import annotations

import argparse
import audioop
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import wave

from gamevoice import ffmpeg_path, HIDDEN, DEVICE

RATE, CHANNELS, WIDTH = 48000, 2, 2
BYTES_PER_SECOND = RATE * CHANNELS * WIDTH
CONTROL = Path(os.environ['LOCALAPPDATA']) / 'GameVoice' / 'sample-collection.json'


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    from voice_pipeline import atomic_json
    atomic_json(path,value)


def active_session():
    try:
        value = json.loads(CONTROL.read_text(encoding='utf8'))
        if value.get('active') and time.time() < value['expires_unix']:
            return Path(value['session'])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


class SubtitleSampleSink:
    """Bounded asynchronous sidecar: preserve original OCR and speaker candidates.

    Caption visibility is evidence for later review, not proven audio attribution.
    Only the HDMI subtitle crop is saved; no desktop or microphone is accessed.
    """
    def __init__(self):
        self.items = queue.Queue(maxsize=128)
        self.stopping = threading.Event()
        self.session = None
        self.checked = 0
        self.last_groups = None
        self.last_observation = 0
        self.last_image = 0
        self.dropped = 0
        self.thread = threading.Thread(target=self.writer, daemon=True)
        self.thread.start()

    def observe(self, stamp, frame_number, frame, roi, raw, groups, accepted, save_image=True):
        now = time.monotonic()
        if now - self.checked >= 1:
            self.session = active_session()
            self.checked = now
        if self.session is None:
            return
        groups = [list(group) for group in groups]
        changed = groups != self.last_groups
        if not accepted and (not changed or now - self.last_observation < .25):
            return
        image = None
        if save_image and groups and now - self.last_image >= 1:
            w, h = frame.size
            image = frame.crop(tuple(round(v * s) for v, s in zip(roi, (w, h, w, h))))
        value = {'type': 'caption_observation', 'utc': utc_now(),
                 'frame_monotonic': stamp, 'observed_monotonic': now,
                 'frame': frame_number, 'raw_ocr': raw, 'groups': groups,
                 'accepted': [{'speaker': x.speaker, 'text': x.text,
                               'first_seen_monotonic': x.stamp} for x in accepted],
                 'roi': list(roi), 'dropped_records': self.dropped,
                 'attribution': 'unverified_caption_visibility'}
        try:
            self.items.put_nowait((self.session, value, image))
        except queue.Full:
            self.dropped += 1
            return
        self.last_groups = groups
        self.last_observation = now
        if image is not None:
            self.last_image = now

    def writer(self):
        handle, current, image_count = None, None, 0
        try:
            while not self.stopping.is_set() or not self.items.empty():
                try:
                    session, value, image = self.items.get(timeout=.2)
                except queue.Empty:
                    continue
                try:
                    if session != current:
                        if handle:
                            handle.close()
                        session.mkdir(parents=True, exist_ok=True)
                        handle = (session / 'captions.jsonl').open('a', encoding='utf8')
                        current, image_count = session, 0
                    if image is not None and image_count < 2000:
                        name = f"subtitle-{value['frame_monotonic']:.3f}.jpg"
                        image.save(session / name, quality=85)
                        value['subtitle_image'] = name
                        image_count += 1
                    handle.write(json.dumps(value, ensure_ascii=False) + '\n')
                    handle.flush()
                except OSError:
                    self.dropped += 1
        finally:
            if handle:
                handle.close()

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=3)


def record_audio(session, minutes=45, continuous=False):
    """Record HDMI PCM; continuous mode rolls WAVs without reopening the device."""
    if minutes <= 0:
        raise ValueError('Длительность части должна быть больше нуля.')
    session = Path(session).resolve()
    session.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(session).free < 2 * 1024**3:
        raise RuntimeError('Для сбора образцов нужно хотя бы 2 ГБ свободного места.')
    if active_session() is not None:
        raise RuntimeError('Сбор образцов уже запущен; второй захват не нужен.')
    started = time.monotonic()
    expires = time.time() + minutes * 60
    segment_seconds = minutes * 60 if continuous else 30
    segment_limit = max(4, int(segment_seconds * RATE) * CHANNELS * WIDTH)
    if segment_limit > 4_000_000_000:
        raise ValueError('Часть WAV слишком велика; уменьшите длительность.')
    meta = {'schema': 1, 'session': str(session), 'started_utc': utc_now(),
            'process_started_monotonic': started, 'device': DEVICE,
            'sample_rate': RATE, 'channels': CHANNELS, 'sample_width': WIDTH,
            'segment_seconds': segment_seconds, 'limit_minutes': None if continuous else minutes,
            'continuous': continuous, 'rollover_minutes': minutes if continuous else None,
            'microphone_captured': False, 'uploaded': False,
            'alignment': 'First PCM arrival minus buffer duration; device/driver latency is unmeasured. Review before attributing a speaker.',
            'purpose': 'Original game audio and subtitle evidence for manual voice-reference selection; not approved clean clones.'}
    write_json(session / 'session.json', meta)
    error_file = (session / 'capture-errors.log').open('wb')
    proc = None
    out = None
    total, segment_bytes, segment_index, last_report = 0, 0, 0, 0
    first_sample = None
    state, error = 'finished', None
    try:
        proc = subprocess.Popen([ffmpeg_path(), '-hide_banner', '-loglevel', 'warning',
            '-rtbufsize', '16M', '-f', 'dshow', '-audio_buffer_size', '50',
            '-i', 'audio=' + DEVICE, '-vn', '-ac', str(CHANNELS), '-ar', str(RATE),
            '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=error_file, creationflags=HIDDEN, bufsize=0)
        control = {'active': True, 'session': str(session), 'continuous': continuous,
                   'expires_unix': time.time() + 30 if continuous else expires,
                   'pid': os.getpid()}
        write_json(CONTROL, control)
        with (session / 'audio-timeline.jsonl').open('a', encoding='utf8') as timeline:
            pending = bytearray()
            while continuous or (time.time() < expires and total < minutes * 60 * BYTES_PER_SECOND):
                if (session / 'STOP').exists():
                    break
                block = proc.stdout.read(19200)
                if not block:
                    raise RuntimeError('HDMI-аудиопоток остановился; см. capture-errors.log.')
                pending.extend(block)
                aligned = len(pending) // 4 * 4
                if not aligned:
                    continue
                data = bytes(pending[:aligned])
                del pending[:aligned]
                if first_sample is None:
                    first_sample = time.monotonic() - len(data) / BYTES_PER_SECOND
                    meta['estimated_first_sample_monotonic'] = first_sample
                    meta['first_pcm_utc'] = utc_now()
                    write_json(session / 'session.json', meta)
                while data:
                    if out is None:
                        name = f'audio-{segment_index:04}.wav'
                        out = wave.open(str(session / name), 'wb')
                        out.setparams((CHANNELS, WIDTH, RATE, 0, 'NONE', 'not compressed'))
                        timeline.write(json.dumps({'type': 'segment', 'file': name,
                            'sample_offset': total // 4,
                            'estimated_monotonic': first_sample + total / BYTES_PER_SECOND}) + '\n')
                        timeline.flush()
                    take = min(len(data), segment_limit - segment_bytes)
                    chunk, data = data[:take], data[take:]
                    out.writeframes(chunk)
                    total += take
                    segment_bytes += take
                    if total - last_report >= BYTES_PER_SECOND:
                        last_report = total
                        rms = audioop.rms(chunk, WIDTH)
                        status = {'state': 'recording', 'utc': utc_now(), 'pid': os.getpid(),
                            'seconds': round(total / BYTES_PER_SECOND, 2),
                            'segments': segment_index + 1, 'rms_pcm16': rms,
                            'peak_pcm16': audioop.max(chunk, WIDTH),
                            'source': DEVICE, 'session': str(session)}
                        write_json(session / 'status.json', status)
                        if continuous and control['expires_unix'] - time.time() < 20:
                            control['expires_unix'] = time.time() + 30
                            write_json(CONTROL, control)
                        timeline.write(json.dumps({'type': 'level', **status}) + '\n')
                        timeline.flush()
                        if shutil.disk_usage(session).free < 1024**3:
                            raise RuntimeError('Запись остановлена: осталось меньше 1 ГБ места.')
                    if segment_bytes == segment_limit:
                        out.close()
                        out, segment_bytes = None, 0
                        segment_index += 1
    except Exception as exc:
        state, error = 'error', str(exc)
    finally:
        if out:
            out.close()
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            proc.stdout.close()
        error_file.close()
        # Expired bounded sessions must also clear their flag. Never clear a
        # different recorder's control file during cleanup.
        try:
            owner = json.loads(CONTROL.read_text(encoding='utf8'))
            if owner.get('session') == str(session) and owner.get('pid') == os.getpid():
                write_json(CONTROL, {'active': False, 'session': str(session)})
        except (OSError, ValueError):
            pass
        write_json(session / 'status.json', {'state': state, 'error': error,
            'utc': utc_now(), 'seconds': round(total / BYTES_PER_SECOND, 2),
            'segments': segment_index + bool(segment_bytes), 'session': str(session)})
    if error:
        raise RuntimeError(error)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', required=True)
    parser.add_argument('--minutes', type=float, default=45)
    parser.add_argument('--continuous', action='store_true',
                        help='Continue until STOP; --minutes sets the length of each WAV.')
    args = parser.parse_args()
    record_audio(args.session, args.minutes, args.continuous)
