"""clawproxy bridge plugin for Hermes Agent."""
try:
    from .adapter import register
except ImportError:  # imported outside a package (pytest's collector); Hermes always loads it as one
    register = None  # type: ignore[assignment]

__all__ = ["register"]
