"""
MCP-Wrapper für die Hermes Playbook Registry.

Dünner Adapter: jeder MCP-Tool-Call wird auf einen REST-Call gegen den
playbook-registry-Service gemappt. author_agent / validator_agent werden
serverseitig aus der ENV AGENT_ID befüllt — der Client kann sich nicht
selbst eine andere Identität geben.

Transports:
  MCP_TRANSPORT=stdio  (Default, für In-Process bei einem Claude-Agent)
  MCP_TRANSPORT=http   (Container-Modus, streamable-http auf MCP_PORT)

Konfiguration via ENV:
  PLAYBOOK_REGISTRY_URL  http://playbook-registry:8000
  AGENT_ID               z.B. "hermes" — Pflicht, in Writes eingesetzt
  MCP_TRANSPORT          stdio | http   (Default: stdio)
  MCP_PORT               8001 (nur bei http)
"""

import json
import logging
import os
import uuid

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mcp-wrapper")

REGISTRY_URL = os.environ.get("PLAYBOOK_REGISTRY_URL", "http://playbook-registry:8000")
AGENT_ID = os.environ.get("AGENT_ID")
if not AGENT_ID:
    raise RuntimeError(
        "AGENT_ID muss gesetzt sein — wird als author_agent / validator_agent "
        "an die Registry weitergegeben."
    )

logger.info(f"MCP-Wrapper startet: AGENT_ID={AGENT_ID} REGISTRY_URL={REGISTRY_URL}")

mcp = FastMCP("hermes-playbook-registry")
client = httpx.AsyncClient(base_url=REGISTRY_URL, timeout=10.0)


def _new_idempotency_key() -> str:
    return str(uuid.uuid4())


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


# ---------- Write tools (AGENT_ID server-side) ----------

# Sentinel-Defaults statt T|None — FastMCP 1.13 hat einen issubclass()-Bug bei
# Union-Type-Annotations. "" und negative Zahlen werden auf None gemappt, bevor
# der REST-Call rausgeht. metadata kommt als JSON-String rein und wird geparst.

@mcp.tool()
async def publish_skill(
    skill_id: str,
    problem_domain: str,
    problem_description: str,
    approach: str,
    content: str,
    metadata_json: str = "",
) -> dict:
    """Publish a new playbook (or a new version of an existing skill_id) as 'candidate'.

    author_agent is filled server-side from AGENT_ID, so the client cannot
    impersonate another agent. metadata_json is an optional JSON object as
    string (e.g. '{"latency_ms": 3200, "tags": ["gcp"]}').
    """
    metadata = None
    if metadata_json.strip():
        try:
            metadata = json.loads(metadata_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"metadata_json is not valid JSON: {e}")

    body = {
        "skill_id": skill_id,
        "problem_domain": problem_domain,
        "problem_description": problem_description,
        "approach": approach,
        "content": content,
        "author_agent": AGENT_ID,
        "metadata": metadata,
        "idempotency_key": _new_idempotency_key(),
    }
    r = await client.post("/playbooks/candidate", json=body)
    r.raise_for_status()
    logger.info(f"published skill_id={skill_id} as {AGENT_ID}")
    return r.json()


@mcp.tool()
async def rate_skill(
    playbook_id: int,
    success: bool,
    latency_ms: int = -1,
    model_used: str = "",
    notes: str = "",
) -> dict:
    """Report a validation result for a playbook (success or failure).

    validator_agent is filled server-side from AGENT_ID — clients cannot fake
    another agent's signature. Cross-validation by ≥2 external successes feeds
    Auto-Promote on the registry side.

    Sentinel-Werte: latency_ms=-1 oder leerer String werden als "nicht
    gesetzt" interpretiert und gehen als null an die Registry.
    """
    body = {
        "validator_agent": AGENT_ID,
        "success": success,
        "latency_ms": latency_ms if latency_ms >= 0 else None,
        "model_used": model_used or None,
        "notes": notes or None,
        "idempotency_key": _new_idempotency_key(),
    }
    r = await client.post(f"/playbooks/{playbook_id}/validate", json=body)
    r.raise_for_status()
    logger.info(f"rated id={playbook_id} success={success} as {AGENT_ID}")
    return r.json()


@mcp.tool()
async def promote_skill(playbook_id: int) -> dict:
    """Manually escalate a candidate to verified.

    Most promotions happen automatically via Cross-Validation on the registry
    side; this is the explicit override for operational urgency.
    """
    r = await client.post(f"/playbooks/{playbook_id}/promote")
    r.raise_for_status()
    logger.info(f"manually promoted id={playbook_id} as {AGENT_ID}")
    return r.json()


# ---------- Entrypoint ----------

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
    elif transport == "http":
        # streamable-http transport: standard MCP over HTTP, suitable for
        # sidecar containers in a docker-compose network.
        port = int(os.environ.get("MCP_PORT", "8001"))
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        raise RuntimeError(f"unknown MCP_TRANSPORT={transport!r} (expected stdio|http)")
