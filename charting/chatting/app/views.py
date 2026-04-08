from django.db.models import Q
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.contrib.auth import authenticate, login, logout
from django.views.decorators.csrf import csrf_exempt
from .models import Message, User
from rest_framework.decorators import api_view, permission_classes # <--- Add permission_classes
from rest_framework.permissions import AllowAny 

@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny]) # Ensure this is imported from rest_framework.permissions
def login_user(request):
    email = request.data.get('email')
    password = request.data.get('password')
    
    # IMPORTANT: Check if you are using 'email' as the 'username' in your DB
    user = authenticate(request, username=email, password=password)

    if user is not None:
        login(request, user) # <--- THIS CREATES THE COOKIE
        role = user.profile.role if hasattr(user, 'profile') else 'employee'
        return Response({
            "id": user.id,
            "name": user.username,
            "email": user.email,
            "role": role,
            "isAuthenticated": True
        }, status=200)
    
    return Response({"error": "Wrong email or password"}, status=401)
@api_view(["GET"])
def get_messages(request, target_id):
    if not request.user.is_authenticated:
        return Response({"error": "Not authenticated"}, status=401)

    messages = Message.objects.filter(
        Q(group__isnull=True) & (
            (Q(sender=request.user, receiver_id=target_id)) |
            (Q(sender_id=target_id, receiver=request.user))
        )
    ).order_by("timestamp")

    data = [{"id": m.id, "text": m.content, "sender": "me" if m.sender == request.user else "them", "createdAt": m.timestamp} for m in messages]
    return Response(data)

@api_view(["GET"])
def get_users(request):
    users = User.objects.exclude(id=request.user.id) if request.user.is_authenticated else User.objects.all()
    data = [{"id": u.id, "name": u.username, "email": u.email} for u in users]
    return Response(data)

@csrf_exempt
@api_view(["POST"])
def create_employee(request):
    data = request.data
    try:
        if User.objects.filter(username=data.get('email')).exists():
            return Response({"error": "Exists"}, status=400)
        User.objects.create_user(username=data.get('email'), email=data.get('email'), password=data.get('password'), first_name=data.get('name'))
        return Response({"message": "Created"}, status=201)
    except Exception as e:
        return Response({"error": str(e)}, status=400)