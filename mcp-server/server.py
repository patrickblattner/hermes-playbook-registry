"""
MCP-Wrapper für die Hermes Playbook Registry.

Ein einziger Container, der von beliebig vielen Agenten im selben
Docker-Network genutzt wird. Agent-Identität wird beim Tool-Call übergeben
(`as_agent`-Parameter); fehlt der, fällt der Server auf die ENV
DEFAULT_AGENT_ID zurück. Trust kommt aus der Netzwerk-Isolation — wer
ans MCP-Endpoint kommt, ist im hermes-net und damit autorisiert.

Transports:
  MCP_TRANSPORT=stdio  (Default, für In-Process bei einem Claude-Agent)
  MCP_TRANSPORT=http   (Container-Modus, streamable-http auf MCP_PORT)

Konfiguration via ENV:
  PLAYBOOK_REGISTRY_URL   http://playbook-registry:8000  (REST-Backend)
  DEFAULT_AGENT_ID        Default-Identity wenn `as_agent` leer ist
                          (sinnvoll für Single-Agent-Setups: ENV setzen,
                          Tools rufen ohne as_agent)
  MCP_TRANSPORT           stdio | http   (Default: stdio)
  MCP_PORT                8001 (nur bei http)
"""

import json
import logging
import os
import uuid
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

AGENT_GUIDE_PATH = Path(__file__).resolve().parent / "AGENT_GUIDE.md"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mcp-wrapper")

REGISTRY_URL = os.environ.get("PLAYBOOK_REGISTRY_URL", "http://playbook-registry:8000")
DEFAULT_AGENT_ID = os.environ.get("DEFAULT_AGENT_ID", "anonymous")

logger.info(
    f"MCP-Wrapper startet: REGISTRY_URL={REGISTRY_URL} "
    f"DEFAULT_AGENT_ID={DEFAULT_AGENT_ID}"
)

mcp = FastMCP("hermes-playbook-registry")
client = httpx.AsyncClient(base_url=REGISTRY_URL, timeout=10.0)


def _new_idempotency_key() -> str:
    return str(uuid.uuid4())


def _agent(as_agent: str) -> str:
    """Wähle Agent-ID: explicit Tool-Arg vor DEFAULT_AGENT_ID."""
    return as_agent.strip() or DEFAULT_AGENT_ID


# ---------- Resource: Agent Guide (when/why to use the registry) ----------

@mcp.resource(
    "playbook-registry://agent-guide",
    name="Hermes Playbook Registry — Agent Guide",
    description="When and how an agent should consult, submit to, and rate the registry.",
    mime_type="text/markdown",
)
def agent_guide() -> str:
    """Live-served from AGENT_GUIDE.md beside this server file."""
    return AGENT_GUIDE_PATH.read_text(encoding="utf-8")


# ---------- Read tools ----------

@mcp.tool()
async def search_skills(query: str, status: str = "verified", limit: int = 5) -> dict:
    """Search the registry for matching playbooks, ranked by relevance and confidence.

    FTS5 full-text on skill_id, problem_domain, problem_description, approach,
    and content. Ties broken by Wilson-Score-Lower-Bound (so 9/10 outranks 1/1).
    Default status='verified' returns only cross-validated skills; pass
    'candidate' or 'all' to widen the result set.
    """
    r = await client.get(
        "/playbooks/search",
        params={"q": query, "status": status, "limit": limit},
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def get_skill(playbook_id: int) -> dict:
    """Fetch one playbook by id, including its full validation history."""
    r = await client.get(f"/playbooks/{playbook_id}")
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def list_skill_versions(skill_id: str) -> list:
    """List all versions of a skill_id (oldest first).

    Useful for "wie hat Agent X das anders gelöst"-Vergleiche zwischen
    divergenten Lösungen für dasselbe Problem.
    """
    r = await client.get(f"/playbooks/by-skill/{skill_id}/versions")
    r.raise_for_status()
    return r.json()


# ---------- Write tools ----------
#
# Sentinel-Defaults statt T|None: FastMCP 1.13 hat einen issubclass()-Bug
# bei Union-Type-Annotations. "" und negative Zahlen werden auf None gemappt,
# bevor der REST-Call rausgeht. metadata kommt als JSON-String rein.

@mcp.tool()
async def publish_skill(
    skill_id: str,
    problem_domain: str,
    problem_description: str,
    approach: str,
    content: str,
    as_agent: str = "",
    metadata_json: str = "",
) -> dict:
    """Publish a new playbook (or a new version of an existing skill_id) as 'candidate'.

    as_agent identifies the publishing agent (z.B. "hermes" oder "hermine").
    Leer → DEFAULT_AGENT_ID-ENV des MCP-Servers; sinnvoll für Single-Agent-
    Setups. Bei mehreren Agenten gegen denselben MCP-Server: as_agent immer
    explizit setzen.

    metadata_json ist ein optionales JSON-Objekt als String, z.B.
    '{"latency_ms": 3200, "tags": ["gcp"]}'.
    """
    metadata = None
    if metadata_json.strip():
        try:
            metadata = json.loads(metadata_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"metadata_json is not valid JSON: {e}")

    agent_id = _agent(as_agent)
    body = {
        "skill_id": skill_id,
        "problem_domain": problem_domain,
        "problem_description": problem_description,
        "approach": approach,
        "content": content,
        "author_agent": agent_id,
        "metadata": metadata,
        "idempotency_key": _new_idempotency_key(),
    }
    r = await client.post("/playbooks/candidate", json=body)
    r.raise_for_status()
    logger.info(f"published skill_id={skill_id} as {agent_id}")
    return r.json()


@mcp.tool()
async def rate_skill(
    playbook_id: int,
    success: bool,
    as_agent: str = "",
    latency_ms: int = -1,
    model_used: str = "",
    notes: str = "",
) -> dict:
    """Report a validation result for a playbook (success or failure).

    as_agent identifies the validating agent. Leer → DEFAULT_AGENT_ID. Cross-
    validation (Successes von Agents != author_agent) feeds Auto-Promote auf
    der Registry-Seite.

    Sentinel-Werte: latency_ms=-1 oder leerer String werden als 'nicht
    gesetzt' interpretiert und gehen als null an die Registry.
    """
    agent_id = _agent(as_agent)
    body = {
        "validator_agent": agent_id,
        "success": success,
        "latency_ms": latency_ms if latency_ms >= 0 else None,
        "model_used": model_used or None,
        "notes": notes or None,
        "idempotency_key": _new_idempotency_key(),
    }
    r = await client.post(f"/playbooks/{playbook_id}/validate", json=body)
    r.raise_for_status()
    logger.info(f"rated id={playbook_id} success={success} as {agent_id}")
    return r.json()


@mcp.tool()
async def promote_skill(playbook_id: int) -> dict:
    """Manually escalate a candidate to verified.

    Most promotions happen automatically via Cross-Validation on the registry
    side; this is the explicit override for operational urgency.
    """
    r = await client.post(f"/playbooks/{playbook_id}/promote")
    r.raise_for_status()
    logger.info(f"manually promoted id={playbook_id}")
    return r.json()


# ---------- Entrypoint ----------

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
    elif transport == "http":
        port = int(os.environ.get("MCP_PORT", "8001"))
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        raise RuntimeError(f"unknown MCP_TRANSPORT={transport!r} (expected stdio|http)")
