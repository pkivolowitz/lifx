"""Contrib sensors — local signal producers.

Each sensor reads data from a local source (hardware, filesystem,
network) and publishes signals into the GlowUp MQTT bus.  Sensors
are self-contained and deployable to any host in the fleet.
"""

__version__: str = "1.0.0"
