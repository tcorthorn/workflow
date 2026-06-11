import csv

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.core.exceptions import PermissionDenied
from django.db.models import Count, F, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .forms import TaskAttachmentForm, TaskCommentForm, WorkflowInstanceForm
from .models import TaskAttachment, TaskComment, TaskHistory, TaskInstance, WorkflowInstance

ROLE_GROUPS = {'Admin', 'Administrador', 'Gerencia', 'Workflow Manager'}


def can_user_manage_task(user, task):
    if not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    if task.responsable_id == user.id:
        return True
    return user.groups.filter(name__in=ROLE_GROUPS).exists()


def notify_task_event(task, subject, message):
    if task.responsable and task.responsable.email:
        send_mail(
            subject,
            message,
            None,
            [task.responsable.email],
            fail_silently=True,
        )


def _next_url(request, task=None):
    default = reverse('dashboard') if task is None else reverse('workflow_detail', args=[task.workflow_id])
    candidate = request.POST.get('next') or request.GET.get('next')
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return default


def _require_task_permission(request, task):
    if not can_user_manage_task(request.user, task):
        raise PermissionDenied('No tienes permiso para modificar esta tarea.')


@login_required
def dashboard(request):
    hoy = timezone.localdate()
    tareas_qs = TaskInstance.objects.select_related(
        'workflow', 'responsable'
    ).prefetch_related('comentarios', 'adjuntos').order_by(
        F('fecha_limite').asc(nulls_last=True), 'workflow__nombre', 'orden'
    )

    estado = request.GET.get('estado', '')
    responsable = request.GET.get('responsable', '')
    proyecto = request.GET.get('proyecto', '')
    q = request.GET.get('q', '').strip()

    if estado:
        tareas_qs = tareas_qs.filter(estado=estado)
    if responsable:
        tareas_qs = tareas_qs.filter(responsable_id=responsable)
    if proyecto:
        tareas_qs = tareas_qs.filter(workflow_id=proyecto)
    if q:
        tareas_qs = tareas_qs.filter(
            Q(nombre__icontains=q)
            | Q(descripcion__icontains=q)
            | Q(workflow__nombre__icontains=q)
            | Q(responsable__username__icontains=q)
            | Q(responsable__first_name__icontains=q)
            | Q(responsable__last_name__icontains=q)
        )

    tareas = list(tareas_qs[:80])
    for tarea in tareas:
        tarea.can_manage = can_user_manage_task(request.user, tarea)
    flujos = WorkflowInstance.objects.prefetch_related('tareas').select_related('plantilla', 'propietario').all()[:20]
    flujos_en_curso = WorkflowInstance.objects.filter(estado=WorkflowInstance.Estado.EN_CURSO).count()
    tareas_pendientes = TaskInstance.objects.filter(estado=TaskInstance.Estado.PENDIENTE).count()
    tareas_en_curso = TaskInstance.objects.filter(estado=TaskInstance.Estado.EN_CURSO).count()
    tareas_completadas = TaskInstance.objects.filter(estado=TaskInstance.Estado.TERMINADA).count()
    tareas_rechazadas = TaskInstance.objects.filter(estado=TaskInstance.Estado.RECHAZADA).count()
    tareas_atrasadas = TaskInstance.objects.exclude(
        estado=TaskInstance.Estado.TERMINADA
    ).filter(fecha_limite__lt=hoy).count()

    estados = TaskInstance.Estado.choices
    kanban = []
    for value, label in estados:
        kanban.append({
            'value': value,
            'label': label,
            'tareas': [t for t in tareas if t.estado == value],
        })

    responsables = get_user_model().objects.filter(taskinstance__isnull=False).distinct().order_by('username')
    proyectos = WorkflowInstance.objects.filter(tareas__isnull=False).distinct().order_by('nombre')
    comentarios_recientes = TaskComment.objects.select_related('tarea', 'usuario', 'tarea__workflow')[:8]
    adjuntos_recientes = TaskAttachment.objects.select_related('tarea', 'uploaded_by', 'tarea__workflow')[:8]
    historial_reciente = TaskHistory.objects.select_related('tarea', 'usuario', 'tarea__workflow')[:12]

    resumen = {
        'flujos_en_curso': flujos_en_curso,
        'tareas_en_curso': tareas_en_curso,
        'tareas_atrasadas': tareas_atrasadas,
        'tareas_terminadas': tareas_completadas,
        'tareas_rechazadas': tareas_rechazadas,
    }
    return render(request, 'workflows/dashboard.html', {
        'flujos': flujos,
        'tareas': tareas,
        'total_flujos': flujos_en_curso,
        'tareas_pendientes': tareas_pendientes,
        'tareas_en_curso': tareas_en_curso,
        'tareas_completadas': tareas_completadas,
        'tareas_rechazadas': tareas_rechazadas,
        'tareas_atrasadas': tareas_atrasadas,
        'resumen': resumen,
        'estados': estados,
        'kanban': kanban,
        'responsables': responsables,
        'proyectos': proyectos,
        'filtros': {'estado': estado, 'responsable': responsable, 'proyecto': proyecto, 'q': q},
        'comentarios_recientes': comentarios_recientes,
        'adjuntos_recientes': adjuntos_recientes,
        'historial_reciente': historial_reciente,
    })


