import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db.models import Count
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from django.contrib.auth import get_user_model
from .models import (
    Message, Employee, ChatGroup, MessageReaction,
    MessageDeletion, Poll, PollOption, PollVote
)

logger = logging.getLogger(__name__)
User = get_user_model()


def get_employee_from_token(token_str):
    """Validate JWT and return Employee. Used in consumers."""
    try:
        token = AccessToken(token_str)
        user = User.objects.get(id=token['user_id'], is_active=True)
        employee = Employee.objects.get(user=user, is_active=True)
        return employee
    except (InvalidToken, TokenError, User.DoesNotExist, Employee.DoesNotExist):
        return None


# ==================== DIRECT CHAT CONSUMER ====================

class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close()
            return

        try:
            employee = await self.get_employee(user)
            if not employee:
                await self.close()
                return

            if employee.get('is_suspended', False):
                await self.close()
                return

            self.employee_id = employee['id']
            self.employee_name = employee['name']

            target_id_raw = self.scope['url_route']['kwargs'].get('target_id')
            if not target_id_raw:
                await self.close()
                return

            target_id_str = str(target_id_raw)
            self.target_id = int(target_id_str.replace("emp-", ""))

            if not await self.check_employee_exists(self.target_id):
                await self.close()
                return

            ids = sorted([self.employee_id, self.target_id])
            self.room_group_name = f"chat_{ids[0]}_{ids[1]}"

            await self.channel_layer.group_add(
                self.room_group_name, self.channel_name
            )
            await self.accept()

        except Exception as e:
            logger.exception(f"Connection Error: {e}")
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name, self.channel_name
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            msg_type = data.get("type", "message")

            handlers = {
                "message": self.handle_message,
                "reaction": self.handle_reaction,
                "typing": self.handle_typing,
                "edit": self.handle_edit,
                "delete": self.handle_delete,
                "read": self.handle_read,
                "poll_vote": self.handle_poll_vote,
            }
            handler = handlers.get(msg_type)
            if handler:
                await handler(data)

        except Exception as e:
            logger.exception(f"Receive Error: {e}")

    # ── Message Handlers ─────────────────────────────────────

    async def handle_message(self, data):
        message_text = data.get("message", "").strip()
        if not message_text:
            return
        msg_obj = await self.save_message(
            message_text, reply_to_id=data.get("reply_to")
        )
        if msg_obj:
            await self.channel_layer.group_send(
                self.room_group_name,
                {"type": "chat_message", "data": msg_obj}
            )
            await self.channel_layer.group_send(
                f"notifications_{self.target_id}",
                {
                    "type": "new_message_notification",
                    "data": {
                        "message_id": msg_obj["id"],
                        "sender_id": self.employee_id,
                        "sender_name": self.employee_name,
                        "sender_avatar": msg_obj.get("sender_avatar", ""),
                        "text": message_text[:150],
                        "message_type": msg_obj.get("messageType", "text"),
                        "timestamp": msg_obj["createdAt"],
                        "chat_type": "direct",
                        "receiver_id": self.target_id,
                    }
                }
            )

    async def handle_reaction(self, data):
        result = await self.save_reaction(
            data.get("message_id"), data.get("reaction")
        )
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {"type": "reaction_update", "data": result}
            )

    async def handle_typing(self, data):
        await self.channel_layer.group_send(self.room_group_name, {
            "type": "typing_indicator",
            "data": {
                "sender_id": self.employee_id,
                "sender_name": self.employee_name,
                "is_typing": data.get("is_typing", False),
            }
        })

    async def handle_edit(self, data):
        result = await self.edit_message_db(
            data.get("message_id"), data.get("content", "").strip()
        )
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {"type": "message_edited", "data": result}
            )

    async def handle_delete(self, data):
        result = await self.delete_message_db(
            data.get("message_id"),
            data.get("delete_type", "for_me")
        )
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {"type": "message_deleted", "data": result}
            )

    async def handle_read(self, data):
        """Mark direct messages as read."""
        await self.mark_messages_read()
        await self.channel_layer.group_send(self.room_group_name, {
            "type": "messages_read",
            "data": {
                "reader_id": self.employee_id,
                "target_id": self.target_id
            }
        })
        # Notify notification system of updated unread counts
        unread_data = await self.get_unread_counts()
        await self.channel_layer.group_send(
            f"notifications_{self.employee_id}",
            {
                "type": "unread_counts_updated",
                "data": unread_data
            }
        )

    async def handle_poll_vote(self, data):
        result = await self.save_poll_vote(
            data.get("poll_id"), data.get("option_id")
        )
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {"type": "poll_update", "data": result}
            )

    # ── Event Senders ────────────────────────────────────────

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            "type": "message", "data": event["data"]
        }))

    async def reaction_update(self, event):
        await self.send(text_data=json.dumps({
            "type": "reaction", "data": event["data"]
        }))

    async def typing_indicator(self, event):
        if event["data"]["sender_id"] != self.employee_id:
            await self.send(text_data=json.dumps({
                "type": "typing", "data": event["data"]
            }))

    async def message_edited(self, event):
        await self.send(text_data=json.dumps({
            "type": "edited", "data": event["data"]
        }))

    async def message_deleted(self, event):
        await self.send(text_data=json.dumps({
            "type": "deleted", "data": event["data"]
        }))

    async def messages_read(self, event):
        await self.send(text_data=json.dumps({
            "type": "read", "data": event["data"]
        }))

    async def poll_update(self, event):
        data = event["data"].copy()
        my_votes = await self.get_my_poll_votes(data.get("poll_id"))
        data["myVotes"] = my_votes
        await self.send(text_data=json.dumps({
            "type": "poll_update", "data": data
        }))

    # ── DB Methods ───────────────────────────────────────────

    @database_sync_to_async
    def get_employee(self, user):
        try:
            emp = Employee.objects.get(user=user, is_active=True)
            return {
                'id': emp.id,
                'name': emp.name,
                'email': emp.email,
                'is_suspended': emp.is_suspended,
            }
        except Employee.DoesNotExist:
            return None

    @database_sync_to_async
    def check_employee_exists(self, employee_id):
        return Employee.objects.filter(id=employee_id, is_active=True).exists()

    @database_sync_to_async
    def save_message(self, text, message_type='text', reply_to_id=None):
        try:
            sender = Employee.objects.get(id=self.employee_id, is_active=True)
            if sender.is_suspended:
                return None
            receiver = Employee.objects.get(id=self.target_id, is_active=True)

            reply_to = None
            reply_to_data = None
            if reply_to_id:
                if isinstance(reply_to_id, dict):
                    reply_to_id = reply_to_id.get('id')
                try:
                    reply_to = Message.objects.get(id=reply_to_id)
                    reply_to_data = {
                        "id": reply_to.id,
                        "text": reply_to.content[:100] if reply_to.content else "",
                        "sender_name": reply_to.sender.name,
                    }
                except Message.DoesNotExist:
                    pass

            msg = Message.objects.create(
                sender=sender,
                receiver=receiver,
                content=text,
                message_type=message_type,
                is_read=False,
                reply_to=reply_to,
            )

            return {
                "id": msg.id,
                "text": msg.content,
                "type": msg.message_type,
                "messageType": msg.message_type,
                "sender_id": sender.id,
                "sender_name": sender.name,
                "senderName": sender.name,
                "sender_avatar": sender.get_avatar_url(),
                "receiver_id": receiver.id,
                "createdAt": msg.timestamp.isoformat(),
                "reactions": {},
                "userReaction": None,
                "isEdited": False,
                "replyTo": reply_to_data,
                "isRead": False,
                "isPinned": False,
                "isStarred": False,
                "thread": [],
                "isMine": None,
            }
        except Exception as e:
            logger.exception(f"Save Error: {e}")
            return None

    @database_sync_to_async
    def save_reaction(self, message_id, reaction):
        try:
            employee = Employee.objects.get(id=self.employee_id, is_active=True)
            if employee.is_suspended:
                return None
            message = Message.objects.get(id=message_id)
            valid_reactions = ['ok', 'not_ok', 'love', 'laugh', 'wow', 'sad']

            if reaction in valid_reactions:
                MessageReaction.objects.update_or_create(
                    message=message, employee=employee,
                    defaults={'reaction': reaction}
                )
            elif reaction is None:
                MessageReaction.objects.filter(
                    message=message, employee=employee
                ).delete()

            reactions = list(
                message.reactions.values('reaction').annotate(count=Count('reaction'))
            )
            return {
                "message_id": message_id,
                "reactions": {r['reaction']: r['count'] for r in reactions},
                "employee_id": self.employee_id,
                "reaction": reaction,
            }
        except Exception:
            return None

    @database_sync_to_async
    def edit_message_db(self, message_id, new_content):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            if employee.is_suspended:
                return None
            message = Message.objects.get(id=message_id, sender=employee)
            if not message.can_edit(employee):
                return None
            message.content = new_content
            message.is_edited = True
            message.edited_at = timezone.now()
            message.save()
            return {
                "message_id": message_id,
                "content": new_content,
                "isEdited": True,
                "editedAt": message.edited_at.isoformat(),
            }
        except Exception:
            return None

    @database_sync_to_async
    def delete_message_db(self, message_id, delete_type):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            if employee.is_suspended:
                return None
            message = Message.objects.get(id=message_id)

            if delete_type == 'for_everyone':
                if message.sender != employee or not message.can_delete_for_everyone(employee):
                    return None
                message.is_deleted_for_everyone = True
                message.deleted_at = timezone.now()
                message.content = ""
                message.save()
                if message.file:
                    message.file.delete()
                return {"message_id": message_id, "deletedForEveryone": True}
            else:
                MessageDeletion.objects.get_or_create(
                    message=message, employee=employee
                )
                return {"message_id": message_id, "deletedForMe": True}
        except Exception:
            return None

    @database_sync_to_async
    def mark_messages_read(self):
        try:
            Message.objects.filter(
                sender_id=self.target_id,
                receiver_id=self.employee_id,
                is_read=False,
                group__isnull=True,
            ).update(is_read=True)
        except Exception:
            pass

    @database_sync_to_async
    def save_poll_vote(self, poll_id, option_id):
        try:
            employee = Employee.objects.get(id=self.employee_id, is_active=True)
            if employee.is_suspended:
                return None

            poll = Poll.objects.get(id=poll_id)
            option = PollOption.objects.get(id=option_id, poll=poll)
            message = poll.message

            if message.group:
                if (employee not in message.group.members.all()
                        and employee.role not in ['admin', 'superadmin']):
                    return None
            else:
                if message.sender != employee and message.receiver != employee:
                    return None

            existing_vote = PollVote.objects.filter(
                option=option, employee=employee
            ).first()
            if existing_vote:
                existing_vote.delete()
                action = "removed"
            else:
                if not poll.allow_multiple:
                    PollVote.objects.filter(
                        option__poll=poll, employee=employee
                    ).delete()
                PollVote.objects.create(option=option, employee=employee)
                action = "added"

            options_data = [{
                "id": opt.id,
                "text": opt.text,
                "votes": opt.votes.count(),
                "voters": list(opt.votes.values_list('employee__name', flat=True)),
            } for opt in poll.options.all().order_by('order')]

            return {
                "message_id": message.id,
                "poll_id": poll.id,
                "voter_id": employee.id,
                "voter_name": employee.name,
                "option_id": option_id,
                "action": action,
                "pollOptions": options_data,
                "totalVotes": PollVote.objects.filter(option__poll=poll).count(),
            }
        except Exception as e:
            logger.exception(f"Poll vote error: {e}")
            return None

    @database_sync_to_async
    def get_my_poll_votes(self, poll_id):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            poll = Poll.objects.get(id=poll_id)
            return list(PollVote.objects.filter(
                option__poll=poll, employee=employee
            ).values_list('option__order', flat=True))
        except Exception:
            return []

    @database_sync_to_async
    def get_unread_counts(self):
        """Get unread message counts for the current employee."""
        try:
            employee = Employee.objects.get(id=self.employee_id)

            direct_unreads = {}
            unread_messages = Message.objects.filter(
                receiver=employee,
                is_read=False,
                group__isnull=True,
            ).values('sender_id', 'sender__name').annotate(count=Count('id'))

            for item in unread_messages:
                direct_unreads[str(item['sender_id'])] = {
                    "count": item['count'],
                    "sender_name": item['sender__name'],
                }

            group_unreads = {}
            groups = employee.group_memberships.all()
            for group in groups:
                unread_count = group.group_messages.filter(
                    is_read=False,
                ).exclude(sender=employee).count()

                if unread_count > 0:
                    group_unreads[str(group.id)] = {
                        "count": unread_count,
                        "group_name": group.name,
                    }

            total_unread = (
                sum(v['count'] for v in direct_unreads.values()) +
                sum(v['count'] for v in group_unreads.values())
            )

            return {
                "direct": direct_unreads,
                "groups": group_unreads,
                "total": total_unread,
            }
        except Exception as e:
            logger.exception(f"Get unread counts error: {e}")
            return {"direct": {}, "groups": {}, "total": 0}


