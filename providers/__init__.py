"""Provider modules: one file per upstream service.

Each module exposes a `get_<name>_*` data-fetch function that the
`agent_quota` orchestrator calls (lazily) plus a `main()` entry point
for standalone debugging via `python -m providers.<name>`.
"""
