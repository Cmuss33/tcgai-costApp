from django.http import JsonResponse
from .api_clients import LLMClient
from .llm_provider_a_adapter import LLMProviderAAdapter

def get_cost(request):
    adapter = LLMProviderAAdapter()
    client = LLMClient(adapter)
    cost_data = client.get_cost()
    return JsonResponse(cost_data)
