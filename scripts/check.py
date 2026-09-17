"""Run every free check, in order, before spending anything on a call.

    uv run python scripts/check.py

Stage 1 needs no API keys and costs nothing: it proves the code itself is
sound. Stage 2 only runs if you have a .env, and tests your keys against the
live APIs. Nothing here places a call or synthesises speech.

Cross-platform on purpose -- this is the one script someone runs before a demo.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FREE_SUITES = [
    ("Actions the bot can take", "tests/test_tools.py"),
    ("System prompt reaches the model", "tests/test_prompt_delivery.py"),
    ("A whole call, end to end", "tests/test_pipeline_e2e.py"),
]


def run(path: str) -> tuple[bool, str]:
    """Run one suite, returning (passed, last meaningful line of output)."""
    result = subprocess.run(
        [sys.executable, str(ROOT / path)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=600,
    )
    lines = [
        line
        for line in (result.stdout + result.stderr).splitlines()
        if line.strip() and "nltk" not in line and not line.startswith("[")
    ]
    summary = next(
        (line for line in reversed(lines) if "passed" in line or "provider" in line),
        lines[-1] if lines else "no output",
    )
    return result.returncode == 0, summary.strip()


def main() -> int:
    print("\n" + "=" * 62)
    print("STAGE 1  Free. No API keys needed. Nothing is charged.")
    print("=" * 62)

    failures = []
    for label, path in FREE_SUITES:
        print(f"\n  {label}")
        print(f"  running {path} ...", flush=True)
        ok, summary = run(path)
        print(f"  {'PASS' if ok else 'FAIL'}  {summary}")
        if not ok:
            failures.append(label)

    if failures:
        print("\n" + "=" * 62)
        print(f"STOP. {len(failures)} suite(s) failed: {', '.join(failures)}")
        print("Do not spend credits until these pass. Paste the output above.")
        print("=" * 62)
        return 1

    print("\n" + "=" * 62)
    print("Stage 1 clean: the code works. Nothing was charged.")
    print("=" * 62)

    if not (ROOT / ".env").exists():
        print("\nSTAGE 2 skipped -- no .env yet.\n")
        print("  cp .env.example .env")
        print("\nThen fill in these three and run this script again:")
        print("  DEEPGRAM_API_KEY=      console.deepgram.com")
        print("  GOOGLE_API_KEY=        aistudio.google.com/apikey  (free tier)")
        print("  ELEVENLABS_API_KEY=    elevenlabs.io")
        print("\nFor ELEVENLABS_VOICE_ID:  uv run python scripts/list_voices.py\n")
        return 0

    print("\n" + "=" * 62)
    print("STAGE 2  Checking your keys against the live APIs (no calls made).")
    print("=" * 62 + "\n")
    preflight = subprocess.run([sys.executable, str(ROOT / "scripts" / "preflight.py")], cwd=ROOT)
    if preflight.returncode != 0:
        print("\nFix the failures above before running the demo.\n")
        return 1

    print("\n" + "=" * 62)
    print("Ready. Start the browser demo:   uv run bot.py")
    print("Then open http://localhost:7860 and click Connect.")
    print("Follow docs/TESTSKRIPT.md -- it lists what to say and what to expect.")
    print("=" * 62 + "\n")
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
