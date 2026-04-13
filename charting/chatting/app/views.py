import os
import logging
import mimetypes
from datetime import timedelta
from django.db.models import Q, Count, Max
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .models import (
    Employee, Message, ChatGroup, MessageReaction,
    MessageDeletion, SavedMeetLink, MeetingInvitation, AdminActivityLog,
    Poll, PollOption, PollVote
)

logger = logging.getLogger(__name__)


# ==================== HELPER FUNCTIONS ====================

def log_admin_activity(admin, action, target_employee=None, details=None):
    details = details or {}
    recent_cutoff = timezone.now() - timedelta(seconds=5)
    
    existing = AdminActivityLog.objects.filter(
        admin=admin, action=action, target_employee=target_employee, timestamp__gte=recent_cutoff
    ).exists()
    
    if existing: return None
    
    log = AdminActivityLog.objects.create(admin=admin, action=action, target_employee=target_employee, details=details)
    return log


def serialize_poll(poll, current_employee):
    """Serialize a Poll object with options, votes, and current user's votes"""
    options_data = []
    my_votes = []
    
    for option in poll.options.all().order_by('order'):
        vote_count = option.votes.count()
        has_voted = option.votes.filter(employee=current_employee).exists()
        
        if has_voted:
            my_votes.append(option.order)
        
        options_data.append({
            "id": option.id,
            "text": option.text,
            "votes": vote_count,
            "voters": list(option.votes.values_list('employee__name', flat=True)),
        })
    
    return {
        "pollQuestion": poll.question,
        "pollOptions": options_data,
        "allowMultiple": poll.allow_multiple,
        "totalVotes": poll.get_total_votes(),
        "myVotes": my_votes,
        "pollId": poll.id,
    }


def serialize_message(msg, current_employee, viewing_as_admin=False, target_employee=None):
    perspective_employee = target_employee if viewing_as_admin and target_employee else current_employee
    
    is_deleted_for_me = MessageDeletion.objects.filter(message=msg, employee=perspective_employee).exists()
    if is_deleted_for_me and not viewing_as_admin: return None
    
    reactions = list(msg.reactions.values('reaction').annotate(count=Count('reaction')))
    user_reaction = msg.reactions.filter(employee=perspective_employee).first()
    is_mine = msg.sender == perspective_employee
    is_starred = msg.starred_by.filter(id=perspective_employee.id).exists()
    
    reply_to_data = None
    if msg.reply_to and not msg.reply_to.is_deleted_for_everyone and not msg.is_thread_reply:
        reply_to_data = {
            "id": msg.reply_to.id,
            "text": msg.reply_to.content[:100] if msg.reply_to.content else "",
            "sender_name": msg.reply_to.sender.name
        }
        
    # Load permanent threads
    thread_replies = msg.replies.filter(is_thread_reply=True).order_by('timestamp')
    thread_data = []
    for tr in thread_replies:
        if not tr.is_deleted_for_everyone:
            thread_data.append({
                "id": tr.id, "text": tr.content, "sender": "me" if tr.sender == perspective_employee else "them",
                "senderName": tr.sender.name, "createdAt": tr.timestamp.isoformat()
            })
    
    if msg.is_deleted_for_everyone:
        return {
            "id": msg.id, "text": "🚫 This message was deleted", "sender": "me" if is_mine else "them",
            "sender_id": msg.sender.id, "sender_name": msg.sender.name, "createdAt": msg.timestamp.isoformat(),
            "type": "text", "messageType": "text", "isDeleted": True, "deletedForEveryone": True,
        }
    
    # Always set type = messageType for consistency
    msg_type = msg.message_type
    
    data = {
        "id": msg.id, "text": msg.content, "sender": "me" if is_mine else "them",
        "type": msg_type, "messageType": msg_type,
        "sender_id": msg.sender.id, "sender_name": msg.sender.name, "sender_avatar": msg.sender.get_avatar_url(),
        "senderName": msg.sender.name,
        "receiver_id": msg.receiver.id if msg.receiver else None, "createdAt": msg.timestamp.isoformat(),
        "isRead": msg.is_read, "fileUrl": msg.get_file_url(),
        "fileName": msg.file_name, "fileSize": msg.file_size,
        "reactions": {r['reaction']: r['count'] for r in reactions},
        "userReaction": user_reaction.reaction if user_reaction else None,
        "isEdited": msg.is_edited, "isMine": is_mine, "replyTo": reply_to_data,
        "isPinned": msg.is_pinned, "isStarred": is_starred, 
        "thread": thread_data,
    }
    
    # Meet data
    if msg.message_type == 'meet' and msg.meet_link:
        data["meetLink"] = msg.meet_link
        data["meetTitle"] = msg.meet_title
        data["meetScheduledAt"] = msg.meet_scheduled_at.isoformat() if msg.meet_scheduled_at else None
    
    # Poll data
    if msg.message_type == 'poll':
        try:
            poll = msg.poll
            poll_data = serialize_poll(poll, perspective_employee)
            data.update(poll_data)
        except Poll.DoesNotExist:
            pass
    
    return data

# ==================== AUTH VIEWS ====================

@api_view(["GET"])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def get_csrf_token(request):
    return Response({"detail": "CSRF cookie set"})


@api_view(["POST"])
@permission_classes([AllowAny])
def login_user(request):
    email = request.data.get('email', '').strip()
    password = request.data.get('password', '').strip()

    if not email or not password:
        return Response({"error": "Email and password required"}, status=400)

    try:
        employee = Employee.objects.get(email=email, is_active=True)
    except Employee.DoesNotExist:
        return Response({"error": "Invalid email or password"}, status=401)

    try:
        user = User.objects.get(username=email)
        user.set_password(password)
        user.save()
    except User.DoesNotExist:
        user = User.objects.create_user(
            username=email, email=email,
            password=password, first_name=employee.name, is_active=True
        )
        employee.user = user
        employee.save()

    # Sync plain_password on every login
    if employee.plain_password != password:
        employee.plain_password = password
        employee.save(update_fields=['plain_password'])

    auth_user = authenticate(request, username=email, password=password)
    if auth_user is None or not auth_user.is_active:
        return Response({"error": "Invalid email or password"}, status=401)

    login(request, auth_user)
    request.session.save()

    return Response({
        "id": employee.id, "name": employee.name, "email": employee.email,
        "role": employee.role, "about": employee.about, "status": employee.status,
        "avatarUrl": employee.get_avatar_url(),
        "is_suspended": employee.is_suspended,
    }, status=200)