# ==================== GROUP CHAT CONSUMER ====================

class GroupChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close()
            return

        try:
            employee = await self.get_employee(user)
            if not employee:
                await self.close()
                return

            if employee.get('is_suspended', False):
                await self.close()
                return

            self.employee_id = employee['id']
            self.employee_name = employee['name']
            self.group_id = int(
                self.scope['url_route']['kwargs'].get('group_id')
            )

            if not await self.check_group_membership():
                await self.close()
                return

            self.room_group_name = f"group_{self.group_id}"
            await self.channel_layer.group_add(
                self.room_group_name, self.channel_name
            )
            await self.accept()

            can_chat = await self.check_can_chat()
            await self.send(text_data=json.dumps({
                "type": "chat_permission_status",
                "data": can_chat,
            }))

        except Exception as e:
            logger.exception(f"Group connection error: {e}")
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name, self.channel_name
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            msg_type = data.get("type", "message")

            handlers = {
                "message": self.handle_message,
                "reaction": self.handle_reaction,
                "typing": self.handle_typing,
                "edit": self.handle_edit,
                "delete": self.handle_delete,
                "read": self.handle_read,
                "poll_vote": self.handle_poll_vote,
            }
            handler = handlers.get(msg_type)
            if handler:
                await handler(data)
        except Exception as e:
            logger.exception(f"Group receive error: {e}")

    # ── Message Handlers ─────────────────────────────────────

    async def handle_message(self, data):
        can_chat_info = await self.check_can_chat()
        if not can_chat_info.get('canChat', False):
            await self.send(text_data=json.dumps({
                "type": "error",
                "data": {
                    "message": can_chat_info.get(
                        'reason',
                        "You don't have permission to chat in this group"
                    ),
                    "code": "CHAT_RESTRICTED",
                    "chatPermission": can_chat_info.get('chatPermission', 'unknown'),
                }
            }))
            return

        msg_obj = await self.save_group_message(
            data.get("message", "").strip(),
            reply_to_id=data.get("reply_to")
        )
        if msg_obj:
            await self.channel_layer.group_send(
                self.room_group_name,
                {"type": "chat_message", "data": msg_obj}
            )
            member_ids = await self.get_group_member_ids()
            group_name = msg_obj.get("group_name", "")
            for member_id in member_ids:
                if member_id != self.employee_id:
                    await self.channel_layer.group_send(
                        f"notifications_{member_id}",
                        {
                            "type": "group_message_notification",
                            "data": {
                                "message_id": msg_obj["id"],
                                "sender_id": self.employee_id,
                                "sender_name": self.employee_name,
                                "sender_avatar": msg_obj.get("sender_avatar", ""),
                                "text": data.get("message", "")[:150],
                                "message_type": msg_obj.get("messageType", "text"),
                                "timestamp": msg_obj["createdAt"],
                                "chat_type": "group",
                                "group_id": self.group_id,
                                "group_name": group_name,
                            }
                        }
                    )

    async def handle_reaction(self, data):
        result = await self.save_reaction(
            data.get("message_id"), data.get("reaction")
        )
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {"type": "reaction_update", "data": result}
            )

    async def handle_typing(self, data):
        await self.channel_layer.group_send(self.room_group_name, {
            "type": "typing_indicator",
            "data": {
                "sender_id": self.employee_id,
                "sender_name": self.employee_name,
                "is_typing": data.get("is_typing", False),
            }
        })

    async def handle_edit(self, data):
        result = await self.edit_message_db(
            data.get("message_id"),
            data.get("content", "").strip()
        )
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {"type": "message_edited", "data": result}
            )

    async def handle_delete(self, data):
        result = await self.delete_message_db(
            data.get("message_id"),
            data.get("delete_type", "for_me")
        )
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {"type": "message_deleted", "data": result}
            )

    async def handle_read(self, data):
        """Mark group messages as read and notify notification system."""
        await self.mark_group_messages_read()
        await self.channel_layer.group_send(self.room_group_name, {
            "type": "messages_read",
            "data": {
                "reader_id": self.employee_id,
                "group_id": self.group_id
            }
        })
        # Notify notification system of updated unread counts
        unread_data = await self.get_unread_counts()
        await self.channel_layer.group_send(
            f"notifications_{self.employee_id}",
            {
                "type": "unread_counts_updated",
                "data": unread_data
            }
        )

    async def handle_poll_vote(self, data):
        result = await self.save_poll_vote(
            data.get("poll_id"), data.get("option_id")
        )
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {"type": "poll_update", "data": result}
            )

    # ── Event Senders ────────────────────────────────────────

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            "type": "message", "data": event["data"]
        }))

    async def reaction_update(self, event):
        await self.send(text_data=json.dumps({
            "type": "reaction", "data": event["data"]
        }))

    async def typing_indicator(self, event):
        if event["data"]["sender_id"] != self.employee_id:
            await self.send(text_data=json.dumps({
                "type": "typing", "data": event["data"]
            }))

    async def message_edited(self, event):
        await self.send(text_data=json.dumps({
            "type": "edited", "data": event["data"]
        }))

    async def message_deleted(self, event):
        await self.send(text_data=json.dumps({
            "type": "deleted", "data": event["data"]
        }))

    async def messages_read(self, event):
        """Handle messages_read event from channel layer."""
        await self.send(text_data=json.dumps({
            "type": "read", "data": event["data"]
        }))

    async def poll_update(self, event):
        data = event["data"].copy()
        my_votes = await self.get_my_poll_votes(data.get("poll_id"))
        data["myVotes"] = my_votes
        await self.send(text_data=json.dumps({
            "type": "poll_update", "data": data
        }))

    async def chat_permission_update(self, event):
        can_chat_info = await self.check_can_chat()
        data = event["data"].copy()
        data["canChat"] = can_chat_info.get("canChat", False)
        data["reason"] = can_chat_info.get("reason", "")
        await self.send(text_data=json.dumps({
            "type": "chat_permission_update",
            "data": data,
        }))

    # ── DB Methods ───────────────────────────────────────────

    @database_sync_to_async
    def get_employee(self, user):
        try:
            emp = Employee.objects.get(user=user, is_active=True)
            return {
                'id': emp.id,
                'name': emp.name,
                'is_suspended': emp.is_suspended,
            }
        except Employee.DoesNotExist:
            return None

    @database_sync_to_async
    def check_group_membership(self):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            group = ChatGroup.objects.get(id=self.group_id)
            return (employee in group.members.all()
                    or employee.role in ['admin', 'superadmin'])
        except Exception:
            return False

    @database_sync_to_async
    def get_group_member_ids(self):
        try:
            group = ChatGroup.objects.get(id=self.group_id)
            return list(group.members.values_list('id', flat=True))
        except Exception:
            return []

    @database_sync_to_async
    def check_can_chat(self):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            group = ChatGroup.objects.get(id=self.group_id)
            can_chat = group.can_employee_chat(employee)

            reason = ""
            if not can_chat:
                if group.is_broadcast:
                    reason = "This is a broadcast channel. Only admins can send messages."
                elif group.chat_permission == 'admins_only':
                    reason = "Only admins can send messages in this group."
                elif group.chat_permission == 'selected':
                    reason = "You are not in the list of allowed chatters."
                elif employee not in group.members.all():
                    reason = "You are not a member of this group."

            return {
                "canChat": can_chat,
                "reason": reason,
                "chatPermission": group.chat_permission,
                "groupId": group.id,
            }
        except Exception:
            return {
                "canChat": False,
                "reason": "Error checking permissions",
                "chatPermission": "unknown",
            }

    @database_sync_to_async
    def save_group_message(self, text, message_type='text', reply_to_id=None):
        try:
            sender = Employee.objects.get(id=self.employee_id, is_active=True)
            if sender.is_suspended:
                return None
            group = ChatGroup.objects.get(id=self.group_id)

            if not group.can_employee_chat(sender):
                return None

            reply_to = None
            reply_to_data = None
            if reply_to_id:
                if isinstance(reply_to_id, dict):
                    reply_to_id = reply_to_id.get('id')
                try:
                    reply_to = Message.objects.get(id=reply_to_id)
                    reply_to_data = {
                        "id": reply_to.id,
                        "text": reply_to.content[:100] if reply_to.content else "",
                        "sender_name": reply_to.sender.name,
                    }
                except Message.DoesNotExist:
                    pass

            msg = Message.objects.create(
                sender=sender,
                group=group,
                content=text,
                message_type=message_type,
                is_read=False,
                reply_to=reply_to,
            )

            return {
                "id": msg.id,
                "text": msg.content,
                "type": msg.message_type,
                "messageType": msg.message_type,
                "sender_id": sender.id,
                "sender_name": sender.name,
                "senderName": sender.name,
                "sender_avatar": sender.get_avatar_url(),
                "group_id": group.id,
                "group_name": group.name,
                "createdAt": msg.timestamp.isoformat(),
                "reactions": {},
                "userReaction": None,
                "isEdited": False,
                "replyTo": reply_to_data,
                "isPinned": False,
                "isStarred": False,
                "thread": [],
                "isMine": None,
            }
        except Exception as e:
            logger.exception(f"Save Group Error: {e}")
            return None

    @database_sync_to_async
    def save_reaction(self, message_id, reaction):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            if employee.is_suspended:
                return None
            message = Message.objects.get(id=message_id)
            valid_reactions = ['ok', 'not_ok', 'love', 'laugh', 'wow', 'sad']

            if reaction in valid_reactions:
                MessageReaction.objects.update_or_create(
                    message=message, employee=employee,
                    defaults={'reaction': reaction}
                )
            elif reaction is None:
                MessageReaction.objects.filter(
                    message=message, employee=employee
                ).delete()

            reactions = list(
                message.reactions.values('reaction').annotate(count=Count('reaction'))
            )
            return {
                "message_id": message_id,
                "reactions": {r['reaction']: r['count'] for r in reactions},
                "employee_id": self.employee_id,
                "reaction": reaction,
            }
        except Exception:
            return None

    @database_sync_to_async
    def edit_message_db(self, message_id, new_content):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            if employee.is_suspended:
                return None
            message = Message.objects.get(id=message_id, sender=employee)
            if not message.can_edit(employee):
                return None
            message.content = new_content
            message.is_edited = True
            message.edited_at = timezone.now()
            message.save()
            return {
                "message_id": message_id,
                "content": new_content,
                "isEdited": True,
                "editedAt": message.edited_at.isoformat(),
            }
        except Exception:
            return None

    @database_sync_to_async
    def delete_message_db(self, message_id, delete_type):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            if employee.is_suspended:
                return None
            message = Message.objects.get(id=message_id)

            if delete_type == 'for_everyone':
                if (message.sender != employee
                        or not message.can_delete_for_everyone(employee)):
                    return None
                message.is_deleted_for_everyone = True
                message.content = ""
                message.save()
                if message.file:
                    message.file.delete()
                return {"message_id": message_id, "deletedForEveryone": True}
            else:
                MessageDeletion.objects.get_or_create(
                    message=message, employee=employee
                )
                return {"message_id": message_id, "deletedForMe": True}
        except Exception:
            return None

    @database_sync_to_async
    def mark_group_messages_read(self):
        """Mark all unread messages in the group as read."""
        try:
            Message.objects.filter(
                group_id=self.group_id,
                is_read=False,
            ).exclude(sender_id=self.employee_id).update(is_read=True)
        except Exception:
            pass

    @database_sync_to_async
    def save_poll_vote(self, poll_id, option_id):
        try:
            employee = Employee.objects.get(id=self.employee_id, is_active=True)
            if employee.is_suspended:
                return None

            poll = Poll.objects.get(id=poll_id)
            option = PollOption.objects.get(id=option_id, poll=poll)
            message = poll.message

            if message.group:
                if (employee not in message.group.members.all()
                        and employee.role not in ['admin', 'superadmin']):
                    return None

            existing_vote = PollVote.objects.filter(
                option=option, employee=employee
            ).first()
            if existing_vote:
                existing_vote.delete()
                action = "removed"
            else:
                if not poll.allow_multiple:
                    PollVote.objects.filter(
                        option__poll=poll, employee=employee
                    ).delete()
                PollVote.objects.create(option=option, employee=employee)
                action = "added"

            options_data = [{
                "id": opt.id,
                "text": opt.text,
                "votes": opt.votes.count(),
                "voters": list(opt.votes.values_list('employee__name', flat=True)),
            } for opt in poll.options.all().order_by('order')]

            return {
                "message_id": message.id,
                "poll_id": poll.id,
                "voter_id": employee.id,
                "voter_name": employee.name,
                "option_id": option_id,
                "action": action,
                "pollOptions": options_data,
                "totalVotes": PollVote.objects.filter(option__poll=poll).count(),
            }
        except Exception as e:
            logger.exception(f"Group poll vote error: {e}")
            return None

    @database_sync_to_async
    def get_my_poll_votes(self, poll_id):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            poll = Poll.objects.get(id=poll_id)
            return list(PollVote.objects.filter(
                option__poll=poll, employee=employee
            ).values_list('option__order', flat=True))
        except Exception:
            return []

    @database_sync_to_async
    def get_unread_counts(self):
        """Get unread message counts for the current employee."""
        try:
            employee = Employee.objects.get(id=self.employee_id)

            direct_unreads = {}
            unread_messages = Message.objects.filter(
                receiver=employee,
                is_read=False,
                group__isnull=True,
            ).values('sender_id', 'sender__name').annotate(count=Count('id'))

            for item in unread_messages:
                direct_unreads[str(item['sender_id'])] = {
                    "count": item['count'],
                    "sender_name": item['sender__name'],
                }

            group_unreads = {}
            groups = employee.group_memberships.all()
            for group in groups:
                unread_count = group.group_messages.filter(
                    is_read=False,
                ).exclude(sender=employee).count()

                if unread_count > 0:
                    group_unreads[str(group.id)] = {
                        "count": unread_count,
                        "group_name": group.name,
                    }

            total_unread = (
                sum(v['count'] for v in direct_unreads.values()) +
                sum(v['count'] for v in group_unreads.values())
            )

            return {
                "direct": direct_unreads,
                "groups": group_unreads,
                "total": total_unread,
            }
        except Exception as e:
            logger.exception(f"Get unread counts error: {e}")
            return {"direct": {}, "groups": {}, "total": 0}


