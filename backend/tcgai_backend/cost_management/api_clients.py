from abc import ABC, abstractmethod

class LLMAdapter(ABC):
    @abstractmethod
    def get_cost(self):
        pass
    @abstractmethod
    def get_tokens(self):
        pass