@login_required
def dashboard_data(request):
    tareas = TaskInstance.objects.select_related('workflow', 'responsable').all().order_by('fecha_limite')
    timeline = []
    for t in tareas:
        timeline.append({
            'flujo': t.workflow.nombre,
            'tarea': t.nombre,
            'responsable': t.responsable.get_full_name() or t.responsable.username if t.responsable else 'Sin responsable',
            'estado': t.get_estado_display(),
            'inicio': t.fecha_inicio.date().isoformat() if t.fecha_inicio else None,
            'limite': t.fecha_limite.isoformat() if t.fecha_limite else None,
            'termino': t.fecha_termino.date().isoformat() if t.fecha_termino else None,
        })
    por_estado = list(TaskInstance.objects.values('estado').annotate(total=Count('id')).order_by('estado'))
    return JsonResponse({'timeline': timeline, 'por_estado': por_estado})


@login_required
def workflow_list(request):
    flujos = WorkflowInstance.objects.select_related('plantilla', 'propietario').all()
    return render(request, 'workflows/workflow_list.html', {'flujos': flujos})


@login_required
def workflow_create(request):
    if request.method == 'POST':
        form = WorkflowInstanceForm(request.POST)
        if form.is_valid():
            flujo = form.save()
            messages.success(request, 'Flujo creado.')
            return redirect('workflow_detail', pk=flujo.pk)
    else:
        form = WorkflowInstanceForm(initial={'propietario': request.user, 'fecha_inicio': timezone.localdate()})
    return render(request, 'workflows/workflow_form.html', {'form': form})


@login_required
def workflow_detail(request, pk):
    flujo = get_object_or_404(WorkflowInstance.objects.select_related('plantilla', 'propietario'), pk=pk)
    tareas = flujo.tareas.select_related('responsable').prefetch_related('historial', 'comentarios', 'adjuntos').all()
    return render(request, 'workflows/workflow_detail.html', {'flujo': flujo, 'tareas': tareas})


@login_required
def workflow_start(request, pk):
    flujo = get_object_or_404(WorkflowInstance, pk=pk)
    flujo.iniciar(usuario=request.user)
    messages.success(request, 'Flujo iniciado. Se activaron las tareas disponibles.')
    return redirect('workflow_detail', pk=flujo.pk)


@login_required
def task_complete(request, pk):
    tarea = get_object_or_404(TaskInstance, pk=pk)
    _require_task_permission(request, tarea)
    if request.method == 'POST':
        comentario = request.POST.get('comentario', '')
        tarea.terminar(usuario=request.user, comentario=comentario)
        notify_task_event(tarea, 'Tarea terminada', f'La tarea "{tarea.nombre}" fue terminada. {comentario}')
        messages.success(request, 'Tarea terminada. El flujo activó las siguientes tareas disponibles.')
    return redirect(_next_url(request, tarea))


