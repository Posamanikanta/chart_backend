from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
        re_path(r'ws/chat/(?P<target_id>\w+)/$', consumers.ChatConsumer.as_asgi()),
        re_path(r'ws/group/(?P<group_id>\d+)/$', consumers.GroupChatConsumer.as_asgi()),
        # ✅ NEW: Presence & Notifications
        re_path(r'ws/presence/$', consumers.PresenceConsumer.as_asgi()),
        re_path(r'ws/notifications/$', consumers.NotificationConsumer.as_asgi()),
    ]