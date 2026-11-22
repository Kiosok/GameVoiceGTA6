"""Local subtitle labels and persistent character -> built-in voice assignments."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from dataclasses import dataclass

VOICES = ('cedar', 'marin', 'ash', 'coral', 'ballad', 'sage', 'echo',
          'shimmer', 'verse', 'alloy')
_LABEL = re.compile(r'^\s*[-–—]?\s*([А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z0-9’\'\-]*(?:\s+(?:[А-ЯЁA-Z][А-Яа-яЁёA-Za-z0-9’\'\-]*|[0-9]+)){0,3})\s*[:：]\s*')
_NOT_NAMES = {'слушай', 'смотри', 'внимание', 'итак', 'скажи', 'например', 'подсказка', 'цель',
              'я','мы','ты','он','она','это','прости'}
_CONFUSABLES = str.maketrans('ABCEHKMOPTXYabceopxy', 'АВСЕНКМОРТХУавсеорху')
FEMALE_ROLES = ('Охранница','Сотрудница','Сотрудница полиции','Местная жительница',
                'Незнакомка','Полицейская','Работница','Водительница','Туристка','Продавщица',
                'Местные жительницы','Бездомная','Бездомные женщины','Охранницы',
                'Сотрудницы','Сотрудницы полиции','Незнакомки','Полицейские женщины',
                'Работницы','Водительницы','Туристки','Продавщицы','Заложница','Заложницы')
ROLE_NAMES = (*FEMALE_ROLES,'Охранник','Сотрудник','Сотрудник полиции','Местный житель',
              'Местные жители','Незнакомец','Незнакомый','Незнакомый человек','Полицейский',
              'Работник','Работники','Водитель','Водители','Турист','Продавец','Механик',
              'Бездомный','Бездомные','Охранники','Сотрудники','Сотрудники полиции',
              'Незнакомцы','Полицейские','Туристы','Продавцы','Механики','Заложник','Заложники')
ROLE_KEYS = {name.casefold() for name in ROLE_NAMES}


def speaker_key(name):
    value = ' '.join(re.sub(r'[^а-яa-z0-9\s]', '', name.translate(_CONFUSABLES).casefold().replace('ё', 'е')).split())
    # Both spellings have appeared in the same game's OCR output.
    return {'люсия': 'лусия'}.get(value, value)


def is_interface_label(name):
    """Reject UI headings even if old OCR has saved them as a character."""
    key=speaker_key(name)
    if key in ROLE_KEYS: return False
    if not key: return False
    words=key.split()
    if key in {'те','эо'}: return True
    if any(re.search(r'[а-яa-z]\d|\d[а-яa-z]',word) for word in words): return True
    return bool(re.match(r'^(?:цена|стоимость|тюнинг|оружие|глава|уровень|название|'
                         r'пистолет|револьвер|винтовка|дробовик|боеприпасы|патроны|'
                         r'малокалиберные|спортивные|трекер|питание|настройки|'
                         r'инвентарь|карта|задания|сохранение|загрузка)(?:\s|$)',key))


def resolve_speaker_key(name, known):
    """Resolve a unique closest spelling, including corruption near the start."""
    key = speaker_key(name)
    if key in ROLE_KEYS: return key
    if not key or key in known: return key
    known = tuple(dict.fromkeys(known))
    fused = [old for old in known if len(old)>=5 and edit_distance(key[:len(old)],old)<=1
             and 0<len(key)-len(old)<=4 and ' ' not in key[len(old):]
             and re.findall(r'\d+',key)==re.findall(r'\d+',old)]
    if len(fused)==1:return fused[0]
    matches = []
    for old in dict.fromkeys(known):
        contracted = (len(key)==4 and len(old)==5 and key[:1]==old[:1] and key[-2:]==old[-2:])
        if min(len(key),len(old)) < 5 and not contracted: continue
        if re.findall(r'\d+',key) != re.findall(r'\d+',old): continue
        limit = 2 if contracted else (1 if min(len(key),len(old)) <= 5 else 2)
        if abs(len(key)-len(old)) > limit: continue
        distance = edit_distance(key,old)
        if distance <= limit: matches.append((distance,old))
    matches.sort()
    return matches[0][1] if matches and (len(matches)==1 or matches[0][0]<matches[1][0]) else key


def edit_distance(a,b):
    previous = list(range(len(b)+1))
    for i,left in enumerate(a,1):
        current = [i]
        for j,right in enumerate(b,1):
            current.append(min(current[-1]+1,previous[j]+1,previous[j-1]+(left!=right)))
        previous = current
    return previous[-1]


def resolve_trusted_speaker_key(name, known):
    """Correct OCR only against assigned identities; never merge short names."""
    key = speaker_key(name)
    if not key or key in known: return key
    # A zero inside a word is an observed OCR error (д0ббс). Keep actual
    # numbered characters such as 'Полицейский 2' distinct.
    corrected = re.sub(r'(?<=[а-я])0(?=[а-я])', 'о', key)
    words = corrected.split()
    if len(words) > 1:
        if len(words[0]) == 1 and ' '.join(words[1:]) in known:
            return ' '.join(words[1:])
        if len(words[-1]) == 1 and ' '.join(words[:-1]) in known:
            return ' '.join(words[:-1])
    canonical = resolve_speaker_key(corrected, known)
    if canonical in known: return canonical
    # Observed мапиа -> маттиа loses two interior glyphs. Keep both ends,
    # require a unique match, and never apply this to short names.
    interior = [old for old in known if min(len(corrected),len(old)) >= 5
                and corrected[:2] == old[:2] and corrected[-2:] == old[-2:]
                and abs(len(corrected)-len(old)) <= 1
                and re.findall(r'\d+',corrected) == re.findall(r'\d+',old)
                and edit_distance(corrected,old) <= 2]
    return interior[0] if len(interior) == 1 else key


def split_fused_label(label,known):
    """Recover 'ДжейсонАга' without swallowing the spoken 'Ага'."""
    known=set(known) or {'джейсон','лусия','брайан','эндрю'}
    if speaker_key(label) in ROLE_KEYS or speaker_key(label) in known:return label,''
    whole=speaker_key(label)
    canonical=resolve_speaker_key(whole,known)
    if canonical in known and edit_distance(whole,canonical)<=2:return label,''
    candidates=[]
    for index in range(2,len(label)):
        key=speaker_key(label[:index])
        tail=label[index:].lstrip(' :：;')
        tail_length=len(speaker_key(tail))
        canonical=resolve_speaker_key(key,known)
        # A fused phrase starts with a new capitalized word. Never turn a
        # lowercase name ending (Водител+ь / Незнакоме+ц) into dialogue.
        if canonical in known and len(canonical)>=5 and 0<tail_length<=20 and tail[:1].isupper():
            candidates.append((edit_distance(key,canonical),abs(len(key)-len(canonical)),-index,label[:index],tail))
    if candidates:
        _,_,_,name,tail=min(candidates)
        return name,tail
    return label,''


def split_speaker(text, known=()):
    """Only a leading Name: is metadata; colons inside dialogue are retained."""
    text = ' '.join(text.split()).strip(' |')
    # A stray colon/speck before the actual label must not hide 'Кайл:'.
    text_without_specks = text.lstrip(' :：;|.．…\'"—–-')
    if text_without_specks != text and _LABEL.match(text_without_specks):
        text = text_without_specks
    known = tuple(known) or ('джейсон','лусия','брайан','эндрю')
    role = re.match(r'^([^:：]{2,48})[:：]\s*',text)
    if role and speaker_key(role[1]) in ROLE_KEYS:
        return role[1].strip(),text[role.end():].strip()
    match = _LABEL.match(text)
    if match and is_interface_label(match[1]): return '',text
    if match and 2 <= len(speaker_key(match[1])) <= 48 and speaker_key(match[1]) not in _NOT_NAMES:
        name,tail=split_fused_label(match[1],known)
        return name,' '.join((tail,text[match.end():])).strip()
    # Garbage before a coloured label must not turn that label into speech.
    delimiter = re.search(r'[:：;]',text[:70])
    if delimiter:
        prefix = text[:delimiter.start()]
        tokens = re.findall(r"[А-Яа-яЁёA-Za-z0-9'’\-]+",prefix)
        for count in range(min(4,len(tokens)),0,-1):
            name = ' '.join(tokens[-count:]).strip('-–—')
            if not is_interface_label(name) and resolve_speaker_key(name,known) in known:
                return name, text[delimiter.end():].strip()
    # OCR may drop the colon or turn its two dots into a period. Only strip
    # a known name before a capitalized sentence; a comma remains an address.
    tokens = list(re.finditer(r"[А-Яа-яЁёA-Za-z0-9'’\-]+",text))
    for count in range(min(4,len(tokens)-1),0,-1):
        name = text[:tokens[count-1].end()].strip('-–— ')
        rest = text[tokens[count-1].end():]
        boundary = re.match(r'^(?:\s+|[.．]\s*)(?=[А-ЯЁA-Z])',rest)
        if boundary and not is_interface_label(name) and resolve_speaker_key(name,known) in known:
            return name, rest[boundary.end():].strip()
    return '', text


@dataclass(frozen=True)
class AcceptedSubtitle:
    text: str
    speaker: str
    stamp: float


class SpeakerVoices:
    def __init__(self, path=None, voices=VOICES, seeds=None, allow_voice_ids=False,
                 fallback_voices=None):
        self.available_voices = tuple(voices)
        self.allow_voice_ids = allow_voice_ids
        # A provider catalog can include other characters' clones. Only an
        # explicit fallback pool may be used to allocate unknown identities.
        self.fallback_voices = tuple(fallback_voices) if fallback_voices is not None else None
        seeds = seeds if seeds is not None else {'джейсон': ('Джейсон','cedar'), 'лусия': ('Лусия','marin')}
        self.path = Path(path) if path is not None else Path(os.environ['LOCALAPPDATA']) / 'GameVoice' / 'voices.json'
        self.entries = {}
        self.aliases = {}
        self.automatic = {}
        self.automatic_revision = None
        self.default_voice = self.available_voices[0]
        self.speed = 1.25
        self.catch_up = True
        try:
            data = json.loads(self.path.read_text(encoding='utf8'))
            if self.valid_voice(data.get('default_voice')): self.default_voice = data['default_voice']
            self.speed = min(1.5, max(1.0, float(data.get('speed', 1.25))))
            self.catch_up = bool(data.get('catch_up', True))
            for entry in data.get('speakers', []):
                name, voice = entry['name'], entry['voice']
                if isinstance(name, str) and name.strip() and self.valid_voice(voice):
                    key = speaker_key(name)
                    canonical = key if entry.get('manual') else resolve_speaker_key(name,self.entries)
                    if canonical in self.entries and not entry.get('manual'):
                        if canonical != key: self.aliases[key] = canonical
                    else:
                        self.entries[key] = {'name': name, 'voice': voice,
                                             'manual': bool(entry.get('manual',False))}
            for alias,canonical in data.get('aliases',{}).items():
                if canonical in self.entries: self.aliases[alias] = canonical
        except (OSError, ValueError, TypeError, KeyError):
            pass
        for key,(name,voice) in seeds.items():
            self.entries.setdefault(key, {'name': name, 'voice': voice})

    def valid_voice(self, voice):
        return isinstance(voice,str) and (voice in self.available_voices or
            self.allow_voice_ids and re.fullmatch(r'[A-Za-z0-9_-]{20,64}',voice) is not None)

    def voice_for(self, name):
        if not name: return self.default_voice
        key = self.resolve(name)
        automatic = self.automatic.get(key)
        if automatic and self.automatic_allowed(key, automatic):
            return automatic['voice_id']
        if key not in self.entries:
            used = {entry['voice'] for entry in self.entries.values()}
            pool = self.available_voices if self.fallback_voices is None else self.fallback_voices
            voice = next((voice for voice in pool if voice not in used), self.default_voice)
            self.entries[key] = {'name': name, 'voice': voice}
            self.save()
        entry = self.entries[key]
        if self.fallback_voices is not None and not entry.get('manual'):
            reserved = {v['voice'] for v in self.entries.values() if v.get('manual')}
            reserved.update(v['voice_id'] for v in self.automatic.values())
            if entry['voice'] in reserved:
                return self.default_voice
        return entry['voice']

    def resolve(self,name):
        key = speaker_key(name)
        if key in ROLE_KEYS: return key
        trusted = {k for k,v in self.entries.items() if v.get('manual')}
        trusted.update(self.automatic)
        # Saved OCR mistakes must not shadow an assigned character, even if
        # they already exist as exact keys in the legacy registry.
        canonical = resolve_trusted_speaker_key(name, trusted)
        if canonical in trusted: return canonical
        return self.aliases.get(key, resolve_speaker_key(name,dict.fromkeys((*self.entries,*self.automatic))))

    def display_name(self,name):
        key = self.resolve(name)
        return self.entries.get(key,self.automatic.get(key,{})).get('name',name)

    def automatic_allowed(self, key, item):
        current = self.entries.get(key)
        return not current or not current.get('manual') or current['voice'] in (item.get('expected_voice'),item['voice_id'])

    def effective_entries(self):
        result = dict(self.entries)
        for key,item in self.automatic.items():
            if self.automatic_allowed(key,item):
                result[key] = {'name':item['name'],'voice':item['voice_id'],'automatic':True}
        return result

    def reload_automatic(self, path):
        """Separate overlay: cannot race manual assignments and tempo saves."""
        try:
            data = json.loads(Path(path).read_text(encoding='utf8'))
            revision = data['revision']
            if revision == self.automatic_revision:return False
            values = {}
            for item in data['characters']:
                if (item.get('state') != 'ready' or not self.valid_voice(item.get('voice_id'))
                        or float(item.get('clean_speech_seconds',0)) < 60):
                    raise ValueError('Некорректное автоматическое назначение.')
                key = speaker_key(item['name'])
                if not key or key in values:raise ValueError('Повтор имени.')
                values[key] = item
        except (OSError,ValueError,KeyError,TypeError):return False
        self.automatic, self.automatic_revision = values, revision
        return True

    def assign(self, name, voice):
        name = ' '.join(name.split()).strip(' :：')
        if not name or len(name) > 48 or not self.valid_voice(voice):
            raise ValueError('Укажите имя персонажа и голос из списка.')
        self.entries[speaker_key(name)] = {'name': name, 'voice': voice, 'manual': True}
        self.aliases.pop(speaker_key(name),None)
        self.save()

    def voices(self):
        return list(dict.fromkeys([self.default_voice, *(e['voice'] for e in self.effective_entries().values())]))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {'default_voice': self.default_voice, 'speed': self.speed,
                'catch_up': self.catch_up, 'speakers': list(self.entries.values()), 'aliases': self.aliases}
        temporary = self.path.with_suffix('.pending')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')
        temporary.replace(self.path)
