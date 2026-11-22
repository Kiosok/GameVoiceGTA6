"""Shared capture region and explicit output-speaker preferences."""
import json
import os
from pathlib import Path
import sounddevice as sd

ROI_DEFAULTS = {
    'large': (.08, .74, .92, .918),
    'medium': (.16049382716049382, .7894736842105263, .8320987654320988, .9122807017543859),
    'small': (.16049382716049382, 576/720, .8320987654320988, 658/720),
}
ROI_LABELS = {'small':'Маленькие', 'medium':'Средние', 'large':'Большие'}

def path():
    return Path(os.environ['LOCALAPPDATA'])/'GameVoice'/'devices.json'

def settings():
    try:
        data=json.loads(path().read_text(encoding='utf8'))
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError):return {}

def save_setting(name,value):
    data=settings();data[name]=value
    write_settings(data)

def write_settings(data):
    target=path();target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_suffix('.pending')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
    temporary.replace(target)

def valid_roi(value, default=None):
    try:
        left,top,right,bottom=map(float,value)
        if 0<=left<right<=1 and 0<=top<bottom<=1 and right-left>=.12 and bottom-top>=.025:
            return left,top,right,bottom
    except (TypeError,ValueError):pass
    return default

def selected_roi_profile():
    profile=settings().get('roi_profile','medium')
    return profile if isinstance(profile,str) and profile in ROI_DEFAULTS else 'medium'

def roi_profiles():
    data=settings()
    saved=data.get('roi_profiles',{})
    if not isinstance(saved,dict): saved={}
    profiles={name:valid_roi(saved.get(name), default) for name,default in ROI_DEFAULTS.items()}
    # Upgrade the former two-row preset without overwriting custom rectangles.
    old_small=(.16049382716049382,600/720,.8320987654320988,658/720)
    if profiles['small']==old_small:profiles['small']=ROI_DEFAULTS['small']
    # Preserve the user's existing medium-sized region on the first upgrade.
    if not saved and 'roi_profile' not in data:
        profiles['medium']=valid_roi(data.get('roi'), profiles['medium'])
    return profiles

def load_roi(default):
    data=settings()
    if 'roi_profile' not in data:
        return valid_roi(data.get('roi'),default)
    return roi_profiles()[selected_roi_profile()]

def save_roi_profile(profile, roi=None):
    if profile not in ROI_DEFAULTS: raise ValueError('Неизвестный размер субтитров')
    profiles=roi_profiles()
    if roi is not None:
        value=valid_roi(roi)
        if value is None: raise ValueError('Некорректная область субтитров')
        profiles[profile]=value
    data=settings()
    data.update(roi_profile=profile, roi_profiles=profiles, roi=profiles[profile])
    write_settings(data)
    return profiles[profile]

def roi_ocr_options(profile):
    return (2, 10) if profile=='small' else (1,17)

def output_names():
    # One host API avoids three display copies of the same Windows speaker.
    preferred=int(sd.default.hostapi)
    return list(dict.fromkeys(device['name'] for device in sd.query_devices()
        if device['max_output_channels']>0 and device['hostapi']==preferred))

def output_index(name):
    if not name:return None
    matches=[(i,device) for i,device in enumerate(sd.query_devices())
        if device['max_output_channels']>0 and device['name']==name]
    if not matches:raise RuntimeError(f'Колонка «{name}» недоступна. Выберите выход звука в программе.')
    preferred=int(sd.default.hostapi)
    return next((i for i,device in matches if device['hostapi']==preferred),matches[0][0])
