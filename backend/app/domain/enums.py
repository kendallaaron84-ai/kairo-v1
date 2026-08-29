from enum import StrEnum


class RecordStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class CellStatus(StrEnum):
    INITIALIZING = "INITIALIZING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    HALTED_FOR_DAY = "HALTED_FOR_DAY"
    REPLICATION_READY = "REPLICATION_READY"
    DECOMMISSIONED = "DECOMMISSIONED"


class AutonomyTier(StrEnum):
    APPRENTICE = "APPRENTICE"
    GUARDED = "GUARDED"
    CAPITAL_BUILDER = "CAPITAL_BUILDER"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderPurpose(StrEnum):
    ENTRY = "ENTRY"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"
    TREASURY_PURCHASE = "TREASURY_PURCHASE"


class OptionRight(StrEnum):
    CALL = "CALL"
    PUT = "PUT"


class RiskVerdict(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    BLOCKED = "BLOCKED"


class TrustOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    DEMOTE = "DEMOTE"
