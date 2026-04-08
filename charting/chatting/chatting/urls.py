from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    # This connects your app's URLs to the main project
    path('', include('app.urls')), 
    
]