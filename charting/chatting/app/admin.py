# app/admin.py
from django.contrib import admin
from .models import Employee, ChatGroup, Message


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    """Admin panel for Employee with visible password"""
    list_display = ('id', 'name', 'email', 'password', 'role', 'is_active', 'created_at')
    list_filter = ('role', 'is_active', 'created_at')
    search_fields = ('name', 'email')
    list_editable = ('is_active',)
    ordering = ('-created_at',)
    
    fieldsets = (
        ('Personal Information', {
            'fields': ('name', 'email', 'password', 'role')
        }),
        ('Status', {
            'fields': ('is_active',)
        }),
        ('System Link', {
            'fields': ('user',),
            'classes': ('collapse',)
        }),
    )
    
    readonly_fields = ('created_at',)


@admin.register(ChatGroup)
class ChatGroupAdmin(admin.ModelAdmin):
    """Admin panel for Chat Groups"""
    list_display = ('id', 'name', 'created_by', 'member_count', 'created_at')
    filter_horizontal = ('members',)
    search_fields = ('name',)
    
    def member_count(self, obj):
        return obj.members.count()
    member_count.short_description = 'Total Members'


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    """Admin panel for Messages"""
    list_display = ('id', 'sender', 'receiver', 'group', 'content_preview', 'is_read', 'timestamp')
    list_filter = ('timestamp', 'is_read', 'sender')
    search_fields = ('content', 'sender__name', 'sender__email')
    list_editable = ('is_read',)
    
    def content_preview(self, obj):
        return obj.content[:50] + "..." if len(obj.content) > 50 else obj.content
    content_preview.short_description = 'Message Content'