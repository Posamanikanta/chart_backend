from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.models import Session
from django.contrib.auth import get_user_model
from urllib.parse import parse_qs
import logging

logger = logging.getLogger(__name__)
User = get_user_model()


class WebSocketAuthMiddleware(BaseMiddleware):
    """
    Custom middleware for WebSocket authentication using Django sessions.
    """
    
    async def __call__(self, scope, receive, send):
        # Try to get session from cookies
        cookies = {}
        headers = dict(scope.get('headers', []))
        
        cookie_header = headers.get(b'cookie', b'').decode('utf-8')
        if cookie_header:
            for item in cookie_header.split(';'):
                item = item.strip()
                if '=' in item:
                    key, value = item.split('=', 1)
                    cookies[key.strip()] = value.strip()
        
        session_key = cookies.get('sessionid')
        
        if session_key:
            scope['user'] = await self.get_user_from_session(session_key)
        else:
            scope['user'] = AnonymousUser()
        
        return await super().__call__(scope, receive, send)
    
    @database_sync_to_async
    def get_user_from_session(self, session_key):
        try:
            session = Session.objects.get(session_key=session_key)
            session_data = session.get_decoded()
            user_id = session_data.get('_auth_user_id')
            
            if user_id:
                return User.objects.get(id=user_id)
        except (Session.DoesNotExist, User.DoesNotExist) as e:
            logger.warning(f"Session/User not found: {e}")
        except Exception as e:
            logger.error(f"Error getting user from session: {e}")
        
        return AnonymousUser()