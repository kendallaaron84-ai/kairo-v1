from dataclasses import dataclass

from app.db.models.intelligence import IntelligenceEvidenceLedger


@dataclass(frozen=True)
class AuthorityPolicyV1:
    POLICY_VERSION = "INTEL-VETO-MACRO-v1"
    ALLOWED_REASON_CODES = ("CRITICAL_MACRO_WINDOW_ACTIVE",)
    ALLOWED_EVENT_TYPES = ("CPI", "FOMC")
    WINDOW_BEFORE_MINUTES = 5
    WINDOW_AFTER_MINUTES = 5
    CAN_ORIGINATE_TRADE = False
    CAN_RESIZE_TRADE = False
    CAN_CHANGE_LIMIT_PRICE = False
    CAN_ONLY_SUPPRESS = True

    @classmethod
    def classify_event(cls, event: IntelligenceEvidenceLedger) -> str | None:
        """Fail-closed mapping from canonical primary-source evidence to policy type."""
        if event.event_type != "MACRO" or event.source_type != "PRIMARY":
            return None
        title = event.title.upper()
        source = event.source_name.upper()
        if "FOMC" in title and "FEDERAL" in source:
            return "FOMC"
        if "CPI" in title and ("BLS" in source or "LABOR" in source):
            return "CPI"
        return None
