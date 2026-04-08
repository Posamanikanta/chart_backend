from django.urls import path
from . import views  # Import views from the current directory

urlpatterns = [
    path('api/users/', views.get_users, name='get_users'),
    path('api/messages/<int:target_id>/', views.get_messages, name='get_messages'),
    path('api/create-employee/', views.create_employee, name='create_employee'),
     path('api/login/', views.login_user, name='login_user'),
]