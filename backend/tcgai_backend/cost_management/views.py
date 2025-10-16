from django.http import JsonResponse
from .llm_provider_a_adapter import AnthropicAdapter

llmprovider = AnthropicAdapter()

def get_cost(request):
    cost_data = llmprovider.get_cost()
    return JsonResponse(cost_data)