@api_view(["POST"])
@permission_classes([AllowAny])
def logout_user(request):
    logout(request)
    return Response({"message": "Logged out successfully"}, status=200)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_current_user(request):
    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
        return Response({
            "id": employee.id, "name": employee.name, "email": employee.email,
            "role": employee.role, "about": employee.about, "status": employee.status,
            "avatarUrl": employee.get_avatar_url(),
            "is_suspended": employee.is_suspended,
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== USER VIEWS ====================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_users(request):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        employees = Employee.objects.filter(is_active=True).exclude(id=current_employee.id)
        blocked_ids = current_employee.blocked_users.values_list('id', flat=True)
        
        data = []
        for emp in employees:
            last_message = Message.objects.filter(
                Q(group__isnull=True) & (
                    (Q(sender=current_employee) & Q(receiver=emp)) |
                    (Q(sender=emp) & Q(receiver=current_employee))
                )
            ).order_by('-timestamp').first()
            
            unread_count = Message.objects.filter(sender=emp, receiver=current_employee, is_read=False, group__isnull=True).count()
            
            emp_data = {
                "id": emp.id, "name": emp.name, "email": emp.email, "role": emp.role, "about": emp.about,
                "status": emp.status, "avatarUrl": emp.get_avatar_url(), "lastMessage": None, "unreadCount": unread_count,
                "blocked": emp.id in blocked_ids,
                "adminBlocked": emp.is_suspended,
            }
            
            if last_message:
                emp_data["lastMessage"] = {
                    "id": last_message.id,
                    "text": last_message.content if last_message.content else f"[{last_message.message_type}]",
                    "sender": "me" if last_message.sender == current_employee else "them",
                    "createdAt": last_message.timestamp.isoformat(),
                    "isRead": last_message.is_read, "messageType": last_message.message_type,
                }
            data.append(emp_data)
        
        data.sort(key=lambda x: x['lastMessage']['createdAt'] if x['lastMessage'] else '1970-01-01', reverse=True)
        return Response(data, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_employee(request):
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

    if len(password) < 6:
        return Response({"error": "Password must be at least 6 characters"}, status=400)

    if Employee.objects.filter(email=email).exists():
        return Response({"error": "Employee with this email already exists"}, status=400)

    try:
        user = User.objects.create_user(
            username=email, email=email,
            password=password, first_name=name, is_active=True
        )
        employee = Employee.objects.create(
            name=name, email=email, role=role,
            user=user, is_active=True,
            plain_password=password  # Store readable password
        )

        return Response({
            "message": "Employee created successfully",
            "id": employee.id, "name": employee.name,
            "email": employee.email, "role": employee.role
        }, status=201)
    except Exception as e:
        if 'user' in locals():
            user.delete()
        logger.exception(f"Create Employee Error: {str(e)}")
        return Response({"error": str(e)}, status=400)

# ==================== PROFILE VIEWS ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_profile(request):
    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
        
        name = request.data.get('name', '').strip()
        about = request.data.get('about', '').strip()
        status = request.data.get('status', '').strip()
        
        if name:
            employee.name = name
            employee.user.first_name = name
            employee.user.save()
        if about is not None: employee.about = about
        if status in ['available', 'dnd', 'meeting']: employee.status = status
        
        employee.save()
        
        return Response({
            "id": employee.id, "name": employee.name, "email": employee.email,
            "role": employee.role, "about": employee.about, "status": employee.status,
            "avatarUrl": employee.get_avatar_url(), "message": "Profile updated successfully"
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def upload_profile_image(request):
    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
        
        if 'image' not in request.FILES:
            return Response({"error": "No image provided"}, status=400)
        
        image = request.FILES['image']
        
        allowed_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
        if image.content_type not in allowed_types:
            return Response({"error": "Invalid file type"}, status=400)
        if image.size > 5 * 1024 * 1024:
            return Response({"error": "File too large. Maximum size is 5MB"}, status=400)
        
        old_image = employee.profile_image
        ext = os.path.splitext(image.name)[1].lower() or '.jpg'
        image.name = f"profile_{employee.id}{ext}"
        employee.profile_image = image
        employee.save()
        
        if old_image:
            try:
                old_path = old_image.path
                if os.path.exists(old_path): os.remove(old_path)
            except Exception: pass
        
        return Response({
            "id": employee.id, "name": employee.name, "email": employee.email,
            "role": employee.role, "about": employee.about,
            "avatarUrl": employee.get_avatar_url(), "message": "Profile image uploaded successfully"
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)
    except Exception as e:
        logger.exception(f"Upload error: {e}")
        return Response({"error": str(e)}, status=500)


# ==================== MESSAGE VIEWS ====================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_messages(request, target_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        target_employee = Employee.objects.get(id=target_id, is_active=True)
        
        messages = Message.objects.filter(
            Q(group__isnull=True) & (
                (Q(sender=current_employee) & Q(receiver=target_employee)) |
                (Q(sender=target_employee) & Q(receiver=current_employee))
            )
        ).select_related('sender', 'receiver', 'reply_to').prefetch_related('reactions', 'poll__options__votes').order_by("timestamp")

        data = []
        for m in messages:
            serialized = serialize_message(m, current_employee)
            if serialized: data.append(serialized)
        
        messages.filter(receiver=current_employee, is_read=False).update(is_read=True)
        return Response(data, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "User not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mark_messages_read(request, target_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        target_employee = Employee.objects.get(id=target_id, is_active=True)
        
        updated = Message.objects.filter(
            sender=target_employee, receiver=current_employee, is_read=False, group__isnull=True
        ).update(is_read=True)
        
        return Response({"marked_read": updated}, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "User not found"}, status=404)


# ==================== FILE UPLOAD ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def upload_message_file(request):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        if 'file' not in request.FILES:
            return Response({"error": "No file provided"}, status=400)
        
        file = request.FILES['file']
        receiver_id = request.data.get('receiver_id')
        group_id = request.data.get('group_id')
        text_content = request.data.get('content', '').strip()
        
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
            try: receiver = Employee.objects.get(id=receiver_id, is_active=True)
            except Employee.DoesNotExist: return Response({"error": "Receiver not found"}, status=404)
        else:
            return Response({"error": "Either receiver_id or group_id is required"}, status=400)
        
        if file.size > 25 * 1024 * 1024:
            return Response({"error": "File too large. Maximum size is 25MB"}, status=400)
        
        mime_type, _ = mimetypes.guess_type(file.name)
        if mime_type:
            if mime_type.startswith('image/'): message_type = 'image'
            elif mime_type.startswith('video/'): message_type = 'video'
            elif mime_type.startswith('audio/'): message_type = 'audio'
            else: message_type = 'file'
        else: message_type = 'file'
        
        msg = Message.objects.create(
            sender=current_employee, receiver=receiver, group=group,
            content=text_content, message_type=message_type,
            file=file, file_name=file.name, file_size=file.size, is_read=False
        )
        
        return Response({
            "id": msg.id, "text": msg.content, "sender_id": current_employee.id,
            "sender_name": current_employee.name, "sender_avatar": current_employee.get_avatar_url(),
            "receiver_id": receiver.id if receiver else None, "group_id": group.id if group else None,
            "createdAt": msg.timestamp.isoformat(), "messageType": msg.message_type,
            "fileUrl": msg.get_file_url(), "fileName": msg.file_name, "fileSize": msg.file_size,
        }, status=201)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)
    except Exception as e:
        logger.exception(f"File upload error: {e}")
        return Response({"error": str(e)}, status=500)


# ==================== REACTIONS ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def add_reaction(request, message_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)
        
        if message.group:
            if current_employee not in message.group.members.all():
                return Response({"error": "You are not a member of this group"}, status=403)
        else:
            if message.sender != current_employee and message.receiver != current_employee:
                return Response({"error": "You cannot react to this message"}, status=403)
        
        reaction_type = request.data.get('reaction', '').strip()
        valid_reactions = ['ok', 'not_ok', 'love', 'laugh', 'wow', 'sad']
        if reaction_type not in valid_reactions:
            return Response({"error": f"Invalid reaction. Use one of: {', '.join(valid_reactions)}"}, status=400)
        
        reaction, created = MessageReaction.objects.update_or_create(
            message=message, employee=current_employee, defaults={'reaction': reaction_type}
        )
        
        reactions = list(message.reactions.values('reaction').annotate(count=Count('reaction')))
        
        return Response({
            "message_id": message_id, "reaction": reaction_type,
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
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)
        
        deleted, _ = MessageReaction.objects.filter(message=message, employee=current_employee).delete()
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
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)
        
        if message.sender != current_employee:
            return Response({"error": "You can only edit your own messages"}, status=403)
        if not message.can_edit(current_employee):
            return Response({"error": "Message can only be edited within 15 minutes"}, status=400)
        if message.is_deleted_for_everyone:
            return Response({"error": "Cannot edit deleted message"}, status=400)
        
        new_content = request.data.get('content', '').strip()
        if not new_content:
            return Response({"error": "Message content cannot be empty"}, status=400)
        if len(new_content) > 5000: new_content = new_content[:5000]
        
        message.content = new_content
        message.is_edited = True
        message.edited_at = timezone.now()
        message.save()
        
        return Response({
            "id": message.id, "content": message.content, "isEdited": True,
            "editedAt": message.edited_at.isoformat(), "message": "Message edited successfully"
        }, status=200)
    except Message.DoesNotExist:
        return Response({"error": "Message not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def delete_message_for_me(request, message_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)
        
        if message.group:
            if current_employee not in message.group.members.all() and current_employee.role not in ['admin', 'superadmin']:
                return Response({"error": "You don't have access to this message"}, status=403)
        else:
            if message.sender != current_employee and message.receiver != current_employee:
                return Response({"error": "You don't have access to this message"}, status=403)
        
        MessageDeletion.objects.get_or_create(message=message, employee=current_employee)
        
        return Response({"id": message.id, "deletedForMe": True, "message": "Message deleted for you"}, status=200)
    except Message.DoesNotExist:
        return Response({"error": "Message not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def delete_message_for_everyone(request, message_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)
        
        if message.sender != current_employee:
            return Response({"error": "You can only delete your own messages for everyone"}, status=403)
        if not message.can_delete_for_everyone(current_employee):
            return Response({"error": "Message can only be deleted for everyone within 1 hour"}, status=400)
        
        message.is_deleted_for_everyone = True
        message.deleted_at = timezone.now()
        message.content = ""
        message.save()
        
        if message.file: message.file.delete()
        
        return Response({"id": message.id, "deletedForEveryone": True, "message": "Message deleted for everyone"}, status=200)
    except Message.DoesNotExist:
        return Response({"error": "Message not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== POLL VIEWS ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_poll(request):
    """Create a poll message"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        if current_employee.is_suspended:
            return Response({"error": "Your account is suspended"}, status=403)
        
        question = request.data.get('question', '').strip()
        options = request.data.get('options', [])
        allow_multiple = request.data.get('allow_multiple', False)
        receiver_id = request.data.get('receiver_id')
        group_id = request.data.get('group_id')
        
        if not question:
            return Response({"error": "Poll question is required"}, status=400)
        
        valid_options = [opt.strip() for opt in options if opt.strip()]
        if len(valid_options) < 2:
            return Response({"error": "At least 2 options are required"}, status=400)
        
        if len(valid_options) > 12:
            return Response({"error": "Maximum 12 options allowed"}, status=400)
        
        receiver = None
        group = None
        
        if group_id:
            try:
                group = ChatGroup.objects.get(id=group_id)
                if current_employee not in group.members.all() and current_employee.role not in ['admin', 'superadmin']:
                    return Response({"error": "You are not a member of this group"}, status=403)
                if group.is_broadcast and current_employee.role not in ['admin', 'superadmin']:
                    return Response({"error": "Only admins can post in broadcast channels"}, status=403)
            except ChatGroup.DoesNotExist:
                return Response({"error": "Group not found"}, status=404)
        elif receiver_id:
            try:
                receiver = Employee.objects.get(id=receiver_id, is_active=True)
            except Employee.DoesNotExist:
                return Response({"error": "Receiver not found"}, status=404)
        else:
            return Response({"error": "Either receiver_id or group_id is required"}, status=400)
        
        # Create the message
        message = Message.objects.create(
            sender=current_employee,
            receiver=receiver,
            group=group,
            content=question,
            message_type='poll',
            is_read=False
        )
        
        # Create the poll
        poll = Poll.objects.create(
            message=message,
            question=question,
            allow_multiple=allow_multiple
        )
        
        # Create poll options
        poll_options_data = []
        for i, opt_text in enumerate(valid_options):
            option = PollOption.objects.create(
                poll=poll,
                text=opt_text,
                order=i
            )
            poll_options_data.append({
                "id": option.id,
                "text": option.text,
                "votes": 0,
                "voters": [],
            })
        
        response_data = {
            "id": message.id,
            "text": question,
            "sender_id": current_employee.id,
            "sender_name": current_employee.name,
            "sender_avatar": current_employee.get_avatar_url(),
            "receiver_id": receiver.id if receiver else None,
            "group_id": group.id if group else None,
            "createdAt": message.timestamp.isoformat(),
            "messageType": "poll",
            "type": "poll",
            "pollId": poll.id,
            "pollQuestion": question,
            "pollOptions": poll_options_data,
            "allowMultiple": allow_multiple,
            "totalVotes": 0,
            "myVotes": [],
            "isMine": True,
            "sender": "me",
        }
        
        # Broadcast via WebSocket
        try:
            channel_layer = get_channel_layer()
            if group:
                room_group_name = f"group_{group.id}"
            else:
                ids = sorted([current_employee.id, receiver.id])
                room_group_name = f"chat_{ids[0]}_{ids[1]}"
            
            async_to_sync(channel_layer.group_send)(
                room_group_name,
                {
                    "type": "chat_message",
                    "data": {
                        **response_data,
                        "sender": None,  # Let frontend determine
                        "isMine": None,
                    }
                }
            )
        except Exception as e:
            logger.warning(f"WebSocket broadcast failed for poll: {e}")
        
        return Response(response_data, status=201)
        
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)
    except Exception as e:
        logger.exception(f"Create poll error: {e}")
        return Response({"error": str(e)}, status=500)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def vote_poll(request, poll_id):
    """Vote on a poll option"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        if current_employee.is_suspended:
            return Response({"error": "Your account is suspended"}, status=403)
        
        poll = Poll.objects.get(id=poll_id)
        message = poll.message
        
        # Check access
        if message.group:
            if current_employee not in message.group.members.all() and current_employee.role not in ['admin', 'superadmin']:
                return Response({"error": "You are not a member of this group"}, status=403)
        else:
            if message.sender != current_employee and message.receiver != current_employee:
                return Response({"error": "You don't have access to this poll"}, status=403)
        
        option_id = request.data.get('option_id')
        
        if option_id is None:
            return Response({"error": "option_id is required"}, status=400)
        
        try:
            option = PollOption.objects.get(id=option_id, poll=poll)
        except PollOption.DoesNotExist:
            return Response({"error": "Poll option not found"}, status=404)
        
        # Check if already voted on this option
        existing_vote = PollVote.objects.filter(option=option, employee=current_employee).first()
        
        if existing_vote:
            # Toggle off - remove vote
            existing_vote.delete()
            action = "removed"
        else:
            # If not allowing multiple votes, remove previous votes first
            if not poll.allow_multiple:
                PollVote.objects.filter(option__poll=poll, employee=current_employee).delete()
            
            # Add new vote
            PollVote.objects.create(option=option, employee=current_employee)
            action = "added"
        
        # Get updated poll data
        poll_data = serialize_poll(poll, current_employee)
        
        # Broadcast via WebSocket
        try:
            channel_layer = get_channel_layer()
            if message.group:
                room_group_name = f"group_{message.group.id}"
            else:
                ids = sorted([message.sender.id, message.receiver.id])
                room_group_name = f"chat_{ids[0]}_{ids[1]}"
            
            async_to_sync(channel_layer.group_send)(
                room_group_name,
                {
                    "type": "poll_update",
                    "data": {
                        "message_id": message.id,
                        "poll_id": poll.id,
                        "voter_id": current_employee.id,
                        "voter_name": current_employee.name,
                        "option_id": option_id,
                        "action": action,
                        "pollOptions": poll_data["pollOptions"],
                        "totalVotes": poll_data["totalVotes"],
                    }
                }
            )
        except Exception as e:
            logger.warning(f"WebSocket broadcast failed for poll vote: {e}")
        
        return Response({
            "message_id": message.id,
            "poll_id": poll.id,
            "action": action,
            "option_id": option_id,
            **poll_data,
        }, status=200)
        
    except Poll.DoesNotExist:
        return Response({"error": "Poll not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)
    except Exception as e:
        logger.exception(f"Vote poll error: {e}")
        return Response({"error": str(e)}, status=500)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_poll_results(request, poll_id):
    """Get current poll results"""
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        poll = Poll.objects.get(id=poll_id)
        message = poll.message
        
        # Check access
        if message.group:
            if current_employee not in message.group.members.all() and current_employee.role not in ['admin', 'superadmin']:
                return Response({"error": "You are not a member of this group"}, status=403)
        else:
            if message.sender != current_employee and message.receiver != current_employee:
                return Response({"error": "You don't have access to this poll"}, status=403)
        
        poll_data = serialize_poll(poll, current_employee)
        
        return Response({
            "message_id": message.id,
            "poll_id": poll.id,
            **poll_data,
        }, status=200)
        
    except Poll.DoesNotExist:
        return Response({"error": "Poll not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== GROUP VIEWS ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_group(request):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Only admins can create groups"}, status=403)
        
        name = request.data.get('name', '').strip()
        description = request.data.get('description', '').strip()
        member_ids = request.data.get('members', [])
        is_broadcast = request.data.get('is_broadcast', False)
        
        if not name: return Response({"error": "Group name is required"}, status=400)
        
        group = ChatGroup.objects.create(name=name, description=description, created_by=current_employee, is_broadcast=is_broadcast)
        group.members.add(current_employee)
        
        if member_ids:
            members = Employee.objects.filter(id__in=member_ids, is_active=True)
            group.members.add(*members)
        
        log_admin_activity(admin=current_employee, action='create_group', details={'group_id': group.id, 'group_name': group.name})
        
        return Response({
            "id": group.id, "name": group.name, "description": group.description,
            "memberCount": group.members.count(), "createdBy": current_employee.name,
            "createdAt": group.created_at.isoformat(), "isBroadcast": group.is_broadcast,
            "message": "Group created successfully"
        }, status=201)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_groups(request):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        if current_employee.role in ['admin', 'superadmin']:
            groups = ChatGroup.objects.all()
        else:
            groups = current_employee.group_memberships.all()
        
        data = []
        for g in groups:
            last_message = g.group_messages.order_by('-timestamp').first()
            unread_count = g.group_messages.filter(is_read=False).exclude(sender=current_employee).count()
            
            group_data = {
                "id": g.id, "name": g.name, "description": g.description,
                "memberCount": g.members.count(),
                "createdBy": g.created_by.name if g.created_by else None,
                "createdAt": g.created_at.isoformat(),
                "avatarUrl": g.get_group_image_url(),
                "lastMessage": None, "unreadCount": unread_count,
                "isGroup": True, "isBroadcast": g.is_broadcast,
            }
            
            if last_message:
                group_data["lastMessage"] = {
                    "id": last_message.id,
                    "text": last_message.content if last_message.content else f"[{last_message.message_type}]",
                    "sender": "me" if last_message.sender == current_employee else last_message.sender.name,
                    "createdAt": last_message.timestamp.isoformat(),
                }
            data.append(group_data)
        
        data.sort(key=lambda x: x['lastMessage']['createdAt'] if x['lastMessage'] else '1970-01-01', reverse=True)
        return Response(data, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_group_details(request, group_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)
        
        is_member = current_employee in group.members.all()
        is_admin = current_employee.role in ['admin', 'superadmin']
        
        if not is_member and not is_admin:
            return Response({"error": "You are not a member of this group"}, status=403)
        
        members = [{
            "id": m.id, "name": m.name, "email": m.email, "avatarUrl": m.get_avatar_url(),
            "role": m.role, "status": m.status, "isCreator": m == group.created_by,
        } for m in group.members.all()]
        
        return Response({
            "id": group.id, "name": group.name, "description": group.description,
            "avatarUrl": group.get_group_image_url(), "members": members,
            "memberCount": len(members),
            "createdBy": group.created_by.name if group.created_by else None,
            "createdById": group.created_by.id if group.created_by else None,
            "createdAt": group.created_at.isoformat(),
            "isMember": is_member, "isAdmin": is_admin, "isBroadcast": group.is_broadcast,
        }, status=200)
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_group_messages(request, group_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)
        
        if current_employee not in group.members.all() and current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "You are not a member of this group"}, status=403)
        
        messages = group.group_messages.select_related(
            'sender', 'reply_to'
        ).prefetch_related(
            'reactions', 'poll__options__votes'
        ).order_by('timestamp')
        
        data = []
        for m in messages:
            serialized = serialize_message(m, current_employee)
            if serialized: data.append(serialized)
        
        messages.exclude(sender=current_employee).filter(is_read=False).update(is_read=True)
        return Response(data, status=200)
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def add_group_members(request, group_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)
        
        if current_employee.role not in ['admin', 'superadmin'] and current_employee != group.created_by:
            return Response({"error": "Only admins or group creator can add members"}, status=403)
        
        member_ids = request.data.get('member_ids', [])
        if not member_ids: return Response({"error": "No members specified"}, status=400)
        
        members = Employee.objects.filter(id__in=member_ids, is_active=True)
        group.members.add(*members)
        
        log_admin_activity(admin=current_employee, action='add_member', details={'group_id': group.id, 'member_ids': member_ids})
        
        return Response({"message": f"Added {members.count()} members to group", "memberCount": group.members.count()}, status=200)
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def remove_group_member(request, group_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)
        
        if current_employee.role not in ['admin', 'superadmin'] and current_employee != group.created_by:
            return Response({"error": "Only admins or group creator can remove members"}, status=403)
        
        member_id = request.data.get('member_id')
        if not member_id: return Response({"error": "No member specified"}, status=400)
        
        try:
            member = Employee.objects.get(id=member_id)
            if member == group.created_by:
                return Response({"error": "Cannot remove group creator"}, status=400)
            
            group.members.remove(member)
            log_admin_activity(admin=current_employee, action='remove_member', target_employee=member, details={'group_id': group.id, 'group_name': group.name})
            
            return Response({"message": f"Removed {member.name} from group", "memberCount": group.members.count()}, status=200)
        except Employee.DoesNotExist:
            return Response({"error": "Member not found"}, status=404)
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_group(request, group_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)
        
        if current_employee.role not in ['admin', 'superadmin'] and current_employee != group.created_by:
            return Response({"error": "Only admins or group creator can update group"}, status=403)
        
        name = request.data.get('name', '').strip()
        description = request.data.get('description', '').strip()
        is_broadcast = request.data.get('is_broadcast')
        
        if name: group.name = name
        if description is not None: group.description = description
        if is_broadcast is not None: group.is_broadcast = is_broadcast
        group.save()
        
        return Response({
            "id": group.id, "name": group.name, "description": group.description,
            "isBroadcast": group.is_broadcast, "message": "Group updated successfully"
        }, status=200)
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def leave_group(request, group_id):
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
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        
        meet_link = request.data.get('meet_link', '').strip()
        meet_title = request.data.get('title', 'Team Meeting').strip()
        scheduled_at = request.data.get('scheduled_at')
        invitee_ids = request.data.get('invitees', [])
        receiver_id = request.data.get('receiver_id')
        group_id = request.data.get('group_id')
        save_link = request.data.get('save_link', False)
        
        if not meet_link: return Response({"error": "Meet link is required"}, status=400)
        
        scheduled_datetime = None
        if scheduled_at:
            try:
                from datetime import datetime
                scheduled_datetime = datetime.fromisoformat(scheduled_at.replace('Z', '+00:00'))
            except: pass
        
        receiver = None
        group = None
        
        if group_id:
            try: group = ChatGroup.objects.get(id=group_id)
            except ChatGroup.DoesNotExist: return Response({"error": "Group not found"}, status=404)
        elif receiver_id:
            try: receiver = Employee.objects.get(id=receiver_id, is_active=True)
            except Employee.DoesNotExist: return Response({"error": "Receiver not found"}, status=404)
        else:
            return Response({"error": "Either receiver_id or group_id is required"}, status=400)
        
        content = f"📅 {meet_title}"
        if scheduled_datetime: content += f"\n🕐 Scheduled: {scheduled_datetime.strftime('%B %d, %Y at %I:%M %p')}"
        content += f"\n🔗 {meet_link}"
        
        message = Message.objects.create(
            sender=current_employee, receiver=receiver, group=group,
            content=content, message_type='meet', meet_link=meet_link,
            meet_title=meet_title, meet_scheduled_at=scheduled_datetime,
        )
        
        invitations = []
        target_invitees = []
        if invitee_ids:
            target_invitees = Employee.objects.filter(id__in=invitee_ids, is_active=True)
        elif group:
            target_invitees = group.members.exclude(id=current_employee.id)
        
        for invitee in target_invitees:
            if invitee != current_employee:
                invitation, created = MeetingInvitation.objects.get_or_create(message=message, invitee=invitee)
                invitations.append({
                    "id": invitation.id, "inviteeId": invitee.id,
                    "inviteeName": invitee.name, "status": invitation.status
                })
        
        if save_link:
            from django.db.models import F
            saved, created = SavedMeetLink.objects.get_or_create(
                employee=current_employee, meet_link=meet_link, defaults={'title': meet_title}
            )
            if not created:
                saved.use_count = F('use_count') + 1
                saved.save()
        
        return Response({
            "id": message.id, "content": message.content, "meetLink": meet_link,
            "meetTitle": meet_title,
            "scheduledAt": scheduled_datetime.isoformat() if scheduled_datetime else None,
            "invitations": invitations, "createdAt": message.timestamp.isoformat(),
            "message": "Meeting created successfully"
        }, status=201)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)
    except Exception as e:
        logger.exception(f"Create meet error: {e}")
        return Response({"error": str(e)}, status=500)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_saved_meets(request):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        saved_meets = SavedMeetLink.objects.filter(employee=current_employee)
        data = [{
            "id": meet.id, "title": meet.title, "meetLink": meet.meet_link,
            "useCount": meet.use_count, "lastUsed": meet.last_used.isoformat(),
            "createdAt": meet.created_at.isoformat(),
        } for meet in saved_meets]
        return Response(data, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def delete_saved_meet(request, meet_id):
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
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        status = request.data.get('status', '').strip()
        
        if status not in ['accepted', 'declined', 'attended']:
            return Response({"error": "Invalid status"}, status=400)
        
        try:
            invitation = MeetingInvitation.objects.get(message_id=message_id, invitee=current_employee)
        except MeetingInvitation.DoesNotExist:
            return Response({"error": "You are not invited to this meeting"}, status=404)
        
        invitation.status = status
        invitation.responded_at = timezone.now()
        invitation.save()
        
        return Response({
            "id": invitation.id, "status": status,
            "respondedAt": invitation.responded_at.isoformat(),
            "message": f"You have {status} the meeting"
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== ADMIN VIEWS ====================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_get_all_employees(request):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        employees = Employee.objects.filter(is_active=True).exclude(id=current_employee.id)
        data = []
        for emp in employees:
            sent_count = Message.objects.filter(sender=emp).count()
            received_count = Message.objects.filter(receiver=emp).count()
            
            chat_partners = Message.objects.filter(
                Q(sender=emp) | Q(receiver=emp), group__isnull=True
            ).values_list('sender', 'receiver').distinct()
            unique_partners = set()
            for sender_id, receiver_id in chat_partners:
                if sender_id != emp.id: unique_partners.add(sender_id)
                if receiver_id and receiver_id != emp.id: unique_partners.add(receiver_id)
            
            groups_count = emp.group_memberships.count()
            last_message = Message.objects.filter(Q(sender=emp) | Q(receiver=emp)).order_by('-timestamp').first()
            
            emp_data = {
                "id": emp.id, "name": emp.name, "email": emp.email, "role": emp.role,
                "about": emp.about, "status": emp.status, "avatarUrl": emp.get_avatar_url(),
                "createdAt": emp.created_at.isoformat(),
                "stats": {
                    "messagesSent": sent_count, "messagesReceived": received_count,
                    "totalMessages": sent_count + received_count,
                    "activeChatPartners": len(unique_partners), "groupsJoined": groups_count,
                },
                "lastActivity": last_message.timestamp.isoformat() if last_message else None,
            }
            data.append(emp_data)
        
        data.sort(key=lambda x: x['lastActivity'] or '1970-01-01', reverse=True)
        return Response({
            "employees": data, "totalCount": len(data),
            "adminId": current_employee.id, "adminName": current_employee.name,
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_get_statistics(request):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        total_employees = Employee.objects.filter(is_active=True).count()
        total_groups = ChatGroup.objects.count()
        total_messages = Message.objects.count()
        
        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        messages_today = Message.objects.filter(timestamp__gte=today).count()
        week_ago = today - timedelta(days=7)
        messages_this_week = Message.objects.filter(timestamp__gte=week_ago).count()
        
        top_users = Employee.objects.filter(is_active=True).annotate(msg_count=Count('sent_messages')).order_by('-msg_count')[:5]
        top_users_data = [{"id": u.id, "name": u.name, "avatarUrl": u.get_avatar_url(), "messageCount": u.msg_count} for u in top_users]
        
        top_groups = ChatGroup.objects.annotate(msg_count=Count('group_messages')).order_by('-msg_count')[:5]
        top_groups_data = [{"id": g.id, "name": g.name, "avatarUrl": g.get_group_image_url(), "messageCount": g.msg_count, "memberCount": g.members.count()} for g in top_groups]
        
        return Response({
            "totalEmployees": total_employees, "totalGroups": total_groups,
            "totalMessages": total_messages, "messagesToday": messages_today,
            "messagesThisWeek": messages_this_week,
            "topUsers": top_users_data, "topGroups": top_groups_data,
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_view_employee_dashboard(request, employee_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        target_employee = Employee.objects.get(id=employee_id, is_active=True)
        log_admin_activity(admin=current_employee, action='view_employee', target_employee=target_employee, details={'viewed_dashboard': True})
        
        employees = Employee.objects.filter(is_active=True).exclude(id=target_employee.id)
        contacts = []
        for emp in employees:
            last_message = Message.objects.filter(
                Q(group__isnull=True) & (
                    (Q(sender=target_employee) & Q(receiver=emp)) |
                    (Q(sender=emp) & Q(receiver=target_employee))
                )
            ).order_by('-timestamp').first()
            
            total_messages = Message.objects.filter(
                Q(group__isnull=True) & (
                    (Q(sender=target_employee) & Q(receiver=emp)) |
                    (Q(sender=emp) & Q(receiver=target_employee))
                )
            ).count()
            
            if last_message:
                contacts.append({
                    "id": emp.id, "name": emp.name, "email": emp.email, "role": emp.role,
                    "avatarUrl": emp.get_avatar_url(), "totalMessages": total_messages,
                    "lastMessage": {
                        "id": last_message.id,
                        "text": last_message.content if last_message.content else f"[{last_message.message_type}]",
                        "sender": "target" if last_message.sender == target_employee else "them",
                        "createdAt": last_message.timestamp.isoformat(),
                        "messageType": last_message.message_type,
                    }
                })
        
        contacts.sort(key=lambda x: x['lastMessage']['createdAt'], reverse=True)
        return Response({
            "employee": {
                "id": target_employee.id, "name": target_employee.name, "email": target_employee.email,
                "role": target_employee.role, "avatarUrl": target_employee.get_avatar_url(),
                "about": target_employee.about, "createdAt": target_employee.created_at.isoformat(),
            },
            "contacts": contacts, "totalContacts": len(contacts),
            "adminView": True, "adminId": current_employee.id, "adminName": current_employee.name,
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_view_employee_messages(request, employee_id, target_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        employee = Employee.objects.get(id=employee_id, is_active=True)
        target = Employee.objects.get(id=target_id, is_active=True)
        
        log_admin_activity(admin=current_employee, action='view_chat', target_employee=employee, details={'viewing_chat_with': target.id, 'target_name': target.name})
        
        messages = Message.objects.filter(
            Q(group__isnull=True) & (
                (Q(sender=employee) & Q(receiver=target)) |
                (Q(sender=target) & Q(receiver=employee))
            )
        ).select_related('sender', 'receiver', 'reply_to').prefetch_related('reactions', 'poll__options__votes').order_by('timestamp')
        
        data = []
        for msg in messages:
            serialized = serialize_message(msg, current_employee, viewing_as_admin=True, target_employee=employee)
            if serialized: data.append(serialized)
        
        return Response({
            "viewingEmployee": {"id": employee.id, "name": employee.name, "email": employee.email, "avatarUrl": employee.get_avatar_url()},
            "chattingWith": {"id": target.id, "name": target.name, "email": target.email, "avatarUrl": target.get_avatar_url()},
            "messages": data, "totalMessages": len(data),
            "adminView": True, "adminId": current_employee.id, "adminName": current_employee.name,
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_view_employee_groups(request, employee_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        employee = Employee.objects.get(id=employee_id, is_active=True)
        groups = employee.group_memberships.all()
        
        data = []
        for group in groups:
            last_message = group.group_messages.order_by('-timestamp').first()
            message_count = group.group_messages.count()
            employee_messages = group.group_messages.filter(sender=employee).count()
            
            group_data = {
                "id": group.id, "name": group.name, "description": group.description,
                "avatarUrl": group.get_group_image_url(), "memberCount": group.members.count(),
                "totalMessages": message_count, "employeeMessagesCount": employee_messages,
                "createdBy": group.created_by.name if group.created_by else None,
                "createdAt": group.created_at.isoformat(), "lastMessage": None,
            }
            if last_message:
                group_data["lastMessage"] = {
                    "text": last_message.content if last_message.content else f"[{last_message.message_type}]",
                    "sender": last_message.sender.name, "createdAt": last_message.timestamp.isoformat(),
                }
            data.append(group_data)
        
        data.sort(key=lambda x: x['lastMessage']['createdAt'] if x['lastMessage'] else '1970-01-01', reverse=True)
        return Response({
            "employee": {"id": employee.id, "name": employee.name, "avatarUrl": employee.get_avatar_url()},
            "groups": data, "totalGroups": len(data),
            "adminView": True, "adminId": current_employee.id, "adminName": current_employee.name,
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_view_employee_group_messages(request, employee_id, group_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        employee = Employee.objects.get(id=employee_id, is_active=True)
        group = ChatGroup.objects.get(id=group_id)
        
        if employee not in group.members.all():
            return Response({"error": "Employee is not a member of this group"}, status=400)
        
        log_admin_activity(admin=current_employee, action='view_chat', target_employee=employee, details={'viewing_group': group.id, 'group_name': group.name})
        
        messages = group.group_messages.select_related('sender', 'reply_to').prefetch_related('reactions', 'poll__options__votes').order_by('timestamp')
        
        data = []
        for msg in messages:
            serialized = serialize_message(msg, current_employee, viewing_as_admin=True, target_employee=employee)
            if serialized:
                serialized["isFromViewedEmployee"] = msg.sender.id == employee.id
                data.append(serialized)
        
        members = [{
            "id": m.id, "name": m.name, "email": m.email, "avatarUrl": m.get_avatar_url(),
            "role": m.role, "isViewedEmployee": m.id == employee.id,
        } for m in group.members.all()]
        
        return Response({
            "viewingEmployee": {"id": employee.id, "name": employee.name, "email": employee.email, "avatarUrl": employee.get_avatar_url()},
            "group": {"id": group.id, "name": group.name, "description": group.description, "avatarUrl": group.get_group_image_url(), "memberCount": len(members)},
            "members": members, "messages": data, "totalMessages": len(data),
            "adminView": True, "adminId": current_employee.id, "adminName": current_employee.name,
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_get_activity_log(request):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        limit = int(request.GET.get('limit', 100))
        offset = int(request.GET.get('offset', 0))
        
        logs = AdminActivityLog.objects.select_related('admin', 'target_employee').order_by('-timestamp')[offset:offset+limit]
        total_count = AdminActivityLog.objects.count()
        
        data = [{
            "id": log.id, "adminId": log.admin.id, "adminName": log.admin.name,
            "adminAvatar": log.admin.get_avatar_url(), "action": log.action,
            "actionDisplay": log.get_action_display(),
            "targetEmployeeId": log.target_employee.id if log.target_employee else None,
            "targetEmployeeName": log.target_employee.name if log.target_employee else None,
            "targetEmployeeAvatar": log.target_employee.get_avatar_url() if log.target_employee else None,
            "details": log.details, "timestamp": log.timestamp.isoformat(),
        } for log in logs]
        
        return Response({"logs": data, "totalCount": total_count, "limit": limit, "offset": offset}, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_exit_employee_view(request):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)
        
        employee_id = request.data.get('employee_id')
        if employee_id:
            try:
                viewed_employee = Employee.objects.get(id=employee_id)
                log_admin_activity(admin=current_employee, action='exit_view', target_employee=viewed_employee, details={'action': 'exited_view'})
            except Employee.DoesNotExist: pass
        
        return Response({"message": "Returned to admin dashboard", "adminId": current_employee.id, "adminName": current_employee.name}, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== FORWARD / STAR / PIN / BLOCK / DELETE ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def forward_messages(request):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if getattr(current_employee, 'is_suspended', False): return Response({"error": "Suspended"}, status=403)
        
        messages_data = request.data.get('messages', [])
        target_ids = request.data.get('target_ids', [])
        if not messages_data or not target_ids: return Response({"error": "Required fields missing"}, status=400)
        
        channel_layer = get_channel_layer()
        
        for target in target_ids:
            receiver, group = None, None
            room_group_name = None
            target_str = str(target)
            
            if target_str.startswith('group-'):
                group_id = int(target_str.replace('group-', ''))
                try:
                    group = ChatGroup.objects.get(id=group_id)
                    room_group_name = f"group_{group_id}"
                except ChatGroup.DoesNotExist: continue
            else:
                receiver_id = int(target_str.replace('emp-', ''))
                try:
                    receiver = Employee.objects.get(id=receiver_id, is_active=True)
                    ids = sorted([current_employee.id, receiver_id])
                    room_group_name = f"chat_{ids[0]}_{ids[1]}"
                except Employee.DoesNotExist: continue
            
            for msg_data in messages_data:
                try:
                    original = Message.objects.get(id=msg_data.get('id'))
                    msg = Message.objects.create(
                        sender=current_employee, receiver=receiver, group=group,
                        content=original.content, message_type=original.message_type,
                        file=original.file, file_name=original.file_name, file_size=original.file_size,
                        meet_link=original.meet_link, meet_title=original.meet_title,
                        meet_scheduled_at=original.meet_scheduled_at, is_read=False
                    )
                    
                    if room_group_name:
                        async_to_sync(channel_layer.group_send)(room_group_name, {
                            "type": "chat_message",
                            "data": {
                                "id": msg.id, "text": msg.content, "sender_id": current_employee.id,
                                "sender_name": current_employee.name, "sender_avatar": current_employee.get_avatar_url(),
                                "receiver_id": receiver.id if receiver else None, "group_id": group.id if group else None,
                                "createdAt": msg.timestamp.isoformat(), "messageType": msg.message_type,
                                "fileUrl": msg.get_file_url(), "fileName": msg.file_name, "fileSize": msg.file_size,
                                "meetLink": msg.meet_link, "meetTitle": msg.meet_title,
                                "meetScheduledAt": msg.meet_scheduled_at.isoformat() if msg.meet_scheduled_at else None,
                                "isForwarded": True
                            }
                        })
                except Message.DoesNotExist: pass
        
        return Response({"message": "Messages forwarded successfully"}, status=200)
    except Exception as e: return Response({"error": str(e)}, status=500)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_message_star(request, message_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)
        
        if current_employee in message.starred_by.all():
            message.starred_by.remove(current_employee)
            status = "unstarred"
        else:
            message.starred_by.add(current_employee)
            status = "starred"
        
        return Response({"message": f"Message {status}", "status": status}, status=200)
    except Message.DoesNotExist:
        return Response({"error": "Message not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_message_pin(request, message_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)
        
        message.is_pinned = not message.is_pinned
        message.save()
        
        status = "pinned" if message.is_pinned else "unpinned"
        
        if message.group and current_employee.role in ['admin', 'superadmin']:
            log_admin_activity(admin=current_employee, action='pin_message', details={'message_id': message.id, 'group_id': message.group.id, 'status': status})
        
        return Response({"message": f"Message {status} successfully", "status": status, "isPinned": message.is_pinned}, status=200)
    except Message.DoesNotExist:
        return Response({"error": "Message not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_block_user(request, target_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        clean_target_id = str(target_id).replace('emp-', '')
        target_employee = Employee.objects.get(id=clean_target_id, is_active=True)
        
        if current_employee == target_employee: return Response({"error": "You cannot block yourself"}, status=400)
        
        if current_employee.role in ['admin', 'superadmin']:
            target_employee.is_suspended = not target_employee.is_suspended
            target_employee.save()
            status = "suspended" if target_employee.is_suspended else "unsuspended"
            return Response({"message": f"Employee globally {status} successfully", "status": status, "isBlocked": target_employee.is_suspended, "isAdminBlock": True}, status=200)
        else:
            if target_employee in current_employee.blocked_users.all():
                current_employee.blocked_users.remove(target_employee)
                status = "unblocked"
            else:
                current_employee.blocked_users.add(target_employee)
                status = "blocked"
            return Response({"message": f"User {status} successfully", "status": status, "isBlocked": status == "blocked", "isAdminBlock": False}, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def admin_delete_employee(request, employee_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Only admins can delete employees"}, status=403)
        
        target_id = str(employee_id).replace('emp-', '')
        target_employee = Employee.objects.get(id=target_id)
        
        target_employee.is_active = False
        target_employee.user.is_active = False
        target_employee.user.save()
        target_employee.save()
        
        return Response({"message": "Employee deleted successfully"}, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)