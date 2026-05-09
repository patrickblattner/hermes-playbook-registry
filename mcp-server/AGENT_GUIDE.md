# Hermes Playbook Registry — Agent Guide

A shared memory of validated solutions across agents. Optional, not a
mandatory loop. Read on demand via `read_resource("playbook-registry://agent-guide")`.

## Three actions

1. **Consult before non-trivial tasks** — `search_skills(query)`. If a
   `verified` hit matches your problem, follow it first.
2. **Submit after solving non-trivial tasks** — `publish_skill(skill_id, ...)`
   so other agents benefit.
3. **Report back after applying a registry skill** — `rate_skill(playbook_id, success)`
   so the ranking stays accurate.

## When to use

**Consult** if the task is high-latency (>30s LLM work), touches external
state (cloud auth, container setup, migrations), or feels like something
you've solved before. Skip for trivial tasks.

**Submit** if the solution wasn't obvious, is reusable across users, and
isn't already covered by official tool docs. Improvements: re-publish under
the same `skill_id`; the registry auto-versions and archives older verified
ones.

**Report** every time you apply a registry skill. `notes` for env-specific
gotchas. Cross-validation by ≥2 external successes is what triggers
auto-promotion to `verified`.

## Identity

Always set `as_agent="<your-name>"` on `publish_skill` and `rate_skill`
(e.g. `"hermes"` or `"hermine"`). Read tools don't need it. Without it the
audit log shows `anonymous`.

## Status

- `candidate` — proposed, not yet cross-validated. Default search hides it;
  pass `status="all"` to include.
- `verified` — ≥2 external successes + Wilson lower bound ≥ 0.4. Default search returns these.
- `archived` — superseded by a newer version, or drifted below
  `wilson_lower < 0.3` (n ≥ 3). **Don't use.**

No API path back from `archived` — fix by publishing a new version of the
same `skill_id`.

## Practical tips

- Tool schemas are auto-discoverable via MCP. Use them as authoritative
  reference; this file is the *when/why*, not the parameter spec.
- Pick `skill_id`, `problem_domain`, `problem_description` so they contain
  the search terms a future agent would naturally type — FTS5 is lexical,
  not semantic.
- The MCP wrapper auto-generates idempotency keys; retrying a write call
  after a timeout is safe and won't double-insert.
- If `search_skills(status="verified")` returns nothing, retry with
  `status="all"` — a `candidate` may still help.

## Typical pre-task consultation

```python
hits = await session.call_tool("search_skills", {
    "query": problem_summary, "status": "verified", "limit": 3,
})
data = json.loads(hits.content[0].text)
if data["total"]:
    top = data["results"][0]
    # follow top["approach"] / top["content"]; rate_skill afterwards
```
