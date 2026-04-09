# app/consumers.py - COMPLETE
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Message, Employee, ChatGroup, MessageReaction
from django.db.models import Count


class ChatConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for real-time private chat"""
    
    async def connect(self):
        """Handle WebSocket connection"""
        user = self.scope.get("user")
        
        print(f"\n🔌 WebSocket connect attempt")
        print(f"   User: {user}")
        print(f"   Authenticated: {user.is_authenticated if user else False}")
        
        if not user or not user.is_authenticated:
            print("❌ WebSocket: User not authenticated")
            await self.close()
            return
        
        try:
            employee = await self.get_employee(user)
            if not employee:
                print(f"❌ No employee found for user: {user.username}")
                await self.close()
                return
            
            self.employee_id = employee['id']
            self.employee_name = employee['name']
            
            url_kwargs = self.scope['url_route']['kwargs']
            target_id_raw = url_kwargs.get('target_id')
            
            if not target_id_raw:
                print("❌ No target_id in URL")
                await self.close()
                return
                
            self.target_id = int(str(target_id_raw).replace("emp-", ""))
            
            target_exists = await self.check_employee_exists(self.target_id)
            if not target_exists:
                print(f"❌ Target employee {self.target_id} not found")
                await self.close()
                return
            
            ids = sorted([self.employee_id, self.target_id])
            self.room_group_name = f"chat_{ids[0]}_{ids[1]}"
            
            await self.channel_layer.group_add(
                self.room_group_name,
                self.channel_name
            )
            
            await self.accept()
            print(f"✅ WebSocket connected: {employee['name']} (ID: {self.employee_id}) → Target: {self.target_id}")
            print(f"   Room: {self.room_group_name}")
            
        except Exception as e:
            print(f"❌ Connection Error: {e}")
            import traceback
            traceback.print_exc()
            await self.close()

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection"""
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
            print(f"🔌 Disconnected from {self.room_group_name} (code: {close_code})")

    async def receive(self, text_data):
        """Receive message from WebSocket"""
        try:
            data = json.loads(text_data)
            msg_type = data.get("type", "message")
            
            if msg_type == "message":
                await self.handle_message(data)
            elif msg_type == "reaction":
                await self.handle_reaction(data)
            elif msg_type == "typing":
                await self.handle_typing(data)
            elif msg_type == "edit":
                await self.handle_edit(data)
            elif msg_type == "delete":
                await self.handle_delete(data)
                
        except json.JSONDecodeError:
            print("❌ Invalid JSON received")
        except Exception as e:
            print(f"❌ Receive Error: {e}")
            import traceback
            traceback.print_exc()

    async def handle_message(self, data):
        """Handle incoming text message"""
        message_text = data.get("message", "").strip()
        
        if not message_text:
            print("⚠️ Empty message received")
            return
        
        msg_obj = await self.save_message(message_text)
        
        if msg_obj:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "chat_message",
                    "data": msg_obj
                }
            )
            print(f"📤 Message sent to room {self.room_group_name}: {message_text[:30]}...")
        else:
            print("❌ Failed to save message")

    async def handle_reaction(self, data):
        """Handle reaction update"""
        message_id = data.get("message_id")
        reaction = data.get("reaction")  # 'ok', 'not_ok', or None to remove
        
        result = await self.save_reaction(message_id, reaction)
        
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "reaction_update",
                    "data": result
                }
            )

    async def handle_typing(self, data):
        """Handle typing indicator"""
        is_typing = data.get("is_typing", False)
        
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "typing_indicator",
                "data": {
                    "sender_id": self.employee_id,
                    "sender_name": self.employee_name,
                    "is_typing": is_typing
                }
            }
        )

    async def handle_edit(self, data):
        """Handle message edit via WebSocket"""
        message_id = data.get("message_id")
        new_content = data.get("content", "").strip()
        
        result = await self.edit_message_db(message_id, new_content)
        
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "message_edited",
                    "data": result
                }
            )

    async def handle_delete(self, data):
        """Handle message delete via WebSocket"""
        message_id = data.get("message_id")
        delete_type = data.get("delete_type", "for_me")  # 'for_me' or 'for_everyone'
        
        result = await self.delete_message_db(message_id, delete_type)
        
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "message_deleted",
                    "data": result
                }
            )

    async def chat_message(self, event):
        """Send message to WebSocket"""
        await self.send(text_data=json.dumps({
            "type": "message",
            "data": event["data"]
        }))

    async def reaction_update(self, event):
        """Send reaction update to WebSocket"""
        await self.send(text_data=json.dumps({
            "type": "reaction",
            "data": event["data"]
        }))

    async def typing_indicator(self, event):
        """Send typing indicator to WebSocket"""
        if event["data"]["sender_id"] != self.employee_id:
            await self.send(text_data=json.dumps({
                "type": "typing",
                "data": event["data"]
            }))

    async def file_message(self, event):
        """Send file message to WebSocket"""
        await self.send(text_data=json.dumps({
            "type": "file",
            "data": event["data"]
        }))

    async def message_edited(self, event):
        """Send edited message notification"""
        await self.send(text_data=json.dumps({
            "type": "edited",
            "data": event["data"]
        }))

    async def message_deleted(self, event):
        """Send deleted message notification"""
        await self.send(text_data=json.dumps({
            "type": "deleted",
            "data": event["data"]
        }))

    @database_sync_to_async
    def get_employee(self, user):
        """Get employee from Django user"""
        try:
            employee = Employee.objects.get(user=user, is_active=True)
            return {
                'id': employee.id,
                'name': employee.name,
                'email': employee.email
            }
        except Employee.DoesNotExist:
            return None

    @database_sync_to_async
    def check_employee_exists(self, employee_id):
        """Check if employee exists"""
        return Employee.objects.filter(id=employee_id, is_active=True).exists()

    @database_sync_to_async
    def save_message(self, text, message_type='text', file_url=None, file_name=None, file_size=None):
        """Save message to database"""
        try:
            sender = Employee.objects.get(id=self.employee_id, is_active=True)
            receiver = Employee.objects.get(id=self.target_id, is_active=True)
            
            msg = Message.objects.create(
                sender=sender,
                receiver=receiver,
                content=text,
                message_type=message_type,
                is_read=False
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
            }
        except Employee.DoesNotExist as e:
            print(f"❌ Employee not found: {e}")
            return None
        except Exception as e:
            print(f"❌ Save Error: {e}")
            import traceback
            traceback.print_exc()
            return None

    @database_sync_to_async
    def save_reaction(self, message_id, reaction):
        """Save or remove reaction"""
        try:
            employee = Employee.objects.get(id=self.employee_id, is_active=True)
            message = Message.objects.get(id=message_id)
            
            if reaction in ['ok', 'not_ok']:
                MessageReaction.objects.update_or_create(
                    message=message,
                    employee=employee,
                    defaults={'reaction': reaction}
                )
            elif reaction is None:
                MessageReaction.objects.filter(
                    message=message,
                    employee=employee
                ).delete()
            
            # Get updated counts
            reactions = list(message.reactions.values('reaction').annotate(count=Count('reaction')))
            
            return {
                "message_id": message_id,
                "reactions": {r['reaction']: r['count'] for r in reactions},
                "employee_id": self.employee_id,
                "reaction": reaction
            }
        except Exception as e:
            print(f"❌ Reaction Error: {e}")
            return None

    @database_sync_to_async
    def edit_message_db(self, message_id, new_content):
        """Edit message in database"""
        try:
            from django.utils import timezone
            employee = Employee.objects.get(id=self.employee_id)
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
        except Exception as e:
            print(f"❌ Edit Error: {e}")
            return None

    @database_sync_to_async
    def delete_message_db(self, message_id, delete_type):
        """Delete message in database"""
        try:
            from django.utils import timezone
            from .models import MessageDeletion
            
            employee = Employee.objects.get(id=self.employee_id)
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
                
                return {
                    "message_id": message_id,
                    "deletedForEveryone": True,
                }
            else:
                MessageDeletion.objects.get_or_create(
                    message=message,
                    employee=employee
                )
                return {
                    "message_id": message_id,
                    "deletedForMe": True,
                    "employee_id": self.employee_id,
                }
        except Exception as e:
            print(f"❌ Delete Error: {e}")
            return None


class GroupChatConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for group chat"""
    
    async def connect(self):
        """Handle WebSocket connection"""
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
            
            url_kwargs = self.scope['url_route']['kwargs']
            self.group_id = int(url_kwargs.get('group_id'))
            
            # Check if user is member of group
            is_member = await self.check_group_membership()
            if not is_member:
                print(f"❌ User {self.employee_id} is not a member of group {self.group_id}")
                await self.close()
                return
            
            self.room_group_name = f"group_{self.group_id}"
            
            await self.channel_layer.group_add(
                self.room_group_name,
                self.channel_name
            )
            
            await self.accept()
            print(f"✅ WebSocket connected to group {self.group_id}: {employee['name']}")
            
        except Exception as e:
            print(f"❌ Connection Error: {e}")
            import traceback
            traceback.print_exc()
            await self.close()

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection"""
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    async def receive(self, text_data):
        """Receive message from WebSocket"""
        try:
            data = json.loads(text_data)
            msg_type = data.get("type", "message")
            
            if msg_type == "message":
                await self.handle_message(data)
            elif msg_type == "reaction":
                await self.handle_reaction(data)
            elif msg_type == "typing":
                await self.handle_typing(data)
            elif msg_type == "edit":
                await self.handle_edit(data)
            elif msg_type == "delete":
                await self.handle_delete(data)
                
        except json.JSONDecodeError:
            print("❌ Invalid JSON received")
        except Exception as e:
            print(f"❌ Receive Error: {e}")

    async def handle_message(self, data):
        """Handle incoming message"""
        message_text = data.get("message", "").strip()
        
        if not message_text:
            return
        
        msg_obj = await self.save_group_message(message_text)
        
        if msg_obj:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "chat_message",
                    "data": msg_obj
                }
            )

    async def handle_reaction(self, data):
        """Handle reaction"""
        message_id = data.get("message_id")
        reaction = data.get("reaction")
        
        result = await self.save_reaction(message_id, reaction)
        
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "reaction_update",
                    "data": result
                }
            )

    async def handle_typing(self, data):
        """Handle typing indicator"""
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "typing_indicator",
                "data": {
                    "sender_id": self.employee_id,
                    "sender_name": self.employee_name,
                    "is_typing": data.get("is_typing", False)
                }
            }
        )

    async def handle_edit(self, data):
        """Handle message edit"""
        message_id = data.get("message_id")
        new_content = data.get("content", "").strip()
        
        result = await self.edit_message_db(message_id, new_content)
        
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "message_edited",
                    "data": result
                }
            )

    async def handle_delete(self, data):
        """Handle message delete"""
        message_id = data.get("message_id")
        delete_type = data.get("delete_type", "for_me")
        
        result = await self.delete_message_db(message_id, delete_type)
        
        if result:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "message_deleted",
                    "data": result
                }
            )

    async def chat_message(self, event):
        """Send message to WebSocket"""
        await self.send(text_data=json.dumps({
            "type": "message",
            "data": event["data"]
        }))

    async def reaction_update(self, event):
        """Send reaction update"""
        await self.send(text_data=json.dumps({
            "type": "reaction",
            "data": event["data"]
        }))

    async def typing_indicator(self, event):
        """Send typing indicator"""
        if event["data"]["sender_id"] != self.employee_id:
            await self.send(text_data=json.dumps({
                "type": "typing",
                "data": event["data"]
            }))

    async def message_edited(self, event):
        """Send edited message notification"""
        await self.send(text_data=json.dumps({
            "type": "edited",
            "data": event["data"]
        }))

    async def message_deleted(self, event):
        """Send deleted message notification"""
        await self.send(text_data=json.dumps({
            "type": "deleted",
            "data": event["data"]
        }))

    @database_sync_to_async
    def get_employee(self, user):
        """Get employee from user"""
        try:
            employee = Employee.objects.get(user=user, is_active=True)
            return {
                'id': employee.id,
                'name': employee.name,
                'email': employee.email,
                'avatar': employee.get_avatar_url()
            }
        except Employee.DoesNotExist:
            return None

    @database_sync_to_async
    def check_group_membership(self):
        """Check if user is member of group"""
        try:
            employee = Employee.objects.get(id=self.employee_id)
            group = ChatGroup.objects.get(id=self.group_id)
            return employee in group.members.all() or employee.role in ['admin', 'superadmin']
        except:
            return False

    @database_sync_to_async
    def save_group_message(self, text, message_type='text'):
        """Save group message"""
        try:
            sender = Employee.objects.get(id=self.employee_id, is_active=True)
            group = ChatGroup.objects.get(id=self.group_id)
            
            msg = Message.objects.create(
                sender=sender,
                group=group,
                content=text,
                message_type=message_type,
                is_read=False
            )
            
            return {
                "id": msg.id,
                "text": msg.content,
                "sender_id": sender.id,
                "sender_name": sender.name,
                "sender_avatar": sender.get_avatar_url(),
                "group_id": group.id,
                "createdAt": msg.timestamp.isoformat(),
                "messageType": msg.message_type,
                "reactions": {},
                "userReaction": None,
                "isEdited": False,
                "canEdit": True,
                "canDeleteForEveryone": True,
            }
        except Exception as e:
            print(f"❌ Save Error: {e}")
            return None

    @database_sync_to_async
    def save_reaction(self, message_id, reaction):
        """Save reaction"""
        try:
            employee = Employee.objects.get(id=self.employee_id)
            message = Message.objects.get(id=message_id)
            
            if reaction in ['ok', 'not_ok']:
                MessageReaction.objects.update_or_create(
                    message=message,
                    employee=employee,
                    defaults={'reaction': reaction}
                )
            elif reaction is None:
                MessageReaction.objects.filter(
                    message=message,
                    employee=employee
                ).delete()
            
            reactions = list(message.reactions.values('reaction').annotate(count=Count('reaction')))
            
            return {
                "message_id": message_id,
                "reactions": {r['reaction']: r['count'] for r in reactions},
                "employee_id": self.employee_id,
                "reaction": reaction
            }
        except Exception as e:
            print(f"❌ Reaction Error: {e}")
            return None

    @database_sync_to_async
    def edit_message_db(self, message_id, new_content):
        """Edit message in database"""
        try:
            from django.utils import timezone
            employee = Employee.objects.get(id=self.employee_id)
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
        except Exception as e:
            print(f"❌ Edit Error: {e}")
            return None

    @database_sync_to_async
    def delete_message_db(self, message_id, delete_type):
        """Delete message in database"""
        try:
            from django.utils import timezone
            from .models import MessageDeletion
            
            employee = Employee.objects.get(id=self.employee_id)
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
                
                return {
                    "message_id": message_id,
                    "deletedForEveryone": True,
                }
            else:
                MessageDeletion.objects.get_or_create(
                    message=message,
                    employee=employee
                )
                return {
                    "message_id": message_id,
                    "deletedForMe": True,
                    "employee_id": self.employee_id,
                }
        except Exception as e:
            print(f"❌ Delete Error: {e}")
            return None