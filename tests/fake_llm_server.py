"""A local server that speaks OpenAI's streaming chat protocol, for free tests.

The point is to exercise the *real* code path -- the real OpenAILLMService, the
real adapter, Pipecat's real tool-call parsing and dispatch, and the real tools
writing to a real database -- without paying a provider. Only the model's
judgement is faked; every line of our own plumbing runs for real.
"""

from __future__ import annotations

import json

from aiohttp import web


class FakeLLM:
    """Replays a scripted list of turns, one per request it receives.

    Each turn is either ``{"text": "..."}`` or
    ``{"tool": "name", "args": {...}}``.
    """

    def __init__(self, turns: list[dict]):
        self.turns = turns
        self.requests: list[dict] = []  # what the pipeline actually sent
        self.runner: web.AppRunner | None = None
        self.port: int | None = None

    # --- the OpenAI wire format -------------------------------------------

    @staticmethod
    def _chunk(delta: dict, finish: str | None = None) -> bytes:
        payload = {
            "id": "fake",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "fake-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload)}\n\n".encode()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(body)

        turn = self.turns[min(len(self.requests) - 1, len(self.turns) - 1)]

        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await response.prepare(request)

        if "tool" in turn:
            await response.write(
                self._chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_fake_1",
                                "type": "function",
                                "function": {"name": turn["tool"], "arguments": ""},
                            }
                        ],
                    }
                )
            )
            # Arguments arrive split across chunks in real life; do the same so
            # the accumulation logic is genuinely exercised.
            arguments = json.dumps(turn.get("args", {}))
            for piece in (arguments[: len(arguments) // 2], arguments[len(arguments) // 2 :]):
                await response.write(
                    self._chunk({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]})
                )
            await response.write(self._chunk({}, finish="tool_calls"))
        else:
            await response.write(self._chunk({"role": "assistant", "content": ""}))
            for word in turn["text"].split(" "):
                await response.write(self._chunk({"content": word + " "}))
            await response.write(self._chunk({}, finish="stop"))

        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> str:
        """Start on an ephemeral port and return the base_url to point a service at."""
        app = web.Application()
        app.router.add_post("/chat/completions", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    # --- assertions helpers ----------------------------------------------

    def tools_offered(self) -> list[str]:
        """Tool names the pipeline advertised on its first request."""
        if not self.requests:
            return []
        return [t["function"]["name"] for t in self.requests[0].get("tools", [])]

    def system_prompt(self) -> str:
        """The system prompt the pipeline actually sent."""
        for message in self.requests[0].get("messages", []) if self.requests else []:
            if message.get("role") in ("system", "developer"):
                return str(message.get("content", ""))
        return ""

    def sent_tool_result(self) -> bool:
        """Whether a tool result was fed back on a later request."""
        return any(
            any(m.get("role") == "tool" for m in r.get("messages", [])) for r in self.requests[1:]
        )