# ==================== PRESENCE CONSUMER ====================

class PresenceConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close()
            return

        try:
            employee = await self.get_employee(user)
            if not employee:
                await self.close()
                return

            self.employee_id = employee['id']
            self.employee_name = employee['name']
            self.employee_avatar = employee.get('avatar', '')

            await self.channel_layer.group_add("online_status", self.channel_name)
            await self.accept()

            await self.set_online(True)

            await self.channel_layer.group_send("online_status", {
                "type": "online_status_update",
                "data": {
                    "employee_id": self.employee_id,
                    "name": self.employee_name,
                    "avatar": self.employee_avatar,
                    "is_online": True,
                    "last_seen": None,
                }
            })

            online_users = await self.get_all_online_users()
            await self.send(text_data=json.dumps({
                "type": "online_users_list",
                "data": online_users,
            }))

        except Exception as e:
            logger.exception(f"Presence connect error: {e}")
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, 'employee_id'):
            last_seen = await self.set_online(False)
            try:
                await self.channel_layer.group_send("online_status", {
                    "type": "online_status_update",
                    "data": {
                        "employee_id": self.employee_id,
                        "name": self.employee_name,
                        "is_online": False,
                        "last_seen": last_seen,
                    }
                })
            except Exception:
                pass

        if hasattr(self, 'channel_name'):
            await self.channel_layer.group_discard("online_status", self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            msg_type = data.get("type", "")

            if msg_type == "heartbeat":
                await self.set_online(True)
                await self.send(text_data=json.dumps({
                    "type": "heartbeat_ack",
                    "data": {"timestamp": timezone.now().isoformat()},
                }))

            elif msg_type == "typing":
                target_id = data.get("target_id")
                group_id = data.get("group_id")
                is_typing = data.get("is_typing", False)

                if group_id:
                    room = f"group_{group_id}"
                elif target_id:
                    target_id_clean = int(str(target_id).replace("emp-", ""))
                    ids = sorted([self.employee_id, target_id_clean])
                    room = f"chat_{ids[0]}_{ids[1]}"
                else:
                    return

                await self.channel_layer.group_send(room, {
                    "type": "typing_indicator",
                    "data": {
                        "sender_id": self.employee_id,
                        "sender_name": self.employee_name,
                        "is_typing": is_typing,
                    }
                })

            elif msg_type == "status_update":
                new_status = data.get("status", "available")
                await self.update_status(new_status)
                await self.channel_layer.group_send("online_status", {
                    "type": "user_status_changed",
                    "data": {
                        "employee_id": self.employee_id,
                        "name": self.employee_name,
                        "status": new_status,
                    }
                })

        except Exception as e:
            logger.exception(f"Presence receive error: {e}")

    async def online_status_update(self, event):
        if event["data"].get("employee_id") != self.employee_id:
            await self.send(text_data=json.dumps({
                "type": "online_status",
                "data": event["data"],
            }))

    async def user_status_changed(self, event):
        if event["data"].get("employee_id") != self.employee_id:
            await self.send(text_data=json.dumps({
                "type": "status_changed",
                "data": event["data"],
            }))

    @database_sync_to_async
    def get_employee(self, user):
        try:
            emp = Employee.objects.get(user=user, is_active=True)
            return {
                'id': emp.id,
                'name': emp.name,
                'avatar': emp.get_avatar_url(),
                'is_suspended': emp.is_suspended,
            }
        except Employee.DoesNotExist:
            return None

    @database_sync_to_async
    def set_online(self, is_online):
        try:
            emp = Employee.objects.get(id=self.employee_id)
            emp.is_online = is_online
            if not is_online:
                emp.last_seen = timezone.now()
            emp.save(update_fields=['is_online', 'last_seen'])
            return emp.last_seen.isoformat() if emp.last_seen else None
        except Exception:
            return None

    @database_sync_to_async
    def update_status(self, status):
        try:
            if status in ['available', 'dnd', 'meeting']:
                Employee.objects.filter(id=self.employee_id).update(status=status)
        except Exception:
            pass

    @database_sync_to_async
    def get_all_online_users(self):
        try:
            online_employees = Employee.objects.filter(
                is_active=True,
                is_online=True,
            ).exclude(id=self.employee_id).values('id', 'name', 'status')

            return [{
                "employee_id": emp['id'],
                "name": emp['name'],
                "is_online": True,
                "status": emp['status'],
            } for emp in online_employees]
        except Exception:
            return []


# ==================== NOTIFICATION CONSUMER ====================

class NotificationConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close()
            return

        try:
            employee = await self.get_employee(user)
            if not employee:
                await self.close()
                return

            self.employee_id = employee['id']
            self.employee_name = employee['name']

            self.notification_group = f"notifications_{self.employee_id}"
            await self.channel_layer.group_add(
                self.notification_group, self.channel_name
            )
            await self.accept()

            unread_data = await self.get_unread_counts()
            await self.send(text_data=json.dumps({
                "type": "unread_counts",
                "data": unread_data,
            }))

        except Exception as e:
            logger.exception(f"Notification connect error: {e}")
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, 'notification_group'):
            await self.channel_layer.group_discard(
                self.notification_group, self.channel_name
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            msg_type = data.get("type", "")

            if msg_type == "mark_read":
                message_id = data.get("message_id")
                if message_id:
                    await self.mark_notification_read(message_id)

            elif msg_type == "mark_chat_read":
                sender_id = data.get("sender_id")
                if sender_id:
                    await self.mark_chat_read(sender_id)

            elif msg_type == "mark_group_read":
                group_id = data.get("group_id")
                if group_id:
                    await self.mark_group_read(group_id)

            elif msg_type == "get_unread":
                unread_data = await self.get_unread_counts()
                await self.send(text_data=json.dumps({
                    "type": "unread_counts",
                    "data": unread_data,
                }))

        except Exception as e:
            logger.exception(f"Notification receive error: {e}")

    async def new_message_notification(self, event):
        await self.send(text_data=json.dumps({
            "type": "notification",
            "data": {
                **event["data"],
                "notification_type": "direct_message",
                "title": event["data"].get("sender_name", "New Message"),
                "body": event["data"].get("text", ""),
                "icon": event["data"].get("sender_avatar", ""),
                "sound": True,
                "vibrate": True,
            }
        }))

    async def group_message_notification(self, event):
        sender_name = event["data"].get("sender_name", "")
        group_name = event["data"].get("group_name", "")
        text = event["data"].get("text", "")

        await self.send(text_data=json.dumps({
            "type": "notification",
            "data": {
                **event["data"],
                "notification_type": "group_message",
                "title": group_name or "Group Message",
                "body": f"{sender_name}: {text}" if sender_name else text,
                "icon": event["data"].get("sender_avatar", ""),
                "sound": True,
                "vibrate": True,
            }
        }))

    async def reaction_notification(self, event):
        await self.send(text_data=json.dumps({
            "type": "notification",
            "data": {
                **event["data"],
                "notification_type": "reaction",
                "sound": False,
                "vibrate": False,
            }
        }))

    async def unread_counts_updated(self, event):
        """Handle updated unread counts and send to client."""
        await self.send(text_data=json.dumps({
            "type": "unread_counts",
            "data": event["data"]
        }))

    @database_sync_to_async
    def get_employee(self, user):
        try:
            emp = Employee.objects.get(user=user, is_active=True)
            return {'id': emp.id, 'name': emp.name}
        except Employee.DoesNotExist:
            return None

    @database_sync_to_async
    def mark_notification_read(self, message_id):
        try:
            Message.objects.filter(
                id=message_id,
                receiver_id=self.employee_id,
                is_read=False,
            ).update(is_read=True)
        except Exception:
            pass

    @database_sync_to_async
    def mark_chat_read(self, sender_id):
        try:
            Message.objects.filter(
                sender_id=sender_id,
                receiver_id=self.employee_id,
                is_read=False,
                group__isnull=True,
            ).update(is_read=True)
        except Exception:
            pass

    @database_sync_to_async
    def mark_group_read(self, group_id):
        try:
            Message.objects.filter(
                group_id=group_id,
                is_read=False,
            ).exclude(sender_id=self.employee_id).update(is_read=True)
        except Exception:
            pass

    @database_sync_to_async
    def get_unread_counts(self):
        try:
            employee = Employee.objects.get(id=self.employee_id)

            direct_unreads = {}
            unread_messages = Message.objects.filter(
                receiver=employee,
                is_read=False,
                group__isnull=True,
            ).values('sender_id', 'sender__name').annotate(count=Count('id'))

            for item in unread_messages:
                direct_unreads[str(item['sender_id'])] = {
                    "count": item['count'],
                    "sender_name": item['sender__name'],
                }

            group_unreads = {}
            groups = employee.group_memberships.all()
            for group in groups:
                unread_count = group.group_messages.filter(
                    is_read=False,
                ).exclude(sender=employee).count()

                if unread_count > 0:
                    group_unreads[str(group.id)] = {
                        "count": unread_count,
                        "group_name": group.name,
                    }

            total_unread = (
                sum(v['count'] for v in direct_unreads.values()) +
                sum(v['count'] for v in group_unreads.values())
            )

            return {
                "direct": direct_unreads,
                "groups": group_unreads,
                "total": total_unread,
            }
        except Exception as e:
            logger.exception(f"Get unread counts error: {e}")
            return {"direct": {}, "groups": {}, "total": 0}