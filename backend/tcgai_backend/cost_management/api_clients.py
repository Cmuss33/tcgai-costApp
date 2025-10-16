from abc import ABC, abstractmethod

class LLMAdapter(ABC):
    @abstractmethod
    def get_cost(self, *args, **kwargs):
        pass
