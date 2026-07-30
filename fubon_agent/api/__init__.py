"""Fubon order-agent API package.

Prefer:
  from fubon_agent.api.fubon_api import get_or_create_agent, OrderAgent
"""

from fubon_agent.api.fubon_api import OrderAgent, get_or_create_agent, get_order_agent

__all__ = ["OrderAgent", "get_or_create_agent", "get_order_agent"]
