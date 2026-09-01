from abc import ABC, abstractmethod

from engine.intelligence.models import IntelligenceIngestPayload


class BaseFeedAdapter(ABC):
    authority_mode = "OBSERVE_ONLY"

    def __init__(self, http_client: object) -> None:
        self.http = http_client

    @property
    @abstractmethod
    def provider_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def poll_feed(self) -> list[IntelligenceIngestPayload]:
        raise NotImplementedError
