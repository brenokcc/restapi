from rest_framework import permissions, urls
from django.contrib import admin
from django.urls import path, include
from drf_yasg.views import get_schema_view
from drf_yasg import openapi
from rest_framework.authtoken import views
from .serializers import router

schema_view = get_schema_view(
   openapi.Info(
      title="REST API",
      default_version='v1',
      description="Test description",
      terms_of_service="https://www.google.com/policies/terms/",
      contact=openapi.Contact(email="contact@snippets.local"),
      license=openapi.License(name="BSD License"),
   ),
   public=True,
   permission_classes=[permissions.AllowAny],
)


urlpatterns = [
    path('', schema_view.with_ui('swagger', cache_timeout=0)),
    path('admin/', admin.site.urls),
    path('', include(router.urls)),
    # path('api-auth/', include('rest_framework.urls')),
    path('token/', views.obtain_auth_token)
]

