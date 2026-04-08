# app/consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Message, Employee


class ChatConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for real-time chat"""
    
    async def connect(self):
        """Handle WebSocket connection"""
        # Check if user is authenticated
        if not self.scope["user"].is_authenticated:
            print("❌ Unauthenticated WebSocket connection attempt")
            await self.close()
            return
        
        try:
            # Get employee from authenticated user
            employee = await self.get_employee(self.scope["user"])
            if not employee:
                print("❌ No employee found for user")
                await self.close()
                return
            
            self.employee_id = employee['id']
            
            # Get target_id from URL
            target_id_raw = self.scope['url_route']['kwargs']['target_id']
            self.target_id = int(str(target_id_raw).replace("emp-", ""))
            
            # Create unique room name (sorted to ensure same room for both users)
            ids = sorted([self.employee_id, self.target_id])
            self.room_group_name = f"chat_{ids[0]}_{ids[1]}"
            
            # Join room group
            await self.channel_layer.group_add(
                self.room_group_name,
                self.channel_name
            )
            
            await self.accept()
            print(f"✅ WebSocket connected: Employee {self.employee_id} → {self.target_id} (Room: {self.room_group_name})")
            
        except Exception as e:
            print(f"❌ Connection Error: {e}")
            await self.close()

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection"""
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
            print(f"🔌 Disconnected from {self.room_group_name}")

    async def receive(self, text_data):
        """Receive message from WebSocket"""
        try:
            data = json.loads(text_data)
            message_text = data.get("message", "").strip()
            
            if not message_text:
                return
            
            # Save message to database
            msg_obj = await self.save_message(message_text)
            
            if msg_obj:
                # Broadcast to room group
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "chat_message",
                        "data": msg_obj
                    }
                )
        except json.JSONDecodeError:
            print("❌ Invalid JSON received")
        except Exception as e:
            print(f"❌ Receive Error: {e}")

    async def chat_message(self, event):
        """Receive message from room group and send to WebSocket"""
        await self.send(text_data=json.dumps(event["data"]))

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
    def save_message(self, text):
        """Save message to database"""
        try:
            sender = Employee.objects.get(id=self.employee_id)
            receiver = Employee.objects.get(id=self.target_id)
            
            msg = Message.objects.create(
                sender=sender,
                receiver=receiver,
                content=text
            )
            
            return {
                "id": msg.id,
                "text": msg.content,
                "sender_id": sender.id,
                "receiver_id": receiver.id,
                "createdAt": msg.timestamp.isoformat()
            }
        except Employee.DoesNotExist as e:
            print(f"❌ Employee not found: {e}")
            return None
        except Exception as e:
            print(f"❌ Save Error: {e}")
            return None