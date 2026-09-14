"""Entry point. The Pipecat runner looks for `bot()` in the module it is called
from, so this file exists to expose it and to mount the office dashboard.

    uv run bot.py                      # browser demo, open http://localhost:7860
    uv run bot.py -t twilio -x <host>  # phone, <host> is your ngrok hostname
"""

from receptionist import dashboard  # noqa: F401  (registers /office routes)
from receptionist.bot import bot  # noqa: F401  (the runner calls this)

if __name__ == "__main__":
    import os
    import sys

    from loguru import logger

    if not os.getenv("DEEPGRAM_API_KEY"):
        logger.error("DEEPGRAM_API_KEY is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)

    from pipecat.runner.run import main

    main()
