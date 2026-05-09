# Examples

Beispiel-Compose-Dateien für Agent-seitiges Setup. Beide Stacks (Registry
und Agents) laufen typischerweise auf demselben Docker-Host als getrennte
Compose-Konfigurationen, verbunden über das gemeinsame Bridge-Network
`hermes-net`.

## Architektur

```
        ┌─────────────────── Docker host ───────────────────┐
        │                                                   │
        │   Registry-Stack (setup.sh)                       │
        │   ┌────────────────────────────────────────┐     │
        │   │ playbook-registry        :8000         │     │
        │   │ playbook-registry-mcp-hermes  :8001    │     │
        │   │ playbook-registry-mcp-hermine :8001    │     │
        │   └─────────────┬──────────────────────────┘     │
        │                 │                                 │
        │           hermes-net (bridge, kein Port-Mapping)  │
        │                 │                                 │
        │   Agent-Stack (deine Compose)                     │
        │   ┌─────────────┴──────────────────────────┐     │
        │   │ hermes-agent-1   AGENT_ID=hermes       │     │
        │   │ hermes-agent-2   AGENT_ID=hermine      │     │
        │   └────────────────────────────────────────┘     │
        │                                                   │
        └───────────────────────────────────────────────────┘

  Von außen: kein Zugriff auf Registry / MCP / Agents.
  Innerhalb: Service-Namen sind via embedded DNS auflösbar.
```

## Inbetriebnahme — Reihenfolge

```bash
# 1. Registry-Stack hochziehen (legt hermes-net an, startet REST + MCPs)
curl -fsSL https://raw.githubusercontent.com/patrickblattner/hermes-playbook-registry/main/setup.sh | bash

# 2. Agent-Stack starten (hängt sich ins existierende hermes-net)
cp hermes-agent-stack.yml ~/my-agents/docker-compose.yml
cd ~/my-agents/
# Image:-Zeile anpassen, dann:
docker compose up -d
```

`hermes-net` bleibt nach `docker compose down` bestehen — beide Stacks können
unabhängig hoch- und runtergefahren werden, das Network überlebt. Erst
`docker network rm hermes-net` zerstört die Verbindung.

## Wahl: REST direkt vs. MCP-Wrapper

| | REST direkt | MCP-Wrapper |
|--|--|--|
| URL | `http://playbook-registry:8000` | `http://playbook-registry-mcp-<agent>:8001/mcp` |
| `author_agent` / `validator_agent` | Agent setzt selbst im Body | MCP-Wrapper befüllt aus seiner ENV |
| Identity-Schutz | keiner — Client kann sich umbenennen | gegeben — Container-ENV ist die Wahrheit |
| Tool-Schemas / Auto-Complete | manuell, OpenAPI | automatisch über MCP-Discovery |
| Use-Case | Cron-Jobs, Skripte, Debugging | Agent-zu-Agent (Hermes/Hermine) |

Beide Varianten gleichzeitig zu nutzen ist OK — das Datenmodell ist atomar
abgesichert (Idempotency-Keys, atomare UPDATEs).

## Mehr Agents hinzufügen

Pro zusätzlichem Agent: einen weiteren MCP-Wrapper-Container im
Registry-Stack (eigene `AGENT_ID` und eigener `container_name`), und im
Agent-Stack auf `http://playbook-registry-mcp-<neuername>:8001/mcp` zeigen.

Der Auto-Promote-Schwellenwert `external_success_count ≥ 2` skaliert
natürlich mit der Anzahl Agents — siehe SPEC.md "Lifecycle und Bewertung".
