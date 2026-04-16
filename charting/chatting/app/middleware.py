from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
import logging

logger = logging.getLogger(__name__)
User = get_user_model()


class WebSocketAuthMiddleware(BaseMiddleware):
    """
    JWT-based WebSocket authentication middleware.
    Reads token from:
    1. Query string: ?token=<access_token>
    2. Cookie: access_token=<access_token>
    """

    async def __call__(self, scope, receive, send):
        token = None

        # 1. Try query string first: ws://.../?token=xxx
        query_string = scope.get('query_string', b'').decode('utf-8')
        if query_string:
            params = {}
            for part in query_string.split('&'):
                if '=' in part:
                    k, v = part.split('=', 1)
                    params[k] = v
            token = params.get('token')

        # 2. Fallback: try cookie
        if not token:
            headers = dict(scope.get('headers', []))
            cookie_header = headers.get(b'cookie', b'').decode('utf-8')
            if cookie_header:
                for item in cookie_header.split(';'):
                    item = item.strip()
                    if '=' in item:
                        key, value = item.split('=', 1)
                        if key.strip() == 'access_token':
                            token = value.strip()
                            break

        if token:
            scope['user'] = await self.get_user_from_token(token)
        else:
            scope['user'] = AnonymousUser()

        return await super().__call__(scope, receive, send)

    @database_sync_to_async
    def get_user_from_token(self, token):
        try:
            access_token = AccessToken(token)
            user_id = access_token['user_id']
            return User.objects.get(id=user_id, is_active=True)
        except (InvalidToken, TokenError) as e:
            logger.warning(f"Invalid JWT token: {e}")
        except User.DoesNotExist:
            logger.warning(f"User not found for JWT token")
        except Exception as e:
            logger.error(f"Error authenticating WebSocket via JWT: {e}")
        return AnonymousUser()