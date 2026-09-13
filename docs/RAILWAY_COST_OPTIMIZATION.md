# Railway cost optimization

The Football Railway service intentionally keeps one continuously available web process and one scheduler process, but only the scheduler owns the full historical model snapshot.

The web child uses `sports_ai.dashboard.low_memory_app:app`. It serves persisted fixtures, predictions, decisions, paper-trading state, performance outputs and runtime state exactly as before, while reconstructing only a tiny snapshot metadata view from the persisted prediction ledger. The web child does not replay GAP history, refit conversion/corner models, load Route A artifacts for inference, call providers, or generate predictions.

The scheduler child remains `sports_ai.dashboard.scheduler_worker` and is unchanged. It continues to build the full model, run the configured fixture/market cadence, generate predictions, persist decisions, settle paper positions and maintain provider/runtime state.

This optimization is performance-neutral by design: model inputs, parameters, thresholds, provider cadence, xG, Dixon-Coles pricing, context vetoes, portfolio selection and settlement are unchanged. The change removes duplicate in-memory model state only.
