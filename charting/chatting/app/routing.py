# app/routing.py
from django.urls import re_path
from .consumers import ChatConsumer, GroupChatConsumer

websocket_urlpatterns = [
    # Private chat
    re_path(r'ws/chat/(?P<target_id>[\w-]+)/$', ChatConsumer.as_asgi()),
    re_path(r'ws/chat/private/(?P<user_id>[\w-]+)/(?P<target_id>[\w-]+)/$', ChatConsumer.as_asgi()),
    
    # Group chat
    re_path(r'ws/chat/group/(?P<group_id>\d+)/$', GroupChatConsumer.as_asgi()),
]