# app/routing.py
from django.urls import re_path
from .consumers import ChatConsumer

websocket_urlpatterns = [
    # Matches: /ws/chat/2/ or /ws/chat/emp-2/
    re_path(r'ws/chat/(?P<target_id>[\w-]+)/$', ChatConsumer.as_asgi()),
    
    # Also matches: /ws/chat/private/emp-4/emp-2/ (for frontend compatibility)
    re_path(r'ws/chat/private/(?P<user_id>[\w-]+)/(?P<target_id>[\w-]+)/$', ChatConsumer.as_asgi()),
]