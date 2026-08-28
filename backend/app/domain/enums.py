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