@login_required
def task_reject(request, pk):
    tarea = get_object_or_404(TaskInstance, pk=pk)
    _require_task_permission(request, tarea)
    if request.method == 'POST':
        comentario = request.POST.get('comentario', '')
        tarea.rechazar(usuario=request.user, comentario=comentario)
        notify_task_event(tarea, 'Tarea rechazada', f'La tarea "{tarea.nombre}" fue rechazada/devuelta. {comentario}')
        messages.warning(request, 'Tarea rechazada/devuelta al responsable.')
    return redirect(_next_url(request, tarea))


@login_required
def task_reopen(request, pk):
    tarea = get_object_or_404(TaskInstance, pk=pk)
    _require_task_permission(request, tarea)
    if request.method == 'POST':
        comentario = request.POST.get('comentario', '')
        tarea.reabrir(usuario=request.user, comentario=comentario)
        notify_task_event(tarea, 'Tarea reactivada', f'La tarea "{tarea.nombre}" fue reactivada. {comentario}')
        messages.info(request, 'Tarea devuelta/reactivada en curso.')
    return redirect(_next_url(request, tarea))


@login_required
def task_comment(request, pk):
    tarea = get_object_or_404(TaskInstance, pk=pk)
    _require_task_permission(request, tarea)
    if request.method == 'POST':
        form = TaskCommentForm(request.POST)
        if form.is_valid():
            comentario = form.save(commit=False)
            comentario.tarea = tarea
            comentario.usuario = request.user
            comentario.save()
            notify_task_event(tarea, 'Nuevo comentario en tarea', f'{request.user} comentó en "{tarea.nombre}": {comentario.comentario}')
            messages.success(request, 'Comentario agregado.')
    return redirect(_next_url(request, tarea))


@login_required
def task_attachment(request, pk):
    tarea = get_object_or_404(TaskInstance, pk=pk)
    _require_task_permission(request, tarea)
    if request.method == 'POST':
        form = TaskAttachmentForm(request.POST, request.FILES)
        if form.is_valid():
            adjunto = form.save(commit=False)
            adjunto.tarea = tarea
            adjunto.uploaded_by = request.user
            adjunto.save()
            notify_task_event(tarea, 'Nuevo documento adjunto', f'Se adjuntó "{adjunto.archivo.name}" a la tarea "{tarea.nombre}".')
            messages.success(request, 'Documento adjuntado.')
    return redirect(_next_url(request, tarea))


@login_required
def task_report_csv(request):
    response = HttpResponse('\ufeff', content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="tareas_workflow.csv"'
    writer = csv.writer(response)
    writer.writerow([
        'ID', 'Tarea', 'Proyecto/flujo', 'Responsable', 'Estado', 'Fecha inicio',
        'Fecha límite', 'Fecha término', 'Atrasada', 'Comentarios', 'Adjuntos'
    ])
    tareas = TaskInstance.objects.select_related('workflow', 'responsable').annotate(
        total_comentarios=Count('comentarios', distinct=True),
        total_adjuntos=Count('adjuntos', distinct=True),
    ).order_by('workflow__nombre', 'orden')
    for tarea in tareas:
        responsable = tarea.responsable.get_full_name() or tarea.responsable.username if tarea.responsable else 'Sin responsable'
        writer.writerow([
            tarea.pk,
            tarea.nombre,
            tarea.workflow.nombre,
            responsable,
            tarea.get_estado_display(),
            tarea.fecha_inicio.date().isoformat() if tarea.fecha_inicio else '',
            tarea.fecha_limite.isoformat() if tarea.fecha_limite else '',
            tarea.fecha_termino.date().isoformat() if tarea.fecha_termino else '',
            'Sí' if tarea.atrasada else 'No',
            tarea.total_comentarios,
            tarea.total_adjuntos,
        ])
    return response
