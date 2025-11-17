from django.urls import path
from . import views

urlpatterns = [
    path('get_cost/', views.get_cost, name='get_cost'),
    path('get_tokens/', views.get_tokens, name='get_cost'),
    path('log_message/', views.log_message, name='log_message'),
    path('get_messages/', views.get_messages, name='get_messages'),
    path('get_chat_ids/', views.get_chat_ids, name='get_chat_ids'),
    path('get_messages_by_chat_id/<str:chat_id>/', views.get_messages_by_chat_id, name='get_messages_by_chat_id'),
]
