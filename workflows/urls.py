from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('flujos/', views.workflow_list, name='workflow_list'),
    path('flujos/nuevo/', views.workflow_create, name='workflow_create'),
    path('flujos/<int:pk>/', views.workflow_detail, name='workflow_detail'),
    path('flujos/<int:pk>/iniciar/', views.workflow_start, name='workflow_start'),
    path('tareas/<int:pk>/terminar/', views.task_complete, name='task_complete'),
    path('tareas/<int:pk>/rechazar/', views.task_reject, name='task_reject'),
    path('tareas/<int:pk>/reactivar/', views.task_reopen, name='task_reopen'),
    path('tareas/<int:pk>/comentarios/', views.task_comment, name='task_comment'),
    path('tareas/<int:pk>/adjuntos/', views.task_attachment, name='task_attachment'),
    path('reportes/tareas.csv', views.task_report_csv, name='task_report_csv'),
    path('api/dashboard/', views.dashboard_data, name='dashboard_data'),
]
