from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import TaskInstance, TaskTemplate, WorkflowInstance, WorkflowTemplate


class DashboardViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="admin", password="admin123"
        )
        self.workflow_template = WorkflowTemplate.objects.create(nombre="Venta inmobiliaria")
        self.task_template = TaskTemplate.objects.create(
            workflow=self.workflow_template,
            nombre="Revisar documentos",
            orden=1,
        )
        self.workflow = WorkflowInstance.objects.create(
            plantilla=self.workflow_template,
            nombre="Operación Las Condes",
            propietario=self.user,
            estado=WorkflowInstance.Estado.EN_CURSO,
            fecha_inicio=timezone.localdate(),
        )
        self.task = TaskInstance.objects.create(
            workflow=self.workflow,
            plantilla_tarea=self.task_template,
            nombre="Revisar promesa",
            responsable=self.user,
            estado=TaskInstance.Estado.EN_CURSO,
            fecha_limite=timezone.localdate(),
            orden=1,
        )

    def test_dashboard_context_includes_tasks_and_counters_used_by_template(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Revisar promesa")
        self.assertContains(response, "Operación Las Condes")
        self.assertEqual(response.context["total_flujos"], 1)
        self.assertEqual(response.context["tareas_pendientes"], 0)
        self.assertEqual(response.context["tareas_en_curso"], 1)
        self.assertEqual(response.context["tareas_completadas"], 0)
        self.assertIn(self.task, list(response.context["tareas"]))
