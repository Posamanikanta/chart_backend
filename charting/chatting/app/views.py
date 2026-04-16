import os
import logging
import mimetypes
import random
from datetime import timedelta, datetime
from django.db.models import Q, Count
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .models import (
    Employee, Message, ChatGroup, MessageReaction,
    MessageDeletion, SavedMeetLink, MeetingInvitation, AdminActivityLog,
    Poll, PollOption, PollVote
)

logger = logging.getLogger(__name__)


# ==================== HELPER FUNCTIONS ====================

def get_tokens_for_employee(employee):
    """Generate JWT access + refresh tokens for an employee."""
    if not employee.user:
        user = User.objects.create_user(
            username=employee.email,
            email=employee.email,
            first_name=employee.name,
            is_active=True
        )
        user.set_unusable_password()
        user.save()
        employee.user = user
        employee.save(update_fields=['user'])

    refresh = RefreshToken.for_user(employee.user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }


def log_admin_activity(admin, action, target_employee=None, details=None):
    details = details or {}
    recent_cutoff = timezone.now() - timedelta(seconds=5)

    existing = AdminActivityLog.objects.filter(
        admin=admin,
        action=action,
        target_employee=target_employee,
        timestamp__gte=recent_cutoff
    ).exists()

    if existing:
        return None

    return AdminActivityLog.objects.create(
        admin=admin,
        action=action,
        target_employee=target_employee,
        details=details
    )


def serialize_poll(poll, current_employee):
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


def serialize_message(
    msg, current_employee, viewing_as_admin=False, target_employee=None
):
    perspective_employee = (
        target_employee
        if viewing_as_admin and target_employee
        else current_employee
    )

    is_deleted_for_me = MessageDeletion.objects.filter(
        message=msg, employee=perspective_employee
    ).exists()
    if is_deleted_for_me and not viewing_as_admin:
        return None

    reactions = list(
        msg.reactions.values('reaction').annotate(count=Count('reaction'))
    )
    user_reaction = msg.reactions.filter(employee=perspective_employee).first()
    is_mine = msg.sender == perspective_employee
    is_starred = msg.starred_by.filter(id=perspective_employee.id).exists()

    reply_to_data = None
    if (msg.reply_to
            and not msg.reply_to.is_deleted_for_everyone
            and not msg.is_thread_reply):
        reply_to_data = {
            "id": msg.reply_to.id,
            "text": msg.reply_to.content[:100] if msg.reply_to.content else "",
            "sender_name": msg.reply_to.sender.name
        }

    thread_replies = msg.replies.filter(is_thread_reply=True).order_by('timestamp')
    thread_data = []
    for tr in thread_replies:
        if not tr.is_deleted_for_everyone:
            thread_data.append({
                "id": tr.id,
                "text": tr.content,
                "sender": "me" if tr.sender == perspective_employee else "them",
                "senderName": tr.sender.name,
                "createdAt": tr.timestamp.isoformat()
            })

    if msg.is_deleted_for_everyone:
        return {
            "id": msg.id,
            "text": "🚫 This message was deleted",
            "sender": "me" if is_mine else "them",
            "sender_id": msg.sender.id,
            "sender_name": msg.sender.name,
            "createdAt": msg.timestamp.isoformat(),
            "type": "text",
            "messageType": "text",
            "isDeleted": True,
            "deletedForEveryone": True,
        }

    msg_type = msg.message_type

    data = {
        "id": msg.id,
        "text": msg.content,
        "sender": "me" if is_mine else "them",
        "type": msg_type,
        "messageType": msg_type,
        "sender_id": msg.sender.id,
        "sender_name": msg.sender.name,
        "sender_avatar": msg.sender.get_avatar_url(),
        "senderName": msg.sender.name,
        "receiver_id": msg.receiver.id if msg.receiver else None,
        "createdAt": msg.timestamp.isoformat(),
        "isRead": msg.is_read,
        "fileUrl": msg.get_file_url(),
        "fileName": msg.file_name,
        "fileSize": msg.file_size,
        "reactions": {r['reaction']: r['count'] for r in reactions},
        "userReaction": user_reaction.reaction if user_reaction else None,
        "isEdited": msg.is_edited,
        "isMine": is_mine,
        "replyTo": reply_to_data,
        "isPinned": msg.is_pinned,
        "isStarred": is_starred,
        "thread": thread_data,
    }

    if msg.message_type == 'meet' and msg.meet_link:
        data["meetLink"] = msg.meet_link
        data["meetTitle"] = msg.meet_title
        data["meetScheduledAt"] = (
            msg.meet_scheduled_at.isoformat() if msg.meet_scheduled_at else None
        )

    if msg.message_type == 'poll':
        try:
            poll_data = serialize_poll(msg.poll, perspective_employee)
            data.update(poll_data)
        except Poll.DoesNotExist:
            pass

    return data


# ==================== AUTH VIEWS ====================

