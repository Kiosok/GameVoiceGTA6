"""Export prepared samples or explicitly upload them into a user's account."""
import argparse
import json
import os
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE/'outputs/GameVoice'))
from voice_pack import PackAPI, export_pack, import_pack, install_assignments, read_pack
from voice_pipeline import InstanceLock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    export = modes.add_parser('export', help='Local only: snapshot current prepared references')
    export.add_argument('--installations', type=Path, required=True)
    export.add_argument('--overlay', type=Path, required=True)
    export.add_argument('--output', type=Path, required=True)
    upload = modes.add_parser('import', help='Validate by default; uploading requires --upload')
    upload.add_argument('pack', type=Path)
    upload.add_argument('--app-dir', type=Path, default=Path(os.environ['LOCALAPPDATA'])/'GameVoice')
    upload.add_argument('--upload', action='store_true')
    upload.add_argument('--confirm-rights', action='store_true', help='Confirm permission to upload and clone the supplied voices')
    upload.add_argument('--retry-rejected', action='store_true')
    upload.add_argument('--install', action='store_true', help='Apply ready voices to the closed app')
    args = parser.parse_args()
    if args.mode == 'export':
        pack = export_pack(args.installations, args.overlay, args.output)
        print(json.dumps({'exported': len(pack['voices']), 'pack_id': pack['pack_id']})); return
    pack = read_pack(args.pack)
    if not args.upload:
        print(json.dumps({'valid': True, 'voices': len(pack['voices']), 'network_calls': 0,
                          'message': 'Dry run. Add --upload --confirm-rights to create account voices.'})); return
    if not args.confirm_rights:
        parser.error('--confirm-rights is required before sending voice samples')
    args.app_dir.mkdir(parents=True, exist_ok=True)
    with InstanceLock(args.app_dir/'voice-pack-import.lock'):
        api = PackAPI()
        try:
            state = import_pack(args.pack, args.app_dir/'voice-pack-imports', api,
                                retry_rejected=args.retry_rejected)
            if args.install:
                install_assignments(state, args.app_dir)
            print(json.dumps({'ready': len(state['voices']), 'installed': args.install}))
        except Exception as exc:
            print(json.dumps({'error': str(exc).replace(api.key, '[key]')[:300]}, ensure_ascii=True))
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
