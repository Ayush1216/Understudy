"""The LLM-driven discovery loop and the recorder that compiles a run into a capability. The
only package allowed to import an LLM SDK; replay never imports it."""

from .client import (
    ConfigurationError,
    ModelClient,
    ModelTurn,
    OpenAICompatClient,
    ScriptedModelClient,
    ToolCall,
    load_env_file,
)
from .loop import DiscoveryOptions, DiscoveryResult, DiscoveryRun
from .prompt import build_system, render_observation
from .recorder import Finish, TraceEntry, compile, derive_name
from .tools import TOOLS

__all__ = [n for n in dir() if not n.startswith("_")]
