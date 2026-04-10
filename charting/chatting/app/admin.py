from django.contrib import admin
from django.utils.html import format_html
from .models import (
    Employee, ChatGroup, Message, MessageReaction, 
    AdminActivityLog, MessageDeletion, SavedMeetLink, MeetingInvitation
)


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ('id', 'profile_thumbnail', 'name', 'email', 'role', 'status', 'is_active', 'created_at')
    list_filter = ('role', 'status', 'is_active', 'created_at')
    search_fields = ('name', 'email')
    list_editable = ('is_active',)
    ordering = ('-created_at',)
    
    # ✅ ADDED: Makes it easy to block/unblock people directly from Django Admin
    filter_horizontal = ('blocked_users',) 
    
    readonly_fields = ('created_at', 'profile_preview')
    
    def profile_thumbnail(self, obj):
        if obj.profile_image:
            return format_html('<img src="{}" width="40" height="40" style="border-radius:50%;object-fit:cover;" />', obj.profile_image.url)
        return format_html('<div style="width:40px;height:40px;border-radius:50%;background:#00a884;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:bold;">{}</div>', obj.name[0].upper() if obj.name else 'U')
    profile_thumbnail.short_description = 'Avatar'
    
    def profile_preview(self, obj):
        if obj.profile_image:
            return format_html('<img src="{}" width="150" height="150" style="border-radius:10px;object-fit:cover;" />', obj.profile_image.url)
        return "No image uploaded"
    profile_preview.short_description = 'Image Preview'


@admin.register(ChatGroup)
class ChatGroupAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'created_by', 'member_count', 'is_broadcast', 'created_at')
    filter_horizontal = ('members',)
    search_fields = ('name', 'description')
    list_filter = ('is_broadcast', 'created_at', 'created_by')
    
    def member_count(self, obj):
        return obj.members.count()
    member_count.short_description = 'Total Members'


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'sender', 'receiver', 'group', 'message_type', 'is_pinned', 'content_preview', 'is_read', 'is_edited', 'timestamp')
    list_filter = ('timestamp', 'is_read', 'is_pinned', 'message_type', 'is_edited', 'is_deleted_for_everyone')
    search_fields = ('content', 'sender__name', 'sender__email')
    
    # ✅ ADDED: Lets you see and manage who starred the message in Admin
    filter_horizontal = ('starred_by',)
    
    def content_preview(self, obj):
        if obj.is_deleted_for_everyone:
            return "[Deleted]"
        if obj.content:
            return obj.content[:50] + "..." if len(obj.content) > 50 else obj.content
        return "[No text]"
    content_preview.short_description = 'Content'


@admin.register(MessageReaction)
class MessageReactionAdmin(admin.ModelAdmin):
    list_display = ('id', 'message', 'employee', 'reaction', 'created_at')
    list_filter = ('reaction', 'created_at')


@admin.register(MessageDeletion)
class MessageDeletionAdmin(admin.ModelAdmin):
    list_display = ('id', 'message', 'employee', 'deleted_at')
    list_filter = ('deleted_at',)


@admin.register(SavedMeetLink)
class SavedMeetLinkAdmin(admin.ModelAdmin):
    list_display = ('id', 'employee', 'title', 'meet_link', 'use_count', 'last_used')
    list_filter = ('last_used', 'employee')


@admin.register(MeetingInvitation)
class MeetingInvitationAdmin(admin.ModelAdmin):
    list_display = ('id', 'message', 'invitee', 'status', 'responded_at')
    list_filter = ('status', 'responded_at')


@admin.register(AdminActivityLog)
class AdminActivityLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'admin', 'action', 'target_employee', 'timestamp')
    list_filter = ('action', 'timestamp', 'admin')
    readonly_fields = ('admin', 'action', 'target_employee', 'details', 'timestamp')
    
    def has_add_permission(self, request):
        return False
    
    def has_change_permission(self, request, obj=None):
        return False