# app/views.py
from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.contrib.auth import authenticate, login, logout
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.models import User
from .models import Employee, Message, ChatGroup


@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def login_user(request):
    """Login endpoint - checks Employee table"""
    email = request.data.get('email')
    password = request.data.get('password')
    
    if not email or not password:
        return Response({"error": "Email and password required"}, status=400)
    
    try:
        # Check Employee table for credentials
        employee = Employee.objects.get(email=email, password=password, is_active=True)
        
        # Get or create Django User for session
        user, created = User.objects.get_or_create(
            username=email,
            defaults={'email': email, 'first_name': employee.name}
        )
        
        if created:
            user.set_password(password)
            user.save()
        
        # Link Employee to User if not linked
        if not employee.user:
            employee.user = user
            employee.save()
        
        # Authenticate and create session
        auth_user = authenticate(request, username=email, password=password)
        if auth_user:
            login(request, auth_user)
        
        return Response({
            "id": employee.id,
            "name": employee.name,
            "email": employee.email,
            "role": employee.role,
            "isAuthenticated": True
        }, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Invalid email or password"}, status=401)


@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def logout_user(request):
    """Logout endpoint"""
    logout(request)
    return Response({"message": "Logged out successfully"}, status=200)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_users(request):
    """Get all employees except current user"""
    try:
        current_employee = Employee.objects.get(user=request.user)
        employees = Employee.objects.filter(is_active=True).exclude(id=current_employee.id)
        
        data = [{
            "id": e.id,
            "name": e.name,
            "email": e.email,
            "role": e.role
        } for e in employees]
        
        return Response(data, status=200)
        
    except Employee.DoesNotExist:
        # If no employee linked, return all employees
        employees = Employee.objects.filter(is_active=True)
        data = [{
            "id": e.id,
            "name": e.name,
            "email": e.email,
            "role": e.role
        } for e in employees]
        return Response(data, status=200)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_messages(request, target_id):
    """Get messages between current user and target user"""
    try:
        current_employee = Employee.objects.get(user=request.user)
        
        messages = Message.objects.filter(
            Q(group__isnull=True) & (
                (Q(sender=current_employee) & Q(receiver_id=target_id)) |
                (Q(sender_id=target_id) & Q(receiver=current_employee))
            )
        ).order_by("timestamp")

        data = [{
            "id": m.id,
            "text": m.content,
            "sender": "me" if m.sender == current_employee else "them",
            "sender_id": m.sender.id,
            "createdAt": m.timestamp.isoformat()
        } for m in messages]
        
        return Response(data, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def create_employee(request):
    """Create new employee"""
    data = request.data
    
    # Validation
    name = data.get('name', '').strip()
    email = data.get('email', '').strip()
    password = data.get('password', '')
    role = data.get('role', 'employee')
    
    if not email:
        return Response({"error": "Email is required"}, status=400)
    
    if not password:
        return Response({"error": "Password is required"}, status=400)
    
    if len(password) < 8:
        return Response({"error": "Password must be at least 8 characters"}, status=400)
    
    if not name:
        name = email.split('@')[0]  # Use email prefix as name
    
    # Check if employee already exists
    if Employee.objects.filter(email=email).exists():
        return Response({"error": "Employee with this email already exists"}, status=400)
    
    if User.objects.filter(username=email).exists():
        return Response({"error": "User with this email already exists"}, status=400)
    
    try:
        # Create Django User for authentication
        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            first_name=name
        )
        
        # Create Employee record with plain password
        employee = Employee.objects.create(
            name=name,
            email=email,
            password=password,  # Plain text password (visible in admin)
            role=role,
            user=user,
            is_active=True
        )
        
        return Response({
            "message": "Employee created successfully",
            "id": employee.id,
            "name": employee.name,
            "email": employee.email,
            "role": employee.role
        }, status=201)
        
    except Exception as e:
        # Rollback: Delete user if employee creation fails
        if 'user' in locals():
            user.delete()
        return Response({"error": str(e)}, status=400)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_current_user(request):
    """Get current logged in user info"""
    try:
        employee = Employee.objects.get(user=request.user)
        return Response({
            "id": employee.id,
            "name": employee.name,
            "email": employee.email,
            "role": employee.role,
            "isAuthenticated": True
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Not authenticated"}, status=401)