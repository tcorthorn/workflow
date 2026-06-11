from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from .forms import WorkflowInstanceForm, TaskCommentForm
from .models import WorkflowInstance, TaskInstance, TaskComment


@login_required
def dashboard(request):
    hoy = timezone.localdate()
    flujos = WorkflowInstance.objects.prefetch_related('tareas').all()[:20]
    tareas_usuario = TaskInstance.objects.filter(responsable=request.user).exclude(estado=TaskInstance.Estado.TERMINADA)
    resumen = {
        'flujos_en_curso': WorkflowInstance.objects.filter(estado=WorkflowInstance.Estado.EN_CURSO).count(),
        'tareas_en_curso': TaskInstance.objects.filter(estado=TaskInstance.Estado.EN_CURSO).count(),
        'tareas_atrasadas': TaskInstance.objects.exclude(estado=TaskInstance.Estado.TERMINADA).filter(fecha_limite__lt=hoy).count(),
        'tareas_terminadas': TaskInstance.objects.filter(estado=TaskInstance.Estado.TERMINADA).count(),
    }
    return render(request, 'workflows/dashboard.html', {
        'flujos': flujos,
        'tareas_usuario': tareas_usuario,
        'resumen': resumen,
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
    tareas = flujo.tareas.select_related('responsable').prefetch_related('historial').all()
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
    if request.method == 'POST':
        comentario = request.POST.get('comentario', '')
        tarea.terminar(usuario=request.user, comentario=comentario)
        messages.success(request, 'Tarea terminada. El flujo activó las siguientes tareas disponibles.')
    return redirect('workflow_detail', pk=tarea.workflow_id)
