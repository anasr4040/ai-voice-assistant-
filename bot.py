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

    # Load Pipecat's sentence tokenizer now, before the first call. Otherwise
    # the first caller after an install waits in silence while its data is
    # downloaded.
    from pipecat.utils.prewarm import warm_deferred_imports

    logger.info("Loading sentence tokenizer (downloaded once per machine)...")
    warm_deferred_imports()

    from pipecat.runner.run import main

    main()
