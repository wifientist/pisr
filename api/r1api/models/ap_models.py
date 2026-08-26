"""
AP Model Metadata

Centralized source of truth for AP model capabilities:
- LAN port counts
- Uplink port designations
- Whether a model supports LAN port configuration

This is used by:
- Audit endpoints (to query only relevant ports)
- LAN port configuration (to skip uplink ports)
- Frontend (to render correct UI)
"""

# Models with configurable LAN ports and their uplink port
# Key: model prefix, Value: uplink port ID
# Based on Ruckus hardware documentation - uplink is typically the PoE-in port
MODEL_UPLINK_PORTS = {
    # LAN1 is uplink (older models)
    "R500": "LAN1",
    "R510": "LAN1",
    "R600": "LAN1",
    "R610": "LAN1",
    "R710": "LAN1",
    "T610": "LAN1",
    "T610S": "LAN1",
    "T710": "LAN1",
    "T710S": "LAN1",
    # LAN2 is uplink (newer outdoor/high-end models)
    "R550": "LAN2",
    "R560": "LAN2",
    "R575": "LAN2",
    "R650": "LAN2",
    "R670": "LAN2",
    "R720": "LAN2",
    "R730": "LAN2",
    "R750": "LAN2",
    "R760": "LAN2",
    "R770": "LAN2",
    "R850": "LAN2",
    "R860": "LAN2",
    "T350": "LAN2",
    "T670": "LAN2",
    "T670SN": "LAN2",
    "T811CM": "LAN2",
    # LAN3 is uplink (2-port wall-plates and some T-series)
    "H320": "LAN3",
    "H350": "LAN3",
    "T750": "LAN3",
    "T750SE": "LAN3",
    # LAN5 is uplink (4-port wall-plates and switches)
    "H510": "LAN5",
    "H550": "LAN5",
    "H670": "LAN5",
    "C110": "LAN5",
    "C111": "LAN5",
}

# Number of CONFIGURABLE LAN ports per model (excludes uplink)
MODEL_PORT_COUNTS = {
    # 1-port models with LAN1 uplink (LAN2 configurable)
    "R500": 1,
    "R510": 1,
    "R600": 1,
    "R610": 1,
    "R710": 1,
    "T610": 1,
    "T610S": 1,
    "T710": 1,
    "T710S": 1,
    # 1-port models with LAN2 uplink (LAN1 configurable)
    "R550": 1,
    "R560": 1,
    "R575": 1,
    "R650": 1,
    "R670": 1,
    "R720": 1,
    "R730": 1,
    "R750": 1,
    "R760": 1,
    "R770": 1,
    "R850": 1,
    "R860": 1,
    "T350": 1,
    "T670": 1,
    "T670SN": 1,
    "T811CM": 1,
    # 2-port wall-plates (LAN3 is uplink, LAN1 & LAN2 configurable)
    "H320": 2,
    "H350": 2,
    "T750": 2,
    "T750SE": 2,
    # 4-port wall-plates (LAN5 is uplink, LAN1-4 configurable)
    "H510": 4,
    "H550": 4,
    "H670": 4,
    # 4-port switches (LAN5 is uplink, LAN1-4 configurable)
    "C110": 4,
    "C111": 4,
}


def get_model_info(model: str) -> dict:
    """
    Get capability info for an AP model.

    Args:
        model: AP model string (e.g., "H510", "R750", "R350")

    Returns:
        dict with:
        - has_lan_ports: bool
        - port_count: int (number of configurable ports)
        - uplink_port: str | None (e.g., "LAN3")
        - configurable_ports: list[str] (e.g., ["LAN1", "LAN2", "LAN4", "LAN5"])
    """
    if not model:
        return {
            'has_lan_ports': False,
            'port_count': 0,
            'uplink_port': None,
            'configurable_ports': [],
            'all_ports': []
        }

    model_upper = model.upper()

    # Find matching model prefix
    port_count = 0
    uplink_port = None

    for prefix, count in MODEL_PORT_COUNTS.items():
        if model_upper.startswith(prefix.upper()):
            port_count = count
            break

    for prefix, uplink in MODEL_UPLINK_PORTS.items():
        if model_upper.startswith(prefix.upper()):
            uplink_port = uplink
            break

    # Determine all ports and configurable ports based on model
    all_ports = []
    configurable_ports = []

    if port_count > 0:
        # Determine total physical ports based on model type
        if model_upper.startswith(('H510', 'H550', 'H670', 'C110', 'C111')):
            # 5-port models (LAN1-5, LAN5 is uplink)
            all_ports = ['LAN1', 'LAN2', 'LAN3', 'LAN4', 'LAN5']
        elif model_upper.startswith(('H320', 'H350', 'T750')):
            # 3-port models (LAN1-3, LAN3 is uplink)
            all_ports = ['LAN1', 'LAN2', 'LAN3']
        else:
            # Standard 2-port APs
            all_ports = ['LAN1', 'LAN2']

        # Configurable = all ports minus uplink
        configurable_ports = [p for p in all_ports if p != uplink_port]

    return {
        'has_lan_ports': port_count > 0,
        'port_count': port_count,
        'uplink_port': uplink_port,
        'configurable_ports': configurable_ports,
        'all_ports': all_ports
    }


def has_configurable_lan_ports(model: str) -> bool:
    """Check if a model has configurable LAN ports."""
    return get_model_info(model)['has_lan_ports']


def get_port_count(model: str) -> int:
    """Get the number of configurable LAN ports for a model."""
    return get_model_info(model)['port_count']


def get_uplink_port(model: str) -> str | None:
    """Get the uplink port ID for a model."""
    return get_model_info(model)['uplink_port']


def get_configurable_ports(model: str) -> list[str]:
    """Get list of configurable port IDs for a model."""
    return get_model_info(model)['configurable_ports']


def get_all_ports(model: str) -> list[str]:
    """Get list of all physical port IDs for a model."""
    return get_model_info(model)['all_ports']
