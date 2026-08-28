from enum import StrEnum


class RecordStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class CellStatus(StrEnum):
    APPRENTICE = "APPRENTICE"
    GUARDED = "GUARDED"
    HALTED = "HALTED"
    RETIRED = "RETIRED"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class RiskVerdict(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    BLOCKED = "BLOCKED"


class TrustOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    DEMOTE = "DEMOTE"
