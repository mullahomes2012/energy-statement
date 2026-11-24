"""Constants for the Energy Statement integration."""

from __future__ import annotations

DOMAIN = "energy_statement"
VERSION = "1.2.2"

# Supply kinds
KIND_ELECTRICITY = "electricity"
KIND_GAS = "gas"
KIND_WATER = "water"
KINDS = [KIND_ELECTRICITY, KIND_GAS, KIND_WATER]

KIND_NAMES = {
    KIND_ELECTRICITY: "Electricity",
    KIND_GAS: "Gas",
    KIND_WATER: "Water",
}
KIND_ICONS = {
    KIND_ELECTRICITY: "mdi:flash",
    KIND_GAS: "mdi:fire",
    KIND_WATER: "mdi:water",
}

SOURCE_SENSOR = "sensor"
SOURCE_MANUAL = "manual"
SOURCE_FIXED = "fixed"

# Gas meter units
UNIT_M3 = "m3"
UNIT_FT3 = "ft3"  # imperial meter, reads in hundreds of cubic feet
UNIT_KWH = "kwh"
FT3_100_TO_M3 = 2.83168

DEFAULT_GAS_CORRECTION = 1.02264
DEFAULT_GAS_CV = 39.5

# Billing cycles
CYCLE_MONTHLY = "monthly"
CYCLE_QUARTERLY = "quarterly"
CYCLE_WEEKLY = "weekly"

MODE_AUTO = "auto"
MODE_APPROVE = "approve"
MODE_MANUAL = "manual"  # print and hand over, no email

SECURITY_STARTTLS = "starttls"
SECURITY_SSL = "ssl"
SECURITY_NONE = "none"

# Option section keys
OPT_SUPPLIES = "supplies"
OPT_SCHEDULE = "schedule"
OPT_PEOPLE = "people"
OPT_PAYMENT = "payment"
OPT_EMAIL = "email"

# Pending statement states
STATUS_IDLE = "idle"
STATUS_WAITING = "waiting_for_readings"
STATUS_APPROVAL = "awaiting_approval"

# Mobile notification actions
ACTION_APPROVE = "ESTMT_APPROVE"
ACTION_SKIP = "ESTMT_SKIP"
ACTION_READING = "ESTMT_READING"

PDF_URL_BASE = "/api/energy_statement"
STORAGE_VERSION = 1
UPDATE_MINUTES = 15
