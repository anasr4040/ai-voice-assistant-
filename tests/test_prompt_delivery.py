"""Prove the system prompt actually reaches the model, for every provider.

This exists because the failure it guards against is silent. Passing the prompt
as an initial "system" message in LLMContext looks fine, works on OpenAI, and is
dropped without warning by the Google and Anthropic adapters -- you get a fluent
assistant that has never heard of the driving school, and nothing in the logs
says why.

Run: uv run python tests/test_prompt_delivery.py
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
import tempfile
import warnings
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loguru import logger  # noqa: E402

from receptionist import bot, store  # noqa: E402

# The services log the entire system prompt at DEBUG on construction.
logger.remove()

# These must be set AFTER importing bot, and by assignment rather than
# setdefault: bot.py calls load_dotenv(override=True), so a freshly copied
# .env -- which has every key present but blank -- replaces placeholders set
# earlier with empty strings, and the service constructors then raise
# "Missing credentials". This test only inspects what *would* be sent and never
# makes a request, so a dummy key is correct, and forcing it also keeps the
# test off the user's real keys.
for _var in ("OPENAI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ[_var] = "placeholder-no-request-is-made"
from receptionist.prompts import system_prompt  # noqa: E402
from receptionist.tools import TOOLS  # noqa: E402

# A phrase that only ever appears in our prompt, so finding it proves delivery.
CANARY = "Fahrschule Infinity"

ADAPTERS = {
    "openai": ("pipecat.adapters.services.open_ai_adapter", "OpenAILLMAdapter"),
    "google": ("pipecat.adapters.services.gemini_adapter", "GeminiLLMAdapter"),
    "anthropic": ("pipecat.adapters.services.anthropic_adapter", "AnthropicLLMAdapter"),
}
# The service passes its own system_instruction into the adapter at request
# time (see pipecat/services/openai/base_llm.py). Mirror that exactly, so this
# test reproduces the real call rather than a convenient approximation.
EXTRA_ARGS = {"convert_developer_to_user": False, "enable_prompt_caching": False}


def prompt_reaches_model(provider: str) -> tuple[bool, str]:
    """Build the real service and context, then inspect what would be sent."""
    from pipecat.processors.aggregators.llm_context import LLMContext

    os.environ["LLM_PROVIDER"] = provider
    service = bot.build_llm(system_prompt())
    context = LLMContext(tools=TOOLS)

    module, name = ADAPTERS[provider]
    adapter = getattr(importlib.import_module(module), name)()
    signature = inspect.signature(adapter.get_llm_invocation_params)
    kwargs = {k: v for k, v in EXTRA_ARGS.items() if k in signature.parameters}

    configured_prompt = getattr(service._settings, "system_instruction", "") or ""
    if "system_instruction" not in signature.parameters:
        return False, "adapter cannot accept a system instruction"
    kwargs["system_instruction"] = configured_prompt

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        params = adapter.get_llm_invocation_params(context, **kwargs)

    # The service holds the prompt; the adapter decides whether it is sent.
    configured = CANARY in str(configured_prompt)

    dedicated = str(params.get("system") or params.get("system_instruction") or "")
    in_messages = any(
        isinstance(m, dict) and m.get("role") == "system" and CANARY in str(m.get("content", ""))
        for m in (params.get("messages") or [])
    )
    sent = CANARY in dedicated or in_messages

    where = "system field" if CANARY in dedicated else ("messages" if in_messages else "NOWHERE")
    return (configured and sent), where


def main() -> int:
    store.init()
    failed = []

    print("\nDoes the system prompt actually reach the model?\n")
    for provider in ADAPTERS:
        ok, where = prompt_reaches_model(provider)
        print(f"  {'PASS' if ok else 'FAIL'}  {provider:<10} delivered via: {where}")
        if not ok:
            failed.append(provider)

    print()
    if failed:
        print(f"{len(failed)} provider(s) would run with NO system prompt: {', '.join(failed)}")
        print("The bot would answer as a generic assistant. Fix before demoing.")
        return 1
    print("All providers deliver the prompt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
