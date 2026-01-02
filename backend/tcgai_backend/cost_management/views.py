from django.http import JsonResponse
from .llm_provider_adapter_implementations import AnthropicAdapter
from .models import Chat, Message
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
import json

llmprovider = AnthropicAdapter()

def get_cost(request):
    year = request.GET.get("year")
    month = request.GET.get("month")
    response = llmprovider.get_cost(year=year, month=month)
    return JsonResponse(response, safe=False)

def get_tokens(request):
    year = request.GET.get("year")
    month = request.GET.get("month")
    response = llmprovider.get_tokens(year=year, month=month)
    return JsonResponse(response, safe=False)

@csrf_exempt
@require_http_methods(["POST"])
def log_message(request):
    try:
        data = json.loads(request.body)
        chat_id = data.get('chat_id')
        content = data.get('content')
        llm_formatted_message = data.get('llm_formatted_message')
        returned_content = data.get('returned_content')
        llm_formatted_returned_message = data.get('llm_formatted_returned_message')
        tokens_in = data.get('tokens_in')
        tokens_out = data.get('tokens_out')
        model = data.get('model')

        chat, created = Chat.objects.get_or_create(chat_id=chat_id, defaults={"model": model})

        message = Message.objects.create(
            chat=chat,
            content=content,
            llm_formatted_message=llm_formatted_message,
            returned_content=returned_content,
            llm_formatted_returned_message=llm_formatted_returned_message,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model
        )
        return JsonResponse({'status': 'success'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

def get_messages(request):
    try:
        chats = Chat.objects.all()
        result = []
        for chat in chats:
            messages = Message.objects.filter(chat=chat).order_by('-timestamp')
            chat_data = {
                'chat_id': chat.chat_id,
                'messages': list(messages.values())
            }
            result.append(chat_data)
        return JsonResponse(result, safe=False)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

def get_chat_ids(request):
    try:
        chats = list(Chat.objects.values())
        return JsonResponse(chats, safe=False)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

def get_messages_by_chat_id(request, chat_id):
    try:
        chat = Chat.objects.get(chat_id=chat_id)
        messages = Message.objects.filter(chat=chat).order_by('timestamp')
        return JsonResponse(list(messages.values()), safe=False)
    except Chat.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Chat not found'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
