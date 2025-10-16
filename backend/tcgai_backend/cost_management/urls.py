from django.urls import path
from . import views

urlpatterns = [
    path('get_cost/', views.get_cost, name='get_cost'),
]
