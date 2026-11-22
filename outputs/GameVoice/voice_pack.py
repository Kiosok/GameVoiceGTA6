"""Portable original-sample packs and resumable, account-scoped IVC import.

Exports contain original English WAV references, never cloud voice models, API
keys or account voice IDs. Network mutations require an explicit CLI opt-in.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import time
from urllib.parse import urlencode
import uuid

from elevenlabs_clones import create_ivc, references
from pipeline_install import API
from reference_policy import require_minimum_audio
from speaker_voices import speaker_key
from voice_pipeline import atomic_json, load


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def pack_digest(voices):
    return sha(json.dumps(voices, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode('utf8'))


def read_pack(directory):
    root = Path(directory).resolve()
    manifest = root/'manifest.json'
    if manifest.stat().st_size > 1024*1024:
        raise ValueError('Manifest is too large.')
    value = json.loads(manifest.read_text(encoding='utf-8'))
    voices = value.get('voices', [])
    if value.get('schema') != 1 or not 1 <= len(voices) <= 100 or value.get('pack_id') != pack_digest(voices):
        raise ValueError('Invalid or changed voice pack manifest.')
    names = set()
    for item in voices:
        name = item.get('name')
        if not isinstance(name, str) or not 1 <= len(name) <= 48 or not speaker_key(name):
            raise ValueError('Invalid character name.')
        identity = speaker_key(name)
        if identity in names:
            raise ValueError('Duplicate character name.')
        names.add(identity)
        if float(item.get('clean_speech_seconds', 0)) < 60:
            raise ValueError('At least 60 seconds of prepared clean speech is required.')
        if not 1 <= len(item.get('samples', [])) <= 20:
            raise ValueError('Use 1 to 20 samples per character.')
        paths = []
        for sample in item['samples']:
            relative = PurePosixPath(sample['path'])
            if relative.is_absolute() or '..' in relative.parts or '\\' in sample['path'] or ':' in sample['path']:
                raise ValueError('Sample path must stay inside the pack.')
            path = (root/Path(*relative.parts)).resolve()
            if not path.is_relative_to(root) or path.suffix.lower() != '.wav':
                raise ValueError('Sample path must be a WAV inside the pack.')
            if path.stat().st_size > 10*1024*1024 or sha(path.read_bytes()) != sample['sha256']:
                raise ValueError('Sample checksum mismatch or sample too large.')
            paths.append(path)
        files, _ = references(paths)
        require_minimum_audio(files)
    return value


def export_pack(installations, overlay, target):
    """Snapshot only the currently installed, hash-matched prepared references."""
    entries = load(overlay, {}).get('characters', [])
    records = []
    for path in Path(installations).glob('**/installation.json'):
        record = load(path, {})
        if record.get('state') == 'complete':
            records.append((path.parent, record))
    chosen = []
    for item in entries:
        if item.get('state') != 'ready':
            continue
        matches = [(folder, record) for folder, record in records
                   if speaker_key(record.get('name', '')) == speaker_key(item['name'])
                   and record.get('sha256') == item.get('reference_sha256')]
        if len(matches) != 1:
            raise ValueError('No unique prepared reference for ' + item['name'])
        folder, record = matches[0]
        source = folder/record.get('reference_file', 'reference-60s.wav')
        raw = source.read_bytes()
        if sha(raw) != record['sha256']:
            raise ValueError('Prepared reference changed: ' + item['name'])
        files, _ = references([source]); require_minimum_audio(files)
        chosen.append((item, source, record))
    target = Path(target)
    if (target/'manifest.json').exists():
        raise ValueError('Export into a new directory; existing packs are immutable.')
    voices = []
    for item, source, record in sorted(chosen, key=lambda row: speaker_key(row[0]['name'])):
        relative = 'samples/' + record['sha256'] + '.wav'
        destination = target/relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        voices.append({'name': item['name'], 'clean_speech_seconds': record['clean_speech_seconds'],
                       'milestone': record.get('milestone', 60),
                       'samples': [{'path': relative, 'sha256': record['sha256']}]})
    atomic_json(target/'manifest.json', {'schema': 1, 'pack_id': pack_digest(voices), 'voices': voices})
    return read_pack(target)


def list_voices(api):
    result, token, seen = [], None, set()
    for _ in range(100):
        query = {'page_size': 100}
        if token:
            query['next_page_token'] = token
        page = api.request('GET', '/v2/voices?' + urlencode(query))
        result.extend(page['voices'])
        if not page.get('has_more'):
            return result
        token = page.get('next_page_token')
        if not token or token in seen:
            raise ValueError('Cannot safely read all account voices.')
        seen.add(token)
    raise ValueError('Voice catalog pagination limit exceeded.')


def import_pack(directory, state_root, api, *, retry_rejected=False):
    """Caller holds InstanceLock. No synthesis, deletes, edits, or blind POST retry."""
    pack = read_pack(directory)
    user = api.request('GET', '/v1/user')
    if not user.get('user_id') or not user.get('workspace_id'):
        raise ValueError('Cannot bind the import to an account and workspace.')
    account = sha((user['user_id'] + ':' + user['workspace_id']).encode())
    path = Path(state_root)/account/(pack['pack_id'] + '.json')
    state = load(path, {'schema': 1, 'pack_id': pack['pack_id'], 'account': account, 'voices': {}})
    if state.get('account') != account or state.get('pack_id') != pack['pack_id']:
        raise ValueError('Import journal belongs to a different account or pack.')
    remote = list_voices(api)
    by_id = {v['voice_id']: v for v in remote}
    candidates = []
    for item in pack['voices']:
        identity = speaker_key(item['name'])
        marker = 'GameVoice pack ' + pack['pack_id'] + ' ' + sha(identity.encode())[:16]
        matches = [v for v in remote if v.get('description') == marker and v.get('category') == 'cloned']
        if len(matches) > 1:
            raise ValueError('Duplicate remote pack voices for ' + item['name'])
        previous = state['voices'].get(identity, {})
        if previous.get('state') == 'ready' and previous.get('voice_id') not in by_id:
            raise ValueError('An imported voice was removed; refusing to recreate it automatically.')
        if previous.get('state') in ('submitting', 'uncertain', 'verification_required') and not matches:
            raise ValueError('Previous creation needs account review: ' + item['name'])
        if previous.get('state') == 'rejected' and not retry_rejected:
            raise ValueError('Explicit --retry-rejected is required: ' + item['name'])
        candidates.append((item, identity, marker, matches[0] if matches else None, previous))
    missing = sum(not match and old.get('state') != 'ready' for _, _, _, match, old in candidates)
    plan = user['subscription']
    if missing and (plan.get('can_use_instant_voice_cloning') is not True or
                    int(plan['voice_limit'])-int(plan['voice_slots_used']) < missing):
        raise ValueError(f'Account needs IVC access and {missing} free voice slots. Nothing was uploaded.')
    budget = plan.get('max_voice_add_edits')
    if missing and budget is not None and budget-int(plan.get('voice_add_edit_counter', 0)) < missing:
        raise ValueError('Insufficient voice creation quota. Nothing was uploaded.')
    for item, identity, marker, match, old in candidates:
        if old.get('state') == 'ready':
            continue
        if match:
            detail = api.request('GET', '/v1/voices/' + match['voice_id'])
            verification = detail.get('voice_verification', {}).get('requires_verification',
                                detail.get('requires_verification'))
            record = {'name': item['name'], 'voice_id': match['voice_id'],
                      'state': 'ready' if verification is False else 'verification_required'}
        else:
            files, _ = references([Path(directory)/s['path'] for s in item['samples']])
            state['voices'][identity] = {'name': item['name'], 'state': 'submitting'}
            atomic_json(path, state)
            try:
                result = api.create('GameVoice — ' + item['name'], files, marker)
                identifier = result.get('voice_id', '')
                if not re.fullmatch(r'[A-Za-z0-9_-]{20,64}', identifier):
                    raise ValueError('Voice creation returned no valid ID; review the account.')
            except Exception as exc:
                state['voices'][identity]['state'] = 'rejected' if getattr(exc, 'request_rejected', False) else 'uncertain'
                atomic_json(path, state)
                raise
            record = {'name': item['name'], 'voice_id': identifier,
                      'state': 'ready' if result.get('requires_verification') is False else 'verification_required'}
        state['voices'][identity] = record
        atomic_json(path, state)
        if record['state'] != 'ready':
            raise ValueError('ElevenLabs requires voice verification: ' + item['name'])
    return state


def install_assignments(state, app_dir):
    """Install only into a closed app; preserve unrelated characters and tempo."""
    if any(v.get('state') != 'ready' for v in state['voices'].values()):
        raise ValueError('Some pack voices are not ready.')
    app_dir = Path(app_dir)
    heartbeat = app_dir/'pipeline-app-status.json'
    if heartbeat.exists() and time.time()-heartbeat.stat().st_mtime < 30:
        raise ValueError('Close GameVoice and wait 30 seconds before installing assignments.')
    path = app_dir/'elevenlabs-voices.json'
    old = load(path, {'default_voice': 'JBFqnCBsd6RMkjVDRZzb', 'speed': 1.25,
                      'catch_up': True, 'speakers': [], 'aliases': {}})
    data = dict(old)
    entries = {speaker_key(v['name']): v for v in old.get('speakers', [])}
    for identity, record in state['voices'].items():
        entries[identity] = {'name': record['name'], 'voice': record['voice_id'], 'manual': True}
        data.setdefault('aliases', {}).pop(identity, None)
    data['speakers'] = list(entries.values())
    if path.exists():
        shutil.copyfile(path, path.with_name('elevenlabs-voices.before-pack-' + uuid.uuid4().hex[:8] + '.json'))
    atomic_json(path, data)


class PackAPI(API):
    def create(self, name, files, marker):
        return create_ivc(name, files, marker, key=self.key)
