from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


class TimestampedModel(models.Model):
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class WorkflowTemplate(TimestampedModel):
    nombre = models.CharField(max_length=180, unique=True)
    descripcion = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['nombre']
        verbose_name = 'Plantilla de flujo'
        verbose_name_plural = 'Plantillas de flujos'

    def __str__(self):
        return self.nombre


class TaskTemplate(TimestampedModel):
    class Tipo(models.TextChoices):
        TAREA = 'tarea', 'Tarea'
        APROBACION = 'aprobacion', 'Aprobación'
        HITO = 'hito', 'Hito'

    workflow = models.ForeignKey(WorkflowTemplate, on_delete=models.CASCADE, related_name='tareas')
    nombre = models.CharField(max_length=180)
    descripcion = models.TextField(blank=True)
    tipo = models.CharField(max_length=20, choices=Tipo.choices, default=Tipo.TAREA)
    orden = models.PositiveIntegerField(default=1)
    duracion_dias = models.PositiveIntegerField(default=1)
    responsable_predeterminado = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='plantillas_responsable'
    )
    requiere_aprobacion = models.BooleanField(default=False)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['workflow', 'orden', 'nombre']
        constraints = [
            models.UniqueConstraint(fields=['workflow', 'orden'], name='uniq_orden_por_workflow_template'),
            models.UniqueConstraint(fields=['workflow', 'nombre'], name='uniq_nombre_tarea_por_workflow_template'),
        ]
        indexes = [models.Index(fields=['workflow', 'orden'])]
        verbose_name = 'Tarea de plantilla'
        verbose_name_plural = 'Tareas de plantilla'

    def __str__(self):
        return f'{self.workflow} - {self.orden}. {self.nombre}'


class TaskDependency(TimestampedModel):
    """Permite tareas secuenciales y paralelas: una tarea puede depender de varias anteriores."""
    tarea = models.ForeignKey(TaskTemplate, on_delete=models.CASCADE, related_name='dependencias_entrada')
    depende_de = models.ForeignKey(TaskTemplate, on_delete=models.CASCADE, related_name='dependencias_salida')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['tarea', 'depende_de'], name='uniq_dependencia_template'),
            models.CheckConstraint(condition=~Q(tarea=models.F('depende_de')), name='no_auto_dependencia_template'),
        ]
        verbose_name = 'Dependencia de tarea'
        verbose_name_plural = 'Dependencias de tareas'

    def clean(self):
        if self.tarea.workflow_id != self.depende_de.workflow_id:
            raise ValidationError('Las tareas deben pertenecer al mismo flujo.')

    def __str__(self):
        return f'{self.tarea} depende de {self.depende_de}'


