"""Scheduling package — schedule evaluation and time management.

Extracted from server.py.  Each class lives in its own module.
"""

__version__: str = "1.0"

from scheduling.scheduler_thread import SchedulerThread

__all__: list[str] = ["SchedulerThread"]
