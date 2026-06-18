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
            specs.extend(self._alert_specs_for_task(task))

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
        config = self._alert_config_for_task(task)
        if not config["activa"]:
            return True
        valid_recipient_ids = {
            recipient.id if recipient else None
            for recipient in self._alert_recipients_for_task(task, config)
        }
        if alert.destinatario_id not in valid_recipient_ids:
            return True
        if alert.canal not in config["channels"]:
            return True
        if not self._alert_enabled_by_config(alert, task, config):
            return True
        if alert.tipo == WorkflowAlert.Tipo.TAREA_ATRASADA:
            today = timezone.localdate()
            expected_date = today if config["repetir_atrasadas_diario"] else task.fecha_limite
            if alert.fecha_objetivo != expected_date:
                return True
        return not self._alert_type_applies(task, alert.tipo, alert.fecha_objetivo)

    def _alert_enabled_by_config(self, alert, task, config):
        today = timezone.localdate()
        if alert.tipo == WorkflowAlert.Tipo.TAREA_ATRASADA:
            return bool(config["avisar_atrasadas"])
        if alert.tipo == WorkflowAlert.Tipo.VENCE_HOY:
            return bool(config["avisar_vencen_hoy"] and alert.fecha_objetivo == today)
        if alert.tipo == WorkflowAlert.Tipo.VENCE_MANANA:
            return bool(config["dias_antes_vencimiento"] == 1 and alert.fecha_objetivo == today + timezone.timedelta(days=1))
        if alert.tipo == WorkflowAlert.Tipo.VENCE_PROXIMAMENTE:
            days = config["dias_antes_vencimiento"]
            return bool(days > 1 and alert.fecha_objetivo == today + timezone.timedelta(days=days))
        if alert.tipo == WorkflowAlert.Tipo.SIN_RESPONSABLE:
            return bool(config["avisar_sin_responsable"])
        if alert.tipo == WorkflowAlert.Tipo.TAREA_RECHAZADA:
            return bool(config["avisar_rechazadas"])
        return False

    def _alert_type_applies(self, task, alert_type, target_date):
        today = timezone.localdate()
        if alert_type == WorkflowAlert.Tipo.TAREA_ATRASADA:
            return bool(task.fecha_limite and task.fecha_limite < today and (target_date is None or target_date <= today))
        if alert_type == WorkflowAlert.Tipo.VENCE_HOY:
            return bool(target_date and task.fecha_limite == target_date and target_date == today)
        if alert_type == WorkflowAlert.Tipo.VENCE_MANANA:
            return bool(target_date and task.fecha_limite == target_date and target_date == today + timezone.timedelta(days=1))
        if alert_type == WorkflowAlert.Tipo.VENCE_PROXIMAMENTE:
            return bool(target_date and task.fecha_limite == target_date and target_date > today)
        if alert_type == WorkflowAlert.Tipo.SIN_RESPONSABLE:
            return task.responsable_id is None
        if alert_type == WorkflowAlert.Tipo.TAREA_RECHAZADA:
            return task.estado == TaskInstance.Estado.RECHAZADA
        return False

    def _tasks_for_generation(self):
        return (
            TaskInstance.objects.select_related("workflow", "workflow__propietario", "workflow__alert_config", "responsable")
            .filter(workflow__estado=WorkflowInstance.Estado.EN_CURSO)
            .exclude(estado=TaskInstance.Estado.TERMINADA)
        )

    def _alert_specs_for_task(self, task):
        hoy = timezone.localdate()
        config = self._alert_config_for_task(task)
        if not config["activa"]:
            return []

        specs = []
        alert_types = []

        if config["avisar_atrasadas"] and task.fecha_limite and task.fecha_limite < hoy:
            fecha_alerta = hoy if config["repetir_atrasadas_diario"] else task.fecha_limite
            alert_types.append((WorkflowAlert.Tipo.TAREA_ATRASADA, fecha_alerta))
        if config["avisar_vencen_hoy"] and task.fecha_limite == hoy:
            alert_types.append((WorkflowAlert.Tipo.VENCE_HOY, task.fecha_limite))
        if config["dias_antes_vencimiento"] > 0 and task.fecha_limite == hoy + timezone.timedelta(days=config["dias_antes_vencimiento"]):
            tipo = WorkflowAlert.Tipo.VENCE_MANANA if config["dias_antes_vencimiento"] == 1 else WorkflowAlert.Tipo.VENCE_PROXIMAMENTE
            alert_types.append((tipo, task.fecha_limite))
        if config["avisar_sin_responsable"] and task.responsable_id is None:
            alert_types.append((WorkflowAlert.Tipo.SIN_RESPONSABLE, None))
        if config["avisar_rechazadas"] and task.estado == TaskInstance.Estado.RECHAZADA:
            alert_types.append((WorkflowAlert.Tipo.TAREA_RECHAZADA, task.fecha_limite))

        destinatarios = self._alert_recipients_for_task(task, config)

        for tipo, fecha_objetivo in alert_types:
            for destinatario in destinatarios:
                for channel in config["channels"]:
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

    def _alert_config_for_task(self, task):
        config = getattr(task.workflow, "alert_config", None)
        default_channels = getattr(settings, "WORKFLOW_ALERTS_DEFAULT_CHANNELS", None) or ["telegram"]
        if config is None:
            return {
                "activa": True,
                "channels": default_channels,
                "dias_antes_vencimiento": 1,
                "avisar_vencen_hoy": True,
                "avisar_atrasadas": True,
                "repetir_atrasadas_diario": False,
                "avisar_sin_responsable": True,
                "avisar_rechazadas": True,
                "avisar_propietario": False,
            }
        return {
            "activa": config.activa,
            "channels": config.channels,
            "dias_antes_vencimiento": config.dias_antes_vencimiento,
            "avisar_vencen_hoy": config.avisar_vencen_hoy,
            "avisar_atrasadas": config.avisar_atrasadas,
            "repetir_atrasadas_diario": config.repetir_atrasadas_diario,
            "avisar_sin_responsable": config.avisar_sin_responsable,
            "avisar_rechazadas": config.avisar_rechazadas,
            "avisar_propietario": config.avisar_propietario,
        }

    def _alert_recipients_for_task(self, task, config):
        recipients = [task.responsable or task.workflow.propietario]
        if config["avisar_propietario"] and task.workflow.propietario_id:
            recipients.append(task.workflow.propietario)
        return list(dict.fromkeys(recipients))

    def _dedupe_key(self, task, tipo, fecha_objetivo, destinatario, channel):
        destinatario_key = destinatario.id if destinatario else "none"
        fecha_key = fecha_objetivo.isoformat() if fecha_objetivo else "none"
        return f"task:{task.id}:tipo:{tipo}:fecha:{fecha_key}:dest:{destinatario_key}:canal:{channel}"

    def _subject(self, task, tipo):
        labels = {
            WorkflowAlert.Tipo.TAREA_ATRASADA: "Tarea atrasada",
            WorkflowAlert.Tipo.VENCE_HOY: "Tarea vence hoy",
            WorkflowAlert.Tipo.VENCE_MANANA: "Tarea vence mañana",
            WorkflowAlert.Tipo.VENCE_PROXIMAMENTE: "Tarea vence próximamente",
            WorkflowAlert.Tipo.SIN_RESPONSABLE: "Tarea sin responsable",
            WorkflowAlert.Tipo.TAREA_RECHAZADA: "Tarea rechazada",
        }
        return f"{labels[tipo]}: {task.nombre}"

    def _message(self, task, tipo, fecha_objetivo):
        workflow = task.workflow.nombre
        due = task.fecha_limite.isoformat() if task.fecha_limite else "sin fecha límite"
        messages = {
            WorkflowAlert.Tipo.TAREA_ATRASADA: f"La tarea '{task.nombre}' del workflow '{workflow}' está atrasada. Fecha límite: {due}.",
            WorkflowAlert.Tipo.VENCE_HOY: f"La tarea '{task.nombre}' del workflow '{workflow}' vence hoy ({due}).",
            WorkflowAlert.Tipo.VENCE_MANANA: f"La tarea '{task.nombre}' del workflow '{workflow}' vence mañana ({due}).",
            WorkflowAlert.Tipo.VENCE_PROXIMAMENTE: f"La tarea '{task.nombre}' del workflow '{workflow}' vence próximamente ({due}).",
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
