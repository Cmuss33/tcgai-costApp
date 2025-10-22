from django.urls import path
from . import views

urlpatterns = [
    path('get_cost/', views.get_cost, name='get_cost'),
    path('log_message/', views.log_message, name='log_message'),
    path('get_messages/', views.get_messages, name='get_messages'),
]
