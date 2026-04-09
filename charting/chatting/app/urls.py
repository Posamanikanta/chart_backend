# app/urls.py - COMPLETE
from django.urls import path
from . import views

urlpatterns = [
    # ==================== AUTH ====================
    path('api/login/', views.login_user, name='login_user'),
    path('api/logout/', views.logout_user, name='logout_user'),
    path('api/me/', views.get_current_user, name='get_current_user'),
    
    # ==================== USERS ====================
    path('api/users/', views.get_users, name='get_users'),
    path('api/create-employee/', views.create_employee, name='create_employee'),
    
    # ==================== PROFILE ====================
    path('api/profile/update/', views.update_profile, name='update_profile'),
    path('api/profile/upload-image/', views.upload_profile_image, name='upload_profile_image'),
    
    # ==================== MESSAGES (Direct/Private) ====================
    path('api/messages/<int:target_id>/', views.get_messages, name='get_messages'),
    path('api/messages/<int:target_id>/read/', views.mark_messages_read, name='mark_messages_read'),
    
    # ==================== FILE UPLOAD ====================
    path('api/messages/upload/', views.upload_message_file, name='upload_message_file'),
    
    # ==================== REACTIONS ====================
    path('api/messages/<int:message_id>/react/', views.add_reaction, name='add_reaction'),
    path('api/messages/<int:message_id>/react/remove/', views.remove_reaction, name='remove_reaction'),
    
    # ==================== EDIT/DELETE MESSAGES ====================
    path('api/messages/<int:message_id>/edit/', views.edit_message, name='edit_message'),
    path('api/messages/<int:message_id>/delete-for-me/', views.delete_message_for_me, name='delete_message_for_me'),
    path('api/messages/<int:message_id>/delete-for-everyone/', views.delete_message_for_everyone, name='delete_message_for_everyone'),
    
    # ==================== GROUPS ====================
    path('api/groups/', views.get_groups, name='get_groups'),
    path('api/groups/create/', views.create_group, name='create_group'),
    path('api/groups/<int:group_id>/', views.get_group_details, name='get_group_details'),
    path('api/groups/<int:group_id>/messages/', views.get_group_messages, name='get_group_messages'),
    path('api/groups/<int:group_id>/members/add/', views.add_group_members, name='add_group_members'),
    path('api/groups/<int:group_id>/members/remove/', views.remove_group_member, name='remove_group_member'),
    path('api/groups/<int:group_id>/update/', views.update_group, name='update_group'),
    path('api/groups/<int:group_id>/leave/', views.leave_group, name='leave_group'),
    
    # ==================== GOOGLE MEET ====================
    path('api/meet/create/', views.create_meet, name='create_meet'),
    path('api/meet/saved/', views.get_saved_meets, name='get_saved_meets'),
    path('api/meet/saved/<int:meet_id>/', views.delete_saved_meet, name='delete_saved_meet'),
    path('api/meet/invite/<int:message_id>/respond/', views.respond_to_meet_invite, name='respond_to_meet_invite'),
    
    # ==================== ADMIN ENDPOINTS ====================
    path('api/admin/employees/', views.admin_get_all_employees, name='admin_get_all_employees'),
    path('api/admin/statistics/', views.admin_get_statistics, name='admin_get_statistics'),
    path('api/admin/employee/<int:employee_id>/dashboard/', views.admin_view_employee_dashboard, name='admin_view_employee_dashboard'),
    path('api/admin/employee/<int:employee_id>/messages/<int:target_id>/', views.admin_view_employee_messages, name='admin_view_employee_messages'),
    path('api/admin/employee/<int:employee_id>/groups/', views.admin_view_employee_groups, name='admin_view_employee_groups'),
    path('api/admin/employee/<int:employee_id>/groups/<int:group_id>/messages/', views.admin_view_employee_group_messages, name='admin_view_employee_group_messages'),
    path('api/admin/activity-log/', views.admin_get_activity_log, name='admin_get_activity_log'),
    path('api/admin/exit-employee-view/', views.admin_exit_employee_view, name='admin_exit_employee_view'),
]