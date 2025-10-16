from abc import ABC, abstractmethod

class LLMAdapter(ABC):
    @abstractmethod
    def get_cost(self, *args, **kwargs):
        pass

class LLMClient:
    def __init__(self, adapter: LLMAdapter):
        self.adapter = adapter

    def get_cost(self, *args, **kwargs):
        return self.adapter.get_cost(*args, **kwargs)
