"""Durable, incremental voice bank. No capture device or network dependency."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid


def utc():
    return datetime.now(timezone.utc).isoformat()


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf8') as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        # Windows readers/AV can temporarily hold a handle without FILE_SHARE_DELETE.
        # Keep the complete temp file and retry; never tear a JSON file or kill capture
        # because a status reader happened to overlap this replacement.
        for attempt in range(50):
            try:
                os.replace(temporary,path)
                break
            except PermissionError:
                if attempt==49:raise
                time.sleep(min(.005*(attempt+1),.05))
    finally:
        temporary.unlink(missing_ok=True)


def text_key(text):
    return re.sub(r'[^a-zа-я0-9]', '', text.casefold().replace('ё', 'е'))


def recording_identity(session):
    """Keep a recording's durable identity when its storage moves via a junction."""
    session = Path(session)
    original = load(session/'session.json', {}).get('session')
    if isinstance(original, str) and Path(original).is_absolute():
        return str(Path(original))
    return str(session.resolve())


def closed_parts(session):
    """The following timeline entry proves close+rollover; never touch open WAV."""
    session = Path(session)
    status = load(session / 'status.json', {})
    meta = load(session / 'session.json', {})
    if not meta.get('continuous') or not meta.get('estimated_first_sample_monotonic'):
        return []
    try:
        lines = (session / 'audio-timeline.jsonl').read_text(encoding='utf8').splitlines()
    except OSError:
        return []
    parts = []
    for line in lines:
        try:
            value = json.loads(line)
            if value.get('type') == 'segment':
                parts.append(value)
        except ValueError:
            continue
    result = []
    for index, part in enumerate(parts):
        if index + 1 < len(parts):
            end = parts[index + 1]['sample_offset']
        elif status.get('state') in ('finished', 'error'):
            end = round(status['seconds'] * meta['sample_rate'])
        else:
            continue
        if end > part['sample_offset']:
            result.append({**part, 'end': end, 'start': part['sample_offset'],
                           'session': recording_identity(session), 'rate': meta['sample_rate']})
    return result


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.handle.write(b'0'); self.handle.flush()
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.handle.close(); self.handle = None
            raise RuntimeError('Обработчик голосов уже работает.') from None
        return self

    def __exit__(self, *args):
        import msvcrt
        if self.handle:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()


class Bank:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'bank.sqlite', timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, part TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            error TEXT, updated TEXT);
          CREATE TABLE IF NOT EXISTS clips(
            id TEXT PRIMARY KEY, character TEXT NOT NULL, session TEXT NOT NULL,
            start REAL NOT NULL, end REAL NOT NULL, text_key TEXT NOT NULL,
            pcm_hash TEXT NOT NULL, seconds REAL NOT NULL, path TEXT NOT NULL,
            evidence TEXT NOT NULL, created TEXT NOT NULL,
            UNIQUE(character,text_key), UNIQUE(pcm_hash));
          CREATE TABLE IF NOT EXISTS installs(
            character TEXT PRIMARY KEY, voice_id TEXT NOT NULL, reference_hash TEXT NOT NULL,
            seconds REAL NOT NULL, state TEXT NOT NULL, updated TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')

    def enqueue(self, part):
        identifier = hashlib.sha256(f"{part['session']}|{part['start']}|{part['end']}".encode()).hexdigest()[:24]
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO jobs(id,part,updated) VALUES(?,?,?)',
                            (identifier, json.dumps(part, ensure_ascii=False), utc()))
        return identifier

    def add(self, item):
        if item['seconds'] <= 0 or item['end'] <= item['start'] or not item['text_key']:
            raise ValueError('Пустой фрагмент не увеличивает банк.')
        # Cross-job overlap, replayed captions and identical PCM cannot buy a minute.
        overlap = self.db.execute('SELECT 1 FROM clips WHERE session=? AND start<? AND end>? LIMIT 1',
                                 (item['session'], item['end'] - .02, item['start'] + .02)).fetchone()
        if overlap:
            return False
        with self.db:
            cursor = self.db.execute('''INSERT OR IGNORE INTO clips
                (id,character,session,start,end,text_key,pcm_hash,seconds,path,evidence,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                tuple(item[k] for k in ('id','character','session','start','end','text_key','pcm_hash','seconds','path'))
                + (json.dumps(item['evidence'], ensure_ascii=False), utc()))
        return cursor.rowcount == 1

    def rows(self, name):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM clips WHERE character=? ORDER BY created,id', (name,))]

    def counters(self):
        installed = {r['character']: dict(r) for r in self.db.execute('SELECT * FROM installs')}
        result=[]
        for row in self.db.execute('SELECT character,ROUND(SUM(seconds),4) AS seconds,COUNT(*) AS clips FROM clips GROUP BY character'):
            meta=self.db.execute('SELECT value FROM metadata WHERE key=?',('installed-milestone:'+row['character'],)).fetchone()
            milestone=int(meta['value']) if meta else (60 if row['character'] in installed else 0)
            result.append({**dict(row),'install':installed.get(row['character']),
                'installed_milestone':milestone,'next_threshold':60 if milestone<60 else (160 if milestone<160 else None)})
        return result

    def close(self):
        self.db.close()
