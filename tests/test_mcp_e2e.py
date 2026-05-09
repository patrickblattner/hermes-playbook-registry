"""
End-to-end MCP-Tests gegen einen echten REST-Backend.

Spawnt:
  - uvicorn-Subprocess (main:app) auf zufälligem Port mit fresh DB
  - MCP-Wrapper als Subprocess (STDIO oder streamable-http)
Verbindet sich via MCP-Client und exerziert alle 6 Tools.

Auch: ein MCP-Wrapper, beide Agenten via `as_agent`-Parameter im
Cross-Agent-Auto-Promote-Roundtrip.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = REPO_ROOT / "mcp-server" / "server.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def rest_backend(tmp_path):
    """Uvicorn auf 127.0.0.1:<random> mit fresh DB. Liefert die Base-URL."""
    port = _free_port()
    db_path = tmp_path / "e2e.db"

    env = {
        **os.environ,
        "PLAYBOOK_DB_PATH": str(db_path),
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "main:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/health", timeout=0.5)
            if r.status_code == 200:
                break
        except httpx.RequestError:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise RuntimeError(f"REST backend did not become healthy. stderr:\n{stderr}")

    yield base

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _stdio_params(rest_url: str, default_agent: str = "test-agent") -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PATH)],
        env={
            "DEFAULT_AGENT_ID": default_agent,
            "PLAYBOOK_REGISTRY_URL": rest_url,
            "MCP_TRANSPORT": "stdio",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        },
    )


def _payload(result) -> dict:
    """MCP-CallToolResult → JSON-Dict aus dem ersten Text-Content."""
    return json.loads(result.content[0].text)


def _list_payload(result) -> list:
    """List-Return: FastMCP packt jedes Listen-Item in eigenen TextContent."""
    return [json.loads(c.text) for c in result.content]


# ----- Single-Agent Lifecycle via STDIO -------------------------------------

@pytest.mark.asyncio
async def test_stdio_agent_guide_resource(rest_backend):
    """
    Der MCP-Wrapper exponiert AGENT_GUIDE.md als Resource. Agents können sie
    über read_resource("playbook-registry://agent-guide") on-demand lesen,
    ohne dass der Operator sie ihnen mitgeben muss.
    """
    async with asyncio.timeout(15):
        async with stdio_client(_stdio_params(rest_backend)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                resources = await session.list_resources()
                uris = {str(r.uri) for r in resources.resources}
                assert "playbook-registry://agent-guide" in uris

                guide = await session.read_resource("playbook-registry://agent-guide")
                content = guide.contents[0]
                assert content.mimeType == "text/markdown"
                assert "Hermes Playbook Registry" in content.text
                # Stichprobe der drei Aktionen
                assert "search_skills" in content.text
                assert "publish_skill" in content.text
                assert "rate_skill" in content.text


@pytest.mark.asyncio
async def test_stdio_full_lifecycle_all_six_tools(rest_backend):
    """
    Eine komplette User-Story über STDIO: publish → list_versions → search →
    get → rate → promote. Verifiziert dass alle 6 Tools funktionsfähig sind
    und korrekt mit dem REST-Backend reden. DEFAULT_AGENT_ID greift, weil
    `as_agent` in den Calls leer bleibt.
    """
    async with asyncio.timeout(30):
        async with stdio_client(_stdio_params(rest_backend, "agent-author")) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 1. Empty search initially
                r = await session.call_tool(
                    "search_skills",
                    {"query": "anything", "status": "all", "limit": 5},
                )
                data = _payload(r)
                assert data["total"] == 0

                # 2. Publish
                r = await session.call_tool(
                    "publish_skill",
                    {
                        "skill_id": "e2e-skill",
                        "problem_domain": "e2e-test",
                        "problem_description": "End-to-end smoke",
                        "approach": "stdio mcp",
                        "content": "## v1",
                        "metadata_json": '{"latency_ms": 1234}',
                    },
                )
                pub = _payload(r)
                assert pub["status"] == "candidate"
                assert pub["version"] == 1
                pid = pub["id"]

                # 3. Search now finds it (status=all because still candidate)
                r = await session.call_tool(
                    "search_skills",
                    {"query": "smoke", "status": "all"},
                )
                data = _payload(r)
                assert data["total"] == 1
                assert data["results"][0]["author_agent"] == "agent-author"
                assert data["results"][0]["metadata"]["latency_ms"] == 1234

                # 4. get_skill returns full record + empty validations
                r = await session.call_tool("get_skill", {"playbook_id": pid})
                full = _payload(r)
                assert full["validation_count"] == 0
                assert full["validations"] == []

                # 5. rate_skill — author rates own skill (zählt nicht für promote)
                r = await session.call_tool(
                    "rate_skill",
                    {"playbook_id": pid, "success": True, "latency_ms": 999},
                )
                rate = _payload(r)
                assert rate["recorded"] is True

                # 6. v2 publishen → list_skill_versions zeigt zwei Einträge
                r = await session.call_tool(
                    "publish_skill",
                    {
                        "skill_id": "e2e-skill",
                        "problem_domain": "e2e-test",
                        "problem_description": "End-to-end smoke",
                        "approach": "stdio mcp v2",
                        "content": "## v2",
                    },
                )
                pub2 = _payload(r)
                assert pub2["version"] == 2

                r = await session.call_tool(
                    "list_skill_versions", {"skill_id": "e2e-skill"}
                )
                versions = _list_payload(r)
                assert [v["version"] for v in versions] == [1, 2]

                # 7. promote_skill — manuell (Cross-Validation hat noch nicht
                #    getriggert weil nur self-validations da sind)
                r = await session.call_tool("promote_skill", {"playbook_id": pid})
                pro = _payload(r)
                assert pro["status"] == "verified"
                assert pro["promoted_at"] is not None


# ----- Cross-Agent Auto-Promote via STDIO -----------------------------------

@pytest.mark.asyncio
async def test_stdio_cross_agent_auto_promote(rest_backend):
    """
    Cross-Agent-Roundtrip über EINEN gemeinsamen MCP-Wrapper. Identität
    kommt jeweils aus dem `as_agent`-Tool-Parameter. Hermes published,
    Hermine validates 2× → Auto-Promote feuert serverseitig.

    Zeigt: external_success_count zählt validator_agent != author_agent
    korrekt, auch wenn beide Agenten am selben Wrapper hängen.
    """
    async with asyncio.timeout(30):
        async with stdio_client(_stdio_params(rest_backend)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Hermes published
                pub = await session.call_tool(
                    "publish_skill",
                    {
                        "skill_id": "cross-agent-promote",
                        "problem_domain": "x",
                        "problem_description": "x",
                        "approach": "x",
                        "content": "x",
                        "as_agent": "hermes",
                    },
                )
                pid = _payload(pub)["id"]

                # Hermes self-validates (zählt nicht für external_success)
                await session.call_tool(
                    "rate_skill",
                    {"playbook_id": pid, "success": True, "as_agent": "hermes"},
                )

                # Hermine validates 2× → external_success=2 → Auto-Promote
                await session.call_tool(
                    "rate_skill",
                    {"playbook_id": pid, "success": True, "as_agent": "hermine"},
                )
                await session.call_tool(
                    "rate_skill",
                    {"playbook_id": pid, "success": True, "as_agent": "hermine"},
                )

                # State prüfen
                r = await session.call_tool("get_skill", {"playbook_id": pid})
                full = _payload(r)
                assert full["status"] == "verified"
                assert full["author_agent"] == "hermes"
                assert full["external_success_count"] == 2
                assert full["promoted_at"] is not None


# ----- HTTP-Transport ohne Roundtrip — list_tools + smoke-Call --------------

@asynccontextmanager
async def _http_wrapper(rest_url: str, default_agent: str = "test-default"):
    """Spawnt MCP-Wrapper im streamable-http-Modus auf zufälligem Port."""
    mcp_port = _free_port()
    env = {
        **os.environ,
        "DEFAULT_AGENT_ID": default_agent,
        "PLAYBOOK_REGISTRY_URL": rest_url,
        "MCP_TRANSPORT": "http",
        "MCP_PORT": str(mcp_port),
        "PYTHONPATH": str(REPO_ROOT / "mcp-server"),
    }
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_PATH)],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Warte bis der Port hört
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", mcp_port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise RuntimeError(f"MCP HTTP wrapper did not start. stderr:\n{stderr}")

    try:
        yield f"http://127.0.0.1:{mcp_port}/mcp"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.asyncio
async def test_http_transport_full_roundtrip(rest_backend):
    """
    streamable-http Transport: alle 6 Tools sichtbar, eine publish + search
    Roundtrip durchspielen. Bestätigt dass die HTTP-Variante (für
    Sidecar-Container im hermes-net) funktional ist.
    """
    expected_tools = {
        "search_skills",
        "get_skill",
        "list_skill_versions",
        "publish_skill",
        "rate_skill",
        "promote_skill",
    }

    async with asyncio.timeout(45):
        async with _http_wrapper(rest_backend, "http-agent") as mcp_url:
            async with streamablehttp_client(mcp_url) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()

                    tools = await session.list_tools()
                    assert {t.name for t in tools.tools} == expected_tools

                    # Publish via HTTP
                    pub = await session.call_tool(
                        "publish_skill",
                        {
                            "skill_id": "http-e2e",
                            "problem_domain": "x",
                            "problem_description": "via http transport",
                            "approach": "x",
                            "content": "x",
                        },
                    )
                    pid = _payload(pub)["id"]
                    assert _payload(pub)["status"] == "candidate"

                    # Search findet ihn (status=all)
                    s = await session.call_tool(
                        "search_skills",
                        {"query": "http", "status": "all"},
                    )
                    data = _payload(s)
                    assert data["total"] == 1
                    assert data["results"][0]["id"] == pid
                    # DEFAULT_AGENT_ID greift, da der Client kein as_agent setzte
                    assert data["results"][0]["author_agent"] == "http-agent"
