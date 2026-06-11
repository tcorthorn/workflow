from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('flujos/', views.workflow_list, name='workflow_list'),
    path('flujos/nuevo/', views.workflow_create, name='workflow_create'),
    path('flujos/<int:pk>/', views.workflow_detail, name='workflow_detail'),
    path('flujos/<int:pk>/iniciar/', views.workflow_start, name='workflow_start'),
    path('tareas/<int:pk>/terminar/', views.task_complete, name='task_complete'),
    path('api/dashboard/', views.dashboard_data, name='dashboard_data'),
]
