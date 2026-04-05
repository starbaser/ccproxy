from typing import Any

SERVICE_NAME: str

class Resource:
    @classmethod
    def create(cls, attributes: dict[str, Any]) -> Resource: ...
