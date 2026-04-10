import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'chatting.settings')
django.setup()

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from app.middleware import WebSocketAuthMiddleware
import app.routing

django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        WebSocketAuthMiddleware(
            URLRouter(
                app.routing.websocket_urlpatterns
            )
        )
    ),
})