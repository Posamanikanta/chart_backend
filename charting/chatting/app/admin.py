from django.contrib import admin
from .models import Message, ChatGroup, UserProfile

# 1. Manage User Profiles (Roles like Admin/Employee)
@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role')
    list_filter = ('role',)
    search_fields = ('user__username',)

# 2. Manage Chat Groups
@admin.register(ChatGroup)
class ChatGroupAdmin(admin.ModelAdmin):
    list_display = ('name', 'created_by')
    # This makes it easy to add/remove members in the admin panel
    filter_horizontal = ('members',) 
    search_fields = ('name',)

# 3. Manage Messages (The most important for moderation)
@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    # Shows these columns in the list view
    list_display = ('sender', 'receiver', 'group', 'content_preview', 'timestamp')
    # Adds a sidebar to filter by date or sender
    list_filter = ('timestamp', 'sender')
    # Allows you to search for specific text in messages
    search_fields = ('content', 'sender__username')

    # This helper function prevents long messages from stretching the table
    def content_preview(self, obj):
        return obj.content[:50] + "..." if len(obj.content) > 50 else obj.content
    content_preview.short_description = 'Message Content'