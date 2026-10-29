"""Messaging layer for the FinBot platform"""

from .events import EventBus, event_bus

__all__ = ["event_bus", "EventBus"]
