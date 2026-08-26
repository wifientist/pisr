"""
R1 API Models

Contains model metadata and data structures used across the R1 API client.
"""

from .ap_models import (
    MODEL_UPLINK_PORTS,
    MODEL_PORT_COUNTS,
    get_model_info,
    has_configurable_lan_ports,
    get_port_count,
    get_uplink_port,
    get_configurable_ports,
    get_all_ports,
)

__all__ = [
    'MODEL_UPLINK_PORTS',
    'MODEL_PORT_COUNTS',
    'get_model_info',
    'has_configurable_lan_ports',
    'get_port_count',
    'get_uplink_port',
    'get_configurable_ports',
    'get_all_ports',
]
