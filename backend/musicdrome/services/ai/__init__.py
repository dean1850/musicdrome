"""AI features: provider abstraction, playlist curation, listening analytics."""

from .provider import AIError, AIProvider, get_provider

__all__ = ["AIError", "AIProvider", "get_provider"]
