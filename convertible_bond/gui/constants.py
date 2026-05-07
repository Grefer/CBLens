"""GUI 共享常量.

集中放在这里, 避免 controller mixin 反向 import app 造成循环.
"""
import re


BOND_CODE_RE = re.compile(r"^\d{6}\.[A-Z]{2}$")
DEFAULT_P_DOWN_PCT = 15.0
DEFAULT_DISTRESS_K_PCT = 5.0
DEFAULT_CREDIT_SPREAD_PCT = 3.0
EVENT_SYNC_STALE_HOURS = 24
