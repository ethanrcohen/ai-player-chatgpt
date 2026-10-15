# Research: an agent that extends the existing factory

Research date: 2026-10-11. This is a design recommendation based on primary project documentation and inspection of our code, not a claim that the external projects have been tested on this save.

The target behavior is: given “build a small engine line for blue science near me,” the agent discovers the existing production and transport network, chooses an integration point, builds from available materials, and verifies sustained delivery. A hand-stocked assembler producing one engine does not pass.

## Relevant projects

- [Factorio Learning Environment (FLE)](https://github.com/JackHopkins/factorio-learning-environment): a programmable environment and evaluation framework. Its agent writes Python against game tools and receives execution feedback. It is not a pretrained Factorio brain. Its current README describes a Docker-based setup; replacing our live-game bridge with it would be an integration project.
- [FLE 0.3.0](https://jackhopkins.github.io/factorio-learning-environment/versions/0.3.0.html): documents persistent conversation with summarization, production statistics, and task verification. Its discussion explicitly identifies manual-crafting shortcuts as an evaluation problem. This is particularly relevant to our misleading “first engine” milestone.
- [FLE connection tool](https://raw.githubusercontent.com/JackHopkins/factorio-learning-environment/main/fle/env/tools/agent/connect_entities/agent.md): a useful reference for reusable logistics operations. We should distinguish choosing a source/destination from calculating every belt placement.
- [factorio-planning-agent](https://github.com/chebykinn/factorio-planning-agent): documents area scans, belt tracing, lane analysis, layout checks, persistent plans, and refusal of plans based on stale observations. It places ghosts rather than acting as a full physical builder. Its world-inspection approach is especially relevant to extending a human's existing factory.
- [Factorio Live Agent (flma)](https://github.com/jhjaggars/flma): exposes live production, research, inventory, and building data, with recipe planning based on an export from the running game. Useful reference for calculating requirements against actual technologies and recipes. Its example flags belt estimates as approximate; production arithmetic is not a spatial layout solution.
- [Voyager](https://voyager.minedojo.org/): Minecraft research using reusable code skills, execution feedback, and iterative verification. Borrow the reusable-skill idea; Minecraft results do not establish Factorio reliability.

## Findings in our fork

1. `bridge/agent.py` makes one completion call per request and expects an action array. The model cannot ask inspection questions and use the answers before choosing its actions.
2. `bridge/factorio/mcp_server.py`, `bridge/factorio/rcon.py`, and `mod/scripts/queries.lua` already expose inspection, recipes, placement checks, and character state. Those capabilities exist for external callers but are not connected to the autonomous decision loop.
3. `mod/scripts/perception.lua` caps local detail at 25 nearest entities. Belts can fill that sample and hide the relevant machines. Larger factories receive summaries and attention lists rather than full machine detail.
4. The shared entity description floors coordinates and omits belt-lane contents, belt connectivity, and inserter pickup/drop positions. `inspect_entity` adds direction, but still lacks a sufficient transport model.
5. The recent eight-result history is process-local. It is not a durable plan or a record of which entities the agent owns. Goal strings and accumulated notes can contain obsolete instructions.
6. The action parser can drop one invalid entry and execute the remainder of a dependent batch. That is unsuitable for a planned sequence where a later operation requires the earlier one.

## Recommended implementation sequence

### 1. Let the agent investigate

Add a bounded observe–reason–act loop to `Agent.decide`. Reuse the existing query gateway first. The agent can request several read-only tool calls, see results, and only then submit actions. Keep the transport/provider separate from this loop. Structured JSON query dispatch is an option; native model tools are another. Neither requires changing the game's goal into a hardcoded recipe.

Add generic queries:

- `scan_area`: paginated/filterable entities with exact coordinates, surface, stable IDs, direction, recipes, inventories, status, and observation tick.
- `trace_belt`: upstream/downstream connections, underground pairs, splitters and filters, lane contents, and uncertainty where flow is ambiguous.
- `inspect_inserter`: actual pickup/drop positions, filters, held item, and power/fuel state.
- `get_occupancy` and `get_power`: buildable space, reserved paths, and connections to the existing grid.
- `measure_flow`: scoped observations over game ticks, distinguishing a buffered stockpile from sustained supply.

First milestone: from the unchanged high-level request, the agent produces an evidence-backed explanation of where blue science is, what it lacks, and which existing sources could supply it. No construction until it can do this reliably.

### 2. Give plans a durable structure

Persist a task record keyed by save, surface, and task ID: objective, constraints, discovered sources and consumers, evidence ticks, candidate layout, material budget, next step, completed steps, and created entity IDs. Treat world observations as facts that can expire, rather than permanent notes. Revalidate before acting when the player changes a relevant area.

Have a plan describe connections: source belt/lane → ingredient producer → engine assembler → existing science input. Identify material shortages and effects on existing consumers. Let the agent select the topology; deterministic helpers calculate collisions, routes, machine rates, and material counts.

### 3. Execute with observable outcomes

Provide generic operations such as previewing a belt route, connecting power, placing an assembler with inserters, and checking a connection. Preview returns a bill of materials and conflicts. Apply checks current state and uses actual inventory.

Use precise entity IDs for recipe/inventory operations; avoid nearest-entity ambiguity. Return structured outcomes and refreshed state. Stop dependent steps on failure. Retrying a successful operation should recognize the existing entity rather than duplicate it. Track ownership to clean up only the agent's own failed work.

### 4. Test the actual objective

Use a copy of the save before further live experiments. Clear the manually supplied coordinates and construction instructions from the evaluation's task memory. Give only the original request and generic tool documentation.

Proposed acceptance criteria:

- Discovers source and destination from the live world without coordinate coaching.
- Connects to existing supply and delivers engines to existing blue-science production.
- Sustains the agreed engine rate across multiple observation windows without the agent hand-feeding ingredients. Account for initial buffers and downstream demand so stockpiles or blocked consumers do not create false conclusions.
- Preserves unrelated machines and their recipes; no duplicated placements on retry.
- Survives bridge restart and replans after a belt is moved or blocked.
- Repeats on a changed layout and a different product, demonstrating reusable capability.

Compare the current router with the upgraded loop on the same copied saves, recording interventions, successful deliveries, errors, construction cost, and model usage. Stop repetitive failures instead of spending calls indefinitely.

Recommendation: retain our working mod/RCON connection, replace the action-only router, and borrow tested interface ideas from these projects. Consider a full FLE migration only after a compatibility spike proves it can preserve our live-save, cooperative-game, and authentication requirements. More prompt text or a stronger model alone will not supply missing world information.
