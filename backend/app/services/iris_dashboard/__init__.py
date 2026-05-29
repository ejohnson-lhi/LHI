"""Internal call-review dashboard for Iris (the AI receptionist).

Reads per-call artifacts produced by the agent worker (transcript JSON
files, per-participant OGG recordings, egress metadata JSONs) from the
shared `recordings/` directory and presents them through a small FastAPI
surface under /iris/ with a tiny SPA frontend.

Module layout:
    call_index.py       Filesystem -> call list / call detail (no LLM).
    call_cost.py        Deterministic per-call cost from metrics.
    call_categorize.py  Rule-based categorization from tool calls.
    call_summarizer.py  Claude-generated narrative summary, sidecar JSON.
    audio_merge.py      ffmpeg merge of per-participant OGGs into stereo.

The route layer lives in app/routes/iris_dashboard.py.
"""