class WorkflowInstance(TimestampedModel):
    class Estado(models.TextChoices):
        BORRADOR = 'borrador', 'Borrador'
        EN_CURSO = 'en_curso', 'En curso'
        TERMINADO = 'terminado', 'Terminado'
        PAUSADO = 'pausado', 'Pausado'
        CANCELADO = 'cancelado', 'Cancelado'

    plantilla = models.ForeignKey(WorkflowTemplate, on_delete=models.PROTECT, related_name='instancias')
    nombre = models.CharField(max_length=200)
    descripcion = models.TextField(blank=True)
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.BORRADOR)
    propietario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_limite = models.DateField(null=True, blank=True)
    fecha_termino = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-creado']
        indexes = [models.Index(fields=['estado', 'fecha_limite'])]
        verbose_name = 'Flujo activo'
        verbose_name_plural = 'Flujos activos'

    def __str__(self):
        return self.nombre

    @transaction.atomic
    def iniciar(self, usuario=None):
        if self.tareas.exists():
            return
        hoy = timezone.localdate()
        acumulado = 0
        for plantilla_tarea in self.plantilla.tareas.filter(activo=True).order_by('orden'):
            inicio_estimado = self.fecha_inicio or hoy
            vencimiento = inicio_estimado + timezone.timedelta(days=acumulado + plantilla_tarea.duracion_dias)
            TaskInstance.objects.create(
                workflow=self,
                plantilla_tarea=plantilla_tarea,
                nombre=plantilla_tarea.nombre,
                descripcion=plantilla_tarea.descripcion,
                responsable=plantilla_tarea.responsable_predeterminado,
                estado=TaskInstance.Estado.PENDIENTE,
                fecha_limite=vencimiento,
                orden=plantilla_tarea.orden,
            )
            acumulado += plantilla_tarea.duracion_dias
        self.estado = self.Estado.EN_CURSO
        if not self.fecha_inicio:
            self.fecha_inicio = hoy
        self.save(update_fields=['estado', 'fecha_inicio', 'actualizado'])
        self.activar_tareas_disponibles(usuario=usuario)

    @transaction.atomic
    def activar_tareas_disponibles(self, usuario=None):
        for tarea in self.tareas.filter(estado=TaskInstance.Estado.PENDIENTE):
            dependencias_template = tarea.plantilla_tarea.dependencias_entrada.values_list('depende_de_id', flat=True)
            if not dependencias_template:
                tarea.activar(usuario=usuario)
                continue
            pendientes = self.tareas.filter(
                plantilla_tarea_id__in=dependencias_template
            ).exclude(estado=TaskInstance.Estado.TERMINADA).exists()
            if not pendientes:
                tarea.activar(usuario=usuario)
        if self.tareas.exists() and not self.tareas.exclude(estado=TaskInstance.Estado.TERMINADA).exists():
            self.estado = self.Estado.TERMINADO
            self.fecha_termino = timezone.now()
            self.save(update_fields=['estado', 'fecha_termino', 'actualizado'])

    @property
    def porcentaje_avance(self):
        total = self.tareas.count()
        if total == 0:
            return 0
        terminadas = self.tareas.filter(estado=TaskInstance.Estado.TERMINADA).count()
        return round((terminadas / total) * 100, 1)


class TaskInstance(TimestampedModel):
    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        EN_CURSO = 'en_curso', 'En curso'
        TERMINADA = 'terminada', 'Terminada'
        BLOQUEADA = 'bloqueada', 'Bloqueada'
        RECHAZADA = 'rechazada', 'Rechazada'

    workflow = models.ForeignKey(WorkflowInstance, on_delete=models.CASCADE, related_name='tareas')
    plantilla_tarea = models.ForeignKey(TaskTemplate, on_delete=models.PROTECT, related_name='instancias')
    nombre = models.CharField(max_length=180)
    descripcion = models.TextField(blank=True)
    responsable = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.PENDIENTE)
    orden = models.PositiveIntegerField(default=1)
    fecha_inicio = models.DateTimeField(null=True, blank=True)
    fecha_limite = models.DateField(null=True, blank=True)
    fecha_termino = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['workflow', 'orden', 'fecha_limite']
        indexes = [
            models.Index(fields=['workflow', 'estado']),
            models.Index(fields=['responsable', 'estado']),
            models.Index(fields=['fecha_limite']),
        ]
        verbose_name = 'Tarea activa'
        verbose_name_plural = 'Tareas activas'

    def __str__(self):
        return f'{self.workflow} - {self.nombre}'

    @property
    def atrasada(self):
        return self.estado != self.Estado.TERMINADA and self.fecha_limite and self.fecha_limite < timezone.localdate()

    def activar(self, usuario=None):
        if self.estado == self.Estado.PENDIENTE:
            self.estado = self.Estado.EN_CURSO
            self.fecha_inicio = timezone.now()
            self.save(update_fields=['estado', 'fecha_inicio', 'actualizado'])
            TaskHistory.objects.create(tarea=self, usuario=usuario, accion='activada', detalle='Tarea activada automáticamente.')

    @transaction.atomic
    def terminar(self, usuario=None, comentario=''):
        self.estado = self.Estado.TERMINADA
        self.fecha_termino = timezone.now()
        self.save(update_fields=['estado', 'fecha_termino', 'actualizado'])
        TaskHistory.objects.create(tarea=self, usuario=usuario, accion='terminada', detalle=comentario)
        self.workflow.activar_tareas_disponibles(usuario=usuario)

    def rechazar(self, usuario=None, comentario=''):
        self.estado = self.Estado.RECHAZADA
        self.fecha_termino = None
        self.save(update_fields=['estado', 'fecha_termino', 'actualizado'])
        if self.workflow.estado == WorkflowInstance.Estado.TERMINADO:
            self.workflow.estado = WorkflowInstance.Estado.EN_CURSO
            self.workflow.fecha_termino = None
            self.workflow.save(update_fields=['estado', 'fecha_termino', 'actualizado'])
        TaskHistory.objects.create(tarea=self, usuario=usuario, accion='rechazada', detalle=comentario)

    def reabrir(self, usuario=None, comentario=''):
        self.estado = self.Estado.EN_CURSO
        if not self.fecha_inicio:
            self.fecha_inicio = timezone.now()
        self.fecha_termino = None
        self.save(update_fields=['estado', 'fecha_inicio', 'fecha_termino', 'actualizado'])
        if self.workflow.estado == WorkflowInstance.Estado.TERMINADO:
            self.workflow.estado = WorkflowInstance.Estado.EN_CURSO
            self.workflow.fecha_termino = None
            self.workflow.save(update_fields=['estado', 'fecha_termino', 'actualizado'])
        TaskHistory.objects.create(tarea=self, usuario=usuario, accion='reabierta', detalle=comentario)


