from django.db import models
from django.contrib.auth.models import User


class UserProfile(models.Model):
    ROLE_CHOICES = [
        ('employee', 'Employee'),
        ('admin', 'Admin'),
        ('superadmin', 'Super Admin')
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)


class ChatGroup(models.Model):
    name = models.CharField(max_length=255)

    members = models.ManyToManyField(
        User,
        related_name="group_memberships"   # ✅ FIX
    )

    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="groups_created"     # ✅ FIX
    )

    def __str__(self):
        return self.name


class Message(models.Model):
    sender = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sent_messages"      # ✅ FIX
    )

    receiver = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="received_messages"  # ✅ FIX
    )

    group = models.ForeignKey(
        ChatGroup,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="group_messages"     # ✅ GOOD PRACTICE
    )

    content = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.content

from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance, role='employee')