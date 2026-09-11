"""Playwright implementation of the Surface seam."""

from .perception import RefEntry, RefRegistry, perceive
from .resolver import Vocab
from .session import BrowserSession
from .surface import WebSurface

__all__ = ["BrowserSession", "RefEntry", "RefRegistry", "Vocab", "WebSurface", "perceive"]