class WorkflowAlert(TimestampedModel):
    class Tipo(models.TextChoices):
        TAREA_ATRASADA = 'tarea_atrasada', 'Tarea atrasada'
        VENCE_HOY = 'vence_hoy', 'Vence hoy'
        VENCE_MANANA = 'vence_manana', 'Vence mañana'
        SIN_RESPONSABLE = 'sin_responsable', 'Sin responsable'
        TAREA_RECHAZADA = 'tarea_rechazada', 'Tarea rechazada'

    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        EN_ENVIO = 'en_envio', 'En envío'
        ENVIADA = 'enviada', 'Enviada'
        FALLIDA = 'fallida', 'Fallida'
        CANCELADA = 'cancelada', 'Cancelada'

    tarea = models.ForeignKey(TaskInstance, on_delete=models.CASCADE, related_name='alertas')
    destinatario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='workflow_alertas',
    )
    tipo = models.CharField(max_length=40, choices=Tipo.choices)
    canal = models.CharField(max_length=20, default='telegram')
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.PENDIENTE)
    dedupe_key = models.CharField(max_length=255, unique=True)
    asunto = models.CharField(max_length=255)
    mensaje = models.TextField()
    fecha_objetivo = models.DateField(null=True, blank=True)
    enviar_despues_de = models.DateTimeField(null=True, blank=True)
    enviada_en = models.DateTimeField(null=True, blank=True)
    intentos = models.PositiveIntegerField(default=0)
    ultimo_intento_en = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['estado', 'enviar_despues_de', 'creado']
        indexes = [
            models.Index(fields=['estado', 'canal']),
            models.Index(fields=['tipo', 'fecha_objetivo']),
            models.Index(fields=['tarea', 'tipo']),
        ]
        verbose_name = 'Alerta de workflow'
        verbose_name_plural = 'Alertas de workflow'

    def __str__(self):
        return f'{self.get_tipo_display()} para {self.tarea} ({self.estado})'


class TaskHistory(TimestampedModel):
    tarea = models.ForeignKey(TaskInstance, on_delete=models.CASCADE, related_name='historial')
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    accion = models.CharField(max_length=80)
    detalle = models.TextField(blank=True)

    class Meta:
        ordering = ['-creado']
        verbose_name = 'Historial de tarea'
        verbose_name_plural = 'Historial de tareas'

    def __str__(self):
        return f'{self.tarea} - {self.accion}'


class TaskComment(TimestampedModel):
    tarea = models.ForeignKey(TaskInstance, on_delete=models.CASCADE, related_name='comentarios')
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    comentario = models.TextField()

    class Meta:
        ordering = ['-creado']
        verbose_name = 'Comentario'
        verbose_name_plural = 'Comentarios'

    def __str__(self):
        return f'{self.tarea} - {self.usuario or "Sin usuario"}'


class TaskAttachment(TimestampedModel):
    tarea = models.ForeignKey(TaskInstance, on_delete=models.CASCADE, related_name='adjuntos')
    archivo = models.FileField(upload_to='task_attachments/%Y/%m/')
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ['-creado']
        verbose_name = 'Documento adjunto'
        verbose_name_plural = 'Documentos adjuntos'

    def __str__(self):
        return f'{self.tarea} - {self.archivo.name}'

    @property
    def filename(self):
        return Path(str(self.archivo.name)).name
