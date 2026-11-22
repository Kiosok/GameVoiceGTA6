"""Asynchronous local text-only history of actual speech routing events."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import threading
import uuid


class VoiceRouteLog:
    def __init__(self, fallback, session_getter, provider, on_error):
        self.fallback = Path(fallback)
        self.session_getter = session_getter
        self.provider = provider
        self.on_error = on_error
        self.run_id = uuid.uuid4().hex
        self.items = queue.Queue(maxsize=2048)
        self.stopping = threading.Event()
        self.failed = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def report(self):
        if not self.failed:
            self.failed = True
            self.on_error('Не удалось сохранить историю голосов; часть диагностики может отсутствовать.')

    def append(self, value, revision=''):
        record = dict(value, schema=1, run_id=self.run_id, pid=os.getpid(),
                      provider=self.provider, automatic_revision=revision)
        try:
            self.items.put_nowait(record)
        except queue.Full:
            self.report()

    def run(self):
        while not self.stopping.is_set() or not self.items.empty():
            try:
                record = self.items.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                session = self.session_getter()
                day = datetime.fromtimestamp(record['at_unix'], timezone.utc).strftime('%Y-%m-%d')
                path = (Path(session) / 'voice-routing.jsonl' if session else
                        self.fallback / day / (self.run_id + '.jsonl'))
                path.parent.mkdir(parents=True, exist_ok=True)
                record['recording_session'] = str(session) if session else None
                record['utc'] = datetime.fromtimestamp(record['at_unix'], timezone.utc).isoformat()
                with path.open('a', encoding='utf8') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                self.failed = False
            except (OSError, ValueError, TypeError):
                self.report()

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=3)
