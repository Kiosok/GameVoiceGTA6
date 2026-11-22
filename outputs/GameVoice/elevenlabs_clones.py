"""Explicit one-shot IVC creation from user-reviewed references. No auto retries."""
from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
import re
import uuid
import wave

from elevenlabs_speech import HOST, check_response, folder, key_path
from key_store import load_key
from openai_speech import SpeechFailure, safe_error
from reference_policy import require_minimum_audio


def references(paths):
    files = []
    digest = hashlib.sha256()
    for value in paths:
        path = Path(value)
        raw = path.read_bytes()
        if not raw or len(raw) > 10 * 1024 * 1024:
            raise ValueError('Образец пуст или больше 10 МБ.')
        with wave.open(str(path), 'rb') as audio:
            if audio.getsampwidth() != 2 or audio.getnchannels() not in (1, 2):
                raise ValueError('Нужны образцы WAV PCM16.')
            if audio.getnframes() == 0:
                raise ValueError('В образце нет звука.')
        digest.update(hashlib.sha256(raw).digest())
        files.append(raw)
    if not files or len(files) > 20:
        raise ValueError('Нужно от 1 до 20 проверенных образцов.')
    return files, digest.hexdigest()


def multipart(name, files, description, boundary):
    # Use ASCII filenames so HTTP MIME parsers do not misinterpret Cyrillic.
    body = bytearray()
    for key, value in [('name', name), ('description', description), ('remove_background_noise', 'false')]:
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend(value.encode('utf8') + b'\r\n')
    for index, raw in enumerate(files, 1):
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="reference-{index:02}.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
        body.extend(raw + b'\r\n')
    body.extend(f'--{boundary}--\r\n'.encode())
    return bytes(body)


def create_ivc(name, files, description='', key=None, connection_factory=None):
    require_minimum_audio(files)
    key = load_key(key_path()) if key is None else key
    if not key:
        raise SpeechFailure('Не найден сохранённый ключ ElevenLabs.', False)
    boundary = 'GameVoice' + uuid.uuid4().hex
    body = multipart(name, files, description, boundary)
    factory = connection_factory or http.client.HTTPSConnection
    connection = factory(HOST, timeout=90)
    try:
        # No redirects and no retries: an interrupted POST may already have
        # created a voice. A caller must reconcile account state before retrying.
        connection.request('POST', '/v1/voices/add', body=body,
            headers={'xi-api-key': key, 'Content-Type': 'multipart/form-data; boundary=' + boundary,
                     'Accept': 'application/json'})
        response = connection.getresponse()
        try:
            check_response(response, key)
        except SpeechFailure as exc:
            # A definitive 4xx rejection is different from a lost response to
            # a possibly successful POST. Neither case is retried automatically.
            if 400 <= response.status < 500 and response.status != 408:
                exc.request_rejected = True
                exc.http_status = response.status
            raise
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise SpeechFailure('Слишком большой ответ создания голоса; проверьте аккаунт перед повтором.', False)
        result = json.loads(raw)
        identifier = result.get('voice_id', '')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,64}', identifier):
            raise SpeechFailure('Ответ без корректного voice_id; проверьте аккаунт перед повтором.', False)
        return {'voice_id': identifier,
                'requires_verification': result.get('requires_verification') is not False,
                'library_voice_match': result.get('library_voice_match')}
    finally:
        connection.close()


def save_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.pending')
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf8')
    temp.replace(path)


def create_reviewed_clone(character, paths, *, reviewed=False, storage=None, creator=None, retry_rejected=False):
    if not reviewed:
        raise ValueError('Сначала подтвердите персонажа и качество всех образцов на слух.')
    files, sample_hash = references(paths)
    target = (Path(storage) if storage is not None else folder() / 'clones') / (hashlib.sha256(character.encode('utf8')).hexdigest()[:16] + '.json')
    previous = None
    if target.exists():
        previous = json.loads(target.read_text(encoding='utf8'))
        if previous.get('sample_hash') != sample_hash and not (previous.get('state') == 'rejected' and retry_rejected):
            raise ValueError('Для персонажа уже есть запись клона с другими образцами. Проверьте её перед заменой.')
        if previous.get('state') in ('ready', 'verification_required'):
            return previous
        if previous.get('state') == 'rejected':
            if not retry_rejected:
                raise ValueError('Сервис отклонил предыдущую попытку. После устранения причины нужен явный повтор.')
        else:
            raise ValueError('Предыдущая попытка не завершена однозначно. Проверьте аккаунт перед повторным созданием.')
    record = {'character': character, 'sample_hash': sample_hash, 'sample_count': len(files),
              'reviewed': True, 'state': 'submitting', 'voice_name': 'GameVoice — ' + character,
              'attempt': previous.get('attempt', 1) + 1 if previous else 1}
    require_minimum_audio(files)
    if previous and previous.get('state') == 'rejected':
        record['previous_rejection'] = {key: previous.get(key) for key in ('sample_hash', 'http_status', 'error')}
    save_record(target, record)
    try:
        create = creator or create_ivc
        result = create(record['voice_name'], files,
                        'GameVoice character reference. Sample set SHA256: ' + sample_hash)
    except Exception as exc:
        record.update(state='rejected' if getattr(exc, 'request_rejected', False) else 'attempt_needs_review',
                      http_status=getattr(exc, 'http_status', None), error=safe_error(exc))
        save_record(target, record)
        raise
    record.update(result)
    record['state'] = 'verification_required' if result['requires_verification'] else 'ready'
    save_record(target, record)
    return record
