"""Handler package — HTTP request handlers extracted from server.py.

Each mixin class contains handler methods for one API domain.
GlowUpRequestHandler inherits from all mixins.
"""

__version__: str = "1.0"

from handlers.device import DeviceHandlerMixin
from handlers.plug import PlugHandlerMixin
from handlers.groups import GroupHandlerMixin
from handlers.sensors import SensorHandlerMixin
from handlers.schedule import ScheduleHandlerMixin
from handlers.media import MediaHandlerMixin
from handlers.discovery import DiscoveryHandlerMixin
from handlers.registry import RegistryHandlerMixin
from handlers.dashboard import DashboardHandlerMixin
from handlers.calibration import CalibrationHandlerMixin
from handlers.distributed import DistributedHandlerMixin
from handlers.diagnostics import DiagnosticsHandlerMixin
from handlers.static import StaticHandlerMixin

__all__: list[str] = [
    "DeviceHandlerMixin",
    "PlugHandlerMixin",
    "GroupHandlerMixin",
    "SensorHandlerMixin",
    "ScheduleHandlerMixin",
    "MediaHandlerMixin",
    "DiscoveryHandlerMixin",
    "RegistryHandlerMixin",
    "DashboardHandlerMixin",
    "CalibrationHandlerMixin",
    "DistributedHandlerMixin",
    "DiagnosticsHandlerMixin",
    "StaticHandlerMixin",
]