@api_view(["POST"])
@permission_classes([AllowAny])
def login_user(request):
    email = request.data.get('email', '').strip()
    password = request.data.get('password', '').strip()

    if not email or not password:
        return Response(
            {"error": "Email and password are required"}, status=400
        )

    try:
        employee = Employee.objects.get(email=email, is_active=True)
    except Employee.DoesNotExist:
        return Response({"error": "Invalid email or password"}, status=401)

    if employee.password != password:
        return Response({"error": "Invalid email or password"}, status=401)

    if employee.is_suspended:
        return Response(
            {"error": "Your account has been suspended. Contact admin."},
            status=403
        )

    # Ensure Django User exists for JWT
    if not employee.user:
        user = User.objects.create_user(
            username=employee.email,
            email=employee.email,
            first_name=employee.name,
            is_active=True
        )
        user.set_unusable_password()
        user.save()
        employee.user = user
        employee.save(update_fields=['user'])

    tokens = get_tokens_for_employee(employee)

    employee.is_online = True
    employee.save(update_fields=['is_online'])

    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            "online_status",
            {
                "type": "online_status_update",
                "data": {
                    "employee_id": employee.id,
                    "is_online": True,
                    "last_seen": None,
                }
            }
        )
    except Exception:
        pass

    return Response({
        "id": employee.id,
        "name": employee.name,
        "email": employee.email,
        "role": employee.role,
        "about": employee.about,
        "status": employee.status,
        "avatarUrl": employee.get_avatar_url(),
        "is_suspended": employee.is_suspended,
        "access": tokens['access'],
        "refresh": tokens['refresh'],
    }, status=200)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout_user(request):
    try:
        employee = Employee.objects.get(user=request.user)
        employee.is_online = False
        employee.last_seen = timezone.now()
        employee.save(update_fields=['is_online', 'last_seen'])

        try:
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                "online_status",
                {
                    "type": "online_status_update",
                    "data": {
                        "employee_id": employee.id,
                        "is_online": False,
                        "last_seen": employee.last_seen.isoformat(),
                    }
                }
            )
        except Exception:
            pass
    except Employee.DoesNotExist:
        pass

    # Blacklist the refresh token
    try:
        refresh_token = request.data.get("refresh")
        if refresh_token:
            token = RefreshToken(refresh_token)
            token.blacklist()
    except Exception:
        pass

    return Response({"message": "Logged out successfully"}, status=200)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def refresh_token(request):
    """Refresh access token using refresh token."""
    try:
        refresh = request.data.get("refresh")
        if not refresh:
            return Response({"error": "Refresh token required"}, status=400)

        token = RefreshToken(refresh)
        return Response({
            "access": str(token.access_token),
            "refresh": str(token),
        }, status=200)
    except Exception as e:
        return Response({"error": "Invalid or expired refresh token"}, status=401)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_current_user(request):
    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
        return Response({
            "id": employee.id,
            "name": employee.name,
            "email": employee.email,
            "role": employee.role,
            "about": employee.about,
            "status": employee.status,
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
        employees = Employee.objects.filter(is_active=True).exclude(
            id=current_employee.id
        )
        blocked_ids = current_employee.blocked_users.values_list('id', flat=True)

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
                "status": emp.status,
                "avatarUrl": emp.get_avatar_url(),
                "lastMessage": None,
                "unreadCount": unread_count,
                "blocked": emp.id in blocked_ids,
                "adminBlocked": emp.is_suspended,
                "isOnline": emp.is_online,
                "lastSeen": emp.last_seen.isoformat() if emp.last_seen else None,
            }

            if last_message:
                emp_data["lastMessage"] = {
                    "id": last_message.id,
                    "text": (
                        last_message.content
                        if last_message.content
                        else f"[{last_message.message_type}]"
                    ),
                    "sender": (
                        "me" if last_message.sender == current_employee else "them"
                    ),
                    "createdAt": last_message.timestamp.isoformat(),
                    "isRead": last_message.is_read,
                    "messageType": last_message.message_type,
                }
            data.append(emp_data)

        data.sort(
            key=lambda x: (
                x['lastMessage']['createdAt'] if x['lastMessage'] else '1970-01-01'
            ),
            reverse=True
        )
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

    email = request.data.get('email', '').strip()
    password = request.data.get('password', '').strip()
    name = request.data.get('name', '').strip()
    role = request.data.get('role', 'employee')

    if not email or not password or not name:
        return Response(
            {"error": "Name, email and password are required"}, status=400
        )

    if len(password) < 6:
        return Response(
            {"error": "Password must be at least 6 characters"}, status=400
        )

    if Employee.objects.filter(email=email).exists():
        return Response(
            {"error": "Employee with this email already exists"}, status=400
        )

    try:
        user = User.objects.create_user(
            username=email,
            email=email,
            first_name=name,
            is_active=True
        )
        user.set_unusable_password()
        user.save()

        employee = Employee.objects.create(
            name=name,
            email=email,
            role=role,
            password=password,
            user=user,
            is_active=True,
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
            if employee.user:
                employee.user.first_name = name
                employee.user.save()
        if about is not None:
            employee.about = about
        if status in ['available', 'dnd', 'meeting']:
            employee.status = status

        employee.save()

        return Response({
            "id": employee.id,
            "name": employee.name,
            "email": employee.email,
            "role": employee.role,
            "about": employee.about,
            "status": employee.status,
            "avatarUrl": employee.get_avatar_url(),
            "message": "Profile updated successfully"
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
            return Response(
                {"error": "File too large. Maximum size is 5MB"}, status=400
            )

        old_image = employee.profile_image
        ext = os.path.splitext(image.name)[1].lower() or '.jpg'
        image.name = f"profile_{employee.id}{ext}"
        employee.profile_image = image
        employee.save()

        if old_image:
            try:
                old_path = old_image.path
                if os.path.exists(old_path):
                    os.remove(old_path)
            except Exception:
                pass

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
        ).select_related(
            'sender', 'receiver', 'reply_to'
        ).prefetch_related(
            'reactions', 'poll__options__votes'
        ).order_by("timestamp")

        data = []
        for m in messages:
            serialized = serialize_message(m, current_employee)
            if serialized:
                data.append(serialized)

        messages.filter(
            receiver=current_employee, is_read=False
        ).update(is_read=True)
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
                    return Response(
                        {"error": "You are not a member of this group"}, status=403
                    )
                if not group.can_employee_chat(current_employee):
                    return Response({
                        "error": "You don't have permission to send messages in this group",
                        "chatRestricted": True,
                        "chatPermission": group.chat_permission
                    }, status=403)
            except ChatGroup.DoesNotExist:
                return Response({"error": "Group not found"}, status=404)
        elif receiver_id:
            try:
                receiver = Employee.objects.get(id=receiver_id, is_active=True)
            except Employee.DoesNotExist:
                return Response({"error": "Receiver not found"}, status=404)
        else:
            return Response(
                {"error": "Either receiver_id or group_id is required"}, status=400
            )

        if file.size > 25 * 1024 * 1024:
            return Response(
                {"error": "File too large. Maximum size is 25MB"}, status=400
            )

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
                return Response(
                    {"error": "You are not a member of this group"}, status=403
                )
        else:
            if (message.sender != current_employee
                    and message.receiver != current_employee):
                return Response(
                    {"error": "You cannot react to this message"}, status=403
                )

        reaction_type = request.data.get('reaction', '').strip()
        valid_reactions = ['ok', 'not_ok', 'love', 'laugh', 'wow', 'sad']
        if reaction_type not in valid_reactions:
            return Response({"error": "Invalid reaction"}, status=400)

        reaction, created = MessageReaction.objects.update_or_create(
            message=message,
            employee=current_employee,
            defaults={'reaction': reaction_type}
        )

        reactions = list(
            message.reactions.values('reaction').annotate(count=Count('reaction'))
        )

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
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)

        deleted, _ = MessageReaction.objects.filter(
            message=message, employee=current_employee
        ).delete()

        reactions = list(
            message.reactions.values('reaction').annotate(count=Count('reaction'))
        )

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
            return Response(
                {"error": "You can only edit your own messages"}, status=403
            )
        if not message.can_edit(current_employee):
            return Response(
                {"error": "Message can only be edited within 15 minutes"}, status=400
            )
        if message.is_deleted_for_everyone:
            return Response({"error": "Cannot edit deleted message"}, status=400)

        new_content = request.data.get('content', '').strip()
        if not new_content:
            return Response(
                {"error": "Message content cannot be empty"}, status=400
            )
        if len(new_content) > 5000:
            new_content = new_content[:5000]

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
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)

        if message.group:
            if (current_employee not in message.group.members.all()
                    and current_employee.role not in ['admin', 'superadmin']):
                return Response(
                    {"error": "You don't have access to this message"}, status=403
                )
        else:
            if (message.sender != current_employee
                    and message.receiver != current_employee):
                return Response(
                    {"error": "You don't have access to this message"}, status=403
                )

        MessageDeletion.objects.get_or_create(
            message=message, employee=current_employee
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
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)

        if message.sender != current_employee:
            return Response(
                {"error": "You can only delete your own messages for everyone"},
                status=403
            )
        if not message.can_delete_for_everyone(current_employee):
            return Response(
                {"error": "Message can only be deleted for everyone within 1 hour"},
                status=400
            )

        message.is_deleted_for_everyone = True
        message.deleted_at = timezone.now()
        message.content = ""
        message.save()

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


# ==================== POLL VIEWS ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_poll(request):
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
            return Response(
                {"error": "At least 2 options are required"}, status=400
            )
        if len(valid_options) > 12:
            return Response(
                {"error": "Maximum 12 options allowed"}, status=400
            )

        receiver = None
        group = None

        if group_id:
            try:
                group = ChatGroup.objects.get(id=group_id)
                if (current_employee not in group.members.all()
                        and current_employee.role not in ['admin', 'superadmin']):
                    return Response(
                        {"error": "You are not a member of this group"}, status=403
                    )
                if not group.can_employee_chat(current_employee):
                    return Response({
                        "error": "You don't have permission to send messages in this group",
                        "chatRestricted": True,
                        "chatPermission": group.chat_permission
                    }, status=403)
            except ChatGroup.DoesNotExist:
                return Response({"error": "Group not found"}, status=404)
        elif receiver_id:
            try:
                receiver = Employee.objects.get(id=receiver_id, is_active=True)
            except Employee.DoesNotExist:
                return Response({"error": "Receiver not found"}, status=404)
        else:
            return Response(
                {"error": "Either receiver_id or group_id is required"}, status=400
            )

        message = Message.objects.create(
            sender=current_employee,
            receiver=receiver,
            group=group,
            content=question,
            message_type='poll',
            is_read=False
        )

        poll = Poll.objects.create(
            message=message,
            question=question,
            allow_multiple=allow_multiple
        )

        poll_options_data = []
        for i, opt_text in enumerate(valid_options):
            option = PollOption.objects.create(poll=poll, text=opt_text, order=i)
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
                    "data": {**response_data, "sender": None, "isMine": None}
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
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)

        if current_employee.is_suspended:
            return Response({"error": "Your account is suspended"}, status=403)

        poll = Poll.objects.get(id=poll_id)
        message = poll.message

        if message.group:
            if (current_employee not in message.group.members.all()
                    and current_employee.role not in ['admin', 'superadmin']):
                return Response(
                    {"error": "You are not a member of this group"}, status=403
                )
        else:
            if (message.sender != current_employee
                    and message.receiver != current_employee):
                return Response(
                    {"error": "You don't have access to this poll"}, status=403
                )

        option_id = request.data.get('option_id')
        if option_id is None:
            return Response({"error": "option_id is required"}, status=400)

        try:
            option = PollOption.objects.get(id=option_id, poll=poll)
        except PollOption.DoesNotExist:
            return Response({"error": "Poll option not found"}, status=404)

        existing_vote = PollVote.objects.filter(
            option=option, employee=current_employee
        ).first()

        if existing_vote:
            existing_vote.delete()
            action = "removed"
        else:
            if not poll.allow_multiple:
                PollVote.objects.filter(
                    option__poll=poll, employee=current_employee
                ).delete()
            PollVote.objects.create(option=option, employee=current_employee)
            action = "added"

        poll_data = serialize_poll(poll, current_employee)

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
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        poll = Poll.objects.get(id=poll_id)
        message = poll.message

        if message.group:
            if (current_employee not in message.group.members.all()
                    and current_employee.role not in ['admin', 'superadmin']):
                return Response(
                    {"error": "You are not a member of this group"}, status=403
                )
        else:
            if (message.sender != current_employee
                    and message.receiver != current_employee):
                return Response(
                    {"error": "You don't have access to this poll"}, status=403
                )

        poll_data = serialize_poll(poll, current_employee)
        return Response(
            {"message_id": message.id, "poll_id": poll.id, **poll_data}, status=200
        )

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
            return Response(
                {"error": "Only admins can create groups"}, status=403
            )

        name = request.data.get('name', '').strip()
        description = request.data.get('description', '').strip()
        member_ids = request.data.get('members', [])
        is_broadcast = request.data.get('is_broadcast', False)
        chat_permission = request.data.get('chat_permission', 'all')
        allowed_chatter_ids = request.data.get('allowed_chatters', [])

        if chat_permission not in ['all', 'selected', 'admins_only']:
            chat_permission = 'all'

        if not name:
            return Response({"error": "Group name is required"}, status=400)

        group = ChatGroup.objects.create(
            name=name,
            description=description,
            created_by=current_employee,
            is_broadcast=is_broadcast,
            chat_permission=chat_permission,
        )
        group.members.add(current_employee)

        if member_ids:
            members = Employee.objects.filter(id__in=member_ids, is_active=True)
            group.members.add(*members)

        if chat_permission == 'selected' and allowed_chatter_ids:
            allowed_chatters = Employee.objects.filter(
                id__in=allowed_chatter_ids, is_active=True
            )
            group.allowed_chatters.set(allowed_chatters)

        log_admin_activity(
            admin=current_employee,
            action='create_group',
            details={
                'group_id': group.id,
                'group_name': group.name,
                'chat_permission': chat_permission,
            }
        )

        return Response({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "memberCount": group.members.count(),
            "createdBy": current_employee.name,
            "createdAt": group.created_at.isoformat(),
            "isBroadcast": group.is_broadcast,
            **group.get_chat_permission_info(),
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
            unread_count = g.group_messages.filter(
                is_read=False
            ).exclude(sender=current_employee).count()

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
                "isBroadcast": g.is_broadcast,
                "canChat": g.can_employee_chat(current_employee),
                **g.get_chat_permission_info(),
            }

            if last_message:
                group_data["lastMessage"] = {
                    "id": last_message.id,
                    "text": (
                        last_message.content
                        if last_message.content
                        else f"[{last_message.message_type}]"
                    ),
                    "sender": (
                        "me"
                        if last_message.sender == current_employee
                        else last_message.sender.name
                    ),
                    "createdAt": last_message.timestamp.isoformat(),
                }
            data.append(group_data)

        data.sort(
            key=lambda x: (
                x['lastMessage']['createdAt'] if x['lastMessage'] else '1970-01-01'
            ),
            reverse=True
        )
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
            return Response(
                {"error": "You are not a member of this group"}, status=403
            )

        members = []
        for m in group.members.all():
            members.append({
                "id": m.id,
                "name": m.name,
                "email": m.email,
                "avatarUrl": m.get_avatar_url(),
                "role": m.role,
                "status": m.status,
                "isCreator": m == group.created_by,
                "canChat": group.can_employee_chat(m),
                "isAllowedChatter": group.allowed_chatters.filter(id=m.id).exists(),
            })

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
            "isBroadcast": group.is_broadcast,
            "canChat": group.can_employee_chat(current_employee),
            **group.get_chat_permission_info(),
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

        if (current_employee not in group.members.all()
                and current_employee.role not in ['admin', 'superadmin']):
            return Response(
                {"error": "You are not a member of this group"}, status=403
            )

        messages = group.group_messages.select_related(
            'sender', 'reply_to'
        ).prefetch_related(
            'reactions', 'poll__options__votes'
        ).order_by('timestamp')

        data = []
        for m in messages:
            serialized = serialize_message(m, current_employee)
            if serialized:
                data.append(serialized)

        messages.exclude(
            sender=current_employee
        ).filter(is_read=False).update(is_read=True)

        return Response({
            "messages": data,
            "canChat": group.can_employee_chat(current_employee),
            **group.get_chat_permission_info(),
        }, status=200)
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

        if (current_employee.role not in ['admin', 'superadmin']
                and current_employee != group.created_by):
            return Response(
                {"error": "Only admins or group creator can add members"}, status=403
            )

        member_ids = request.data.get('member_ids', [])
        if not member_ids:
            return Response({"error": "No members specified"}, status=400)

        members = Employee.objects.filter(id__in=member_ids, is_active=True)
        group.members.add(*members)

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
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)

        if (current_employee.role not in ['admin', 'superadmin']
                and current_employee != group.created_by):
            return Response(
                {"error": "Only admins or group creator can remove members"},
                status=403
            )

        member_id = request.data.get('member_id')
        if not member_id:
            return Response({"error": "No member specified"}, status=400)

        member = Employee.objects.get(id=member_id)
        if member == group.created_by:
            return Response({"error": "Cannot remove group creator"}, status=400)

        group.members.remove(member)
        group.allowed_chatters.remove(member)

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


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_group(request, group_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)

        if (current_employee.role not in ['admin', 'superadmin']
                and current_employee != group.created_by):
            return Response(
                {"error": "Only admins or group creator can update group"}, status=403
            )

        name = request.data.get('name', '').strip()
        description = request.data.get('description', '').strip()
        is_broadcast = request.data.get('is_broadcast')

        if name:
            group.name = name
        if description is not None:
            group.description = description
        if is_broadcast is not None:
            group.is_broadcast = is_broadcast
        group.save()

        return Response({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "isBroadcast": group.is_broadcast,
            **group.get_chat_permission_info(),
            "message": "Group updated successfully"
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
            return Response(
                {"error": "You are not a member of this group"}, status=400
            )
        if current_employee == group.created_by:
            return Response(
                {"error": "Group creator cannot leave. Delete the group instead."},
                status=400
            )

        group.members.remove(current_employee)
        group.allowed_chatters.remove(current_employee)

        return Response({"message": "You have left the group"}, status=200)
    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== GROUP CHAT PERMISSION VIEWS ====================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_group_chat_permission(request, group_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)

        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)

        group = ChatGroup.objects.get(id=group_id)

        members_with_status = []
        for member in group.members.all():
            members_with_status.append({
                "id": member.id,
                "name": member.name,
                "email": member.email,
                "avatarUrl": member.get_avatar_url(),
                "role": member.role,
                "canChat": group.can_employee_chat(member),
                "isAllowedChatter": group.allowed_chatters.filter(
                    id=member.id
                ).exists(),
                "isCreator": member == group.created_by,
                "isAdmin": member.role in ['admin', 'superadmin'],
            })

        return Response({
            "groupId": group.id,
            "groupName": group.name,
            "isBroadcast": group.is_broadcast,
            **group.get_chat_permission_info(),
            "members": members_with_status,
            "memberCount": len(members_with_status),
        }, status=200)

    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_group_chat_permission(request, group_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)

        if current_employee.role not in ['admin', 'superadmin']:
            return Response(
                {"error": "Only admins can change chat permissions"}, status=403
            )

        group = ChatGroup.objects.get(id=group_id)

        chat_permission = request.data.get('chat_permission', '').strip()
        allowed_chatter_ids = request.data.get('allowed_chatters', [])

        if chat_permission not in ['all', 'selected', 'admins_only']:
            return Response({
                "error": "Invalid chat_permission. Must be 'all', 'selected', or 'admins_only'"
            }, status=400)

        old_permission = group.chat_permission
        old_allowed = list(group.allowed_chatters.values_list('id', flat=True))

        group.chat_permission = chat_permission
        group.save()

        if chat_permission == 'selected':
            if not allowed_chatter_ids:
                return Response({
                    "error": "You must select at least one employee when using 'selected' permission"
                }, status=400)

            valid_members = group.members.filter(
                id__in=allowed_chatter_ids, is_active=True
            )
            non_member_ids = (
                set(allowed_chatter_ids)
                - set(valid_members.values_list('id', flat=True))
            )

            if non_member_ids:
                non_members = Employee.objects.filter(id__in=non_member_ids)
                non_member_names = list(non_members.values_list('name', flat=True))
                return Response({
                    "error": (
                        f"The following employees are not members of this group: "
                        f"{', '.join(non_member_names)}"
                    ),
                    "nonMemberIds": list(non_member_ids)
                }, status=400)

            group.allowed_chatters.set(valid_members)

        elif chat_permission in ['all', 'admins_only']:
            group.allowed_chatters.clear()

        log_admin_activity(
            admin=current_employee,
            action='change_chat_permission',
            details={
                'group_id': group.id,
                'group_name': group.name,
                'old_permission': old_permission,
                'new_permission': chat_permission,
                'old_allowed_chatters': old_allowed,
                'new_allowed_chatters': (
                    allowed_chatter_ids if chat_permission == 'selected' else []
                ),
            }
        )

        if chat_permission == 'selected':
            allowed_names = list(group.allowed_chatters.values_list('name', flat=True))
            system_content = (
                f"⚙️ {current_employee.name} changed chat permissions: "
                f"Only {', '.join(allowed_names)} can send messages now."
            )
        elif chat_permission == 'admins_only':
            system_content = (
                f"⚙️ {current_employee.name} changed chat permissions: "
                f"Only admins can send messages now."
            )
        else:
            system_content = (
                f"⚙️ {current_employee.name} changed chat permissions: "
                f"All members can send messages now."
            )

        system_msg = Message.objects.create(
            sender=current_employee,
            group=group,
            content=system_content,
            message_type='system',
            is_read=False
        )

        try:
            channel_layer = get_channel_layer()
            room_group_name = f"group_{group.id}"

            async_to_sync(channel_layer.group_send)(room_group_name, {
                "type": "chat_message",
                "data": {
                    "id": system_msg.id,
                    "text": system_content,
                    "sender_id": current_employee.id,
                    "sender_name": current_employee.name,
                    "sender_avatar": current_employee.get_avatar_url(),
                    "group_id": group.id,
                    "createdAt": system_msg.timestamp.isoformat(),
                    "messageType": "system",
                    "type": "system",
                    "isSystemMessage": True,
                }
            })

            async_to_sync(channel_layer.group_send)(room_group_name, {
                "type": "chat_permission_update",
                "data": {
                    "group_id": group.id,
                    **group.get_chat_permission_info(),
                    "updatedBy": current_employee.id,
                    "updatedByName": current_employee.name,
                }
            })
        except Exception as e:
            logger.warning(f"WebSocket broadcast failed for permission update: {e}")

        members_with_status = []
        for member in group.members.all():
            members_with_status.append({
                "id": member.id,
                "name": member.name,
                "email": member.email,
                "avatarUrl": member.get_avatar_url(),
                "role": member.role,
                "canChat": group.can_employee_chat(member),
                "isAllowedChatter": group.allowed_chatters.filter(
                    id=member.id
                ).exists(),
                "isCreator": member == group.created_by,
                "isAdmin": member.role in ['admin', 'superadmin'],
            })

        return Response({
            "message": (
                f"Chat permission updated to '{group.get_chat_permission_display()}'"
            ),
            "groupId": group.id,
            "groupName": group.name,
            **group.get_chat_permission_info(),
            "members": members_with_status,
        }, status=200)

    except ChatGroup.DoesNotExist:
        return Response({"error": "Group not found"}, status=404)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)
    except Exception as e:
        logger.exception(f"Update chat permission error: {e}")
        return Response({"error": str(e)}, status=500)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def check_can_chat(request, group_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        group = ChatGroup.objects.get(id=group_id)

        can_chat = group.can_employee_chat(current_employee)

        reason = ""
        if not can_chat:
            if group.is_broadcast:
                reason = "This is a broadcast channel. Only admins can send messages."
            elif group.chat_permission == 'admins_only':
                reason = "Only admins can send messages in this group."
            elif group.chat_permission == 'selected':
                reason = "You are not in the list of allowed chatters for this group."
            elif current_employee not in group.members.all():
                reason = "You are not a member of this group."

        return Response({
            "canChat": can_chat,
            "reason": reason,
            "groupId": group.id,
            **group.get_chat_permission_info(),
        }, status=200)

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

        if not meet_link:
            return Response({"error": "Meet link is required"}, status=400)

        scheduled_datetime = None
        if scheduled_at:
            try:
                scheduled_datetime = datetime.fromisoformat(
                    scheduled_at.replace('Z', '+00:00')
                )
            except Exception:
                pass

        receiver = None
        group = None

        if group_id:
            try:
                group = ChatGroup.objects.get(id=group_id)
                if not group.can_employee_chat(current_employee):
                    return Response({
                        "error": "You don't have permission to send messages in this group",
                        "chatRestricted": True,
                    }, status=403)
            except ChatGroup.DoesNotExist:
                return Response({"error": "Group not found"}, status=404)
        elif receiver_id:
            try:
                receiver = Employee.objects.get(id=receiver_id, is_active=True)
            except Employee.DoesNotExist:
                return Response({"error": "Receiver not found"}, status=404)
        else:
            return Response(
                {"error": "Either receiver_id or group_id is required"}, status=400
            )

        content = f"📅 {meet_title}"
        if scheduled_datetime:
            content += (
                f"\n🕐 Scheduled: "
                f"{scheduled_datetime.strftime('%B %d, %Y at %I:%M %p')}"
            )
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

        invitations = []
        target_invitees = []
        if invitee_ids:
            target_invitees = Employee.objects.filter(
                id__in=invitee_ids, is_active=True
            )
        elif group:
            target_invitees = group.members.exclude(id=current_employee.id)

        for invitee in target_invitees:
            if invitee != current_employee:
                invitation, _ = MeetingInvitation.objects.get_or_create(
                    message=message, invitee=invitee
                )
                invitations.append({
                    "id": invitation.id,
                    "inviteeId": invitee.id,
                    "inviteeName": invitee.name,
                    "status": invitation.status
                })

        if save_link:
            from django.db.models import F
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
            "scheduledAt": (
                scheduled_datetime.isoformat() if scheduled_datetime else None
            ),
            "invitations": invitations,
            "createdAt": message.timestamp.isoformat(),
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
        status_val = request.data.get('status', '').strip()

        if status_val not in ['accepted', 'declined', 'attended']:
            return Response({"error": "Invalid status"}, status=400)

        invitation = MeetingInvitation.objects.get(
            message_id=message_id, invitee=current_employee
        )
        invitation.status = status_val
        invitation.responded_at = timezone.now()
        invitation.save()

        return Response({
            "id": invitation.id,
            "status": status_val,
            "respondedAt": invitation.responded_at.isoformat(),
            "message": f"You have {status_val} the meeting"
        }, status=200)
    except MeetingInvitation.DoesNotExist:
        return Response(
            {"error": "You are not invited to this meeting"}, status=404
        )
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

        employees = Employee.objects.filter(is_active=True).exclude(
            id=current_employee.id
        )
        data = []
        for emp in employees:
            sent_count = Message.objects.filter(sender=emp).count()
            received_count = Message.objects.filter(receiver=emp).count()

            chat_partners = Message.objects.filter(
                Q(sender=emp) | Q(receiver=emp), group__isnull=True
            ).values_list('sender', 'receiver').distinct()

            unique_partners = set()
            for sender_id, receiver_id in chat_partners:
                if sender_id != emp.id:
                    unique_partners.add(sender_id)
                if receiver_id and receiver_id != emp.id:
                    unique_partners.add(receiver_id)

            groups_count = emp.group_memberships.count()
            last_message = Message.objects.filter(
                Q(sender=emp) | Q(receiver=emp)
            ).order_by('-timestamp').first()

            data.append({
                "id": emp.id,
                "name": emp.name,
                "email": emp.email,
                "role": emp.role,
                "about": emp.about,
                "status": emp.status,
                "avatarUrl": emp.get_avatar_url(),
                "createdAt": emp.created_at.isoformat(),
                "password": emp.password,
                "is_suspended": emp.is_suspended,
                "stats": {
                    "messagesSent": sent_count,
                    "messagesReceived": received_count,
                    "totalMessages": sent_count + received_count,
                    "activeChatPartners": len(unique_partners),
                    "groupsJoined": groups_count,
                },
                "lastActivity": (
                    last_message.timestamp.isoformat() if last_message else None
                ),
            })

        data.sort(
            key=lambda x: x['lastActivity'] or '1970-01-01', reverse=True
        )
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
def admin_get_statistics(request):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)

        total_employees = Employee.objects.filter(is_active=True).count()
        total_groups = ChatGroup.objects.count()
        total_messages = Message.objects.count()

        today = timezone.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        messages_today = Message.objects.filter(timestamp__gte=today).count()
        week_ago = today - timedelta(days=7)
        messages_this_week = Message.objects.filter(
            timestamp__gte=week_ago
        ).count()

        top_users = Employee.objects.filter(is_active=True).annotate(
            msg_count=Count('sent_messages')
        ).order_by('-msg_count')[:5]

        top_groups = ChatGroup.objects.annotate(
            msg_count=Count('group_messages')
        ).order_by('-msg_count')[:5]

        return Response({
            "totalEmployees": total_employees,
            "totalGroups": total_groups,
            "totalMessages": total_messages,
            "messagesToday": messages_today,
            "messagesThisWeek": messages_this_week,
            "topUsers": [{
                "id": u.id,
                "name": u.name,
                "avatarUrl": u.get_avatar_url(),
                "messageCount": u.msg_count
            } for u in top_users],
            "topGroups": [{
                "id": g.id,
                "name": g.name,
                "avatarUrl": g.get_group_image_url(),
                "messageCount": g.msg_count,
                "memberCount": g.members.count()
            } for g in top_groups],
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
        log_admin_activity(
            admin=current_employee,
            action='view_employee',
            target_employee=target_employee,
            details={'viewed_dashboard': True}
        )

        employees = Employee.objects.filter(is_active=True).exclude(
            id=target_employee.id
        )
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
                    "id": emp.id,
                    "name": emp.name,
                    "email": emp.email,
                    "role": emp.role,
                    "avatarUrl": emp.get_avatar_url(),
                    "totalMessages": total_messages,
                    "lastMessage": {
                        "id": last_message.id,
                        "text": (
                            last_message.content
                            if last_message.content
                            else f"[{last_message.message_type}]"
                        ),
                        "sender": (
                            "target"
                            if last_message.sender == target_employee
                            else "them"
                        ),
                        "createdAt": last_message.timestamp.isoformat(),
                        "messageType": last_message.message_type,
                    }
                })

        contacts.sort(
            key=lambda x: x['lastMessage']['createdAt'], reverse=True
        )
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
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)

        employee = Employee.objects.get(id=employee_id, is_active=True)
        target = Employee.objects.get(id=target_id, is_active=True)

        log_admin_activity(
            admin=current_employee,
            action='view_chat',
            target_employee=employee,
            details={'viewing_chat_with': target.id, 'target_name': target.name}
        )

        messages = Message.objects.filter(
            Q(group__isnull=True) & (
                (Q(sender=employee) & Q(receiver=target)) |
                (Q(sender=target) & Q(receiver=employee))
            )
        ).select_related(
            'sender', 'receiver', 'reply_to'
        ).prefetch_related(
            'reactions', 'poll__options__votes'
        ).order_by('timestamp')

        data = []
        for msg in messages:
            serialized = serialize_message(
                msg, current_employee,
                viewing_as_admin=True, target_employee=employee
            )
            if serialized:
                data.append(serialized)

        return Response({
            "viewingEmployee": {
                "id": employee.id, "name": employee.name,
                "email": employee.email, "avatarUrl": employee.get_avatar_url()
            },
            "chattingWith": {
                "id": target.id, "name": target.name,
                "email": target.email, "avatarUrl": target.get_avatar_url()
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
            employee_messages = group.group_messages.filter(
                sender=employee
            ).count()

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
                "canEmployeeChat": group.can_employee_chat(employee),
                **group.get_chat_permission_info(),
            }
            if last_message:
                group_data["lastMessage"] = {
                    "text": (
                        last_message.content
                        if last_message.content
                        else f"[{last_message.message_type}]"
                    ),
                    "sender": last_message.sender.name,
                    "createdAt": last_message.timestamp.isoformat(),
                }
            data.append(group_data)

        data.sort(
            key=lambda x: (
                x['lastMessage']['createdAt'] if x['lastMessage'] else '1970-01-01'
            ),
            reverse=True
        )
        return Response({
            "employee": {
                "id": employee.id,
                "name": employee.name,
                "avatarUrl": employee.get_avatar_url()
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
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response({"error": "Admin access required"}, status=403)

        employee = Employee.objects.get(id=employee_id, is_active=True)
        group = ChatGroup.objects.get(id=group_id)

        if employee not in group.members.all():
            return Response(
                {"error": "Employee is not a member of this group"}, status=400
            )

        log_admin_activity(
            admin=current_employee,
            action='view_chat',
            target_employee=employee,
            details={'viewing_group': group.id, 'group_name': group.name}
        )

        messages = group.group_messages.select_related(
            'sender', 'reply_to'
        ).prefetch_related(
            'reactions', 'poll__options__votes'
        ).order_by('timestamp')

        data = []
        for msg in messages:
            serialized = serialize_message(
                msg, current_employee,
                viewing_as_admin=True, target_employee=employee
            )
            if serialized:
                serialized["isFromViewedEmployee"] = msg.sender.id == employee.id
                data.append(serialized)

        members = [{
            "id": m.id,
            "name": m.name,
            "email": m.email,
            "avatarUrl": m.get_avatar_url(),
            "role": m.role,
            "isViewedEmployee": m.id == employee.id,
            "canChat": group.can_employee_chat(m),
        } for m in group.members.all()]

        return Response({
            "viewingEmployee": {
                "id": employee.id, "name": employee.name,
                "email": employee.email, "avatarUrl": employee.get_avatar_url()
            },
            "group": {
                "id": group.id, "name": group.name,
                "description": group.description,
                "avatarUrl": group.get_group_image_url(),
                "memberCount": len(members)
            },
            "members": members,
            "messages": data,
            "totalMessages": len(data),
            "adminView": True,
            "adminId": current_employee.id,
            "adminName": current_employee.name,
            **group.get_chat_permission_info(),
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

        logs = AdminActivityLog.objects.select_related(
            'admin', 'target_employee'
        ).order_by('-timestamp')[offset:offset + limit]
        total_count = AdminActivityLog.objects.count()

        data = [{
            "id": log.id,
            "adminId": log.admin.id,
            "adminName": log.admin.name,
            "adminAvatar": log.admin.get_avatar_url(),
            "action": log.action,
            "actionDisplay": log.get_action_display(),
            "targetEmployeeId": (
                log.target_employee.id if log.target_employee else None
            ),
            "targetEmployeeName": (
                log.target_employee.name if log.target_employee else None
            ),
            "targetEmployeeAvatar": (
                log.target_employee.get_avatar_url()
                if log.target_employee else None
            ),
            "details": log.details,
            "timestamp": log.timestamp.isoformat(),
        } for log in logs]

        return Response({
            "logs": data,
            "totalCount": total_count,
            "limit": limit,
            "offset": offset
        }, status=200)
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
            "adminName": current_employee.name
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== FORWARD / STAR / PIN / BLOCK ====================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def forward_messages(request):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.is_suspended:
            return Response({"error": "Suspended"}, status=403)

        messages_data = request.data.get('messages', [])
        target_ids = request.data.get('target_ids', [])
        if not messages_data or not target_ids:
            return Response({"error": "Required fields missing"}, status=400)

        channel_layer = get_channel_layer()
        restricted_groups = []

        for target in target_ids:
            receiver, group = None, None
            room_group_name = None
            target_str = str(target)

            if target_str.startswith('group-'):
                group_id = int(target_str.replace('group-', ''))
                try:
                    group = ChatGroup.objects.get(id=group_id)
                    room_group_name = f"group_{group_id}"

                    if not group.can_employee_chat(current_employee):
                        restricted_groups.append(group.name)
                        continue

                except ChatGroup.DoesNotExist:
                    continue
            else:
                receiver_id = int(target_str.replace('emp-', ''))
                try:
                    receiver = Employee.objects.get(
                        id=receiver_id, is_active=True
                    )
                    ids = sorted([current_employee.id, receiver_id])
                    room_group_name = f"chat_{ids[0]}_{ids[1]}"
                except Employee.DoesNotExist:
                    continue

            for msg_data in messages_data:
                try:
                    original = Message.objects.get(id=msg_data.get('id'))
                    msg = Message.objects.create(
                        sender=current_employee,
                        receiver=receiver,
                        group=group,
                        content=original.content,
                        message_type=original.message_type,
                        file=original.file,
                        file_name=original.file_name,
                        file_size=original.file_size,
                        meet_link=original.meet_link,
                        meet_title=original.meet_title,
                        meet_scheduled_at=original.meet_scheduled_at,
                        is_read=False
                    )

                    if room_group_name:
                        async_to_sync(channel_layer.group_send)(
                            room_group_name,
                            {
                                "type": "chat_message",
                                "data": {
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
                                    "meetLink": msg.meet_link,
                                    "meetTitle": msg.meet_title,
                                    "meetScheduledAt": (
                                        msg.meet_scheduled_at.isoformat()
                                        if msg.meet_scheduled_at else None
                                    ),
                                    "isForwarded": True
                                }
                            }
                        )
                except Message.DoesNotExist:
                    pass

        response_data = {"message": "Messages forwarded successfully"}
        if restricted_groups:
            response_data["warning"] = (
                f"Could not forward to groups: {', '.join(restricted_groups)} "
                f"(chat restricted)"
            )
            response_data["restrictedGroups"] = restricted_groups

        return Response(response_data, status=200)
    except Exception as e:
        return Response({"error": str(e)}, status=500)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_message_star(request, message_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        message = Message.objects.get(id=message_id)

        if current_employee in message.starred_by.all():
            message.starred_by.remove(current_employee)
            status_val = "unstarred"
        else:
            message.starred_by.add(current_employee)
            status_val = "starred"

        return Response(
            {"message": f"Message {status_val}", "status": status_val}, status=200
        )
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

        status_val = "pinned" if message.is_pinned else "unpinned"

        if message.group and current_employee.role in ['admin', 'superadmin']:
            log_admin_activity(
                admin=current_employee,
                action='pin_message',
                details={
                    'message_id': message.id,
                    'group_id': message.group.id,
                    'status': status_val
                }
            )

        return Response({
            "message": f"Message {status_val} successfully",
            "status": status_val,
            "isPinned": message.is_pinned
        }, status=200)
    except Message.DoesNotExist:
        return Response({"error": "Message not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_block_user(request, target_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        target_employee = Employee.objects.get(
            id=str(target_id).replace('emp-', ''), is_active=True
        )

        if current_employee == target_employee:
            return Response({"error": "You cannot block yourself"}, status=400)

        if current_employee.role in ['admin', 'superadmin']:
            target_employee.is_suspended = not target_employee.is_suspended
            target_employee.save()
            status_val = (
                "suspended" if target_employee.is_suspended else "unsuspended"
            )
            return Response({
                "message": f"Employee globally {status_val} successfully",
                "status": status_val,
                "isBlocked": target_employee.is_suspended,
                "isAdminBlock": True
            }, status=200)
        else:
            if target_employee in current_employee.blocked_users.all():
                current_employee.blocked_users.remove(target_employee)
                status_val = "unblocked"
            else:
                current_employee.blocked_users.add(target_employee)
                status_val = "blocked"
            return Response({
                "message": f"User {status_val} successfully",
                "status": status_val,
                "isBlocked": status_val == "blocked",
                "isAdminBlock": False
            }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def admin_delete_employee(request, employee_id):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        if current_employee.role not in ['admin', 'superadmin']:
            return Response(
                {"error": "Only admins can delete employees"}, status=403
            )

        target_employee = Employee.objects.get(
            id=str(employee_id).replace('emp-', '')
        )
        target_employee.is_active = False
        if target_employee.user:
            target_employee.user.is_active = False
            target_employee.user.save()
        target_employee.save()

        return Response({"message": "Employee deleted successfully"}, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== ONLINE STATUS ====================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_all_online_status(request):
    try:
        current_employee = Employee.objects.get(user=request.user, is_active=True)
        employees = Employee.objects.filter(is_active=True).exclude(
            id=current_employee.id
        )

        data = [{
            "id": emp.id,
            "name": emp.name,
            "isOnline": emp.is_online,
            "lastSeen": emp.last_seen.isoformat() if emp.last_seen else None,
            "status": emp.status,
        } for emp in employees]

        return Response(data, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_user_online_status(request, target_id):
    try:
        target = Employee.objects.get(id=target_id, is_active=True)
        return Response({
            "id": target.id,
            "name": target.name,
            "isOnline": target.is_online,
            "lastSeen": target.last_seen.isoformat() if target.last_seen else None,
            "status": target.status,
        }, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "User not found"}, status=404)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_online_status(request):
    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
        is_online = request.data.get('is_online', True)

        employee.is_online = is_online
        if not is_online:
            employee.last_seen = timezone.now()
        employee.save(update_fields=['is_online', 'last_seen'])

        try:
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                "online_status",
                {
                    "type": "online_status_update",
                    "data": {
                        "employee_id": employee.id,
                        "is_online": is_online,
                        "last_seen": (
                            employee.last_seen.isoformat()
                            if employee.last_seen else None
                        ),
                    }
                }
            )
        except Exception:
            pass

        return Response({"status": "updated"}, status=200)
    except Employee.DoesNotExist:
        return Response({"error": "Employee not found"}, status=404)


# ==================== PASSWORD RESET / OTP ====================

@api_view(["POST"])
@permission_classes([AllowAny])
def verify_email_exists(request):
    email = request.data.get("email", "").strip().lower()
    user = Employee.objects.filter(email__iexact=email).first()

    if not user:
        return Response({"error": "No account found"}, status=404)

    otp = str(random.randint(100000, 999999))
    user.otp = otp
    user.otp_expiry = timezone.now() + timedelta(minutes=5)
    user.save()

    try:
        send_mail(
            "Your OTP Code",
            f"Your OTP is {otp}",
            settings.EMAIL_HOST_USER,
            [user.email]
        )
    except Exception as e:
        logger.error(f"EMAIL ERROR: {str(e)}")
        return Response({"error": "Email failed", "details": str(e)}, status=500)

    return Response({"status": "success", "message": "OTP sent"})


@api_view(["POST"])
@permission_classes([AllowAny])
def verify_otp(request):
    email = request.data.get("email", "").strip().lower()
    otp = request.data.get("otp", "").strip()

    user = Employee.objects.filter(email__iexact=email).first()

    if not user:
        return Response({"error": "User not found"}, status=404)

    if user.otp != otp:
        return Response({"error": "Invalid OTP"}, status=400)

    if timezone.now() > user.otp_expiry:
        return Response({"error": "OTP expired"}, status=400)

    return Response({"status": "success", "message": "OTP verified"})


@api_view(["PATCH"])
@permission_classes([AllowAny])
def reset_password(request):
    email = request.data.get("email")
    new_password = request.data.get("new_password")

    if not email or not new_password:
        return Response({"error": "Missing data"}, status=400)

    try:
        employee = Employee.objects.get(email=email)
        employee.password = new_password
        employee.otp = None
        employee.otp_expiry = None
        employee.save(update_fields=['password', 'otp', 'otp_expiry'])
        return Response({
            "status": "success",
            "message": "Password updated successfully."
        })
    except Employee.DoesNotExist:
        return Response({"error": "User no longer exists."}, status=404)