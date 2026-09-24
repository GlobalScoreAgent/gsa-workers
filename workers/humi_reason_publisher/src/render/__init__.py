"""Render Stage 2: reasons bilingües + pillar_summary (paridad con SQL index_humi)."""

from __future__ import annotations

from .history import (
    render_history_pillar_summary,
    render_history_reasons,
    render_pillar_history,
)
from .information import (
    render_information_pillar_summary,
    render_information_reasons,
    render_pillar_information,
)
from .measure import render_measure_pillar_summary, render_measure_reasons
from .usage import render_usage_pillar_summary, render_usage_reasons

# Alias usado por assemble.py / specs anteriores.
render_measures_reasons = render_measure_reasons

__all__ = [
    "render_pillar_history",
    "render_history_reasons",
    "render_history_pillar_summary",
    "render_pillar_information",
    "render_information_reasons",
    "render_information_pillar_summary",
    "render_measure_reasons",
    "render_measures_reasons",
    "render_measure_pillar_summary",
    "render_usage_reasons",
    "render_usage_pillar_summary",
]
