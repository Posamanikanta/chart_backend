from django.contrib import admin
from django.contrib.auth.models import User
from django.utils.html import format_html
from .models import (
    Employee, ChatGroup, Message, MessageReaction,
    AdminActivityLog, MessageDeletion, SavedMeetLink, MeetingInvitation,
    Poll, PollOption, PollVote
)


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'profile_thumbnail', 'name', 'email', 'role',
        'password_display', 'status', 'is_suspended', 'is_active', 'created_at'
    )
    list_filter = ('role', 'status', 'is_active', 'is_suspended', 'created_at')
    search_fields = ('name', 'email')
    list_editable = ('is_active', 'is_suspended')
    ordering = ('-created_at',)
    filter_horizontal = ('blocked_users',)
    readonly_fields = ('created_at', 'profile_preview', 'user')

    fieldsets = (
        ('Basic Info', {
            'fields': ('name', 'email', 'role', 'status')
        }),
        ('Password', {
            # ✅ Changed from plain_password to password
            'fields': ('password',),
            'description': (
                'Enter a password here. This is stored as plain text '
                'for admin reference and used directly for login authentication.'
            ),
        }),
        ('Profile', {
            'fields': ('profile_image', 'profile_preview', 'about')
        }),
        ('Account Status', {
            'fields': ('is_active', 'is_suspended', 'blocked_users')
        }),
        ('System', {
            'fields': ('user', 'created_at'),
            'classes': ('collapse',),
        }),
    )

    def profile_thumbnail(self, obj):
        if obj.profile_image:
            return format_html(
                '<img src="{}" width="40" height="40" '
                'style="border-radius:50%;object-fit:cover;" />',
                obj.profile_image.url
            )
        initial = obj.name[0].upper() if obj.name else 'U'
        return format_html(
            '<div style="width:40px;height:40px;border-radius:50%;background:#00a884;'
            'color:#fff;display:flex;align-items:center;justify-content:center;'
            'font-weight:bold;">{}</div>',
            initial
        )
    profile_thumbnail.short_description = 'Avatar'

    def profile_preview(self, obj):
        if obj.profile_image:
            return format_html(
                '<img src="{}" width="150" height="150" '
                'style="border-radius:10px;object-fit:cover;" />',
                obj.profile_image.url
            )
        return "No image uploaded"
    profile_preview.short_description = 'Image Preview'

    def password_display(self, obj):
        # ✅ Changed from obj.plain_password to obj.password
        if obj.password:
            pw = obj.password
            if len(pw) > 3:
                masked = pw[:2] + '•' * (len(pw) - 3) + pw[-1]
            else:
                masked = pw
            return format_html(
                '<span title="Click edit to see full password" '
                'style="font-family:monospace;background:#f0f2f5;padding:2px 8px;'
                'border-radius:4px;cursor:help;">{}</span>',
                masked
            )
        return format_html(
            '<span style="color:#999;font-style:italic;">Not set</span>'
        )
    password_display.short_description = 'Password'

    def save_model(self, request, obj, form, change):
        """
        When saving an Employee from admin:
        - Password is stored directly in Employee.password field
        - Django User is created/updated for session management only
        - Django User gets unusable password (not used for auth)
        """
        # ✅ Use obj.password instead of obj.plain_password
        new_password = obj.password

        if not change:
            # ── Creating new employee ────────────────────────
            if not new_password:
                new_password = 'changeme123'
                obj.password = new_password  # ✅ Fixed field name

            # Create Django User for session management only
            if not obj.user:
                user, created = User.objects.get_or_create(
                    username=obj.email,
                    defaults={
                        'email': obj.email,
                        'first_name': obj.name,
                        'is_active': True,
                    }
                )
                # ✅ Set unusable password - Django User not used for auth
                user.set_unusable_password()
                user.first_name = obj.name
                user.email = obj.email
                user.is_active = True
                user.save()
                obj.user = user

            obj.save()

        else:
            # ── Editing existing employee ────────────────────
            if obj.pk:
                try:
                    old_obj = Employee.objects.get(pk=obj.pk)
                    old_password = old_obj.password  # ✅ Fixed field name
                except Employee.DoesNotExist:
                    old_password = ""

                # Sync Django User info (name/email) but NOT password
                if obj.user:
                    obj.user.first_name = obj.name
                    obj.user.email = obj.email
                    obj.user.is_active = obj.is_active
                    obj.user.save()
                else:
                    # Create Django User if missing
                    user, created = User.objects.get_or_create(
                        username=obj.email,
                        defaults={
                            'email': obj.email,
                            'first_name': obj.name,
                            'is_active': obj.is_active,
                        }
                    )
                    user.set_unusable_password()
                    user.save()
                    obj.user = user

            super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request, obj=None):
        readonly = list(self.readonly_fields)
        return readonly


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
    list_display = (
        'id', 'sender', 'receiver', 'group', 'message_type',
        'is_pinned', 'content_preview', 'is_read', 'is_edited',
        'is_deleted_for_everyone', 'has_poll', 'timestamp'
    )
    list_filter = (
        'timestamp', 'is_read', 'is_pinned', 'message_type',
        'is_edited', 'is_deleted_for_everyone'
    )
    search_fields = ('content', 'sender__name', 'sender__email')
    filter_horizontal = ('starred_by',)

    def content_preview(self, obj):
        if obj.is_deleted_for_everyone:
            return "[Deleted]"
        if obj.message_type == 'poll':
            try:
                return f"📊 {obj.poll.question[:40]}"
            except Poll.DoesNotExist:
                return "[Poll - no data]"
        if obj.message_type == 'meet':
            return f"📅 {obj.meet_title or 'Meeting'}"
        if obj.content:
            return obj.content[:50] + "..." if len(obj.content) > 50 else obj.content
        if obj.file:
            return f"[{obj.message_type}: {obj.file_name or 'file'}]"
        return "[No text]"
    content_preview.short_description = 'Content'

    def has_poll(self, obj):
        if obj.message_type == 'poll':
            try:
                poll = obj.poll
                total = poll.get_total_votes()
                options = poll.options.count()
                return format_html(
                    '<span style="color:#00a884;font-weight:600;">✓ {} opts, {} votes</span>',
                    options, total
                )
            except Poll.DoesNotExist:
                return format_html('<span style="color:#dc2626;">✗ Missing</span>')
        return "—"
    has_poll.short_description = 'Poll'


