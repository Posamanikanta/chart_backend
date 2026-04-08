# app/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('api/login/', views.login_user, name='login_user'),
    path('api/logout/', views.logout_user, name='logout_user'),
    path('api/users/', views.get_users, name='get_users'),
    path('api/messages/<int:target_id>/', views.get_messages, name='get_messages'),
    path('api/create-employee/', views.create_employee, name='create_employee'),
    path('api/me/', views.get_current_user, name='get_current_user'),
]