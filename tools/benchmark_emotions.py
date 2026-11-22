"""Compare tagged/plain Turbo turns using a local, private sample manifest.

This makes paid OpenAI and ElevenLabs calls only with --run. It neither changes
the live app nor plays audio. Results and optional WAVs must remain private.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import time
import wave

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'outputs/GameVoice'))
from emotion_probe import added_delay, audio_prefix, classify_delivery, tagged_text
from elevenlabs_v4 import V4Client
from elevenlabs_speech import key_path as eleven_key_path
from key_store import load_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prefix-seconds', type=float, default=1.2)
    parser.add_argument('--rounds', type=int, default=2, choices=range(1, 5))
    parser.add_argument('--save-audio', action='store_true')
    parser.add_argument('--run', action='store_true', help='Authorize this bounded paid experiment')
    args = parser.parse_args()
    samples = json.loads(args.manifest.read_text(encoding='utf-8'))['samples']
    if not 1 <= len(samples) <= 5:
        raise ValueError('Use one to five samples.')
    if any(not isinstance(s.get('text'), str) or not 1 <= len(s['text']) <= 200 for s in samples):
        raise ValueError('Use subtitle phrases of at most 200 characters.')
    if not args.run:
        print(json.dumps({'dry_run': True, 'audio_analysis_calls': len(samples),
                          'tts_calls': len(samples)*(1+2*args.rounds), 'network_calls': 0})); return
    openai_key, eleven_key = load_key(), load_key(eleven_key_path())
    if not openai_key or not eleven_key:
        raise ValueError('Save both API keys in GameVoice before running.')
    args.output.mkdir(parents=True, exist_ok=True)
    report = {'utc': datetime.now(timezone.utc).isoformat(), 'tts_model': 'eleven_v4_turbo',
              'audio_model': 'gpt-audio-1.5', 'samples': [], 'scope': 'saved clean original speech; no live OCR/capture/player',
              'limits': 'Small paired sample, no blinded listening or emotion accuracy ground truth. '
                        'Original samples are cleaned; live mixture may be harder. Warm turns only; '
                        'projection does not measure actual overlap of speech, OCR and classification.'}

    def save():
        (args.output/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')

    try:
        for index, sample in enumerate(samples):
            wav, duration = audio_prefix(sample['path'], args.prefix_seconds)
            analysis = classify_delivery(wav, openai_key)
            row = {'character': sample['character'], 'text': sample['text'],
                   'prefix_ms': round(duration*1000), 'analysis': analysis, 'turns': []}
            report['samples'].append(row); save()
            print(json.dumps({'sample': index, 'analysis_ms': analysis['analysis_ms'], 'label': analysis['label']}), flush=True)
            client = V4Client(eleven_key, sample['voice_id'])
            try:
                client.synthesize('Хорошо.')  # Excluded voice/session warmup.
                for run in range(args.rounds):
                    order = ('plain', 'tagged') if run % 2 == 0 else ('tagged', 'plain')
                    for condition in order:
                        text = sample['text'] if condition == 'plain' else tagged_text(sample['text'], analysis['label'])
                        start = time.perf_counter()
                        pcm, _ = client.synthesize(text)
                        turn = {'condition': condition, 'round': run, 'first_audio_ms': client.first_chunk_ms,
                                'complete_ms': round((time.perf_counter()-start)*1000), 'audio_seconds': len(pcm)/48000}
                        row['turns'].append(turn); save()
                        if args.save_audio and run == 0:
                            with wave.open(str(args.output/f'{index}-{condition}.wav'), 'wb') as out:
                                out.setparams((1, 2, 24000, 0, 'NONE', 'not compressed')); out.writeframes(pcm)
                        print(json.dumps({'sample': index, **turn}), flush=True)
            finally:
                client.close()
            row['projection_no_overlap_ms'] = added_delay(analysis['analysis_ms'], row['prefix_ms'])
        deltas = {'first_audio_ms': [], 'complete_ms': []}
        for row in report['samples']:
            for run in range(args.rounds):
                turns = {t['condition']: t for t in row['turns'] if t['round'] == run}
                for metric in deltas:
                    deltas[metric].append(turns['tagged'][metric] - turns['plain'][metric])
        report['summary'] = {'pairs': len(deltas['complete_ms']),
            'median_tag_first_audio_delta_ms': statistics.median(deltas['first_audio_ms']),
            'median_tag_complete_delta_ms': statistics.median(deltas['complete_ms']),
            'median_analysis_ms': statistics.median(r['analysis']['analysis_ms'] for r in report['samples']),
            'median_projection_no_overlap_ms': statistics.median(r['projection_no_overlap_ms'] for r in report['samples'])}
        print(json.dumps(report['summary']), flush=True)
    except Exception as exc:
        # Sanitise both secrets, including nested third-party connection errors.
        report['error'] = str(exc).replace(openai_key, '[key]').replace(eleven_key, '[key]')[:300]
        save(); print(json.dumps({'error': report['error']})); return 1
    save(); return 0


if __name__ == '__main__':
    raise SystemExit(main())
