# app/views.py - COMPLETE
from django.db.models import Q, Count, Max, Subquery, OuterRef, F
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from .models import (
    Employee, Message, ChatGroup, MessageReaction, 
    MessageDeletion, SavedMeetLink, MeetingInvitation, AdminActivityLog
)
import os
import mimetypes


# ==================== HELPER FUNCTIONS ====================

def log_admin_activity(admin, action, target_employee=None, details=None):
    """Helper function to log admin activities"""
    AdminActivityLog.objects.create(
        admin=admin,
        action=action,
        target_employee=target_employee,
        details=details or {}
    )


def serialize_message(msg, current_employee, viewing_as_admin=False, target_employee=None):
    """
    Helper to serialize a message with reactions, edit status, and deletion status
    viewing_as_admin: True when admin is viewing another employee's messages
    target_employee: The employee whose perspective we're viewing from
    """
    
    # Determine perspective employee (who's "me")
    perspective_employee = target_employee if viewing_as_admin and target_employee else current_employee
    
    # Check if deleted for this user
    is_deleted_for_me = MessageDeletion.objects.filter(
        message=msg,
        employee=perspective_employee
    ).exists()
    
    if is_deleted_for_me and not viewing_as_admin:
        return None  # Don't include in response unless admin is viewing
    
    reactions = list(msg.reactions.values('reaction').annotate(count=Count('reaction')))
    user_reaction = msg.reactions.filter(employee=perspective_employee).first()
    
    # Check edit/delete permissions from perspective of target employee
    can_edit = msg.can_edit(perspective_employee)
    can_delete_for_everyone = msg.can_delete_for_everyone(perspective_employee)
    is_mine = msg.sender == perspective_employee
    
    # Handle deleted for everyone
    if msg.is_deleted_for_everyone:
        return {
            "id": msg.id,
            "text": "🚫 This message was deleted",
            "sender": "me" if is_mine else "them",
            "sender_id": msg.sender.id,
            "sender_name": msg.sender.name,
            "sender_avatar": msg.sender.get_avatar_url(),
            "receiver_id": msg.receiver.id if msg.receiver else None,
            "createdAt": msg.timestamp.isoformat(),
            "isRead": msg.is_read,
            "messageType": "text",
            "isDeleted": True,
            "deletedForEveryone": True,
            "isDeletedForMe": is_deleted_for_me,
        }
    
    data = {
        "id": msg.id,
        "text": msg.content,
        "sender": "me" if is_mine else "them",
        "sender_id": msg.sender.id,
        "sender_name": msg.sender.name,
        "sender_avatar": msg.sender.get_avatar_url(),
        "receiver_id": msg.receiver.id if msg.receiver else None,
        "createdAt": msg.timestamp.isoformat(),
        "isRead": msg.is_read,
        "messageType": msg.message_type,
        "fileUrl": msg.get_file_url(),
        "fileName": msg.file_name,
        "fileSize": msg.file_size,
        "reactions": {r['reaction']: r['count'] for r in reactions},
        "userReaction": user_reaction.reaction if user_reaction else None,
        "isEdited": msg.is_edited,
        "editedAt": msg.edited_at.isoformat() if msg.edited_at else None,
        "canEdit": can_edit and not viewing_as_admin,
        "canDeleteForEveryone": can_delete_for_everyone and not viewing_as_admin,
        "isMine": is_mine,
        "isDeleted": False,
        "isDeletedForMe": is_deleted_for_me,
    }
    
    # Add meet info if applicable
    if msg.message_type == 'meet' and msg.meet_link:
        invitations = list(msg.invitations.all())
        my_invitation = next((inv for inv in invitations if inv.invitee == perspective_employee), None)
        
        data["meetLink"] = msg.meet_link
        data["meetTitle"] = msg.meet_title
        data["meetScheduledAt"] = msg.meet_scheduled_at.isoformat() if msg.meet_scheduled_at else None
        data["invitations"] = [{
            "inviteeId": inv.invitee.id,
            "inviteeName": inv.invitee.name,
            "status": inv.status
        } for inv in invitations]
        data["myInviteStatus"] = my_invitation.status if my_invitation else None
    
    return data


# ==================== AUTH VIEWS ====================

@api_view(["POST"])
@permission_classes([AllowAny])
@csrf_exempt
def login_user(request):
    """Login endpoint - creates Django session"""
    email = request.data.get('email', '').strip()
    password = request.data.get('password', '').strip()
    
    print(f"\n🔐 Login attempt: {email}")
    
    if not email or not password:
        return Response({"error": "Email and password required"}, status=400)
    
    try:
        employee = Employee.objects.get(email=email, password=password, is_active=True)
    except Employee.DoesNotExist:
        print(f"❌ Invalid credentials for: {email}")
        return Response({"error": "Invalid email or password"}, status=401)
    
    user, created = User.objects.get_or_create(
        username=email,
        defaults={
            'email': email,
            'first_name': employee.name,
            'is_active': True
        }
    )
    
    if created:
        user.set_password(password)
        user.save()
        print(f"✅ Created new User: {email}")
    
    if not employee.user:
        employee.user = user
        employee.save()
        print(f"✅ Linked Employee to User")
    
    auth_user = authenticate(request, username=email, password=password)
    
    if auth_user is None:
        user.set_password(password)
        user.save()
        auth_user = authenticate(request, username=email, password=password)
    
    if auth_user and auth_user.is_active:
        login(request, auth_user)
        request.session.save()
        
        session_key = request.session.session_key
        print(f"✅ User logged in: {email}")
        print(f"✅ Session Key: {session_key}")
        
        response = Response({
            "id": employee.id,
            "name": employee.name,
            "email": employee.email,
            "role": employee.role,
            "about": employee.about,
            "avatarUrl": employee.get_avatar_url(),
        }, status=200)
        
        response.set_cookie(
            key='sessionid',
            value=session_key,
            max_age=86400,
            httponly=False,
            samesite=None,
            secure=False,
        )
        
        return response
    else:
        print(f"❌ Authentication failed for: {email}")
        return Response({"error": "Authentication failed"}, status=401)


