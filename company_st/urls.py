from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),  # Админка должна быть здесь!
    path('', include('company_st.urls')), # Маршруты вашего приложения
]