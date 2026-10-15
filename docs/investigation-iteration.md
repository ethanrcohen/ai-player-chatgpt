# Investigation loop, iteration 1 — 2026-10-14

Implemented a provider-independent JSON query loop with a shared time budget, four inspection rounds, up to three calls per round, typed allowlisted arguments, and bounded outputs. Query evidence is included in the next model call. Invalid mixed requests dispatch nothing; invalid action batches are rejected as a whole.

The bridge now starts in observation-only mode by default (`AI_OBSERVATION_ONLY=1`). This permits only chat, summary, and wait outputs; no construction, transfers, movement, or skills. Set `AI_OBSERVATION_ONLY=0` only when deliberately testing execution. This setting is read at bridge startup.

Mod 0.5.2 adds `scan_area` with recipe/type/name filters and pagination. It returns observation ticks, exact positions, orientation, and inserter pickup/drop positions. Existing inspect_entity now preserves exact positions. This is discovery, not belt tracing: lane contents, network connectivity, flow measurement, and durable plans remain future iterations.

Validation: 29 tests passed, including query feedback, bounded rounds and deadlines, malformed query rejection, observation enforcement, entire-batch rejection, and Lua scan filtering/pagination. All Lua sources compile in the local Lua runtime. Lua tests use a minimal engine fixture, not a running Factorio engine.

Live check: RCON connected to the loaded save; the running mod exposes the older query set. Model investigation and a separate minimal sign-in test both failed with HTTP 520. No model construction commands were submitted. Version 0.5.2 archive has been copied into the Factorio mods directory; a game restart is required to load it.

Next test: save and restart Factorio, reload this factory, confirm scan_area is registered, then run an observation-only model investigation from the original high-level goal. Do not reuse the earlier manually coached engine-layout notes as evaluation evidence. Require actual source/consumer observations and explicit unknowns. Do not call a standalone chest-fed assembler an integrated line.
