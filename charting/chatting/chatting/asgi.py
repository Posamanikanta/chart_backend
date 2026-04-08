import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
import app.routing # <--- Double check this path

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'chatting.settings')

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter(
            app.routing.websocket_urlpatterns
        )
    ),
})