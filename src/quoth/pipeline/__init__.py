"""Non-serving, snapshot-bound query-time pipeline for the control lab.

The MCP server deliberately continues using its original search/evidence path.
"""
from .engine import run
from .registry import catalogue, registry_hash
from .spec import production, spec_hash, validate

__all__ = ["run", "catalogue", "registry_hash", "production", "spec_hash", "validate"]
