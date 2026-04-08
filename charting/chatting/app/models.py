# app/models.py
from django.db import models
from django.contrib.auth.models import User


class Employee(models.Model):
    """Separate Employee Model with visible password"""
    ROLE_CHOICES = [
        ('employee', 'Employee'),
        ('admin', 'Admin'),
        ('superadmin', 'Super Admin')
    ]
    
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    password = models.CharField(max_length=255)  # Plain text password (visible in admin)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='employee')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    # Link to Django User for authentication
    user = models.OneToOneField(
        User, 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True, 
        related_name='employee_profile'
    )
    
    def __str__(self):
        return f"{self.name} ({self.email})"
    
    class Meta:
        verbose_name = 'Employee'
        verbose_name_plural = 'Employees'
        ordering = ['-created_at']


class ChatGroup(models.Model):
    """Group chat model"""
    name = models.CharField(max_length=255)
    members = models.ManyToManyField(Employee, related_name="group_memberships", blank=True)
    created_by = models.ForeignKey(
        Employee, 
        on_delete=models.CASCADE, 
        related_name="groups_created",
        null=True,
        blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.name
    
    class Meta:
        ordering = ['-created_at']


class Message(models.Model):
    """Message model for both direct and group messages"""
    sender = models.ForeignKey(
        Employee, 
        on_delete=models.CASCADE, 
        related_name="sent_messages"
    )
    receiver = models.ForeignKey(
        Employee, 
        null=True, 
        blank=True, 
        on_delete=models.CASCADE, 
        related_name="received_messages"
    )
    group = models.ForeignKey(
        ChatGroup, 
        null=True, 
        blank=True, 
        on_delete=models.CASCADE, 
        related_name="group_messages"
    )
    content = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    
    def __str__(self):
        return f"{self.sender.name}: {self.content[:30]}"
    
    class Meta:
        ordering = ['timestamp']