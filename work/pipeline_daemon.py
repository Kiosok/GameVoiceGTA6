"""Persistent closed-WAV queue, separate from the never-paused HDMI recorder."""
from __future__ import annotations
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'outputs/GameVoice'))
from voice_pipeline import Bank,InstanceLock,atomic_json,closed_parts,load,utc
from pipeline_install import publish
from openai_speech import safe_error
from voice_samples import active_session

LOCAL=Path(os.environ['LOCALAPPDATA'])/'GameVoice'
ROOT=Path('D:/GameVoice/automatic-voices')
PYTHON=BASE/'work/venv/Scripts/python.exe'
HIDDEN=0x08000000|0x00004000


class Service:
    def __init__(self,root=ROOT):
        self.root=Path(root);self.bank=Bank(root);self.errors={};self.last_scan=0
        self.child=None;self.child_log=None;self.job=None;self.child_started=0;self.restart_at=0

    def status(self,state):
        jobs={r['state']:r['n'] for r in self.bank.db.execute('SELECT state,COUNT(*) AS n FROM jobs GROUP BY state')}
        pointer=BASE/'work/current-sample-session.txt'
        try:session=Path(pointer.read_text(encoding='utf-8-sig').strip())
        except OSError:session=None
        value={'schema':1,'state':state,'pid':os.getpid(),'utc':utc(),'updated_unix':time.time(),
            'recording':load(session/'status.json',{}) if session else {},'jobs':jobs,
            'current_job':self.job,'characters':self.bank.counters(),'errors':self.errors,
            'minimum_clean_speech_seconds':60,'raw_recordings_deleted':False}
        value['clone_milestones']=[60,160]
        atomic_json(self.root/'status.json',value)
        atomic_json(LOCAL/'pipeline-status.json',value)

    def discover(self):
        if time.monotonic()-self.last_scan<10:return
        self.last_scan=time.monotonic()
        for session in sorted(Path('D:/GameVoice/voice-samples').glob('session-*')):
            for part in closed_parts(session):self.bank.enqueue(part)

    def recorder_watchdog(self):
        if time.monotonic()<self.restart_at or active_session() is not None:return
        self.restart_at=time.monotonic()+90
        try:
            session=Path((BASE/'work/current-sample-session.txt').read_text(encoding='utf-8-sig').strip())
            if (session/'STOP').exists():return # An explicit stop is never undone.
            status=load(session/'status.json',{})
            # A stale status may mean a blocked driver. Never open a second device
            # while its recorder process is alive: preserve single ownership.
            pid=status.get('pid')
            if pid:
                import ctypes
                handle=ctypes.windll.kernel32.OpenProcess(0x1000,False,pid)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle);return
            if shutil.disk_usage('D:/GameVoice').free<2*1024**3:return
            with (self.root/'recorder-recovery.log').open('ab') as output:
                result=subprocess.run([str(PYTHON),str(BASE/'work/start_continuous_collection.py')],
                    cwd=BASE,stdout=output,stderr=output,stdin=subprocess.DEVNULL,creationflags=HIDDEN,timeout=25)
            if result.returncode:self.errors['recorder']='Не удалось восстановить HDMI-запись; повтор через 90 с.'
            else:self.errors.pop('recorder',None)
        except Exception as exc:self.errors['recorder']=safe_error(exc)

    def start_child(self,stage,folder=None,job=None):
        log=(Path(folder) if folder else self.root)/'processing.log'
        self.child_log=log.open('ab')
        command=[str(PYTHON),str(BASE/'work/pipeline_jobs.py'),stage,'--root',str(self.root)]
        if folder:command.extend(['--folder',str(folder)])
        self.child=subprocess.Popen(command,cwd=BASE,stdout=self.child_log,stderr=self.child_log,
            stdin=subprocess.DEVNULL,creationflags=HIDDEN)
        self.job=job or 'bootstrap';self.child_started=time.monotonic()

    def promote(self):
        registry=load(LOCAL/'elevenlabs-voices.json',{})
        assignments={e['name']:e for e in registry.get('speakers',[]) if e.get('manual')}
        from speaker_voices import speaker_key
        by_key={speaker_key(name):item for name,item in assignments.items()}
        for counter in self.bank.counters():
            name=counter['character']
            if counter['seconds']<60:continue
            try:
                current=by_key.get(speaker_key(name))
                if current is None and counter.get('install'):
                    current={'voice':counter['install']['voice_id']}
                installed=counter['installed_milestone']
                for milestone in (60,160):
                    if counter['seconds']<milestone or milestone<installed:continue
                    result=publish(name,self.bank.rows(name),current,self.root/'installations',LOCAL/'automatic-voices.json',milestone=milestone)
                    with self.bank.db:
                        self.bank.db.execute('INSERT OR REPLACE INTO installs VALUES(?,?,?,?,?,?)',
                            (name,result['voice_id'],result['sha256'],result['clean_speech_seconds'],result['state'],utc()))
                        self.bank.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('installed-milestone:'+name,str(milestone)))
                    current={'voice':result['voice_id']}
                self.errors.pop(name,None)
            except Exception as exc:self.errors[name]=safe_error(exc)

    def run(self):
        self.discover()
        if not self.bank.db.execute("SELECT 1 FROM metadata WHERE key='seed-v1'").fetchone():self.start_child('seed')
        last_promote=0
        while not (self.root/'STOP').exists():
            self.discover()
            config=load(LOCAL/'pipeline-config.json',{})
            enabled=config.get('enabled',True)
            if enabled:self.recorder_watchdog()
            if self.child:
                code=self.child.poll()
                if code is None:
                    self.status('processing');time.sleep(2);continue
                self.child_log.close();self.child_log=None;self.child=None
                if self.job!='bootstrap':
                    folder=self.root/'jobs'/self.job
                    done=load(folder/'bank-result.json',{}).get('state')=='done'
                    if code==0 and done:
                        with self.bank.db:self.bank.db.execute("UPDATE jobs SET state='done',error=NULL,updated=? WHERE id=?",(utc(),self.job))
                        self.errors.pop('processing',None)
                    else:
                        error='Обработка части не завершена; исходник сохранён. См. '+str(folder/'processing.log')
                        with self.bank.db:self.bank.db.execute("UPDATE jobs SET state='retry',retry_at=?,error=?,updated=? WHERE id=?",
                            (time.time()+300,error,utc(),self.job))
                        self.errors['processing']=error
                elif code:self.errors['bootstrap']='Не удалось загрузить прежние образцы; см. processing.log.'
                self.job=None
            if enabled and time.monotonic()-last_promote>300:
                self.status('publishing');self.promote();last_promote=time.monotonic()
            if enabled and shutil.disk_usage(self.root).free>=2*1024**3:
                row=self.bank.db.execute("SELECT * FROM jobs WHERE state IN ('queued','processing','retry') AND retry_at<=? ORDER BY rowid LIMIT 1",(time.time(),)).fetchone()
                if row:
                    folder=self.root/'jobs'/row['id'];folder.mkdir(parents=True,exist_ok=True)
                    if load(folder/'bank-result.json',{}).get('state')=='done':
                        with self.bank.db:self.bank.db.execute("UPDATE jobs SET state='done' WHERE id=?",(row['id'],))
                        continue
                    atomic_json(folder/'part.json',json.loads(row['part']))
                    with self.bank.db:self.bank.db.execute("UPDATE jobs SET state='processing',attempts=attempts+1,updated=? WHERE id=?",(utc(),row['id']))
                    self.start_child('process',folder,row['id'])
                    # Publish every completed batch, without waiting another five minutes.
                    last_promote=0
            self.status('processing' if self.child else ('waiting' if enabled else 'paused'))
            time.sleep(2)
        self.status('stopped')


if __name__=='__main__':
    ROOT.mkdir(parents=True,exist_ok=True)
    try:
        with InstanceLock(ROOT/'service.lock'):
            while not (ROOT/'STOP').exists():
                service=Service()
                try:service.run()
                except Exception as exc:
                    atomic_json(LOCAL/'pipeline-crash.json',{'utc':utc(),'error':safe_error(exc)})
                    time.sleep(30)
                else:break
                finally:service.bank.close()
    except Exception as exc:
        if 'уже работает' not in str(exc):
            atomic_json(LOCAL/'pipeline-crash.json',{'utc':utc(),'error':safe_error(exc)})
            raise
