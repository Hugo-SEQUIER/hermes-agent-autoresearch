import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    extra = {}
    if api_key:
        extra["key"] = api_key
    return APIServerAdapter(PlatformConfig(enabled=True, extra=extra))


class TestAutoResearchEndpoints:
    @pytest.mark.asyncio
    async def test_create_run_and_list_reports(self):
        adapter = _make_adapter()
        app = adapter._build_app()

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/api/research/runs",
                json={"name": "Demo run", "goal": "Research a generic strategy loop"},
            )
            assert resp.status == 201
            data = await resp.json()
            run_id = data["data"]["id"]
            assert data["data"]["report_count"] == 1

            reports_resp = await cli.get(f"/api/research/runs/{run_id}/reports")
            assert reports_resp.status == 200
            reports = await reports_resp.json()
            assert len(reports["data"]) == 1
            assert reports["data"][0]["kind"] == "run_brief"

    @pytest.mark.asyncio
    async def test_resume_pause_stop_flow(self):
        adapter = _make_adapter()
        app = adapter._build_app()

        async with TestClient(TestServer(app)) as cli:
            create_resp = await cli.post(
                "/api/research/runs",
                json={"name": "Demo run", "goal": "Research state transitions"},
            )
            run_id = (await create_resp.json())["data"]["id"]

            resume_resp = await cli.post(f"/api/research/runs/{run_id}/resume")
            assert resume_resp.status == 200
            assert (await resume_resp.json())["data"]["status"] == "running"

            pause_resp = await cli.post(f"/api/research/runs/{run_id}/pause")
            assert pause_resp.status == 200
            assert (await pause_resp.json())["data"]["status"] == "paused"

            stop_resp = await cli.post(f"/api/research/runs/{run_id}/stop")
            assert stop_resp.status == 200
            assert (await stop_resp.json())["data"]["status"] == "stopped"

    @pytest.mark.asyncio
    async def test_chat_mutation_and_once_events_stream(self):
        adapter = _make_adapter()
        app = adapter._build_app()

        async with TestClient(TestServer(app)) as cli:
            create_resp = await cli.post(
                "/api/research/runs",
                json={"name": "Demo run", "goal": "Research event streaming"},
            )
            run_id = (await create_resp.json())["data"]["id"]

            chat_resp = await cli.post(
                f"/api/research/runs/{run_id}/chat",
                json={"message": "Prioritize robustness checks", "scope": "run"},
            )
            assert chat_resp.status == 200
            chat_data = await chat_resp.json()
            assert chat_data["data"]["type"] == "operator.message"

            mutation_resp = await cli.post(
                f"/api/research/runs/{run_id}/request-mutation",
                json={"reason": "Force one more candidate"},
            )
            assert mutation_resp.status == 200
            mutation_data = await mutation_resp.json()
            assert mutation_data["data"]["type"] == "mutation.requested"

            stream_resp = await cli.get(f"/api/research/runs/{run_id}/events?once=true")
            assert stream_resp.status == 200
            assert "text/event-stream" in stream_resp.headers.get("Content-Type", "")
            body = await stream_resp.text()
            assert "event: research.event" in body
            assert "operator.message" in body
            assert "mutation.requested" in body

    @pytest.mark.asyncio
    async def test_iterations_list_and_mutation_audit(self):
        adapter = _make_adapter()
        app = adapter._build_app()

        async with TestClient(TestServer(app)) as cli:
            # Create a run with a couple of iterations
            create_resp = await cli.post(
                "/api/research/runs",
                json={
                    "name": "Iteration test",
                    "goal": "Test iteration and audit endpoints",
                    "max_iterations": 2,
                    "autostart": True,
                },
            )
            assert create_resp.status == 201
            run_id = (await create_resp.json())["data"]["id"]

            # Wait briefly for completion (minimal run with no manifest commands)
            import asyncio
            await asyncio.sleep(1.0)

            # List iterations
            iter_resp = await cli.get(f"/api/research/runs/{run_id}/iterations")
            assert iter_resp.status == 200
            iter_data = await iter_resp.json()
            assert iter_data["object"] == "list"
            assert len(iter_data["data"]) >= 1

            # Get mutation audit for iteration 1
            audit_resp = await cli.get(
                f"/api/research/runs/{run_id}/iterations/1/mutation-audit"
            )
            assert audit_resp.status == 200
            audit_data = await audit_resp.json()
            assert audit_data["object"] == "research.mutation_audit"
            assert audit_data["data"]["iteration"] == 1
            assert "changed_paths" in audit_data["data"]
            assert "diffs" in audit_data["data"]

    @pytest.mark.asyncio
    async def test_iterations_404_for_missing_run(self):
        adapter = _make_adapter()
        app = adapter._build_app()

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/api/research/runs/nonexistent/iterations")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_mutation_audit_invalid_iteration(self):
        adapter = _make_adapter()
        app = adapter._build_app()

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get(
                "/api/research/runs/fake/iterations/abc/mutation-audit"
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_research_routes_require_auth_when_key_is_set(self):
        adapter = _make_adapter(api_key="sk-secret")
        app = adapter._build_app()

        async with TestClient(TestServer(app)) as cli:
            unauthorized = await cli.get("/api/research/runs")
            assert unauthorized.status == 401

            authorized = await cli.get(
                "/api/research/runs",
                headers={"Authorization": "Bearer sk-secret"},
            )
            assert authorized.status == 200
