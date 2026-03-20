"""
OpenAI-compatible API server platform adapter.

Exposes an HTTP server with endpoints:
- POST /v1/chat/completions        — OpenAI Chat Completions format (stateless)
- POST /v1/responses               — OpenAI Responses API format (stateful via previous_response_id)
- GET  /v1/responses/{response_id} — Retrieve a stored response
- DELETE /v1/responses/{response_id} — Delete a stored response
- GET  /v1/models                  — lists hermes-agent as an available model
- GET  /health                     — health check

Any OpenAI-compatible frontend (Open WebUI, LobeChat, LibreChat,
AnythingLLM, NextChat, ChatBox, etc.) can connect to hermes-agent
through this adapter by pointing at http://localhost:8642/v1.

Requires:
- aiohttp (already available in the gateway)
"""

import asyncio
import collections
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
)
from autoresearch.runtime import (
    AutoResearchManager,
    InvalidRunStateError,
    RunNotFoundError,
    TERMINAL_RUN_STATUSES,
)

logger = logging.getLogger(__name__)

# Default settings
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
MAX_STORED_RESPONSES = 100


def check_api_server_requirements() -> bool:
    """Check if API server dependencies are available."""
    return AIOHTTP_AVAILABLE


class ResponseStore:
    """
    In-memory LRU store for Responses API state.

    Each stored response includes the full internal conversation history
    (with tool calls and results) so it can be reconstructed on subsequent
    requests via previous_response_id.
    """

    def __init__(self, max_size: int = MAX_STORED_RESPONSES):
        self._store: collections.OrderedDict[str, Dict[str, Any]] = collections.OrderedDict()
        self._max_size = max_size

    def get(self, response_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a stored response by ID (moves to end for LRU)."""
        if response_id in self._store:
            self._store.move_to_end(response_id)
            return self._store[response_id]
        return None

    def put(self, response_id: str, data: Dict[str, Any]) -> None:
        """Store a response, evicting the oldest if at capacity."""
        if response_id in self._store:
            self._store.move_to_end(response_id)
        self._store[response_id] = data
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)

    def delete(self, response_id: str) -> bool:
        """Remove a response from the store. Returns True if found and deleted."""
        if response_id in self._store:
            del self._store[response_id]
            return True
        return False

    def __len__(self) -> int:
        return len(self._store)


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def cors_middleware(request, handler):
        """Add CORS headers to every response; handle OPTIONS preflight."""
        if request.method == "OPTIONS":
            return web.Response(status=200, headers=_CORS_HEADERS)
        response = await handler(request)
        response.headers.update(_CORS_HEADERS)
        return response
else:
    cors_middleware = None  # type: ignore[assignment]


class APIServerAdapter(BasePlatformAdapter):
    """
    OpenAI-compatible HTTP API server adapter.

    Runs an aiohttp web server that accepts OpenAI-format requests
    and routes them through hermes-agent's AIAgent.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.API_SERVER)
        extra = config.extra or {}
        self._host: str = extra.get("host", os.getenv("API_SERVER_HOST", DEFAULT_HOST))
        self._port: int = int(extra.get("port", os.getenv("API_SERVER_PORT", str(DEFAULT_PORT))))
        self._api_key: str = extra.get("key", os.getenv("API_SERVER_KEY", ""))
        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        self._response_store = ResponseStore()
        research_home = extra.get("research_home")
        self._research_manager = AutoResearchManager(
            base_dir=Path(research_home) if research_home else None
        )
        # Conversation name → latest response_id mapping
        self._conversations: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------

    def _check_auth(self, request: "web.Request") -> Optional["web.Response"]:
        """
        Validate Bearer token from Authorization header.

        Returns None if auth is OK, or a 401 web.Response on failure.
        If no API key is configured, all requests are allowed.
        """
        if not self._api_key:
            return None  # No key configured — allow all (local-only use)

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if token == self._api_key:
                return None  # Auth OK

        return web.json_response(
            {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}},
            status=401,
        )

    @staticmethod
    def _json_error(message: str, *, status: int = 400, code: Optional[str] = None) -> "web.Response":
        error = {"message": message, "type": "invalid_request_error"}
        if code:
            error["code"] = code
        return web.json_response({"error": error}, status=status)

    @staticmethod
    def _parse_bool(value: Any, *, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    async def _parse_json_body(self, request: "web.Request") -> Dict[str, Any]:
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception) as exc:
            raise ValueError("Invalid JSON in request body") from exc
        if not isinstance(body, dict):
            raise ValueError("JSON request body must be an object")
        return body

    def _build_app(self) -> "web.Application":
        app = web.Application(middlewares=[cors_middleware])
        self._register_routes(app)
        return app

    def _register_routes(self, app: "web.Application") -> None:
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/v1/models", self._handle_models)
        app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
        app.router.add_post("/v1/responses", self._handle_responses)
        app.router.add_get("/v1/responses/{response_id}", self._handle_get_response)
        app.router.add_delete("/v1/responses/{response_id}", self._handle_delete_response)
        app.router.add_post("/api/research/chat", self._handle_research_global_chat_post)
        app.router.add_get("/api/research/chat", self._handle_research_global_chat_list)
        app.router.add_get("/api/research/runs", self._handle_research_runs_list)
        app.router.add_post("/api/research/runs", self._handle_research_runs_create)
        app.router.add_get("/api/research/runs/{run_id}", self._handle_research_run_get)
        app.router.add_get("/api/research/runs/{run_id}/events", self._handle_research_run_events)
        app.router.add_get("/api/research/runs/{run_id}/reports", self._handle_research_run_reports)
        app.router.add_get("/api/research/runs/{run_id}/candidates", self._handle_research_run_candidates)
        app.router.add_get("/api/research/runs/{run_id}/metrics", self._handle_research_run_metrics)
        app.router.add_post("/api/research/runs/{run_id}/pause", self._handle_research_run_pause)
        app.router.add_post("/api/research/runs/{run_id}/resume", self._handle_research_run_resume)
        app.router.add_post("/api/research/runs/{run_id}/stop", self._handle_research_run_stop)
        app.router.add_post("/api/research/runs/{run_id}/chat", self._handle_research_run_chat)
        app.router.add_post("/api/research/runs/{run_id}/request-mutation", self._handle_research_run_request_mutation)
        app.router.add_get("/api/research/runs/{run_id}/iterations", self._handle_research_run_iterations)
        app.router.add_get(
            "/api/research/runs/{run_id}/iterations/{iteration}/mutation-audit",
            self._handle_research_iteration_mutation_audit,
        )

        # Serve frontend static files (Next.js static export) if available
        frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend" / "out"
        if frontend_dir.is_dir():
            async def _handle_frontend(request: web.Request) -> web.StreamResponse:
                # Try to serve the exact file
                rel_path = request.match_info.get("path", "")
                file_path = frontend_dir / rel_path
                if file_path.is_file():
                    return web.FileResponse(file_path)
                # Try with index.html for directory paths
                index = file_path / "index.html"
                if index.is_file():
                    return web.FileResponse(index)
                # SPA fallback to root index.html
                root_index = frontend_dir / "index.html"
                if root_index.is_file():
                    return web.FileResponse(root_index)
                return web.Response(status=404, text="Not found")

            app.router.add_get("/{path:.*}", _handle_frontend)

    # ------------------------------------------------------------------
    # Agent creation helper
    # ------------------------------------------------------------------

    def _create_agent(
        self,
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
    ) -> Any:
        """
        Create an AIAgent instance using the gateway's runtime config.

        Uses _resolve_runtime_agent_kwargs() to pick up model, api_key,
        base_url, etc. from config.yaml / env vars.
        """
        from run_agent import AIAgent
        from gateway.run import _resolve_runtime_agent_kwargs, _resolve_gateway_model

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        model = _resolve_gateway_model()

        max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))

        agent = AIAgent(
            model=model,
            **runtime_kwargs,
            max_iterations=max_iterations,
            quiet_mode=True,
            verbose_logging=False,
            ephemeral_system_prompt=ephemeral_system_prompt or None,
            session_id=session_id,
            platform="api_server",
            stream_delta_callback=stream_delta_callback,
        )
        return agent

    # ------------------------------------------------------------------
    # HTTP Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response({"status": "ok", "platform": "hermes-agent"})

    async def _handle_models(self, request: "web.Request") -> "web.Response":
        """GET /v1/models — return hermes-agent as an available model."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        return web.json_response({
            "object": "list",
            "data": [
                {
                    "id": "hermes-agent",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "hermes",
                    "permission": [],
                    "root": "hermes-agent",
                    "parent": None,
                }
            ],
        })

    async def _handle_chat_completions(self, request: "web.Request") -> "web.Response":
        """POST /v1/chat/completions — OpenAI Chat Completions format."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": {"message": "Invalid JSON in request body", "type": "invalid_request_error"}},
                status=400,
            )

        messages = body.get("messages")
        if not messages or not isinstance(messages, list):
            return web.json_response(
                {"error": {"message": "Missing or invalid 'messages' field", "type": "invalid_request_error"}},
                status=400,
            )

        stream = body.get("stream", False)

        # Extract system message (becomes ephemeral system prompt layered ON TOP of core)
        system_prompt = None
        conversation_messages: List[Dict[str, str]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                # Accumulate system messages
                if system_prompt is None:
                    system_prompt = content
                else:
                    system_prompt = system_prompt + "\n" + content
            elif role in ("user", "assistant"):
                conversation_messages.append({"role": role, "content": content})

        # Extract the last user message as the primary input
        user_message = ""
        history = []
        if conversation_messages:
            user_message = conversation_messages[-1].get("content", "")
            history = conversation_messages[:-1]

        if not user_message:
            return web.json_response(
                {"error": {"message": "No user message found in messages", "type": "invalid_request_error"}},
                status=400,
            )

        session_id = str(uuid.uuid4())
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        model_name = body.get("model", "hermes-agent")
        created = int(time.time())

        if stream:
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                _stream_q.put(delta)

            # Start agent in background
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                stream_delta_callback=_on_delta,
            ))

            return await self._write_sse_chat_completion(
                request, completion_id, model_name, created, _stream_q, agent_task
            )

        # Non-streaming: run the agent and return full response
        try:
            result, usage = await self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
            )
        except Exception as e:
            logger.error("Error running agent for chat completions: %s", e, exc_info=True)
            return web.json_response(
                {"error": {"message": f"Internal server error: {e}", "type": "server_error"}},
                status=500,
            )

        final_response = result.get("final_response", "")
        if not final_response:
            final_response = result.get("error", "(No response generated)")

        response_data = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final_response,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        return web.json_response(response_data)

    async def _write_sse_chat_completion(
        self, request: "web.Request", completion_id: str, model: str,
        created: int, stream_q, agent_task,
    ) -> "web.StreamResponse":
        """Write real streaming SSE from agent's stream_delta_callback queue."""
        import queue as _q

        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await response.prepare(request)

        # Role chunk
        role_chunk = {
            "id": completion_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())

        # Stream content chunks as they arrive from the agent
        loop = asyncio.get_event_loop()
        while True:
            try:
                delta = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
            except _q.Empty:
                if agent_task.done():
                    # Drain any remaining items
                    while True:
                        try:
                            delta = stream_q.get_nowait()
                            if delta is None:
                                break
                            content_chunk = {
                                "id": completion_id, "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                            }
                            await response.write(f"data: {json.dumps(content_chunk)}\n\n".encode())
                        except _q.Empty:
                            break
                    break
                continue

            if delta is None:  # End of stream sentinel
                break

            content_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(content_chunk)}\n\n".encode())

        # Get usage from completed agent
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        try:
            result, agent_usage = await agent_task
            usage = agent_usage or usage
        except Exception:
            pass

        # Finish chunk
        finish_chunk = {
            "id": completion_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }
        await response.write(f"data: {json.dumps(finish_chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")

        return response

    async def _write_sse_research_event(
        self,
        response: "web.StreamResponse",
        event: Dict[str, Any],
    ) -> None:
        payload = json.dumps(event)
        await response.write(f"id: {event['seq']}\n".encode())
        await response.write(b"event: research.event\n")
        await response.write(f"data: {payload}\n\n".encode())

    async def _handle_responses(self, request: "web.Request") -> "web.Response":
        """POST /v1/responses — OpenAI Responses API format."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": {"message": "Invalid JSON in request body", "type": "invalid_request_error"}},
                status=400,
            )

        raw_input = body.get("input")
        if raw_input is None:
            return web.json_response(
                {"error": {"message": "Missing 'input' field", "type": "invalid_request_error"}},
                status=400,
            )

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")
        conversation = body.get("conversation")
        store = body.get("store", True)

        # conversation and previous_response_id are mutually exclusive
        if conversation and previous_response_id:
            return web.json_response(
                {"error": {"message": "Cannot use both 'conversation' and 'previous_response_id'", "type": "invalid_request_error"}},
                status=400,
            )

        # Resolve conversation name to latest response_id
        if conversation:
            previous_response_id = self._conversations.get(conversation)
            # No error if conversation doesn't exist yet — it's a new conversation

        # Normalize input to message list
        input_messages: List[Dict[str, str]] = []
        if isinstance(raw_input, str):
            input_messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            for item in raw_input:
                if isinstance(item, str):
                    input_messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    role = item.get("role", "user")
                    content = item.get("content", "")
                    # Handle content that may be a list of content parts
                    if isinstance(content, list):
                        text_parts = []
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "input_text":
                                text_parts.append(part.get("text", ""))
                            elif isinstance(part, dict) and part.get("type") == "output_text":
                                text_parts.append(part.get("text", ""))
                            elif isinstance(part, str):
                                text_parts.append(part)
                        content = "\n".join(text_parts)
                    input_messages.append({"role": role, "content": content})
        else:
            return web.json_response(
                {"error": {"message": "'input' must be a string or array", "type": "invalid_request_error"}},
                status=400,
            )

        # Reconstruct conversation history from previous_response_id
        conversation_history: List[Dict[str, str]] = []
        if previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored is None:
                return web.json_response(
                    {"error": {"message": f"Previous response not found: {previous_response_id}", "type": "invalid_request_error"}},
                    status=404,
                )
            conversation_history = list(stored.get("conversation_history", []))
            # If no instructions provided, carry forward from previous
            if instructions is None:
                instructions = stored.get("instructions")

        # Append new input messages to history (all but the last become history)
        for msg in input_messages[:-1]:
            conversation_history.append(msg)

        # Last input message is the user_message
        user_message = input_messages[-1].get("content", "") if input_messages else ""
        if not user_message:
            return web.json_response(
                {"error": {"message": "No user message found in input", "type": "invalid_request_error"}},
                status=400,
            )

        # Truncation support
        if body.get("truncation") == "auto" and len(conversation_history) > 100:
            conversation_history = conversation_history[-100:]

        # Run the agent
        session_id = str(uuid.uuid4())
        try:
            result, usage = await self._run_agent(
                user_message=user_message,
                conversation_history=conversation_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
            )
        except Exception as e:
            logger.error("Error running agent for responses: %s", e, exc_info=True)
            return web.json_response(
                {"error": {"message": f"Internal server error: {e}", "type": "server_error"}},
                status=500,
            )

        final_response = result.get("final_response", "")
        if not final_response:
            final_response = result.get("error", "(No response generated)")

        response_id = f"resp_{uuid.uuid4().hex[:28]}"
        created_at = int(time.time())

        # Build the full conversation history for storage
        # (includes tool calls from the agent run)
        full_history = list(conversation_history)
        full_history.append({"role": "user", "content": user_message})
        # Add agent's internal messages if available
        agent_messages = result.get("messages", [])
        if agent_messages:
            full_history.extend(agent_messages)
        else:
            full_history.append({"role": "assistant", "content": final_response})

        # Build output items (includes tool calls + final message)
        output_items = self._extract_output_items(result)

        response_data = {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "created_at": created_at,
            "model": body.get("model", "hermes-agent"),
            "output": output_items,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        # Store the complete response object for future chaining / GET retrieval
        if store:
            self._response_store.put(response_id, {
                "response": response_data,
                "conversation_history": full_history,
                "instructions": instructions,
            })
            # Update conversation mapping so the next request with the same
            # conversation name automatically chains to this response
            if conversation:
                self._conversations[conversation] = response_id

        return web.json_response(response_data)

    # ------------------------------------------------------------------
    # GET / DELETE response endpoints
    # ------------------------------------------------------------------

    async def _handle_get_response(self, request: "web.Request") -> "web.Response":
        """GET /v1/responses/{response_id} — retrieve a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        stored = self._response_store.get(response_id)
        if stored is None:
            return web.json_response(
                {"error": {"message": f"Response not found: {response_id}", "type": "invalid_request_error"}},
                status=404,
            )

        return web.json_response(stored["response"])

    async def _handle_delete_response(self, request: "web.Request") -> "web.Response":
        """DELETE /v1/responses/{response_id} — delete a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        deleted = self._response_store.delete(response_id)
        if not deleted:
            return web.json_response(
                {"error": {"message": f"Response not found: {response_id}", "type": "invalid_request_error"}},
                status=404,
            )

        return web.json_response({
            "id": response_id,
            "object": "response",
            "deleted": True,
        })

    async def _handle_research_runs_list(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        limit = request.query.get("limit")
        try:
            limit_value = int(limit) if limit else None
        except ValueError:
            return self._json_error("Query param 'limit' must be an integer")

        runs = self._research_manager.list_runs(limit=limit_value)
        return web.json_response({"object": "list", "data": runs})

    async def _handle_research_runs_create(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            body = await self._parse_json_body(request)
        except ValueError as exc:
            return self._json_error(str(exc))

        manifest = body.get("manifest")
        metadata = body.get("metadata") or {}
        if manifest is not None and not isinstance(manifest, dict):
            return self._json_error("'manifest' must be an object")
        if not isinstance(metadata, dict):
            return self._json_error("'metadata' must be an object")

        try:
            run = self._research_manager.create_run(
                name=body.get("name"),
                goal=str(body.get("goal") or ""),
                manifest_path=body.get("manifest_path"),
                manifest=manifest,
                metadata=metadata,
                max_iterations=body.get("max_iterations"),
                autostart=self._parse_bool(body.get("autostart"), default=False),
            )
        except FileNotFoundError as exc:
            return self._json_error(str(exc), status=404)
        except ValueError as exc:
            return self._json_error(str(exc))

        return web.json_response({"object": "research.run", "data": run}, status=201)

    async def _handle_research_run_get(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        try:
            run = self._research_manager.get_run(run_id)
        except RunNotFoundError:
            return self._json_error(f"Research run not found: {run_id}", status=404)

        return web.json_response({"object": "research.run", "data": run})

    async def _handle_research_run_reports(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        try:
            reports = self._research_manager.list_reports(run_id)
        except RunNotFoundError:
            return self._json_error(f"Research run not found: {run_id}", status=404)

        return web.json_response({"object": "list", "data": reports})

    async def _handle_research_run_candidates(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        try:
            candidates = self._research_manager.list_candidates(run_id)
        except RunNotFoundError:
            return self._json_error(f"Research run not found: {run_id}", status=404)

        return web.json_response({"object": "list", "data": candidates})

    async def _handle_research_run_metrics(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        try:
            metrics = self._research_manager.list_metrics(run_id)
        except RunNotFoundError:
            return self._json_error(f"Research run not found: {run_id}", status=404)

        return web.json_response({"object": "list", "data": metrics})

    async def _handle_research_run_pause(self, request: "web.Request") -> "web.Response":
        return await self._handle_research_state_change(request, action="pause")

    async def _handle_research_run_resume(self, request: "web.Request") -> "web.Response":
        return await self._handle_research_state_change(request, action="resume")

    async def _handle_research_run_stop(self, request: "web.Request") -> "web.Response":
        return await self._handle_research_state_change(request, action="stop")

    async def _handle_research_state_change(self, request: "web.Request", *, action: str) -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        try:
            if action == "pause":
                run = self._research_manager.pause_run(run_id)
            elif action == "resume":
                run = self._research_manager.resume_run(run_id)
            elif action == "stop":
                run = self._research_manager.stop_run(run_id)
            else:
                return self._json_error(f"Unsupported action: {action}", status=500)
        except RunNotFoundError:
            return self._json_error(f"Research run not found: {run_id}", status=404)
        except InvalidRunStateError as exc:
            return self._json_error(str(exc), status=409)

        return web.json_response({"object": "research.run", "data": run})

    async def _handle_research_run_chat(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        try:
            body = await self._parse_json_body(request)
        except ValueError as exc:
            return self._json_error(str(exc))

        try:
            event = self._research_manager.add_operator_message(
                run_id,
                content=str(body.get("message") or ""),
                scope=str(body.get("scope") or "run"),
                author=str(body.get("author") or "operator"),
            )
            run = self._research_manager.get_run(run_id, include_manifest=False)
        except RunNotFoundError:
            return self._json_error(f"Research run not found: {run_id}", status=404)
        except ValueError as exc:
            return self._json_error(str(exc))

        return web.json_response({"object": "research.event", "data": event, "run": run})

    async def _handle_research_global_chat_post(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            body = await self._parse_json_body(request)
        except ValueError as exc:
            return self._json_error(str(exc))

        try:
            message = self._research_manager.add_global_message(
                content=str(body.get("message") or ""),
                author=str(body.get("author") or "operator"),
            )
        except ValueError as exc:
            return self._json_error(str(exc))

        return web.json_response({"object": "research.message", "data": message})

    async def _handle_research_global_chat_list(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        messages = self._research_manager.list_global_messages()
        return web.json_response({"messages": messages})

    async def _handle_research_run_request_mutation(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        try:
            body = await self._parse_json_body(request)
        except ValueError:
            body = {}

        try:
            event = self._research_manager.request_mutation(
                run_id,
                reason=str(body.get("reason") or ""),
            )
            run = self._research_manager.get_run(run_id, include_manifest=False)
        except RunNotFoundError:
            return self._json_error(f"Research run not found: {run_id}", status=404)

        return web.json_response({"object": "research.event", "data": event, "run": run})

    async def _handle_research_run_iterations(self, request: "web.Request") -> "web.Response":
        """GET /api/research/runs/{run_id}/iterations — list iteration summaries."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        try:
            iterations = self._research_manager.list_iterations(run_id)
        except RunNotFoundError:
            return self._json_error(f"Research run not found: {run_id}", status=404)

        return web.json_response({"object": "list", "data": iterations})

    async def _handle_research_iteration_mutation_audit(self, request: "web.Request") -> "web.Response":
        """GET /api/research/runs/{run_id}/iterations/{iteration}/mutation-audit — mutation audit with diffs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        try:
            iteration = int(request.match_info["iteration"])
        except ValueError:
            return self._json_error("Iteration must be an integer")

        try:
            audit = self._research_manager.get_mutation_audit(run_id, iteration)
        except RunNotFoundError:
            return self._json_error(f"Research run not found: {run_id}", status=404)

        return web.json_response({"object": "research.mutation_audit", "data": audit})

    async def _handle_research_run_events(self, request: "web.Request") -> "web.StreamResponse":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        try:
            self._research_manager.get_run(run_id, include_manifest=False)
        except RunNotFoundError:
            return self._json_error(f"Research run not found: {run_id}", status=404)

        after_raw = request.query.get("after", "0")
        try:
            after_seq = int(after_raw)
        except ValueError:
            return self._json_error("Query param 'after' must be an integer")

        once = self._parse_bool(request.query.get("once"), default=False)
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await response.prepare(request)

        await response.write(f": run {run_id}\n\n".encode())

        current_events = self._research_manager.list_events(run_id, after_seq=after_seq)
        for event in current_events:
            after_seq = event["seq"]
            await self._write_sse_research_event(response, event)

        if once:
            await response.write_eof()
            return response

        loop = asyncio.get_event_loop()
        try:
            while True:
                has_new = await loop.run_in_executor(
                    None,
                    lambda: self._research_manager.wait_for_events(run_id, after_seq, 15.0),
                )
                if not has_new:
                    run = self._research_manager.get_run(run_id, include_manifest=False)
                    if run["status"] in TERMINAL_RUN_STATUSES:
                        break
                    await response.write(b": keep-alive\n\n")
                    continue

                for event in self._research_manager.list_events(run_id, after_seq=after_seq):
                    after_seq = event["seq"]
                    await self._write_sse_research_event(response, event)
        except (ConnectionResetError, RuntimeError):
            return response

        await response.write_eof()
        return response

    # ------------------------------------------------------------------
    # Output extraction helper
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_output_items(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Build the full output item array from the agent's messages.

        Walks *result["messages"]* and emits:
        - ``function_call`` items for each tool_call on assistant messages
        - ``function_call_output`` items for each tool-role message
        - a final ``message`` item with the assistant's text reply
        """
        items: List[Dict[str, Any]] = []
        messages = result.get("messages", [])

        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    items.append({
                        "type": "function_call",
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", ""),
                        "call_id": tc.get("id", ""),
                    })
            elif role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", ""),
                })

        # Final assistant message
        final = result.get("final_response", "")
        if not final:
            final = result.get("error", "(No response generated)")

        items.append({
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": final,
                }
            ],
        })
        return items

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------

    async def _run_agent(
        self,
        user_message: str,
        conversation_history: List[Dict[str, str]],
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
    ) -> tuple:
        """
        Create an agent and run a conversation in a thread executor.

        Returns ``(result_dict, usage_dict)`` where *usage_dict* contains
        ``input_tokens``, ``output_tokens`` and ``total_tokens``.
        """
        loop = asyncio.get_event_loop()

        def _run():
            agent = self._create_agent(
                ephemeral_system_prompt=ephemeral_system_prompt,
                session_id=session_id,
                stream_delta_callback=stream_delta_callback,
            )
            result = agent.run_conversation(
                user_message=user_message,
                conversation_history=conversation_history,
            )
            usage = {
                "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
            }
            return result, usage

        return await loop.run_in_executor(None, _run)

    # ------------------------------------------------------------------
    # BasePlatformAdapter interface
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start the aiohttp web server."""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed", self.name)
            return False

        try:
            self._app = self._build_app()

            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()

            self._mark_connected()
            logger.info(
                "[%s] API server listening on http://%s:%d",
                self.name, self._host, self._port,
            )
            return True

        except Exception as e:
            logger.error("[%s] Failed to start API server: %s", self.name, e)
            return False

    async def disconnect(self) -> None:
        """Stop the aiohttp web server."""
        self._mark_disconnected()
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        logger.info("[%s] API server stopped", self.name)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        Not used — HTTP request/response cycle handles delivery directly.
        """
        return SendResult(success=False, error="API server uses HTTP request/response, not send()")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about the API server."""
        return {
            "name": "API Server",
            "type": "api",
            "host": self._host,
            "port": self._port,
        }
