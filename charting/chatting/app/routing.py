from django.urls import re_path
from .consumers import ChatConsumer, GroupChatConsumer

websocket_urlpatterns = [
    # Private chat
    re_path(r'ws/chat/(?P<target_id>[\w-]+)/$', ChatConsumer.as_asgi()),
    
    # Group chat
    re_path(r'ws/chat/group/(?P<group_id>\d+)/$', GroupChatConsumer.as_asgi()),
]