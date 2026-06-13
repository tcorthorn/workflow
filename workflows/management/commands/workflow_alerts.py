import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from workflows.models import TaskInstance, WorkflowAlert, WorkflowInstance


class Command(BaseCommand):
    help = "Genera y lista alertas operativas de workflows."

    def add_arguments(self, parser):
        parser.add_argument("--generate", action="store_true", help="Genera alertas pendientes para tareas activas.")
        parser.add_argument("--pending", action="store_true", help="Lista alertas pendientes listas para envío.")
        parser.add_argument("--claim", action="store_true", help="Reserva alertas pendientes para envío.")
        parser.add_argument("--format", choices=["text", "json"], default="text")
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--mark-sent", type=int, dest="mark_sent")
        parser.add_argument("--mark-failed", type=int, dest="mark_failed")
        parser.add_argument("--error", default="")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        actions = [
            options["generate"],
            options["pending"],
            options["mark_sent"] is not None,
            options["mark_failed"] is not None,
        ]
        if sum(bool(action) for action in actions) != 1:
            raise CommandError("Indica exactamente una acción: --generate, --pending, --mark-sent o --mark-failed.")
        if options["limit"] <= 0:
            raise CommandError("--limit debe ser mayor que 0.")

        if options["generate"]:
            return self.handle_generate(dry_run=options["dry_run"])
        if options["pending"]:
            return self.handle_pending(output_format=options["format"], limit=options["limit"], claim=options["claim"])
        if options["mark_sent"] is not None:
            return self.handle_mark_sent(options["mark_sent"])
        if options["mark_failed"] is not None:
            if not options["error"]:
                raise CommandError("--mark-failed requiere --error TEXT.")
            return self.handle_mark_failed(options["mark_failed"], options["error"])

    def handle_generate(self, dry_run=False):
        channels = getattr(settings, "WORKFLOW_ALERTS_DEFAULT_CHANNELS", None) or ["telegram"]
        self._validate_channels(channels)
        specs = []
        for task in self._tasks_for_generation():
            specs.extend(self._alert_specs_for_task(task, channels))

        created = 0
        existing = 0
        for spec in specs:
            if dry_run:
                if WorkflowAlert.objects.filter(dedupe_key=spec["dedupe_key"]).exists():
                    existing += 1
                else:
                    created += 1
                continue

            try:
                _, was_created = WorkflowAlert.objects.get_or_create(
                    dedupe_key=spec["dedupe_key"],
                    defaults={key: value for key, value in spec.items() if key != "dedupe_key"},
                )
            except IntegrityError:
                was_created = False
            if was_created:
                created += 1
            else:
                existing += 1

        verb = "crearia" if dry_run else "creadas"
        self.stdout.write(f"Alertas {verb}: {created}; existentes: {existing}")

    def handle_pending(self, output_format="text", limit=50, claim=False):
        alerts = self._pending_alerts(limit=limit, claim=claim)
        if output_format == "json":
            self.stdout.write(json.dumps([self._serialize_alert(alert) for alert in alerts], ensure_ascii=False))
            return
        for alert in alerts:
            self.stdout.write(f"{alert.id}\t{alert.tipo}\t{alert.canal}\t{alert.asunto}")

    def handle_mark_sent(self, alert_id):
        with transaction.atomic():
            alert = self._get_alert_for_update(alert_id)
            if alert.estado == WorkflowAlert.Estado.CANCELADA:
                raise CommandError("No se puede marcar como enviada una alerta cancelada.")
            if alert.estado == WorkflowAlert.Estado.ENVIADA:
                self.stdout.write(f"Alerta {alert.id} ya estaba enviada")
                return
            alert.estado = WorkflowAlert.Estado.ENVIADA
            alert.enviada_en = timezone.now()
            alert.error = ""
            alert.enviar_despues_de = None
            alert.save(update_fields=["estado", "enviada_en", "error", "enviar_despues_de", "actualizado"])
        self.stdout.write(f"Alerta {alert.id} marcada como enviada")

    def handle_mark_failed(self, alert_id, error):
        with transaction.atomic():
            alert = self._get_alert_for_update(alert_id)
            if alert.estado in [WorkflowAlert.Estado.ENVIADA, WorkflowAlert.Estado.CANCELADA]:
                raise CommandError("No se puede marcar como fallida una alerta enviada o cancelada.")
            retry_minutes = getattr(settings, "WORKFLOW_ALERTS_RETRY_MINUTES", 15)
            alert.estado = WorkflowAlert.Estado.FALLIDA
            alert.error = error
            alert.enviar_despues_de = timezone.now() + timezone.timedelta(minutes=retry_minutes)
            alert.save(update_fields=["estado", "error", "enviar_despues_de", "actualizado"])
        self.stdout.write(f"Alerta {alert.id} marcada como fallida")

    def _get_alert_for_update(self, alert_id):
        try:
            return WorkflowAlert.objects.select_for_update().get(pk=alert_id)
        except WorkflowAlert.DoesNotExist as exc:
            raise CommandError(f"No existe WorkflowAlert con id {alert_id}.") from exc

    def _get_alert(self, alert_id):
        try:
            return WorkflowAlert.objects.get(pk=alert_id)
        except WorkflowAlert.DoesNotExist as exc:
            raise CommandError(f"No existe WorkflowAlert con id {alert_id}.") from exc

    def _validate_channels(self, channels):
        for channel in channels:
            if not channel or len(channel) > 20:
                raise CommandError("Cada canal debe ser no vacío y de máximo 20 caracteres.")

    def _pending_alerts(self, limit, claim=False):
        with transaction.atomic():
            queryset = self._ready_alerts_queryset()
            if claim:
                queryset = self._select_for_update_skip_locked(queryset)
            candidates = list(queryset[:limit])
            valid_alerts = []
            for alert in candidates:
                if self._is_obsolete(alert):
                    alert.estado = WorkflowAlert.Estado.CANCELADA
                    alert.save(update_fields=["estado", "actualizado"])
                    continue
                valid_alerts.append(alert)

            if claim and valid_alerts:
                now = timezone.now()
                ids = [alert.id for alert in valid_alerts]
                WorkflowAlert.objects.filter(pk__in=ids).update(
                    estado=WorkflowAlert.Estado.EN_ENVIO,
                    intentos=F("intentos") + 1,
                    ultimo_intento_en=now,
                    actualizado=now,
                )
                order_by_id = {alert_id: position for position, alert_id in enumerate(ids)}
                valid_alerts = sorted(
                    WorkflowAlert.objects.select_related("destinatario", "tarea", "tarea__workflow")
                    .filter(pk__in=ids),
                    key=lambda alert: order_by_id[alert.id],
                )
            return valid_alerts

    def _ready_alerts_queryset(self):
        now = timezone.now()
        claim_timeout_minutes = getattr(settings, "WORKFLOW_ALERTS_CLAIM_TIMEOUT_MINUTES", 30)
        stale_claim_cutoff = now - timezone.timedelta(minutes=claim_timeout_minutes)
        retry_ready = Q(estado__in=[WorkflowAlert.Estado.PENDIENTE, WorkflowAlert.Estado.FALLIDA]) & (
            Q(enviar_despues_de__isnull=True) | Q(enviar_despues_de__lte=now)
        )
        stale_in_flight = Q(estado=WorkflowAlert.Estado.EN_ENVIO) & (
            Q(ultimo_intento_en__isnull=True) | Q(ultimo_intento_en__lte=stale_claim_cutoff)
        )
        return (
            WorkflowAlert.objects.select_related(
                "destinatario",
                "tarea",
                "tarea__workflow",
                "tarea__responsable",
                "tarea__workflow__propietario",
            )
            .filter(retry_ready | stale_in_flight)
            .order_by("estado", "enviar_despues_de", "creado")
        )

    def _select_for_update_skip_locked(self, queryset):
        if getattr(connection.features, "has_select_for_update_skip_locked", False):
            return queryset.select_for_update(skip_locked=True)
        return queryset.select_for_update()

    def _is_obsolete(self, alert):
        task = alert.tarea
        if task.estado == TaskInstance.Estado.TERMINADA:
            return True
        if task.workflow.estado != WorkflowInstance.Estado.EN_CURSO:
            return True
        expected_recipient = task.responsable or task.workflow.propietario
        if alert.destinatario_id != (expected_recipient.id if expected_recipient else None):
            return True
        return not self._alert_type_applies(task, alert.tipo, alert.fecha_objetivo)

    def _alert_type_applies(self, task, alert_type, target_date):
        today = timezone.localdate()
        if alert_type == WorkflowAlert.Tipo.TAREA_ATRASADA:
            return bool(target_date and task.fecha_limite == target_date and target_date < today)
        if alert_type == WorkflowAlert.Tipo.VENCE_HOY:
            return bool(target_date and task.fecha_limite == target_date and target_date == today)
        if alert_type == WorkflowAlert.Tipo.VENCE_MANANA:
            return bool(target_date and task.fecha_limite == target_date and target_date == today + timezone.timedelta(days=1))
        if alert_type == WorkflowAlert.Tipo.SIN_RESPONSABLE:
            return task.responsable_id is None
        if alert_type == WorkflowAlert.Tipo.TAREA_RECHAZADA:
            return task.estado == TaskInstance.Estado.RECHAZADA
        return False

    def _tasks_for_generation(self):
        return (
            TaskInstance.objects.select_related("workflow", "workflow__propietario", "responsable")
            .filter(workflow__estado=WorkflowInstance.Estado.EN_CURSO)
            .exclude(estado=TaskInstance.Estado.TERMINADA)
        )

    def _alert_specs_for_task(self, task, channels):
        hoy = timezone.localdate()
        specs = []
        alert_types = []

        if task.fecha_limite and task.fecha_limite < hoy:
            alert_types.append((WorkflowAlert.Tipo.TAREA_ATRASADA, task.fecha_limite))
        if task.fecha_limite == hoy:
            alert_types.append((WorkflowAlert.Tipo.VENCE_HOY, task.fecha_limite))
        if task.fecha_limite == hoy + timezone.timedelta(days=1):
            alert_types.append((WorkflowAlert.Tipo.VENCE_MANANA, task.fecha_limite))
        if task.responsable_id is None:
            alert_types.append((WorkflowAlert.Tipo.SIN_RESPONSABLE, None))
        if task.estado == TaskInstance.Estado.RECHAZADA:
            alert_types.append((WorkflowAlert.Tipo.TAREA_RECHAZADA, task.fecha_limite))

        destinatario = task.responsable or task.workflow.propietario
        for tipo, fecha_objetivo in alert_types:
            for channel in channels:
                specs.append(
                    {
                        "tarea": task,
                        "destinatario": destinatario,
                        "tipo": tipo,
                        "canal": channel,
                        "dedupe_key": self._dedupe_key(task, tipo, fecha_objetivo, destinatario, channel),
                        "asunto": self._subject(task, tipo),
                        "mensaje": self._message(task, tipo, fecha_objetivo),
                        "fecha_objetivo": fecha_objetivo,
                    }
                )
        return specs

    def _dedupe_key(self, task, tipo, fecha_objetivo, destinatario, channel):
        destinatario_key = destinatario.id if destinatario else "none"
        fecha_key = fecha_objetivo.isoformat() if fecha_objetivo else "none"
        return f"task:{task.id}:tipo:{tipo}:fecha:{fecha_key}:dest:{destinatario_key}:canal:{channel}"

    def _subject(self, task, tipo):
        labels = {
            WorkflowAlert.Tipo.TAREA_ATRASADA: "Tarea atrasada",
            WorkflowAlert.Tipo.VENCE_HOY: "Tarea vence hoy",
            WorkflowAlert.Tipo.VENCE_MANANA: "Tarea vence mañana",
            WorkflowAlert.Tipo.SIN_RESPONSABLE: "Tarea sin responsable",
            WorkflowAlert.Tipo.TAREA_RECHAZADA: "Tarea rechazada",
        }
        return f"{labels[tipo]}: {task.nombre}"

    def _message(self, task, tipo, fecha_objetivo):
        workflow = task.workflow.nombre
        due = fecha_objetivo.isoformat() if fecha_objetivo else "sin fecha límite"
        messages = {
            WorkflowAlert.Tipo.TAREA_ATRASADA: f"La tarea '{task.nombre}' del workflow '{workflow}' está atrasada. Fecha límite: {due}.",
            WorkflowAlert.Tipo.VENCE_HOY: f"La tarea '{task.nombre}' del workflow '{workflow}' vence hoy ({due}).",
            WorkflowAlert.Tipo.VENCE_MANANA: f"La tarea '{task.nombre}' del workflow '{workflow}' vence mañana ({due}).",
            WorkflowAlert.Tipo.SIN_RESPONSABLE: f"La tarea '{task.nombre}' del workflow '{workflow}' no tiene responsable asignado.",
            WorkflowAlert.Tipo.TAREA_RECHAZADA: f"La tarea '{task.nombre}' del workflow '{workflow}' fue rechazada y requiere atención.",
        }
        return messages[tipo]

    def _serialize_alert(self, alert):
        user = alert.destinatario
        task = alert.tarea
        return {
            "id": alert.id,
            "tipo": alert.tipo,
            "canal": alert.canal,
            "asunto": alert.asunto,
            "mensaje": alert.mensaje,
            "destinatario": None
            if user is None
            else {
                "id": user.id,
                "username": user.get_username(),
                "email": user.email or None,
            },
            "tarea": {
                "id": task.id,
                "nombre": task.nombre,
                "estado": task.estado,
                "fecha_limite": task.fecha_limite.isoformat() if task.fecha_limite else None,
                "workflow": task.workflow.nombre,
            },
        }