class PollOptionInline(admin.TabularInline):
    model = PollOption
    extra = 0
    readonly_fields = ('vote_count', 'voter_names')
    fields = ('order', 'text', 'vote_count', 'voter_names')

    def vote_count(self, obj):
        if obj.pk:
            count = obj.votes.count()
            if count > 0:
                return format_html(
                    '<span style="background:#00a884;color:#fff;padding:2px 8px;'
                    'border-radius:10px;font-weight:600;">{}</span>',
                    count
                )
            return "0"
        return "—"
    vote_count.short_description = 'Votes'

    def voter_names(self, obj):
        if obj.pk:
            voters = list(
                obj.votes.select_related('employee')
                .values_list('employee__name', flat=True)
            )
            if voters:
                return ", ".join(voters)
            return "No votes yet"
        return "—"
    voter_names.short_description = 'Voters'


@admin.register(Poll)
class PollAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'question_preview', 'message_link', 'allow_multiple',
        'option_count', 'total_votes', 'created_by', 'created_at'
    )
    list_filter = ('allow_multiple', 'created_at')
    search_fields = ('question', 'message__sender__name', 'message__sender__email')
    readonly_fields = ('message', 'created_at', 'poll_summary')
    inlines = [PollOptionInline]

    def question_preview(self, obj):
        q = obj.question
        if len(q) > 60:
            q = q[:60] + "..."
        return format_html('📊 {}', q)
    question_preview.short_description = 'Question'

    def message_link(self, obj):
        return format_html(
            '<a href="/admin/app/message/{}/change/">Message #{}</a>',
            obj.message.id, obj.message.id
        )
    message_link.short_description = 'Message'

    def option_count(self, obj):
        return obj.options.count()
    option_count.short_description = 'Options'

    def total_votes(self, obj):
        total = obj.get_total_votes()
        if total > 0:
            return format_html(
                '<span style="background:#00a884;color:#fff;padding:2px 10px;'
                'border-radius:10px;font-weight:600;">{}</span>',
                total
            )
        return "0"
    total_votes.short_description = 'Total Votes'

    def created_by(self, obj):
        return obj.message.sender.name
    created_by.short_description = 'Created By'

    def poll_summary(self, obj):
        options = obj.options.all().order_by('order')
        total = obj.get_total_votes()

        if not options.exists():
            return "No options"

        rows = []
        for opt in options:
            vote_count = opt.votes.count()
            percentage = round((vote_count / total) * 100) if total > 0 else 0
            voters = list(
                opt.votes.select_related('employee')
                .values_list('employee__name', flat=True)
            )
            voter_str = ", ".join(voters) if voters else "—"
            bar_color = '#00a884' if vote_count > 0 else '#e9edef'

            rows.append(format_html(
                '<div style="margin-bottom:12px;">'
                '  <div style="display:flex;justify-content:space-between;margin-bottom:4px;">'
                '    <strong>{}</strong>'
                '    <span style="color:#667781;">{} votes ({}%)</span>'
                '  </div>'
                '  <div style="background:#e9edef;border-radius:4px;height:8px;overflow:hidden;">'
                '    <div style="background:{};width:{}%;height:100%;border-radius:4px;"></div>'
                '  </div>'
                '  <div style="font-size:11px;color:#8696a0;margin-top:2px;">Voters: {}</div>'
                '</div>',
                opt.text, vote_count, percentage, bar_color, percentage, voter_str
            ))

        header = format_html(
            '<div style="padding:16px;background:#f9fafb;border-radius:8px;border:1px solid #e9edef;">'
            '  <div style="font-size:16px;font-weight:700;margin-bottom:4px;">📊 {}</div>'
            '  <div style="font-size:12px;color:#667781;margin-bottom:16px;">'
            '    {} total votes · {}'
            '  </div>',
            obj.question, total,
            "Multiple answers allowed" if obj.allow_multiple else "Single answer only"
        )

        footer = format_html('</div>')

        return format_html(
            '{}{}{}', header,
            format_html(''.join(str(r) for r in rows)),
            footer
        )
    poll_summary.short_description = 'Poll Results'


