"""Launch the subtitle reader with ElevenLabs v4 Turbo."""
import sys
from subtitle_voice import main

if __name__ == '__main__':
    sys.argv.append('--elevenlabs')
    sys.exit(main())
