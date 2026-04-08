import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Message

class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        try:
            # 1. Get the raw parameters from the URL
            self.user_id_raw = self.scope['url_route']['kwargs']['user_id']
            self.target_id_raw = self.scope['url_route']['kwargs']['target_id']

            # 2. Clean IDs safely: remove "emp-" if it exists, then convert to int
            u_id = int(str(self.user_id_raw).replace("emp-", ""))
            t_id = int(str(self.target_id_raw).replace("emp-", ""))

            # 3. Create a sorted room name
            ids = sorted([u_id, t_id])
            self.room_group_name = f"chat_{ids[0]}_{ids[1]}"

            # 4. Join the room
            await self.channel_layer.group_add(self.room_group_name, self.channel_name)
            
            # 5. ACCEPT THE CONNECTION (Crucial)
            await self.accept()
            print(f"✅ WebSocket connected to room: {self.room_group_name}")

        except Exception as e:
            print(f"❌ WebSocket Connect Error: {e}")
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
            print("🔌 WebSocket Disconnected")

    async def receive(self, text_data):
        data = json.loads(text_data)
        message_text = data.get("message")
        
        msg_obj = await self.save_message(self.user_id_raw, self.target_id_raw, message_text)

        if msg_obj:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "chat_message",
                    "data": msg_obj
                }
            )

    async def chat_message(self, event):
        await self.send(text_data=json.dumps(event["data"]))

    @database_sync_to_async
    def save_message(self, sender_str, receiver_str, text):
        try:
            s_id = int(str(sender_str).replace("emp-", ""))
            r_id = int(str(receiver_str).replace("emp-", ""))
            msg = Message.objects.create(sender_id=s_id, receiver_id=r_id, content=text)
            return {
                "id": msg.id,
                "text": msg.content,
                "sender_id": s_id,
                "createdAt": str(msg.timestamp)
            }
        except Exception as e:
            print(f"Error saving message: {e}")
            return None