import json
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import TaskInstance, TaskTemplate, WorkflowAlert, WorkflowInstance, WorkflowTemplate


@override_settings(WORKFLOW_ALERTS_DEFAULT_CHANNELS=["telegram", "email"])
class WorkflowAlertsCommandTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username="owner", password="pw", email="owner@example.com"
        )
        self.responsible = User.objects.create_user(
            username="responsible", password="pw", email="responsible@example.com"
        )
        self.no_email_user = User.objects.create_user(username="noemail", password="pw")
        self.template = WorkflowTemplate.objects.create(nombre="Flujo alertas")
        self.task_template = TaskTemplate.objects.create(
            workflow=self.template,
            nombre="Tarea base",
            orden=1,
        )
        self.workflow = WorkflowInstance.objects.create(
            plantilla=self.template,
            nombre="Workflow en curso",
            estado=WorkflowInstance.Estado.EN_CURSO,
            propietario=self.owner,
            fecha_inicio=timezone.localdate(),
        )

    def make_task(self, nombre, estado=TaskInstance.Estado.EN_CURSO, fecha_limite=None, responsable=None, workflow=None):
        return TaskInstance.objects.create(
            workflow=workflow or self.workflow,
            plantilla_tarea=self.task_template,
            nombre=nombre,
            responsable=responsable,
            estado=estado,
            fecha_limite=fecha_limite,
            orden=TaskInstance.objects.count() + 1,
        )

    def run_command(self, *args):
        out = StringIO()
        call_command("workflow_alerts", *args, stdout=out)
        return out.getvalue()

    def test_generate_creates_expected_alert_types_and_channels(self):
        hoy = timezone.localdate()
        atrasada = self.make_task("Atrasada", fecha_limite=hoy - timezone.timedelta(days=1), responsable=self.responsible)
        vence_hoy = self.make_task("Vence hoy", fecha_limite=hoy, responsable=self.no_email_user)
        vence_manana = self.make_task("Vence mañana", fecha_limite=hoy + timezone.timedelta(days=1), responsable=self.responsible)
        sin_responsable = self.make_task("Sin responsable", fecha_limite=None, responsable=None)
        rechazada = self.make_task("Rechazada", estado=TaskInstance.Estado.RECHAZADA, fecha_limite=None, responsable=self.responsible)
        terminada = self.make_task("Terminada", estado=TaskInstance.Estado.TERMINADA, fecha_limite=hoy - timezone.timedelta(days=1), responsable=self.responsible)
        paused = WorkflowInstance.objects.create(
            plantilla=self.template,
            nombre="Workflow pausado",
            estado=WorkflowInstance.Estado.PAUSADO,
            propietario=self.owner,
        )
        self.make_task("Ignorada por workflow", fecha_limite=hoy - timezone.timedelta(days=1), responsable=self.responsible, workflow=paused)

        self.run_command("--generate")

        self.assertEqual(WorkflowAlert.objects.filter(tarea=atrasada, tipo=WorkflowAlert.Tipo.TAREA_ATRASADA).count(), 2)
        self.assertEqual(WorkflowAlert.objects.filter(tarea=vence_hoy, tipo=WorkflowAlert.Tipo.VENCE_HOY).count(), 2)
        self.assertEqual(WorkflowAlert.objects.filter(tarea=vence_manana, tipo=WorkflowAlert.Tipo.VENCE_MANANA).count(), 2)
        self.assertEqual(WorkflowAlert.objects.filter(tarea=sin_responsable, tipo=WorkflowAlert.Tipo.SIN_RESPONSABLE).count(), 2)
        self.assertEqual(WorkflowAlert.objects.filter(tarea=rechazada, tipo=WorkflowAlert.Tipo.TAREA_RECHAZADA).count(), 2)
        self.assertFalse(WorkflowAlert.objects.filter(tarea=terminada).exists())
        self.assertFalse(WorkflowAlert.objects.filter(tarea__workflow=paused).exists())
        self.assertEqual(set(WorkflowAlert.objects.values_list("canal", flat=True)), {"telegram", "email"})
        self.assertEqual(WorkflowAlert.objects.get(tarea=sin_responsable, canal="telegram").destinatario, self.owner)
        self.assertEqual(WorkflowAlert.objects.get(tarea=vence_hoy, canal="email").destinatario, self.no_email_user)

    def test_generate_does_not_duplicate_alerts(self):
        self.make_task("Atrasada", fecha_limite=timezone.localdate() - timezone.timedelta(days=1), responsable=self.responsible)

        self.run_command("--generate")
        first_count = WorkflowAlert.objects.count()
        self.run_command("--generate")

        self.assertEqual(WorkflowAlert.objects.count(), first_count)

    def test_dry_run_reports_without_creating(self):
        self.make_task("Atrasada", fecha_limite=timezone.localdate() - timezone.timedelta(days=1), responsable=self.responsible)

        output = self.run_command("--generate", "--dry-run")

        self.assertIn("crearia", output.lower())
        self.assertEqual(WorkflowAlert.objects.count(), 0)

    def test_pending_json_structure(self):
        task = self.make_task("Vence hoy", fecha_limite=timezone.localdate(), responsable=self.no_email_user)
        ready = WorkflowAlert.objects.create(
            tarea=task,
            destinatario=self.no_email_user,
            canal="email",
            tipo=WorkflowAlert.Tipo.VENCE_HOY,
            dedupe_key="ready",
            asunto="Asunto",
            mensaje="Mensaje",
            fecha_objetivo=timezone.localdate(),
        )
        WorkflowAlert.objects.create(
            tarea=task,
            destinatario=self.no_email_user,
            canal="telegram",
            tipo=WorkflowAlert.Tipo.VENCE_HOY,
            dedupe_key="future",
            asunto="Futura",
            mensaje="Mensaje futuro",
            enviar_despues_de=timezone.now() + timezone.timedelta(hours=1),
        )

        payload = json.loads(self.run_command("--pending", "--format=json", "--limit", "10"))

        self.assertEqual(len(payload), 1)
        item = payload[0]
        self.assertEqual(item["id"], ready.id)
        self.assertEqual(item["tipo"], WorkflowAlert.Tipo.VENCE_HOY)
        self.assertEqual(item["canal"], "email")
        self.assertEqual(item["asunto"], "Asunto")
        self.assertEqual(item["mensaje"], "Mensaje")
        self.assertEqual(item["destinatario"], {"id": self.no_email_user.id, "username": "noemail", "email": None})
        self.assertEqual(item["tarea"]["id"], task.id)
        self.assertEqual(item["tarea"]["nombre"], "Vence hoy")
        self.assertEqual(item["tarea"]["estado"], TaskInstance.Estado.EN_CURSO)
        self.assertEqual(item["tarea"]["fecha_limite"], str(timezone.localdate()))
        self.assertEqual(item["tarea"]["workflow"], self.workflow.nombre)

    def test_mark_sent(self):
        task = self.make_task("Atrasada", fecha_limite=timezone.localdate() - timezone.timedelta(days=1), responsable=self.responsible)
        alert = WorkflowAlert.objects.create(
            tarea=task,
            destinatario=self.responsible,
            canal="telegram",
            tipo=WorkflowAlert.Tipo.TAREA_ATRASADA,
            dedupe_key="sent",
            asunto="Asunto",
            mensaje="Mensaje",
            error="previo",
        )

        self.run_command("--mark-sent", str(alert.id))

        alert.refresh_from_db()
        self.assertEqual(alert.estado, WorkflowAlert.Estado.ENVIADA)
        self.assertIsNotNone(alert.enviada_en)
        self.assertEqual(alert.error, "")

    def test_mark_failed(self):
        task = self.make_task("Atrasada", fecha_limite=timezone.localdate() - timezone.timedelta(days=1), responsable=self.responsible)
        alert = WorkflowAlert.objects.create(
            tarea=task,
            destinatario=self.responsible,
            canal="telegram",
            tipo=WorkflowAlert.Tipo.TAREA_ATRASADA,
            dedupe_key="failed",
            asunto="Asunto",
            mensaje="Mensaje",
        )

        self.run_command("--mark-failed", str(alert.id), "--error", "boom")

        alert.refresh_from_db()
        self.assertEqual(alert.estado, WorkflowAlert.Estado.FALLIDA)
        self.assertEqual(alert.error, "boom")
        self.assertIsNotNone(alert.enviar_despues_de)

    def test_pending_cancels_obsolete_alert_if_task_finished(self):
        task = self.make_task("Atrasada", fecha_limite=timezone.localdate() - timezone.timedelta(days=1), responsable=self.responsible)
        alert = WorkflowAlert.objects.create(
            tarea=task,
            destinatario=self.responsible,
            canal="telegram",
            tipo=WorkflowAlert.Tipo.TAREA_ATRASADA,
            dedupe_key="obsolete-finished",
            asunto="Asunto",
            mensaje="Mensaje",
            fecha_objetivo=task.fecha_limite,
        )
        task.estado = TaskInstance.Estado.TERMINADA
        task.save(update_fields=["estado", "actualizado"])

        payload = json.loads(self.run_command("--pending", "--format=json"))

        self.assertEqual(payload, [])
        alert.refresh_from_db()
        self.assertEqual(alert.estado, WorkflowAlert.Estado.CANCELADA)

    def test_pending_claim_marks_alert_in_flight_and_increments_attempts(self):
        task = self.make_task("Vence hoy", fecha_limite=timezone.localdate(), responsable=self.responsible)
        alert = WorkflowAlert.objects.create(
            tarea=task,
            destinatario=self.responsible,
            canal="telegram",
            tipo=WorkflowAlert.Tipo.VENCE_HOY,
            dedupe_key="claim-ready",
            asunto="Asunto",
            mensaje="Mensaje",
            fecha_objetivo=task.fecha_limite,
        )

        payload = json.loads(self.run_command("--pending", "--claim", "--format=json"))

        self.assertEqual([item["id"] for item in payload], [alert.id])
        alert.refresh_from_db()
        self.assertEqual(alert.estado, WorkflowAlert.Estado.EN_ENVIO)
        self.assertEqual(alert.intentos, 1)
        self.assertIsNotNone(alert.ultimo_intento_en)

    def test_mark_sent_is_idempotent_for_already_sent(self):
        task = self.make_task("Atrasada", fecha_limite=timezone.localdate() - timezone.timedelta(days=1), responsable=self.responsible)
        sent_at = timezone.now() - timezone.timedelta(hours=2)
        alert = WorkflowAlert.objects.create(
            tarea=task,
            destinatario=self.responsible,
            canal="telegram",
            tipo=WorkflowAlert.Tipo.TAREA_ATRASADA,
            dedupe_key="already-sent",
            asunto="Asunto",
            mensaje="Mensaje",
            estado=WorkflowAlert.Estado.ENVIADA,
            enviada_en=sent_at,
        )

        self.run_command("--mark-sent", str(alert.id))

        alert.refresh_from_db()
        self.assertEqual(alert.estado, WorkflowAlert.Estado.ENVIADA)
        self.assertEqual(alert.enviada_en, sent_at)

    def test_mark_failed_rejects_sent_alert(self):
        task = self.make_task("Atrasada", fecha_limite=timezone.localdate() - timezone.timedelta(days=1), responsable=self.responsible)
        alert = WorkflowAlert.objects.create(
            tarea=task,
            destinatario=self.responsible,
            canal="telegram",
            tipo=WorkflowAlert.Tipo.TAREA_ATRASADA,
            dedupe_key="sent-cannot-fail",
            asunto="Asunto",
            mensaje="Mensaje",
            estado=WorkflowAlert.Estado.ENVIADA,
            enviada_en=timezone.now(),
        )

        with self.assertRaises(CommandError):
            self.run_command("--mark-failed", str(alert.id), "--error", "boom")

        alert.refresh_from_db()
        self.assertEqual(alert.estado, WorkflowAlert.Estado.ENVIADA)

    def test_failed_alert_ready_for_retry_is_returned_by_pending(self):
        task = self.make_task("Atrasada", fecha_limite=timezone.localdate() - timezone.timedelta(days=1), responsable=self.responsible)
        not_ready = WorkflowAlert.objects.create(
            tarea=task,
            destinatario=self.responsible,
            canal="telegram",
            tipo=WorkflowAlert.Tipo.TAREA_ATRASADA,
            dedupe_key="failed-future",
            asunto="Futura",
            mensaje="Mensaje",
            estado=WorkflowAlert.Estado.FALLIDA,
            fecha_objetivo=task.fecha_limite,
            enviar_despues_de=timezone.now() + timezone.timedelta(hours=1),
        )
        ready = WorkflowAlert.objects.create(
            tarea=task,
            destinatario=self.responsible,
            canal="email",
            tipo=WorkflowAlert.Tipo.TAREA_ATRASADA,
            dedupe_key="failed-ready",
            asunto="Lista",
            mensaje="Mensaje",
            estado=WorkflowAlert.Estado.FALLIDA,
            fecha_objetivo=task.fecha_limite,
            enviar_despues_de=timezone.now() - timezone.timedelta(minutes=1),
        )

        payload = json.loads(self.run_command("--pending", "--format=json", "--limit", "10"))

        self.assertEqual([item["id"] for item in payload], [ready.id])
        not_ready.refresh_from_db()
        self.assertEqual(not_ready.estado, WorkflowAlert.Estado.FALLIDA)

    @override_settings(WORKFLOW_ALERTS_CLAIM_TIMEOUT_MINUTES=10)
    def test_stale_in_flight_alert_is_recovered_for_retry(self):
        task = self.make_task("Atrasada", fecha_limite=timezone.localdate() - timezone.timedelta(days=1), responsable=self.responsible)
        fresh_in_flight = WorkflowAlert.objects.create(
            tarea=task,
            destinatario=self.responsible,
            canal="telegram",
            tipo=WorkflowAlert.Tipo.TAREA_ATRASADA,
            dedupe_key="in-flight-fresh",
            asunto="En envío reciente",
            mensaje="Mensaje",
            estado=WorkflowAlert.Estado.EN_ENVIO,
            fecha_objetivo=task.fecha_limite,
            ultimo_intento_en=timezone.now() - timezone.timedelta(minutes=5),
        )
        stale_in_flight = WorkflowAlert.objects.create(
            tarea=task,
            destinatario=self.responsible,
            canal="email",
            tipo=WorkflowAlert.Tipo.TAREA_ATRASADA,
            dedupe_key="in-flight-stale",
            asunto="En envío vencida",
            mensaje="Mensaje",
            estado=WorkflowAlert.Estado.EN_ENVIO,
            fecha_objetivo=task.fecha_limite,
            ultimo_intento_en=timezone.now() - timezone.timedelta(minutes=30),
        )

        payload = json.loads(self.run_command("--pending", "--claim", "--format=json", "--limit", "10"))

        self.assertEqual([item["id"] for item in payload], [stale_in_flight.id])
        stale_in_flight.refresh_from_db()
        fresh_in_flight.refresh_from_db()
        self.assertEqual(stale_in_flight.estado, WorkflowAlert.Estado.EN_ENVIO)
        self.assertEqual(stale_in_flight.intentos, 1)
        self.assertEqual(fresh_in_flight.estado, WorkflowAlert.Estado.EN_ENVIO)
        self.assertEqual(fresh_in_flight.intentos, 0)
