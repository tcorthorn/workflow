import csv
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    TaskAttachment,
    TaskComment,
    TaskHistory,
    TaskInstance,
    TaskTemplate,
    WorkflowInstance,
    WorkflowTemplate,
)
from .views import can_user_manage_task


@override_settings(MEDIA_ROOT="/tmp/workflow-test-media")
class DashboardViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="admin", password="admin123", email="admin@example.com"
        )
        self.other = User.objects.create_user(username="viewer", password="viewer123")
        self.manager = User.objects.create_user(username="manager", password="manager123")
        Group.objects.create(name="Workflow Manager").user_set.add(self.manager)
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
        self.second_task_template = TaskTemplate.objects.create(
            workflow=self.workflow_template,
            nombre="Firma final",
            orden=2,
        )
        self.pending_task = TaskInstance.objects.create(
            workflow=self.workflow,
            plantilla_tarea=self.second_task_template,
            nombre="Firmar escritura",
            responsable=self.manager,
            estado=TaskInstance.Estado.PENDIENTE,
            fecha_limite=timezone.localdate(),
            orden=2,
        )

    def test_dashboard_shows_kanban_filters_actions_user_and_activity(self):
        self.client.force_login(self.user)
        TaskComment.objects.create(tarea=self.task, usuario=self.user, comentario="Comentario reciente")
        TaskAttachment.objects.create(
            tarea=self.task,
            uploaded_by=self.user,
            archivo=SimpleUploadedFile("contrato.txt", b"abc"),
        )

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Revisar promesa")
        self.assertContains(response, "Operación Las Condes")
        self.assertContains(response, "Vista Kanban")
        self.assertContains(response, "Línea de tiempo")
        self.assertContains(response, "Responsable")
        self.assertContains(response, "Proyecto/flujo")
        self.assertContains(response, "Terminar")
        self.assertContains(response, "Rechazar")
        self.assertContains(response, "Adjuntar")
        self.assertContains(response, "Comentario reciente")
        self.assertContains(response, "contrato")
        self.assertContains(response, ".txt")
        self.assertContains(response, self.user.username)
        self.assertContains(response, "Cerrar sesión")
        self.assertEqual(response.context["total_flujos"], 1)
        self.assertIn(self.task, list(response.context["tareas"]))

    def test_dashboard_filters_by_estado(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard"), {"estado": TaskInstance.Estado.PENDIENTE})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Firmar escritura")
        self.assertNotContains(response, "Revisar promesa")

    def test_can_user_manage_task_helper_allows_responsible_staff_and_group(self):
        self.user.is_staff = True
        self.user.save()
        self.assertTrue(can_user_manage_task(self.user, self.task))
        self.assertTrue(can_user_manage_task(self.manager, self.task))
        self.assertFalse(can_user_manage_task(self.other, self.task))

    def test_complete_task_by_post_changes_estado(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("task_complete", args=[self.task.pk]),
            {"comentario": "Listo", "next": reverse("dashboard")},
        )

        self.assertRedirects(response, reverse("dashboard"))
        self.task.refresh_from_db()
        self.assertEqual(self.task.estado, TaskInstance.Estado.TERMINADA)
        self.assertTrue(TaskHistory.objects.filter(tarea=self.task, accion="terminada").exists())

    def test_task_action_ignores_external_next_url(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("task_complete", args=[self.task.pk]),
            {"comentario": "Listo", "next": "https://evil.example/phishing"},
        )

        self.assertRedirects(
            response,
            reverse("workflow_detail", args=[self.workflow.pk]),
            fetch_redirect_response=False,
        )

    def test_reject_and_reopen_task(self):
        self.client.force_login(self.user)

        reject = self.client.post(
            reverse("task_reject", args=[self.task.pk]),
            {"comentario": "Falta antecedente", "next": reverse("dashboard")},
        )
        self.assertRedirects(reject, reverse("dashboard"))
        self.task.refresh_from_db()
        self.assertEqual(self.task.estado, TaskInstance.Estado.RECHAZADA)

        reopen = self.client.post(
            reverse("task_reopen", args=[self.task.pk]),
            {"comentario": "Corregido", "next": reverse("dashboard")},
        )
        self.assertRedirects(reopen, reverse("dashboard"))
        self.task.refresh_from_db()
        self.assertEqual(self.task.estado, TaskInstance.Estado.EN_CURSO)

    def test_add_comment_from_dashboard(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("task_comment", args=[self.task.pk]),
            {"comentario": "Nuevo comentario", "next": reverse("dashboard")},
        )

        self.assertRedirects(response, reverse("dashboard"))
        self.assertTrue(TaskComment.objects.filter(tarea=self.task, comentario="Nuevo comentario").exists())

    def test_upload_attachment_from_dashboard(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("task_attachment", args=[self.task.pk]),
            {"archivo": SimpleUploadedFile("documento.txt", b"contenido"), "next": reverse("dashboard")},
        )

        self.assertRedirects(response, reverse("dashboard"))
        self.assertTrue(TaskAttachment.objects.filter(tarea=self.task, archivo__contains="documento").exists())

    def test_export_csv_compatible_with_excel(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("task_report_csv"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8-sig")
        content = response.content.decode("utf-8-sig")
        rows = list(csv.DictReader(StringIO(content)))
        self.assertEqual(rows[0]["Tarea"], "Revisar promesa")
        self.assertIn("Proyecto/flujo", rows[0])

    def test_permission_denied_for_user_not_responsible(self):
        self.client.force_login(self.other)

        response = self.client.post(reverse("task_complete", args=[self.task.pk]), {"comentario": "x"})

        self.assertEqual(response.status_code, 403)
        self.task.refresh_from_db()
        self.assertEqual(self.task.estado, TaskInstance.Estado.EN_CURSO)
