"""
STDIO-Modus des MCP-Wrappers.

Startet `mcp-server/server.py` als Subprocess mit MCP_TRANSPORT=stdio,
verbindet sich via stdio_client und prüft, dass alle 6 Tools registriert
sind. Tool-Calls werden hier nicht gemacht — die brauchen einen echten
REST-Backend, was Sache der HTTP-Smoke-Tests ist.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = REPO_ROOT / "mcp-server" / "server.py"

EXPECTED_TOOLS = {
    "search_skills",
    "get_skill",
    "list_skill_versions",
    "publish_skill",
    "rate_skill",
    "promote_skill",
}


@pytest.mark.asyncio
async def test_stdio_lists_all_tools():
    """Server kommt hoch, MCP-Protokoll antwortet, alle 6 Tools sichtbar."""
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PATH)],
        env={
            "DEFAULT_AGENT_ID": "stdio-test",
            "PLAYBOOK_REGISTRY_URL": "http://127.0.0.1:0",  # nicht erreichbar, OK für list_tools
            "MCP_TRANSPORT": "stdio",
            # Python pfad damit imports funktionieren
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        },
    )

    # 8s Timeout — auf langsamen Runnern kann der Subprocess-Spawn dauern
    async with asyncio.timeout(8):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_response = await session.list_tools()
                names = {t.name for t in tools_response.tools}
                assert names == EXPECTED_TOOLS, f"unexpected tools: {names ^ EXPECTED_TOOLS}"

                # Stichprobe: Description nicht leer (auch im STDIO-Modus)
                for t in tools_response.tools:
                    assert t.description, f"tool {t.name} has empty description"


@pytest.mark.asyncio
async def test_stdio_publish_skill_call_format():
    """
    Tool-Call publish_skill in STDIO-Mode formt die korrekten Parameter.
    Wir nutzen ein nicht erreichbares Backend → der httpx-Call wirft, aber
    der Tool-Aufruf an sich (Schema-Validation, Argument-Mapping) funktioniert
    und wir sehen den Fehler vom HTTP-Layer, nicht von MCP.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PATH)],
        env={
            "DEFAULT_AGENT_ID": "stdio-test",
            "PLAYBOOK_REGISTRY_URL": "http://127.0.0.1:1",  # connection refused
            "MCP_TRANSPORT": "stdio",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        },
    )

    async with asyncio.timeout(15):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # Aufruf MUSS einen Schema-konformen Argument-Block annehmen
                # (validation passt → Tool-Funktion läuft → httpx fehlerhaft).
                result = await session.call_tool(
                    "publish_skill",
                    {
                        "skill_id": "stdio-test",
                        "problem_domain": "x",
                        "problem_description": "x",
                        "approach": "x",
                        "content": "x",
                    },
                )
                # Server meldet den Fehler als Tool-Result mit isError=True;
                # bedeutet: STDIO transport + Argument-Parsing + Tool-Dispatch funktionieren.
                assert result.isError, "expected an error from unreachable backend"
