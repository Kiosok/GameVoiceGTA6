"""Launch the subtitle reader with the OpenAI voice engine."""
import sys
from subtitle_voice import main

if __name__ == '__main__':
    sys.argv.append('--openai')
    sys.exit(main())
