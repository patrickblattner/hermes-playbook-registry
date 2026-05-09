# Examples

Beispiel-Compose-Dateien für Agent-seitiges Setup. Beide Stacks (Registry
und Agents) laufen typischerweise auf demselben Docker-Host als getrennte
Compose-Konfigurationen, verbunden über das gemeinsame Bridge-Network
`hermes-net`.

## Architektur

```
        ┌─────────────────── Docker host ───────────────────┐
        │                                                   │
        │   Registry-Stack (deploy.sh)                       │
        │   ┌────────────────────────────────────────┐     │
        │   │ playbook-registry        :8000  (REST) │     │
        │   │ playbook-registry-mcp    :8001  (MCP)  │     │
        │   └─────────────┬──────────────────────────┘     │
        │                 │                                 │
        │           hermes-net (bridge, kein Port-Mapping)  │
        │                 │                                 │
        │   Agent-Stack (deine Compose)                     │
        │   ┌─────────────┴──────────────────────────┐     │
        │   │ hermes-agent-1  → as_agent="hermes"    │     │
        │   │ hermes-agent-2  → as_agent="hermine"   │     │
        │   └────────────────────────────────────────┘     │
        │                                                   │
        └───────────────────────────────────────────────────┘

  Von außen: kein Zugriff auf Registry / MCP / Agents.
  Innerhalb: Service-Namen sind via embedded DNS auflösbar.
  Identität pro Tool-Call: as_agent-Parameter; Trust kommt durchs Network.
```

## Inbetriebnahme — Reihenfolge

```bash
# 1. Registry-Stack hochziehen (legt hermes-net an, startet REST + MCPs)
curl -fsSL https://raw.githubusercontent.com/patrickblattner/hermes-playbook-registry/main/deploy.sh | bash

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
| URL | `http://playbook-registry:8000` | `http://playbook-registry-mcp:8001/mcp` |
| `author_agent` / `validator_agent` | Agent setzt im JSON-Body | Tool-Parameter `as_agent` |
| Tool-Schemas / Auto-Complete | manuell, OpenAPI | automatisch über MCP-Discovery |
| Use-Case | Cron-Jobs, Skripte, Debugging | Agent-zu-Agent (Hermes/Hermine) |

Beide Varianten gleichzeitig zu nutzen ist OK — das Datenmodell ist atomar
abgesichert (Idempotency-Keys, atomare UPDATEs). Trust kommt in beiden Fällen
über die Network-Isolation: niemand außerhalb von `hermes-net` erreicht
Registry oder MCP.

## Mehrere Agents

Alle Agenten teilen sich denselben MCP-Container. Pro Tool-Call setzen sie
`as_agent` auf ihre eigene Identität (z.B. "hermes", "hermine"). Im
Audit-Log (`author_agent` / `validator_agent` der Registry) ist sichtbar,
welcher Agent was geschrieben hat.

Der Auto-Promote-Schwellenwert `external_success_count ≥ 2` skaliert
natürlich mit der Anzahl Agents — siehe SPEC.md "Lifecycle und Bewertung".