@admin.register(PollOption)
class PollOptionAdmin(admin.ModelAdmin):
    list_display = ('id', 'text', 'poll_question', 'order', 'vote_count')
    list_filter = ('poll',)
    search_fields = ('text', 'poll__question')
    ordering = ('poll', 'order')

    def poll_question(self, obj):
        q = obj.poll.question
        return q[:40] + "..." if len(q) > 40 else q
    poll_question.short_description = 'Poll'

    def vote_count(self, obj):
        count = obj.votes.count()
        if count > 0:
            return format_html(
                '<span style="background:#00a884;color:#fff;padding:2px 8px;'
                'border-radius:10px;font-weight:600;">{}</span>',
                count
            )
        return "0"
    vote_count.short_description = 'Votes'


@admin.register(PollVote)
class PollVoteAdmin(admin.ModelAdmin):
    list_display = ('id', 'employee', 'option_text', 'poll_question', 'voted_at')
    list_filter = ('voted_at', 'option__poll')
    search_fields = (
        'employee__name', 'employee__email',
        'option__text', 'option__poll__question'
    )
    readonly_fields = ('option', 'employee', 'voted_at')
    ordering = ('-voted_at',)

    def option_text(self, obj):
        return obj.option.text
    option_text.short_description = 'Option'

    def poll_question(self, obj):
        q = obj.option.poll.question
        return q[:40] + "..." if len(q) > 40 else q
    poll_question.short_description = 'Poll'


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