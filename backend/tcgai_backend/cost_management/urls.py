from django.urls import path
from . import views

urlpatterns = [
    path('get_cost/', views.get_cost, name='get_cost'),
    path('get_tokens/', views.get_tokens, name='get_cost'),
    path('log_message/', views.log_message, name='log_message'),
    path('get_messages/', views.get_messages, name='get_messages'),
    path('get_chat_ids/', views.get_chat_ids, name='get_chat_ids'),
    path('get_messages_by_chat_id/<str:chat_id>/', views.get_messages_by_chat_id, name='get_messages_by_chat_id'),
    path("login/", views.login_view, name="login"),
    path("auth-check/", views.auth_check, name="auth_check"),
    path('evaluate_chat/', views.evaluate_chat, name="evaluate_chat"),
    path('get_avg_eval_score/', views.get_avg_eval_score, name="get_avg_eval_score"),
    path('get_avg_tokens_in/', views.get_avg_tokens_in, name="get_avg_tokens_in"),
    path('get_avg_tokens_out/', views.get_avg_tokens_out, name="get_avg_tokens_out"),
    path('get_avg_conversations_per_day/', views.get_avg_conversations_per_day, name="get_avg_conversations_per_day"),
]
