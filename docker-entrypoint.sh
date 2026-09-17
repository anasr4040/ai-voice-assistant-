#!/bin/sh
# The Pipecat runner takes its public hostname as a CLI flag with no env var
# fallback, so translate the container's environment into its arguments.
set -e

ARGS="--host 0.0.0.0 --port ${PORT:-8080}"

# The hostname Twilio must reach us on. On Fly this is <app>.fly.dev.
# Without it the runner hands Twilio a localhost URL and the call dies silently.
if [ -n "$PUBLIC_HOSTNAME" ]; then
    ARGS="$ARGS -x $PUBLIC_HOSTNAME"
else
    echo "WARNING: PUBLIC_HOSTNAME is not set. Phone calls will not work;" >&2
    echo "         the browser demo still will." >&2
fi

# Optional: restrict to one transport (twilio, telnyx, webrtc...).
if [ -n "$TRANSPORT" ]; then
    ARGS="$ARGS -t $TRANSPORT"
fi

echo "Starting receptionist: $ARGS"
exec uv run --no-sync python bot.py $ARGS
