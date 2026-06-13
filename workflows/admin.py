from django.contrib import admin
from .models import (
    WorkflowTemplate, TaskTemplate, TaskDependency, WorkflowInstance,
    TaskInstance, WorkflowAlert, TaskHistory, TaskComment, TaskAttachment
)


class TaskTemplateInline(admin.TabularInline):
    model = TaskTemplate
    extra = 1
    fields = ('orden', 'nombre', 'tipo', 'duracion_dias', 'responsable_predeterminado', 'requiere_aprobacion', 'activo')


@admin.register(WorkflowTemplate)
class WorkflowTemplateAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'activo', 'creado', 'actualizado')
    search_fields = ('nombre', 'descripcion')
    list_filter = ('activo',)
    inlines = [TaskTemplateInline]


@admin.register(TaskTemplate)
class TaskTemplateAdmin(admin.ModelAdmin):
    list_display = ('workflow', 'orden', 'nombre', 'tipo', 'responsable_predeterminado', 'activo')
    list_filter = ('workflow', 'tipo', 'activo')
    search_fields = ('nombre', 'descripcion')


@admin.register(TaskDependency)
class TaskDependencyAdmin(admin.ModelAdmin):
    list_display = ('tarea', 'depende_de')
    autocomplete_fields = ('tarea', 'depende_de')


class TaskInstanceInline(admin.TabularInline):
    model = TaskInstance
    extra = 0
    readonly_fields = ('fecha_inicio', 'fecha_termino')
    fields = ('orden', 'nombre', 'responsable', 'estado', 'fecha_limite', 'fecha_inicio', 'fecha_termino')


@admin.register(WorkflowInstance)
class WorkflowInstanceAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'plantilla', 'estado', 'propietario', 'fecha_inicio', 'fecha_limite', 'porcentaje_avance')
    list_filter = ('estado', 'plantilla')
    search_fields = ('nombre', 'descripcion')
    inlines = [TaskInstanceInline]
    actions = ['iniciar_flujos']

    @admin.action(description='Iniciar flujos seleccionados')
    def iniciar_flujos(self, request, queryset):
        for flujo in queryset:
            flujo.iniciar(usuario=request.user)


@admin.register(TaskInstance)
class TaskInstanceAdmin(admin.ModelAdmin):
    list_display = ('workflow', 'orden', 'nombre', 'responsable', 'estado', 'fecha_limite', 'atrasada')
    list_filter = ('estado', 'responsable', 'workflow')
    search_fields = ('nombre', 'descripcion', 'workflow__nombre')
    actions = ['terminar_tareas']

    @admin.action(description='Marcar tareas como terminadas')
    def terminar_tareas(self, request, queryset):
        for tarea in queryset:
            tarea.terminar(usuario=request.user, comentario='Terminada desde administración.')


@admin.register(WorkflowAlert)
class WorkflowAlertAdmin(admin.ModelAdmin):
    list_display = ('tipo', 'estado', 'canal', 'tarea', 'destinatario', 'fecha_objetivo', 'enviar_despues_de', 'intentos', 'ultimo_intento_en', 'enviada_en')
    list_filter = ('estado', 'tipo', 'canal', 'destinatario')
    search_fields = ('asunto', 'mensaje', 'tarea__nombre', 'tarea__workflow__nombre', 'dedupe_key')
    readonly_fields = ('creado', 'actualizado', 'enviada_en', 'ultimo_intento_en', 'dedupe_key')


@admin.register(TaskHistory)
class TaskHistoryAdmin(admin.ModelAdmin):
    list_display = ('tarea', 'accion', 'usuario', 'creado')
    list_filter = ('accion', 'usuario')
    search_fields = ('tarea__nombre', 'detalle')


@admin.register(TaskComment)
class TaskCommentAdmin(admin.ModelAdmin):
    list_display = ('tarea', 'usuario', 'creado')
    search_fields = ('comentario', 'tarea__nombre')


@admin.register(TaskAttachment)
class TaskAttachmentAdmin(admin.ModelAdmin):
    list_display = ('tarea', 'archivo', 'uploaded_by', 'creado')
    search_fields = ('archivo', 'tarea__nombre')
    list_filter = ('uploaded_by',)
