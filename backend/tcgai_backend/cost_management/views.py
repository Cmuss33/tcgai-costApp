from django.http import JsonResponse
from .llm_provider_adapter_implementations import AnthropicAdapter
from .models import Chat, Message
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
import json

llmprovider = AnthropicAdapter()

def get_cost(request):
    response = llmprovider.get_cost()
    return JsonResponse(response, safe=False)

@csrf_exempt
@require_http_methods(["POST"])
def log_message(request):
    try:
        data = json.loads(request.body)
        chat_id = data.get('chat_id')
        message_id = data.get('message_id')
        content = data.get('content')
        llm_formatted_message = data.get('llm_formatted_message')
        returned_content = data.get('returned_content')
        llm_formatted_returned_message = data.get('llm_formatted_returned_message')
        tokens_in = data.get('tokens_in')
        tokens_out = data.get('tokens_out')

        chat, created = Chat.objects.get_or_create(chat_id=chat_id)
        message = Message.objects.create(
            message_id=message_id,
            chat=chat,
            content=content,
            llm_formatted_message=llm_formatted_message,
            returned_content=returned_content,
            llm_formatted_returned_message=llm_formatted_returned_message,
            tokens_in=tokens_in,
            tokens_out=tokens_out
        )
        return JsonResponse({'status': 'success', 'message_id': message_id})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