@api_view(["POST"])
@permission_classes([AllowAny])
def logout_user(request):
    """Logout endpoint"""
    logout(request)
    return Response({"message": "Logged out successfully"}, status=200)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_current_user(request):
    """Get current authenticated user"""
    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
        return Response({
            "id": employee.id,
            "name": employee.name,
            "email": employee.email,
            "role": employee.role,
            "about": employee.about,
            "avatarUrl": employee.get_avatar_url(),
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== USER VIEWS ====================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_users(request):
    """Get list of all active employees with last message and unread count"""
    print(f"\n🔍 /api/users/ Request")
    print(f"   User: {request.user.username}")
    
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        employees = Employee.objects.filter(is_active=True).exclude(id=current_employee.id)
        
        data = []
        for emp in employees:
            last_message = Message.objects.filter(
                Q(group__isnull=True) & (
                    (Q(sender=current_employee) & Q(receiver=emp)) |
                    (Q(sender=emp) & Q(receiver=current_employee))
                )
            ).order_by('-timestamp').first()
            
            unread_count = Message.objects.filter(
                sender=emp,
                receiver=current_employee,
                is_read=False,
                group__isnull=True
            ).count()
            
            emp_data = {
                "id": emp.id,
                "name": emp.name,
                "email": emp.email,
                "role": emp.role,
                "about": emp.about,
                "avatarUrl": emp.get_avatar_url(),
                "lastMessage": None,
                "unreadCount": unread_count,
            }
            
            if last_message:
                emp_data["lastMessage"] = {
                    "id": last_message.id,
                    "text": last_message.content if last_message.content else f"[{last_message.message_type}]",
                    "sender": "me" if last_message.sender == current_employee else "them",
                    "createdAt": last_message.timestamp.isoformat(),
                    "isRead": last_message.is_read,
                    "messageType": last_message.message_type,
                }
            
            data.append(emp_data)
        
        data.sort(
            key=lambda x: x['lastMessage']['createdAt'] if x['lastMessage'] else '1970-01-01',
            reverse=True
        )
        
        print(f"✅ Returning {len(data)} employees with messages")
        return Response(data, status=200)
        
    except Employee.DoesNotExist:
        print("⚠️ Employee not found for user, returning all")
        employees = Employee.objects.filter(is_active=True)
        data = [{
            "id": e.id,
            "name": e.name,
            "email": e.email,
            "role": e.role,
            "avatarUrl": e.get_avatar_url(),
            "lastMessage": None,
            "unreadCount": 0,
        } for e in employees]
        return Response(data, status=200)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_employee(request):
    """Create new employee"""
    try:
        current_employee = Employee.objects.get(user=request.user)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Permission denied"}, status=403)
    except Employee.DoesNotExist:
        return Response({"error": "Not authorized"}, status=403)
    
    data = request.data
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()
    name = data.get('name', '').strip()
    role = data.get('role', 'employee')
    
    if not email or not password or not name:
        return Response({"error": "Name, email and password are required"}, status=400)
    
    if len(password) < 8:
        return Response({"error": "Password must be at least 8 characters"}, status=400)
    
    if Employee.objects.filter(email=email).exists():
        return Response({"error": "Employee with this email already exists"}, status=400)
    
    try:
        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            first_name=name,
            is_active=True
        )
        
        employee = Employee.objects.create(
            name=name,
            email=email,
            password=password,
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
        if 'user' in locals():
            user.delete()
        print(f"❌ Create Employee Error: {str(e)}")
        return Response({"error": str(e)}, status=400)


# ==================== PROFILE VIEWS ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_profile(request):
    """Update user profile (name, about)"""
    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
        
        name = request.data.get('name', '').strip()
        about = request.data.get('about', '').strip()
        
        if name:
            employee.name = name
            employee.user.first_name = name
            employee.user.save()
        
        if about is not None:
            employee.about = about
        
        employee.save()
        
        return Response({
            "id": employee.id,
            "name": employee.name,
            "email": employee.email,
            "role": employee.role,
            "about": employee.about,
            "avatarUrl": employee.get_avatar_url(),
            "message": "Profile updated successfully"
        }, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def upload_profile_image(request):
    """Upload profile image"""
    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
        
        if 'image' not in request.FILES:
            return Response({"error": "No image provided"}, status=400)
        
        image = request.FILES['image']
        
        allowed_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
        if image.content_type not in allowed_types:
            return Response({"error": "Invalid file type. Use JPEG, PNG, GIF, or WebP"}, status=400)
        
        if image.size > 5 * 1024 * 1024:
            return Response({"error": "File too large. Maximum size is 5MB"}, status=400)
        
        if employee.profile_image:
            old_image_path = employee.profile_image.path
            if os.path.exists(old_image_path):
                os.remove(old_image_path)
        
        ext = image.name.split('.')[-1]
        image.name = f"profile_{employee.id}.{ext}"
        employee.profile_image = image
        employee.save()
        
        return Response({
            "id": employee.id,
            "name": employee.name,
            "email": employee.email,
            "role": employee.role,
            "about": employee.about,
            "avatarUrl": employee.get_avatar_url(),
            "message": "Profile image uploaded successfully"
        }, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)
    except Exception as e:
        print(f"❌ Upload error: {e}")
        return Response({"error": str(e)}, status=500)


# ==================== MESSAGE VIEWS ====================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_messages(request, target_id):
    """Get message history with a specific user"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        target_employee = Employee.objects.get(id=target_id, is_active=True)
        
        messages = Message.objects.filter(
            Q(group__isnull=True) & (
                (Q(sender=current_employee) & Q(receiver=target_employee)) |
                (Q(sender=target_employee) & Q(receiver=current_employee))
            )
        ).select_related('sender').prefetch_related('reactions').order_by("timestamp")

        data = []
        for m in messages:
            serialized = serialize_message(m, current_employee)
            if serialized:
                data.append(serialized)
        
        # Mark messages as read
        messages.filter(receiver=current_employee, is_read=False).update(is_read=True)
        
        return Response(data, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "User not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mark_messages_read(request, target_id):
    """Mark all messages from target user as read"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        target_employee = Employee.objects.get(id=target_id, is_active=True)
        
        updated = Message.objects.filter(
            sender=target_employee,
            receiver=current_employee,
            is_read=False,
            group__isnull=True
        ).update(is_read=True)
        
        return Response({"marked_read": updated}, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "User not found"}, status=404)


# ==================== FILE UPLOAD ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def upload_message_file(request):
    """Upload file and create message"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        if 'file' not in request.FILES:
            return Response({"error": "No file provided"}, status=400)
        
        file = request.FILES['file']
        receiver_id = request.data.get('receiver_id')
        group_id = request.data.get('group_id')
        text_content = request.data.get('content', '').strip()
        
        # Validate target
        receiver = None
        group = None
        
        if group_id:
            try:
                group = ChatGroup.objects.get(id=group_id)
                if current_employee not in group.members.all():
                    return Response({"error": "You are not a member of this group"}, status=403)
            except ChatGroup.DoesNotExist:
                return Response({"error": "Group not found"}, status=404)
        elif receiver_id:
            try:
                receiver = Employee.objects.get(id=receiver_id, is_active=True)
            except Employee.DoesNotExist:
                return Response({"error": "Receiver not found"}, status=404)
        else:
            return Response({"error": "Either receiver_id or group_id is required"}, status=400)
        
        # Validate file size (max 25MB)
        if file.size > 25 * 1024 * 1024:
            return Response({"error": "File too large. Maximum size is 25MB"}, status=400)
        
        # Determine message type from file
        mime_type, _ = mimetypes.guess_type(file.name)
        if mime_type:
            if mime_type.startswith('image/'):
                message_type = 'image'
            elif mime_type.startswith('video/'):
                message_type = 'video'
            elif mime_type.startswith('audio/'):
                message_type = 'audio'
            else:
                message_type = 'file'
        else:
            message_type = 'file'
        
        # Create message
        msg = Message.objects.create(
            sender=current_employee,
            receiver=receiver,
            group=group,
            content=text_content,
            message_type=message_type,
            file=file,
            file_name=file.name,
            file_size=file.size,
            is_read=False
        )
        
        return Response({
            "id": msg.id,
            "text": msg.content,
            "sender_id": current_employee.id,
            "sender_name": current_employee.name,
            "sender_avatar": current_employee.get_avatar_url(),
            "receiver_id": receiver.id if receiver else None,
            "group_id": group.id if group else None,
            "createdAt": msg.timestamp.isoformat(),
            "messageType": msg.message_type,
            "fileUrl": msg.get_file_url(),
            "fileName": msg.file_name,
            "fileSize": msg.file_size,
        }, status=201)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)
    except Exception as e:
        print(f"❌ File upload error: {e}")
        import traceback
        traceback.print_exc()
        return Response({"error": str(e)}, status=500)


# ==================== REACTIONS ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def add_reaction(request, message_id):
    """Add or update reaction on a message"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)
        
        # Verify user can access this message
        if message.group:
            if current_employee not in message.group.members.all():
                return Response({"error": "You are not a member of this group"}, status=403)
        else:
            if message.sender != current_employee and message.receiver != current_employee:
                return Response({"error": "You cannot react to this message"}, status=403)
        
        reaction_type = request.data.get('reaction', '').strip()
        
        if reaction_type not in ['ok', 'not_ok']:
            return Response({"error": "Invalid reaction. Use 'ok' or 'not_ok'"}, status=400)
        
        # Create or update reaction
        reaction, created = MessageReaction.objects.update_or_create(
            message=message,
            employee=current_employee,
            defaults={'reaction': reaction_type}
        )
        
        # Get updated reaction counts
        reactions = list(message.reactions.values('reaction').annotate(count=Count('reaction')))
        
        return Response({
            "message_id": message_id,
            "reaction": reaction_type,
            "reactions": {r['reaction']: r['count'] for r in reactions},
            "action": "created" if created else "updated"
        }, status=200)
        
    except Message.DoesNotExist:
        return Response({"error": "Message not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def remove_reaction(request, message_id):
    """Remove reaction from a message"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)
        
        # Delete reaction
        deleted, _ = MessageReaction.objects.filter(
            message=message,
            employee=current_employee
        ).delete()
        
        # Get updated reaction counts
        reactions = list(message.reactions.values('reaction').annotate(count=Count('reaction')))
        
        return Response({
            "message_id": message_id,
            "reactions": {r['reaction']: r['count'] for r in reactions},
            "removed": deleted > 0
        }, status=200)
        
    except Message.DoesNotExist:
        return Response({"error": "Message not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== MESSAGE EDIT/DELETE ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def edit_message(request, message_id):
    """Edit a message (sender only, within 15 minutes)"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)
        
        # Check if user is the sender
        if message.sender != current_employee:
            return Response({"error": "You can only edit your own messages"}, status=403)
        
        # Check if message can be edited (within 15 minutes)
        if not message.can_edit(current_employee):
            return Response({"error": "Message can only be edited within 15 minutes"}, status=400)
        
        # Check if already deleted
        if message.is_deleted_for_everyone:
            return Response({"error": "Cannot edit deleted message"}, status=400)
        
        new_content = request.data.get('content', '').strip()
        
        if not new_content:
            return Response({"error": "Message content cannot be empty"}, status=400)
        
        message.content = new_content
        message.is_edited = True
        message.edited_at = timezone.now()
        message.save()
        
        return Response({
            "id": message.id,
            "content": message.content,
            "isEdited": True,
            "editedAt": message.edited_at.isoformat(),
            "message": "Message edited successfully"
        }, status=200)
        
    except Message.DoesNotExist:
        return Response({"error": "Message not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def delete_message_for_me(request, message_id):
    """Delete message for current user only"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)
        
        # Verify user has access to this message
        if message.group:
            if current_employee not in message.group.members.all() and current_employee.role not in ['admin', 'superadmin']:
                return Response({"error": "You don't have access to this message"}, status=403)
        else:
            if message.sender != current_employee and message.receiver != current_employee:
                return Response({"error": "You don't have access to this message"}, status=403)
        
        # Create deletion record
        MessageDeletion.objects.get_or_create(
            message=message,
            employee=current_employee
        )
        
        return Response({
            "id": message.id,
            "deletedForMe": True,
            "message": "Message deleted for you"
        }, status=200)
        
    except Message.DoesNotExist:
        return Response({"error": "Message not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def delete_message_for_everyone(request, message_id):
    """Delete message for everyone (sender only, within 1 hour)"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)
        
        # Check if user is the sender
        if message.sender != current_employee:
            return Response({"error": "You can only delete your own messages for everyone"}, status=403)
        
        # Check if message can be deleted for everyone (within 1 hour)
        if not message.can_delete_for_everyone(current_employee):
            return Response({"error": "Message can only be deleted for everyone within 1 hour"}, status=400)
        
        # Mark as deleted for everyone
        message.is_deleted_for_everyone = True
        message.deleted_at = timezone.now()
        message.content = ""  # Clear content
        message.save()
        
        # Delete any files
        if message.file:
            message.file.delete()
        
        return Response({
            "id": message.id,
            "deletedForEveryone": True,
            "message": "Message deleted for everyone"
        }, status=200)
        
    except Message.DoesNotExist:
        return Response({"error": "Message not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== GROUP VIEWS ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_group(request):
    """Create a new group - Admin only"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Only admins can create groups"}, status=403)
        
        name = request.data.get('name', '').strip()
        description = request.data.get('description', '').strip()
        member_ids = request.data.get('members', [])
        
        if not name:
            return Response({"error": "Group name is required"}, status=400)
        
        group = ChatGroup.objects.create(
            name=name,
            description=description,
            created_by=current_employee
        )
        
        # Add creator as member
        group.members.add(current_employee)
        
        # Add other members
        if member_ids:
            members = Employee.objects.filter(id__in=member_ids, is_active=True)
            group.members.add(*members)
        
        # Log activity
        log_admin_activity(
            admin=current_employee,
            action='create_group',
            details={'group_id': group.id, 'group_name': group.name}
        )
        
        return Response({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "memberCount": group.members.count(),
            "createdBy": current_employee.name,
            "createdAt": group.created_at.isoformat(),
            "message": "Group created successfully"
        }, status=201)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_groups(request):
    """Get all groups for current user"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        if current_employee.role in ['admin', 'superadmin']:
            groups = ChatGroup.objects.all()
        else:
            groups = current_employee.group_memberships.all()
        
        data = []
        for g in groups:
            # Get last message
            last_message = g.group_messages.order_by('-timestamp').first()
            
            # Get unread count
            unread_count = g.group_messages.filter(is_read=False).exclude(sender=current_employee).count()
            
            group_data = {
                "id": g.id,
                "name": g.name,
                "description": g.description,
                "memberCount": g.members.count(),
                "createdBy": g.created_by.name if g.created_by else None,
                "createdAt": g.created_at.isoformat(),
                "avatarUrl": g.get_group_image_url(),
                "lastMessage": None,
                "unreadCount": unread_count,
                "isGroup": True,
            }
            
            if last_message:
                group_data["lastMessage"] = {
                    "id": last_message.id,
                    "text": last_message.content if last_message.content else f"[{last_message.message_type}]",
                    "sender": "me" if last_message.sender == current_employee else last_message.sender.name,
                    "createdAt": last_message.timestamp.isoformat(),
                }
            
            data.append(group_data)
        
        # Sort by last message
        data.sort(
            key=lambda x: x['lastMessage']['createdAt'] if x['lastMessage'] else '1970-01-01',
            reverse=True
        )
        
        return Response(data, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_group_details(request, group_id):
    """Get group details with members"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)
        
        # Check if user is member or admin
        is_member = current_employee in group.members.all()
        is_admin = current_employee.role in ['admin', 'superadmin']
        
        if not is_member and not is_admin:
            return Response({"error": "You are not a member of this group"}, status=403)
        
        members = [{
            "id": m.id,
            "name": m.name,
            "email": m.email,
            "avatarUrl": m.get_avatar_url(),
            "role": m.role,
            "isCreator": m == group.created_by,
        } for m in group.members.all()]
        
        return Response({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "avatarUrl": group.get_group_image_url(),
            "members": members,
            "memberCount": len(members),
            "createdBy": group.created_by.name if group.created_by else None,
            "createdById": group.created_by.id if group.created_by else None,
            "createdAt": group.created_at.isoformat(),
            "isMember": is_member,
            "isAdmin": is_admin,
        }, status=200)
        
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_group_messages(request, group_id):
    """Get messages for a specific group"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)
        
        # Check membership
        if current_employee not in group.members.all() and current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "You are not a member of this group"}, status=403)
        
        messages = group.group_messages.select_related('sender').prefetch_related('reactions').order_by('timestamp')
        
        data = []
        for m in messages:
            serialized = serialize_message(m, current_employee)
            if serialized:
                data.append(serialized)
        
        # Mark messages as read
        messages.exclude(sender=current_employee).filter(is_read=False).update(is_read=True)
        
        return Response(data, status=200)
        
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def add_group_members(request, group_id):
    """Add members to group - Admin only"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)
        
        # Only admin or group creator can add members
        if current_employee.role not in ['admin', 'superadmin'] and current_employee != group.created_by:
            return Response({"error": "Only admins or group creator can add members"}, status=403)
        
        member_ids = request.data.get('member_ids', [])
        
        if not member_ids:
            return Response({"error": "No members specified"}, status=400)
        
        members = Employee.objects.filter(id__in=member_ids, is_active=True)
        group.members.add(*members)
        
        # Log activity
        log_admin_activity(
            admin=current_employee,
            action='add_member',
            details={'group_id': group.id, 'member_ids': member_ids}
        )
        
        return Response({
            "message": f"Added {members.count()} members to group",
            "memberCount": group.members.count()
        }, status=200)
        
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def remove_group_member(request, group_id):
    """Remove a member from group - Admin only"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)
        
        if current_employee.role not in ['admin', 'superadmin'] and current_employee != group.created_by:
            return Response({"error": "Only admins or group creator can remove members"}, status=403)
        
        member_id = request.data.get('member_id')
        
        if not member_id:
            return Response({"error": "No member specified"}, status=400)
        
        try:
            member = Employee.objects.get(id=member_id)
            
            # Cannot remove creator
            if member == group.created_by:
                return Response({"error": "Cannot remove group creator"}, status=400)
            
            group.members.remove(member)
            
            # Log activity
            log_admin_activity(
                admin=current_employee,
                action='remove_member',
                target_employee=member,
                details={'group_id': group.id, 'group_name': group.name}
            )
            
            return Response({
                "message": f"Removed {member.name} from group",
                "memberCount": group.members.count()
            }, status=200)
            
        except Employee.DoesNotExist:
            return Response({"error": "Member not found"}, status=404)
        
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_group(request, group_id):
    """Update group details - Admin only"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)
        
        if current_employee.role not in ['admin', 'superadmin'] and current_employee != group.created_by:
            return Response({"error": "Only admins or group creator can update group"}, status=403)
        
        name = request.data.get('name', '').strip()
        description = request.data.get('description', '').strip()
        
        if name:
            group.name = name
        if description is not None:
            group.description = description
        
        group.save()
        
        return Response({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "message": "Group updated successfully"
        }, status=200)
        
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def leave_group(request, group_id):
    """Leave a group"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)
        
        if current_employee not in group.members.all():
            return Response({"error": "You are not a member of this group"}, status=400)
        
        if current_employee == group.created_by:
            return Response({"error": "Group creator cannot leave. Delete the group instead."}, status=400)
        
        group.members.remove(current_employee)
        
        return Response({"message": "You have left the group"}, status=200)
        
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== GOOGLE MEET ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_meet(request):
    """Create a Google Meet message with invitations"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        meet_link = request.data.get('meet_link', '').strip()
        meet_title = request.data.get('title', 'Team Meeting').strip()
        scheduled_at = request.data.get('scheduled_at')
        invitee_ids = request.data.get('invitees', [])
        receiver_id = request.data.get('receiver_id')
        group_id = request.data.get('group_id')
        save_link = request.data.get('save_link', False)
        
        if not meet_link:
            return Response({"error": "Meet link is required"}, status=400)
        
        # Validate meet link
        if not ('meet.google.com' in meet_link or 'zoom.us' in meet_link):
            return Response({"error": "Please provide a valid Google Meet or Zoom link"}, status=400)
        
        # Parse scheduled time
        scheduled_datetime = None
        if scheduled_at:
            try:
                from datetime import datetime
                scheduled_datetime = datetime.fromisoformat(scheduled_at.replace('Z', '+00:00'))
            except:
                pass
        
        # Determine target
        receiver = None
        group = None
        
        if group_id:
            try:
                group = ChatGroup.objects.get(id=group_id)
                if current_employee not in group.members.all() and current_employee.role not in ['admin', 'superadmin']:
                    return Response({"error": "You are not a member of this group"}, status=403)
            except ChatGroup.DoesNotExist:
                return Response({"error": "Group not found"}, status=404)
        elif receiver_id:
            try:
                receiver = Employee.objects.get(id=receiver_id, is_active=True)
            except Employee.DoesNotExist:
                return Response({"error": "Receiver not found"}, status=404)
        else:
            return Response({"error": "Either receiver_id or group_id is required"}, status=400)
        
        # Create message
        content = f"📅 {meet_title}"
        if scheduled_datetime:
            content += f"\n🕐 Scheduled: {scheduled_datetime.strftime('%B %d, %Y at %I:%M %p')}"
        content += f"\n🔗 {meet_link}"
        
        message = Message.objects.create(
            sender=current_employee,
            receiver=receiver,
            group=group,
            content=content,
            message_type='meet',
            meet_link=meet_link,
            meet_title=meet_title,
            meet_scheduled_at=scheduled_datetime,
        )
        
        # Create invitations for specified employees
        invitations = []
        if invitee_ids:
            invitees = Employee.objects.filter(id__in=invitee_ids, is_active=True)
            for invitee in invitees:
                if invitee != current_employee:
                    invitation, created = MeetingInvitation.objects.get_or_create(
                        message=message,
                        invitee=invitee
                    )
                    invitations.append({
                        "id": invitation.id,
                        "inviteeId": invitee.id,
                        "inviteeName": invitee.name,
                        "status": invitation.status
                    })
        elif group:
            # Invite all group members
            for member in group.members.exclude(id=current_employee.id):
                invitation, created = MeetingInvitation.objects.get_or_create(
                    message=message,
                    invitee=member
                )
                invitations.append({
                    "id": invitation.id,
                    "inviteeId": member.id,
                    "inviteeName": member.name,
                    "status": invitation.status
                })
        
        # Save link for future use if requested
        if save_link:
            saved, created = SavedMeetLink.objects.get_or_create(
                employee=current_employee,
                meet_link=meet_link,
                defaults={'title': meet_title}
            )
            if not created:
                saved.use_count = F('use_count') + 1
                saved.save()
        
        return Response({
            "id": message.id,
            "content": message.content,
            "meetLink": meet_link,
            "meetTitle": meet_title,
            "scheduledAt": scheduled_datetime.isoformat() if scheduled_datetime else None,
            "invitations": invitations,
            "createdAt": message.timestamp.isoformat(),
            "message": "Meeting created successfully"
        }, status=201)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)
    except Exception as e:
        print(f"❌ Create meet error: {e}")
        import traceback
        traceback.print_exc()
        return Response({"error": str(e)}, status=500)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_saved_meets(request):
    """Get user's saved meet links"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        saved_meets = SavedMeetLink.objects.filter(employee=current_employee)
        
        data = [{
            "id": meet.id,
            "title": meet.title,
            "meetLink": meet.meet_link,
            "useCount": meet.use_count,
            "lastUsed": meet.last_used.isoformat(),
            "createdAt": meet.created_at.isoformat(),
        } for meet in saved_meets]
        
        return Response(data, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def delete_saved_meet(request, meet_id):
    """Delete a saved meet link"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        meet = SavedMeetLink.objects.get(id=meet_id, employee=current_employee)
        meet.delete()
        
        return Response({"message": "Saved meet deleted"}, status=200)
        
    except SavedMeetLink.DoesNotExist:
        return Response({"error": "Saved meet not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def respond_to_meet_invite(request, message_id):
    """Respond to a meeting invitation"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        status = request.data.get('status', '').strip()
        
        if status not in ['accepted', 'declined', 'attended']:
            return Response({"error": "Invalid status. Use: accepted, declined, or attended"}, status=400)
        
        try:
            invitation = MeetingInvitation.objects.get(
                message_id=message_id,
                invitee=current_employee
            )
        except MeetingInvitation.DoesNotExist:
            return Response({"error": "You are not invited to this meeting"}, status=404)
        
        invitation.status = status
        invitation.responded_at = timezone.now()
        invitation.save()
        
        return Response({
            "id": invitation.id,
            "status": status,
            "respondedAt": invitation.responded_at.isoformat(),
            "message": f"You have {status} the meeting"
        }, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== ADMIN VIEWS ====================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_get_all_employees(request):
    """
    Admin only: Get all employees with their chat statistics
    Shows total messages sent/received, active chats, etc.
    """
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        # Check if user is admin
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        employees = Employee.objects.filter(is_active=True).exclude(id=current_employee.id)
        
        data = []
        for emp in employees:
            # Get message statistics
            sent_count = Message.objects.filter(sender=emp).count()
            received_count = Message.objects.filter(receiver=emp).count()
            
            # Get unique chat partners count
            chat_partners = Message.objects.filter(
                Q(sender=emp) | Q(receiver=emp),
                group__isnull=True
            ).values_list('sender', 'receiver').distinct()
            unique_partners = set()
            for sender_id, receiver_id in chat_partners:
                if sender_id != emp.id:
                    unique_partners.add(sender_id)
                if receiver_id and receiver_id != emp.id:
                    unique_partners.add(receiver_id)
            
            # Get groups count
            groups_count = emp.group_memberships.count()
            
            # Get last activity
            last_message = Message.objects.filter(
                Q(sender=emp) | Q(receiver=emp)
            ).order_by('-timestamp').first()
            
            emp_data = {
                "id": emp.id,
                "name": emp.name,
                "email": emp.email,
                "role": emp.role,
                "about": emp.about,
                "avatarUrl": emp.get_avatar_url(),
                "createdAt": emp.created_at.isoformat(),
                "stats": {
                    "messagesSent": sent_count,
                    "messagesReceived": received_count,
                    "totalMessages": sent_count + received_count,
                    "activeChatPartners": len(unique_partners),
                    "groupsJoined": groups_count,
                },
                "lastActivity": last_message.timestamp.isoformat() if last_message else None,
            }
            data.append(emp_data)
        
        # Sort by last activity
        data.sort(key=lambda x: x['lastActivity'] or '1970-01-01', reverse=True)
        
        return Response({
            "employees": data,
            "totalCount": len(data),
            "adminId": current_employee.id,
            "adminName": current_employee.name,
        }, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_view_employee_dashboard(request, employee_id):
    """
    Admin only: View an employee's chat dashboard (their contact list with messages)
    This is like "logging in as" the employee to see their chats
    """
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        # Check if user is admin
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        # Get target employee
        target_employee = Employee.objects.get(id=employee_id, is_active=True)
        
        # Log admin activity
        log_admin_activity(
            admin=current_employee,
            action='view_employee',
            target_employee=target_employee,
            details={'viewed_dashboard': True}
        )
        
        # Get all employees this employee has chatted with
        employees = Employee.objects.filter(is_active=True).exclude(id=target_employee.id)
        
        contacts = []
        for emp in employees:
            # Get conversation between target employee and this employee
            last_message = Message.objects.filter(
                Q(group__isnull=True) & (
                    (Q(sender=target_employee) & Q(receiver=emp)) |
                    (Q(sender=emp) & Q(receiver=target_employee))
                )
            ).order_by('-timestamp').first()
            
            # Count total messages
            total_messages = Message.objects.filter(
                Q(group__isnull=True) & (
                    (Q(sender=target_employee) & Q(receiver=emp)) |
                    (Q(sender=emp) & Q(receiver=target_employee))
                )
            ).count()
            
            # Only include if there's chat history
            if last_message:
                contact_data = {
                    "id": emp.id,
                    "name": emp.name,
                    "email": emp.email,
                    "role": emp.role,
                    "avatarUrl": emp.get_avatar_url(),
                    "totalMessages": total_messages,
                    "lastMessage": {
                        "id": last_message.id,
                        "text": last_message.content if last_message.content else f"[{last_message.message_type}]",
                        "sender": "target" if last_message.sender == target_employee else "them",
                        "createdAt": last_message.timestamp.isoformat(),
                        "messageType": last_message.message_type,
                    }
                }
                contacts.append(contact_data)
        
        # Sort by last message time
        contacts.sort(key=lambda x: x['lastMessage']['createdAt'], reverse=True)
        
        return Response({
            "employee": {
                "id": target_employee.id,
                "name": target_employee.name,
                "email": target_employee.email,
                "role": target_employee.role,
                "avatarUrl": target_employee.get_avatar_url(),
                "about": target_employee.about,
                "createdAt": target_employee.created_at.isoformat(),
            },
            "contacts": contacts,
            "totalContacts": len(contacts),
            "adminView": True,
            "adminId": current_employee.id,
            "adminName": current_employee.name,
        }, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_view_employee_messages(request, employee_id, target_id):
    """
    Admin only: View messages between two employees
    Returns messages from the perspective of employee_id
    """
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        # Check if user is admin
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        # Get both employees
        employee = Employee.objects.get(id=employee_id, is_active=True)
        target = Employee.objects.get(id=target_id, is_active=True)
        
        # Log admin activity
        log_admin_activity(
            admin=current_employee,
            action='view_chat',
            target_employee=employee,
            details={'viewing_chat_with': target.id, 'target_name': target.name}
        )
        
        # Get all messages between them
        messages = Message.objects.filter(
            Q(group__isnull=True) & (
                (Q(sender=employee) & Q(receiver=target)) |
                (Q(sender=target) & Q(receiver=employee))
            )
        ).select_related('sender', 'receiver').prefetch_related('reactions').order_by('timestamp')
        
        # Serialize from employee's perspective
        data = []
        for msg in messages:
            serialized = serialize_message(msg, current_employee, viewing_as_admin=True, target_employee=employee)
            if serialized:
                data.append(serialized)
        
        return Response({
            "viewingEmployee": {
                "id": employee.id,
                "name": employee.name,
                "email": employee.email,
                "avatarUrl": employee.get_avatar_url(),
            },
            "chattingWith": {
                "id": target.id,
                "name": target.name,
                "email": target.email,
                "avatarUrl": target.get_avatar_url(),
            },
            "messages": data,
            "totalMessages": len(data),
            "adminView": True,
            "adminId": current_employee.id,
            "adminName": current_employee.name,
        }, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_view_employee_groups(request, employee_id):
    """
    Admin only: View all groups an employee is part of
    """
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        # Check if user is admin
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        # Get target employee
        employee = Employee.objects.get(id=employee_id, is_active=True)
        
        # Get all groups they're a member of
        groups = employee.group_memberships.all()
        
        data = []
        for group in groups:
            last_message = group.group_messages.order_by('-timestamp').first()
            message_count = group.group_messages.count()
            
            # Count messages sent by this employee in this group
            employee_messages = group.group_messages.filter(sender=employee).count()
            
            group_data = {
                "id": group.id,
                "name": group.name,
                "description": group.description,
                "avatarUrl": group.get_group_image_url(),
                "memberCount": group.members.count(),
                "totalMessages": message_count,
                "employeeMessagesCount": employee_messages,
                "createdBy": group.created_by.name if group.created_by else None,
                "createdAt": group.created_at.isoformat(),
                "lastMessage": None,
            }
            
            if last_message:
                group_data["lastMessage"] = {
                    "text": last_message.content if last_message.content else f"[{last_message.message_type}]",
                    "sender": last_message.sender.name,
                    "createdAt": last_message.timestamp.isoformat(),
                }
            
            data.append(group_data)
        
        # Sort by last message
        data.sort(
            key=lambda x: x['lastMessage']['createdAt'] if x['lastMessage'] else '1970-01-01',
            reverse=True
        )
        
        return Response({
            "employee": {
                "id": employee.id,
                "name": employee.name,
                "avatarUrl": employee.get_avatar_url(),
            },
            "groups": data,
            "totalGroups": len(data),
            "adminView": True,
            "adminId": current_employee.id,
            "adminName": current_employee.name,
        }, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_view_employee_group_messages(request, employee_id, group_id):
    """
    Admin only: View group messages from employee's perspective
    """
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        # Check if user is admin
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        # Get employee and group
        employee = Employee.objects.get(id=employee_id, is_active=True)
        group = ChatGroup.objects.get(id=group_id)
        
        # Check if employee is member of group
        if employee not in group.members.all():
            return Response({"error": "Employee is not a member of this group"}, status=400)
        
        # Log activity
        log_admin_activity(
            admin=current_employee,
            action='view_chat',
            target_employee=employee,
            details={'viewing_group': group.id, 'group_name': group.name}
        )
        
        # Get all group messages
        messages = group.group_messages.select_related('sender').prefetch_related('reactions').order_by('timestamp')
        
        # Serialize from employee's perspective
        data = []
        for msg in messages:
            serialized = serialize_message(msg, current_employee, viewing_as_admin=True, target_employee=employee)
            if serialized:
                serialized["isFromViewedEmployee"] = msg.sender.id == employee.id
                data.append(serialized)
        
        # Get group members
        members = [{
            "id": m.id,
            "name": m.name,
            "email": m.email,
            "avatarUrl": m.get_avatar_url(),
            "role": m.role,
            "isViewedEmployee": m.id == employee.id,
        } for m in group.members.all()]
        
        return Response({
            "viewingEmployee": {
                "id": employee.id,
                "name": employee.name,
                "email": employee.email,
                "avatarUrl": employee.get_avatar_url(),
            },
            "group": {
                "id": group.id,
                "name": group.name,
                "description": group.description,
                "avatarUrl": group.get_group_image_url(),
                "memberCount": len(members),
            },
            "members": members,
            "messages": data,
            "totalMessages": len(data),
            "adminView": True,
            "adminId": current_employee.id,
            "adminName": current_employee.name,
        }, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_get_activity_log(request):
    """
    Admin only: Get admin activity log
    """
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        # Check if user is admin
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        # Get query params
        limit = int(request.GET.get('limit', 100))
        offset = int(request.GET.get('offset', 0))
        
        # Get activity logs
        logs = AdminActivityLog.objects.select_related(
            'admin', 'target_employee'
        ).order_by('-timestamp')[offset:offset+limit]
        
        total_count = AdminActivityLog.objects.count()
        
        data = [{
            "id": log.id,
            "adminId": log.admin.id,
            "adminName": log.admin.name,
            "adminAvatar": log.admin.get_avatar_url(),
            "action": log.action,
            "actionDisplay": log.get_action_display(),
            "targetEmployeeId": log.target_employee.id if log.target_employee else None,
            "targetEmployeeName": log.target_employee.name if log.target_employee else None,
            "targetEmployeeAvatar": log.target_employee.get_avatar_url() if log.target_employee else None,
            "details": log.details,
            "timestamp": log.timestamp.isoformat(),
        } for log in logs]
        
        return Response({
            "logs": data,
            "totalCount": total_count,
            "limit": limit,
            "offset": offset,
        }, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_exit_employee_view(request):
    """
    Admin only: Exit employee view mode and return to admin dashboard
    """
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        employee_id = request.data.get('employee_id')
        
        if employee_id:
            try:
                viewed_employee = Employee.objects.get(id=employee_id)
                log_admin_activity(
                    admin=current_employee,
                    action='exit_view',
                    target_employee=viewed_employee,
                    details={'action': 'exited_view'}
                )
            except Employee.DoesNotExist:
                pass
        
        return Response({
            "message": "Returned to admin dashboard",
            "adminId": current_employee.id,
            "adminName": current_employee.name,
        }, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== ADMIN STATISTICS ====================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_get_statistics(request):
    """
    Admin only: Get overall chat statistics
    """
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        # Get counts
        total_employees = Employee.objects.filter(is_active=True).count()
        total_groups = ChatGroup.objects.count()
        total_messages = Message.objects.count()
        
        # Messages today
        from datetime import datetime, timedelta
        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        messages_today = Message.objects.filter(timestamp__gte=today).count()
        
        # Messages this week
        week_ago = today - timedelta(days=7)
        messages_this_week = Message.objects.filter(timestamp__gte=week_ago).count()
        
        # Most active users (by messages sent)
        top_users = Employee.objects.filter(is_active=True).annotate(
            msg_count=Count('sent_messages')
        ).order_by('-msg_count')[:5]
        
        top_users_data = [{
            "id": u.id,
            "name": u.name,
            "avatarUrl": u.get_avatar_url(),
            "messageCount": u.msg_count,
        } for u in top_users]
        
        # Most active groups
        top_groups = ChatGroup.objects.annotate(
            msg_count=Count('group_messages')
        ).order_by('-msg_count')[:5]
        
        top_groups_data = [{
            "id": g.id,
            "name": g.name,
            "avatarUrl": g.get_group_image_url(),
            "messageCount": g.msg_count,
            "memberCount": g.members.count(),
        } for g in top_groups]
        
        return Response({
            "totalEmployees": total_employees,
            "totalGroups": total_groups,
            "totalMessages": total_messages,
            "messagesToday": messages_today,
            "messagesThisWeek": messages_this_week,
            "topUsers": top_users_data,
            "topGroups": top_groups_data,
        }, status=200)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)