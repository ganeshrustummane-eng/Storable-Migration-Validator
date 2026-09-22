from abc import ABC, abstractmethod
from typing import Any, Iterator  # or import your Connection type

class Database(ABC):
    @abstractmethod
    def connect(self) -> Any:
        raise NotImplementedError

    @abstractmethod
    def execute_query(self, query: str) -> Any:
        raise NotImplementedError

    @abstractmethod
    def execute_query_stream(self, query: str, chunksize: int = 50_000) -> Iterator[Any]:
        """Yield the result in bounded-size pandas DataFrame chunks instead of
        materializing the full result (execute_query's fetchall() behavior).
        Used by the large-table hybrid engine (Project/tiered_runner.py) only —
        execute_query() and every existing caller are unaffected."""
        raise NotImplementedError
