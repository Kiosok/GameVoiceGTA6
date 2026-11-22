"""Local PCM time stretching; sample rate and voice pitch stay unchanged."""
import subprocess

from gamevoice import ffmpeg_path, HIDDEN

RATE = 24000


def choose_tempo(base, catch_up, pending, age_seconds):
    base = max(1.0, min(1.5, float(base)))
    if not catch_up: return base
    if pending >= 5 or age_seconds >= 8: return max(base, 1.5)
    if pending >= 3 or age_seconds >= 4: return max(base, 1.4)
    return base


def change_tempo(pcm, factor):
    if factor == 1: return pcm
    factor = max(1.0, min(1.5, float(factor)))
    result = subprocess.run([
        ffmpeg_path(), '-hide_banner', '-loglevel', 'error',
        '-f', 's16le', '-ar', str(RATE), '-ac', '1', '-i', 'pipe:0',
        '-af', f'atempo={factor:.3f}', '-threads', '1',
        '-f', 's16le', '-ar', str(RATE), '-ac', '1', 'pipe:1'],
        input=pcm, capture_output=True, creationflags=HIDDEN, timeout=3)
    if result.returncode or not result.stdout or len(result.stdout) % 2:
        raise RuntimeError('Не удалось изменить темп звука.')
    return result.stdout
