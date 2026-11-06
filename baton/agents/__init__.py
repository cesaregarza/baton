"""Agents compose permissions, mailbox access, thread access and optional secretary work."""

from .agent import Agent
from .permissions import Capability, Permissions

__all__ = ["Agent", "Capability", "Permissions"]
