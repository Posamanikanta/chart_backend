from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class Employee(models.Model):
    ROLE_CHOICES = [
        ('employee', 'Employee'),
        ('admin', 'Admin'),
        ('superadmin', 'Super Admin')
    ]
    
    STATUS_CHOICES = [
        ('available', 'Available'),
        ('dnd', 'Do Not Disturb'),
        ('meeting', 'In a Meeting'),
    ]
    
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='employee')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='available')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    profile_image = models.ImageField(upload_to='profile_images/', null=True, blank=True, default=None)
    about = models.CharField(max_length=500, blank=True, default="Hey there! I'm using Chat App")
    
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True, blank=True, related_name='employee_profile')

    blocked_users = models.ManyToManyField('self', symmetrical=False, blank=True, related_name='blocked_by')
    is_suspended = models.BooleanField(default=False) # ✅ Global suspension flag

    def __str__(self):
        return f"{self.name} ({self.email})"
    
    def get_avatar_url(self):
        if self.profile_image:
            return self.profile_image.url
        return f"https://ui-avatars.com/api/?name={self.name.replace(' ', '+')}&background=random&size=200"
    
    class Meta:
        verbose_name = 'Employee'
        verbose_name_plural = 'Employees'
        ordering = ['-created_at']


class ChatGroup(models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    members = models.ManyToManyField(Employee, related_name="group_memberships", blank=True)
    created_by = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="groups_created", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    group_image = models.ImageField(upload_to='group_images/', null=True, blank=True)
    is_broadcast = models.BooleanField(default=False)
    
    def __str__(self):
        return self.name
    
    def get_group_image_url(self):
        if self.group_image:
            return self.group_image.url
        return f"https://ui-avatars.com/api/?name={self.name.replace(' ', '+')}&background=00a884&color=fff&size=200"
    
    class Meta:
        ordering = ['-created_at']


class Message(models.Model):
    MESSAGE_TYPE_CHOICES = [
        ('text', 'Text'), ('image', 'Image'), ('file', 'File'),
        ('audio', 'Audio'), ('video', 'Video'), ('meet', 'Google Meet'),
    ]
    
    sender = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="sent_messages")
    receiver = models.ForeignKey(Employee, null=True, blank=True, on_delete=models.CASCADE, related_name="received_messages")
    group = models.ForeignKey(ChatGroup, null=True, blank=True, on_delete=models.CASCADE, related_name="group_messages")
    content = models.TextField(blank=True, default="")
    timestamp = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    
    message_type = models.CharField(max_length=10, choices=MESSAGE_TYPE_CHOICES, default='text')
    file = models.FileField(upload_to='message_files/%Y/%m/%d/', null=True, blank=True)
    file_name = models.CharField(max_length=255, blank=True, default="")
    file_size = models.PositiveIntegerField(null=True, blank=True)
    
    is_edited = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)
    
    is_deleted_for_everyone = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    
    reply_to = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='replies')
    is_thread_reply = models.BooleanField(default=False) # ✅ Permanent Thread Flag
    
    meet_link = models.URLField(max_length=500, blank=True, null=True)
    meet_title = models.CharField(max_length=255, blank=True, default="")
    meet_scheduled_at = models.DateTimeField(null=True, blank=True)

    is_pinned = models.BooleanField(default=False)
    starred_by = models.ManyToManyField(Employee, related_name='starred_messages', blank=True)
    
    def __str__(self):
        return f"{self.sender.name}: {self.content[:30] if self.content else '[File]'}"
    
    def get_file_url(self):
        if self.file:
            return self.file.url
        return None
    
    def can_edit(self, employee):
        if self.sender != employee: return False
        if self.is_deleted_for_everyone: return False
        time_diff = timezone.now() - self.timestamp
        return time_diff.total_seconds() < 900
    
    def can_delete_for_everyone(self, employee):
        if self.sender != employee: return False
        time_diff = timezone.now() - self.timestamp
        return time_diff.total_seconds() < 3600
    
    class Meta:
        ordering = ['timestamp']


class MessageDeletion(models.Model):
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="deletions")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="deleted_messages")
    deleted_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['message', 'employee']


class MessageReaction(models.Model):
    REACTION_CHOICES = [
        ('ok', '👍 OK'), ('not_ok', '👎 Not OK'), ('love', '❤️ Love'),
        ('laugh', '😂 Laugh'), ('wow', '😮 Wow'), ('sad', '😢 Sad'),
    ]
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="reactions")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="message_reactions")
    reaction = models.CharField(max_length=10, choices=REACTION_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['message', 'employee']
        ordering = ['-created_at']

class SavedMeetLink(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="saved_meets")
    title = models.CharField(max_length=255)
    meet_link = models.URLField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(auto_now=True)
    use_count = models.PositiveIntegerField(default=0)
    
    class Meta:
        ordering = ['-last_used']

class MeetingInvitation(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'), ('accepted', 'Accepted'),
        ('declined', 'Declined'), ('attended', 'Attended'),
    ]
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="invitations")
    invitee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="meeting_invitations")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    responded_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        unique_together = ['message', 'invitee']

class AdminActivityLog(models.Model):
    ACTION_CHOICES = [
        ('view_employee', 'Viewed Employee Dashboard'), ('view_chat', 'Viewed Chat'),
        ('create_group', 'Created Group'), ('add_member', 'Added Member to Group'),
        ('remove_member', 'Removed Member from Group'), ('exit_view', 'Exited Employee View'),
    ]
    admin = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="admin_activities")
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    target_employee = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="admin_views")
    details = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-timestamp']
        verbose_name = 'Admin Activity Log'
        verbose_name_plural = 'Admin Activity Logs'