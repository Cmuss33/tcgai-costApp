from django.http import JsonResponse
from .llm_provider_adapter_implementations import AnthropicAdapter

llmprovider = AnthropicAdapter()

def get_cost(request):
    response = llmprovider.get_cost()
    return JsonResponse(response, safe=False)
