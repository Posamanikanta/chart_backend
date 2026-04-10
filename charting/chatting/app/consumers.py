import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Message, Employee, ChatGroup, MessageReaction, MessageDeletion
from django.db.models import Count
from django.utils import timezone

logger = logging.getLogger(__name__)


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
            
            # ✅ SECURITY: Drop connection immediately if user is suspended
            if employee.get('is_suspended', False):
                logger.warning(f"Suspended user {employee['email']} attempted to connect.")
                await self.close()
                return
            
            self.employee_id = employee['id']
            self.employee_name = employee['name']
            
            url_kwargs = self.scope['url_route']['kwargs']
            target_id_raw = url_kwargs.get('target_id')
            if not target_id_raw:
                await self.close()
                return
            
            target_id_str = str(target_id_raw)
            if target_id_str.startswith("emp-"): 
                self.target_id = int(target_id_str.replace("emp-", ""))
            else: 
                self.target_id = int(target_id_str)
            
            target_exists = await self.check_employee_exists(self.target_id)
            if not target_exists:
                await self.close()
                return
            
            ids = sorted([self.employee_id, self.target_id])
            self.room_group_name = f"chat_{ids[0]}_{ids[1]}"
            
            await self.channel_layer.group_add(self.room_group_name, self.channel_name)
            await self.accept()
            
        except Exception as e:
            logger.exception(f"Connection Error: {e}")
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            msg_type = data.get("type", "message")
            
            if msg_type == "message": await self.handle_message(data)
            elif msg_type == "reaction": await self.handle_reaction(data)
            elif msg_type == "typing": await self.handle_typing(data)
            elif msg_type == "edit": await self.handle_edit(data)
            elif msg_type == "delete": await self.handle_delete(data)
            elif msg_type == "read": await self.handle_read(data)
                
        except Exception as e:
            logger.exception(f"Receive Error: {e}")

    async def handle_message(self, data):
        message_text = data.get("message", "").strip()
        if not message_text: return
        
        msg_obj = await self.save_message(message_text, reply_to_id=data.get("reply_to"))
        if msg_obj:
            await self.channel_layer.group_send(self.room_group_name, {"type": "chat_message", "data": msg_obj})

    async def handle_reaction(self, data):
        result = await self.save_reaction(data.get("message_id"), data.get("reaction"))
        if result: 
            await self.channel_layer.group_send(self.room_group_name, {"type": "reaction_update", "data": result})

    async def handle_typing(self, data):
        await self.channel_layer.group_send(self.room_group_name, {
            "type": "typing_indicator", "data": { "sender_id": self.employee_id, "is_typing": data.get("is_typing", False) }
        })

    async def handle_edit(self, data):
        result = await self.edit_message_db(data.get("message_id"), data.get("content", "").strip())
        if result: 
            await self.channel_layer.group_send(self.room_group_name, {"type": "message_edited", "data": result})

    async def handle_delete(self, data):
        result = await self.delete_message_db(data.get("message_id"), data.get("delete_type", "for_me"))
        if result: 
            await self.channel_layer.group_send(self.room_group_name, {"type": "message_deleted", "data": result})

    async def handle_read(self, data):
        await self.mark_messages_read()
        await self.channel_layer.group_send(self.room_group_name, {"type": "messages_read", "data": {"reader_id": self.employee_id, "target_id": self.target_id}})

    # --- WEBSOCKET EVENT SENDERS ---
    async def chat_message(self, event): 
        await self.send(text_data=json.dumps({"type": "message", "data": event["data"]}))
        
    async def reaction_update(self, event): 
        await self.send(text_data=json.dumps({"type": "reaction", "data": event["data"]}))
        
    async def typing_indicator(self, event): 
        if event["data"]["sender_id"] != self.employee_id: 
            await self.send(text_data=json.dumps({"type": "typing", "data": event["data"]}))
            
    async def message_edited(self, event): 
        await self.send(text_data=json.dumps({"type": "edited", "data": event["data"]}))
        
    async def message_deleted(self, event): 
        await self.send(text_data=json.dumps({"type": "deleted", "data": event["data"]}))
        
    async def messages_read(self, event): 
        await self.send(text_data=json.dumps({"type": "read", "data": event["data"]}))

    # --- DATABASE METHODS ---
    @database_sync_to_async
    def get_employee(self, user):
        try:
            employee = Employee.objects.get(user=user, is_active=True)
            return { 'id': employee.id, 'name': employee.name, 'email': employee.email, 'is_suspended': employee.is_suspended }
        except Employee.DoesNotExist:
            return None

    @database_sync_to_async
    def check_employee_exists(self, employee_id):
        return Employee.objects.filter(id=employee_id, is_active=True).exists()

    @database_sync_to_async
    def save_message(self, text, message_type='text', file_url=None, file_name=None, file_size=None, reply_to_id=None):
        try:
            sender = Employee.objects.get(id=self.employee_id, is_active=True)
            if sender.is_suspended: return None 

            receiver = Employee.objects.get(id=self.target_id, is_active=True)
            
            reply_to = None
            reply_to_data = None
            if reply_to_id:
                # ✅ FIX: React sometimes sends a full object instead of an ID. Extract the ID safely to prevent crashing.
                if isinstance(reply_to_id, dict):
                    reply_to_id = reply_to_id.get('id')
                
                try: 
                    reply_to = Message.objects.get(id=reply_to_id)
                    reply_to_data = {
                        "id": reply_to.id,
                        "text": reply_to.content[:100] if reply_to.content else "",
                        "sender_name": reply_to.sender.name
                    }
                except Message.DoesNotExist: pass
            
            msg = Message.objects.create(
                sender=sender, receiver=receiver, content=text, 
                message_type=message_type, is_read=False, reply_to=reply_to
            )
            
            return {
                "id": msg.id, 
                "text": msg.content, 
                "sender_id": sender.id, 
                "sender_name": sender.name,
                "sender_avatar": sender.get_avatar_url(), 
                "receiver_id": receiver.id, 
                "createdAt": msg.timestamp.isoformat(),
                "messageType": msg.message_type, 
                "fileUrl": file_url, 
                "fileName": file_name, 
                "fileSize": file_size,
                "reactions": {}, 
                "userReaction": None, 
                "isEdited": False, 
                "canEdit": True, 
                "canDeleteForEveryone": True,
                "replyTo": reply_to_data, 
                "isRead": False,
                "isPinned": False,      
                "isStarred": False,     
                "thread": [],           
            }
        except Exception as e:
            logger.exception(f"Save Error: {e}")
            return None

    @database_sync_to_async
    def save_reaction(self, message_id, reaction):
        try:
            employee = Employee.objects.get(id=self.employee_id, is_active=True)
            if employee.is_suspended: return None # ✅ SECURITY
            
            message = Message.objects.get(id=message_id)
            valid_reactions = ['ok', 'not_ok', 'love', 'laugh', 'wow', 'sad']
            
            if reaction in valid_reactions:
                MessageReaction.objects.update_or_create(
                    message=message, employee=employee, defaults={'reaction': reaction}
                )
            elif reaction is None:
                MessageReaction.objects.filter(message=message, employee=employee).delete()
            
            reactions = list(message.reactions.values('reaction').annotate(count=Count('reaction')))
            return { "message_id": message_id, "reactions": {r['reaction']: r['count'] for r in reactions}, "employee_id": self.employee_id, "reaction": reaction }
        except Exception: return None

    @database_sync_to_async
    def edit_message_db(self, message_id, new_content):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            if employee.is_suspended: return None
            message = Message.objects.get(id=message_id, sender=employee)
            if not message.can_edit(employee): return None
            message.content = new_content
            message.is_edited = True
            message.edited_at = timezone.now()
            message.save()
            return { "message_id": message_id, "content": new_content, "isEdited": True, "editedAt": message.edited_at.isoformat() }
        except Exception: return None

    @database_sync_to_async
    def delete_message_db(self, message_id, delete_type):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            if employee.is_suspended: return None
            message = Message.objects.get(id=message_id)
            
            if delete_type == 'for_everyone':
                if message.sender != employee or not message.can_delete_for_everyone(employee): return None
                message.is_deleted_for_everyone = True
                message.deleted_at = timezone.now()
                message.content = ""
                message.save()
                if message.file: message.file.delete()
                return { "message_id": message_id, "deletedForEveryone": True }
            else:
                MessageDeletion.objects.get_or_create(message=message, employee=employee)
                return { "message_id": message_id, "deletedForMe": True }
        except Exception: return None

    @database_sync_to_async
    def mark_messages_read(self):
        try:
            Message.objects.filter(sender_id=self.target_id, receiver_id=self.employee_id, is_read=False, group__isnull=True).update(is_read=True)
        except Exception: pass


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
            
            # ✅ SECURITY: Drop connection immediately if user is suspended
            if employee.get('is_suspended', False):
                await self.close()
                return
            
            self.employee_id = employee['id']
            self.employee_name = employee['name']
            
            url_kwargs = self.scope['url_route']['kwargs']
            self.group_id = int(url_kwargs.get('group_id'))
            
            if not await self.check_group_membership():
                await self.close()
                return
            
            self.room_group_name = f"group_{self.group_id}"
            await self.channel_layer.group_add(self.room_group_name, self.channel_name)
            await self.accept()
        except Exception:
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            msg_type = data.get("type", "message")
            if msg_type == "message": await self.handle_message(data)
            elif msg_type == "reaction": await self.handle_reaction(data)
            elif msg_type == "typing": await self.handle_typing(data)
            elif msg_type == "edit": await self.handle_edit(data)
            elif msg_type == "delete": await self.handle_delete(data)
        except Exception: pass

    async def handle_message(self, data):
        msg_obj = await self.save_group_message(data.get("message", "").strip(), reply_to_id=data.get("reply_to"))
        if msg_obj: await self.channel_layer.group_send(self.room_group_name, {"type": "chat_message", "data": msg_obj})

    async def handle_reaction(self, data):
        result = await self.save_reaction(data.get("message_id"), data.get("reaction"))
        if result: await self.channel_layer.group_send(self.room_group_name, {"type": "reaction_update", "data": result})

    async def handle_typing(self, data):
        await self.channel_layer.group_send(self.room_group_name, { "type": "typing_indicator", "data": { "sender_id": self.employee_id, "is_typing": data.get("is_typing", False) }})

    async def handle_edit(self, data):
        result = await self.edit_message_db(data.get("message_id"), data.get("content", "").strip())
        if result: await self.channel_layer.group_send(self.room_group_name, {"type": "message_edited", "data": result})

    async def handle_delete(self, data):
        result = await self.delete_message_db(data.get("message_id"), data.get("delete_type", "for_me"))
        if result: await self.channel_layer.group_send(self.room_group_name, {"type": "message_deleted", "data": result})

    async def chat_message(self, event): await self.send(text_data=json.dumps({"type": "message", "data": event["data"]}))
    async def reaction_update(self, event): await self.send(text_data=json.dumps({"type": "reaction", "data": event["data"]}))
    async def typing_indicator(self, event): 
        if event["data"]["sender_id"] != self.employee_id: await self.send(text_data=json.dumps({"type": "typing", "data": event["data"]}))
    async def message_edited(self, event): await self.send(text_data=json.dumps({"type": "edited", "data": event["data"]}))
    async def message_deleted(self, event): await self.send(text_data=json.dumps({"type": "deleted", "data": event["data"]}))

    @database_sync_to_async
    def get_employee(self, user):
        try:
            employee = Employee.objects.get(user=user, is_active=True)
            return { 'id': employee.id, 'name': employee.name, 'is_suspended': employee.is_suspended }
        except Employee.DoesNotExist: return None

    @database_sync_to_async
    def check_group_membership(self):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            group = ChatGroup.objects.get(id=self.group_id)
            return employee in group.members.all() or employee.role in ['admin', 'superadmin']
        except: return False

    @database_sync_to_async
    def save_group_message(self, text, message_type='text', reply_to_id=None):
        try:
            sender = Employee.objects.get(id=self.employee_id, is_active=True)
            if sender.is_suspended: return None # ✅ SECURITY

            group = ChatGroup.objects.get(id=self.group_id)
            if group.is_broadcast and not (group.members.filter(id=sender.id).exists() and sender.role in ['admin', 'superadmin']):
                return None
            
            reply_to = None
            reply_to_data = None
            if reply_to_id:
                # ✅ FIX: React sometimes sends a full object instead of an ID. Extract the ID safely to prevent crashing.
                if isinstance(reply_to_id, dict):
                    reply_to_id = reply_to_id.get('id')
                    
                try: 
                    reply_to = Message.objects.get(id=reply_to_id)
                    reply_to_data = {
                        "id": reply_to.id,
                        "text": reply_to.content[:100] if reply_to.content else "",
                        "sender_name": reply_to.sender.name
                    }
                except Message.DoesNotExist: pass
            
            msg = Message.objects.create(
                sender=sender, group=group, content=text, 
                message_type=message_type, is_read=False, reply_to=reply_to
            )
            
            return {
                "id": msg.id, "text": msg.content, "sender_id": sender.id, "sender_name": sender.name,
                "sender_avatar": sender.get_avatar_url(), "group_id": group.id, "createdAt": msg.timestamp.isoformat(),
                "messageType": msg.message_type, "reactions": {}, "userReaction": None, "isEdited": False, 
                "canEdit": True, "canDeleteForEveryone": True, "replyTo": reply_to_data,
                "isPinned": False, "isStarred": False, "thread": []
            }
        except Exception as e: 
            logger.exception(f"Save Group Error: {e}")
            return None

    @database_sync_to_async
    def save_reaction(self, message_id, reaction):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            if employee.is_suspended: return None
            message = Message.objects.get(id=message_id)
            if reaction in ['ok', 'not_ok', 'love', 'laugh', 'wow', 'sad']:
                MessageReaction.objects.update_or_create(message=message, employee=employee, defaults={'reaction': reaction})
            elif reaction is None:
                MessageReaction.objects.filter(message=message, employee=employee).delete()
            reactions = list(message.reactions.values('reaction').annotate(count=Count('reaction')))
            return { "message_id": message_id, "reactions": {r['reaction']: r['count'] for r in reactions}, "employee_id": self.employee_id, "reaction": reaction }
        except Exception: return None

    @database_sync_to_async
    def edit_message_db(self, message_id, new_content):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            if employee.is_suspended: return None
            message = Message.objects.get(id=message_id, sender=employee)
            if not message.can_edit(employee): return None
            message.content = new_content
            message.is_edited = True
            message.edited_at = timezone.now()
            message.save()
            return { "message_id": message_id, "content": new_content, "isEdited": True, "editedAt": message.edited_at.isoformat() }
        except Exception: return None

    @database_sync_to_async
    def delete_message_db(self, message_id, delete_type):
        try:
            employee = Employee.objects.get(id=self.employee_id)
            if employee.is_suspended: return None
            message = Message.objects.get(id=message_id)
            if delete_type == 'for_everyone':
                if message.sender != employee or not message.can_delete_for_everyone(employee): return None
                message.is_deleted_for_everyone = True
                message.content = ""
                message.save()
                if message.file: message.file.delete()
                return { "message_id": message_id, "deletedForEveryone": True }
            else:
                MessageDeletion.objects.get_or_create(message=message, employee=employee)
                return { "message_id": message_id, "deletedForMe": True }
        except Exception: return None