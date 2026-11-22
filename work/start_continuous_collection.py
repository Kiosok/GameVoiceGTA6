"""Launch one persistent HDMI recorder, with 45-minute lossless WAV rollover."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

base = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(base / 'outputs/GameVoice'))
from voice_samples import active_session, write_json

if active_session() is not None:
    raise SystemExit('Collection already active; no second capture started.')
session = Path('D:/GameVoice/voice-samples') / datetime.now().strftime('session-%Y%m%d-%H%M%S')
session.mkdir(parents=True, exist_ok=False)
write_json(session / 'collection-intent.json', {
    'requested_by_user': True, 'target_characters': 'all named characters',
    'continuous': True, 'wav_rollover_minutes': 45, 'uploaded': False,
    'purpose': 'Original HDMI game audio and subtitle evidence for later character reference review.',
})
with (session / 'recorder-stdout.log').open('wb') as out, (session / 'recorder-stderr.log').open('wb') as err:
    process = subprocess.Popen([
        sys.executable, str(base / 'outputs/GameVoice/voice_samples.py'),
        '--session', str(session), '--minutes', '45', '--continuous',
    ], stdout=out, stderr=err, stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW, cwd=base)
deadline = time.monotonic() + 15
while time.monotonic() < deadline:
    path = session / 'status.json'
    if path.exists():
        status = json.loads(path.read_text(encoding='utf8'))
        if status['state'] == 'recording' and status['seconds'] >= 1:
            pointer = base / 'work/current-sample-session.txt'
            if pointer.exists():
                (base / 'work/previous-sample-session.txt').write_text(pointer.read_text(encoding='utf-8-sig'), encoding='utf8')
            pointer.write_text(str(session), encoding='utf8')
            (base / 'outputs/GameVoice/current-recording.txt').write_text(str(session), encoding='utf8')
            print(json.dumps(status, ensure_ascii=False))
            break
        if status['state'] == 'error':
            raise SystemExit(json.dumps(status, ensure_ascii=False))
    if process.poll() is not None:
        raise SystemExit(f'Recorder exited {process.returncode}; inspect {session}')
    time.sleep(.3)
else:
    raise SystemExit(f'Recorder readiness not confirmed; inspect {session}')
