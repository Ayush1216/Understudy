"""The operator console: run timeline, live frame, take control, approve / resume / abort.
Embedded in the run's own event loop; never launches a browser."""

from .app import build_app, handle_input, serve
from .interventions import ConsoleEscalator, InterventionStore

__all__ = ["ConsoleEscalator", "InterventionStore", "build_app", "handle_input", "serve"]
