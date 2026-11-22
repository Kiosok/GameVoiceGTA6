"""Hidden launcher; the OS lock keeps the worker singleton across app restarts."""
from pathlib import Path
import os
import subprocess
import sys
import time

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'outputs/GameVoice'))
from voice_pipeline import load
LOCAL=Path(os.environ['LOCALAPPDATA'])/'GameVoice'
config=load(LOCAL/'pipeline-config.json',{})
if not config.get('enabled'):raise SystemExit(0)
status=load(LOCAL/'pipeline-status.json',{})
if status.get('state')!='stopped' and time.time()-status.get('updated_unix',0)<25:raise SystemExit(0)
root=Path(config['root']);root.mkdir(parents=True,exist_ok=True)
with (root/'service.log').open('ab') as log:
    subprocess.Popen([str(BASE/'work/venv/Scripts/pythonw.exe'),str(BASE/'work/pipeline_daemon.py')],
        cwd=BASE,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=0x08000000|0x00004000)
