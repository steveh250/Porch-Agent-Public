"""Location-triggered porch light agent.

Two layers, kept apart on purpose:

* The deterministic layer -- OwnTracks parsing, distance, the presence state
  machine, clamping, the timeout and the fallback -- never calls an LLM.
* The decision agent (``agent.py``) chooses a setting and applies it through
  the porch-agent MCP server. Everything it sends passes through the
  deterministic clamps first.
"""

__version__ = "0.1.0"
