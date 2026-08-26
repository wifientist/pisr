"""
RuckusONE API Constants

Contains enums, type values, and other constants used across the R1 API services.
These values are derived from the RuckusONE OpenAPI specifications.
"""

# WiFi Network Types (discriminator values for polymorphic WiFi networks)
# Used in the "type" field when creating WiFi networks
class WifiNetworkType:
    """WiFi network type discriminator values (lowercase required)"""
    PSK = "psk"              # WPA2/WPA3 Personal with passphrase
    OPEN = "open"            # Open network (no authentication)
    AAA = "aaa"              # Enterprise 802.1X (RADIUS)
    DPSK = "dpsk"            # Dynamic PSK (unique keys per device)
    GUEST = "guest"          # Guest portal/captive portal
    HOTSPOT20 = "hotspot20"  # Hotspot 2.0 (Passpoint)


# WLAN Security Types
class WlanSecurity:
    """WLAN security type values for PSK networks"""
    WPA_PERSONAL = "WPAPersonal"        # WPA (legacy, avoid)
    WPA2_PERSONAL = "WPA2Personal"      # WPA2-PSK
    WPA3 = "WPA3"                       # WPA3-SAE
    WPA23_MIXED = "WPA23Mixed"          # WPA2/WPA3 transition mode
    WEP = "WEP"                         # WEP (legacy, insecure)


# Security Type Mapping (user-friendly -> API values)
SECURITY_TYPE_MAP = {
    "WPA3": WlanSecurity.WPA3,
    "WPA2": WlanSecurity.WPA2_PERSONAL,
    "WPA2/WPA3": WlanSecurity.WPA23_MIXED,
    "WPA": WlanSecurity.WPA_PERSONAL,
    "WEP": WlanSecurity.WEP,
}

# Reverse mapping (API values -> user-friendly names)
REVERSE_SECURITY_TYPE_MAP = {v: k for k, v in SECURITY_TYPE_MAP.items()}


# VLAN ID Constraints
class VlanConstraints:
    """VLAN ID validation constraints"""
    MIN_VLAN_ID = 1
    MAX_VLAN_ID = 4094
    DEFAULT_VLAN_ID = 1


# Passphrase Constraints
class PassphraseConstraints:
    """WiFi passphrase validation constraints"""
    MIN_LENGTH = 8
    MAX_LENGTH = 64
    # Pattern: ^[!-_a-~]((?!\$\()[ !-_a-~]){6,61}[!-_a-~]$|^[A-Fa-f0-9]{64}$
    # Allows printable ASCII except $( sequence, or 64 hex chars


# SSID Constraints
class SsidConstraints:
    """SSID validation constraints"""
    MIN_LENGTH = 2
    MAX_LENGTH = 32
    # Pattern: [^`\s]([^`\t\r\n]){0,30}[^`\s]
    # No backticks, no leading/trailing whitespace


# Network Name Constraints
class NetworkNameConstraints:
    """Network name (internal identifier) validation constraints"""
    MIN_LENGTH = 2
    MAX_LENGTH = 32
    # Pattern: (?=^((?!(`|\$\()).){2,32}$)^(\S.*\S)$
    # No backticks, no $(), no leading/trailing whitespace


# DPSK Passphrase Generation
class DpskPassphraseFormat:
    """
    RuckusONE `passphraseFormat` enum for DPSK pools.

    DICTIONARY_WORDS generates word-based passphrases (e.g. "otter-lantern-
    copper"), which are far easier to read out over the phone than the
    KEYBOARD_FRIENDLY default. It is the only format that honours
    `wordCount` and `numericSuffixEnabled`; both are ignored otherwise.
    """
    NUMBERS_ONLY = "NUMBERS_ONLY"
    KEYBOARD_FRIENDLY = "KEYBOARD_FRIENDLY"
    MOST_SECURED = "MOST_SECURED"
    DICTIONARY_WORDS = "DICTIONARY_WORDS"

    ALL = (NUMBERS_ONLY, KEYBOARD_FRIENDLY, MOST_SECURED, DICTIONARY_WORDS)

    # Spec-declared bounds on wordCount (inclusive)
    MIN_WORD_COUNT = 3
    MAX_WORD_COUNT = 6
    DEFAULT_WORD_COUNT = 4


# DPSK Scale Limits
class DpskScaleLimits:
    """
    Ceilings for per-unit DPSK deployments.

    These are RuckusONE platform limits, not spec-declared values — the
    OpenAPI spec does not publish them, so they are recorded here as the
    single place to bump when R1 raises them again.

    SHARED_POOL_MAX_SSIDS applies to ssid_mode='per_unit': one DPSK pool
    backing many SSIDs. A passphrase from that pool works on ANY of those
    SSIDs, which is what keeps roaming working across the property.
    """
    SHARED_POOL_MAX_SSIDS = 1024

    # DPSK pool/service display name cap enforced by R1
    POOL_NAME_MAX_LENGTH = 64


# AP Group Name Constraints
class ApGroupNameConstraints:
    """AP Group name validation constraints"""
    MIN_LENGTH = 2
    MAX_LENGTH = 64
    # Pattern: (?=^((?!(`|\$\()).){2,64}$)^(\S.*\S)$


# HTTP Status Codes
class R1StatusCode:
    """Common RuckusONE API status codes"""
    OK = 200
    CREATED = 201
    ACCEPTED = 202           # Async operations
    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404
    UNPROCESSABLE_ENTITY = 422  # Validation errors
    LOCKED = 423
    INTERNAL_SERVER_ERROR = 500
    NOT_IMPLEMENTED = 501


# API Error Codes
class R1ErrorCode:
    """Common RuckusONE API error codes"""
    WIFI_INVALID_REQUEST = "WIFI-10001"
    # Add more as discovered


# Management Frame Protection
class ManagementFrameProtection:
    """Management Frame Protection (802.11w) settings"""
    DISABLED = "Disabled"
    OPTIONAL = "Optional"
    REQUIRED = "Required"


# Controller Types
class ControllerType:
    """Controller type identifiers"""
    RUCKUS_ONE = "RuckusONE"
    SMARTZONE = "SmartZone"
    UNLEASHED = "Unleashed"


# Controller Subtypes
class ControllerSubtype:
    """Controller subtype identifiers"""
    MSP = "MSP"         # Multi-tenant MSP
    EC = "EC"           # Enterprise Cloud (single tenant)
    STANDALONE = "Standalone"


# API Regions
class R1Region:
    """RuckusONE API regional endpoints"""
    NORTH_AMERICA = "api.ruckus.cloud"
    EUROPE = "api.eu.ruckus.cloud"
    ASIA_PACIFIC = "api.asia.ruckus.cloud"


# Rate Limits (from API documentation)
class RateLimit:
    """API rate limit information"""
    # Note: Specific rate limits vary by endpoint
    # See individual API documentation for details
    DEFAULT_TIMEOUT_MS = 120000  # 2 minutes
    MAX_TIMEOUT_MS = 600000      # 10 minutes
