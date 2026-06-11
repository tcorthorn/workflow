from django import forms
from .models import WorkflowInstance, TaskInstance, TaskComment, TaskAttachment


class WorkflowInstanceForm(forms.ModelForm):
    class Meta:
        model = WorkflowInstance
        fields = ['plantilla', 'nombre', 'descripcion', 'propietario', 'fecha_inicio', 'fecha_limite']
        widgets = {
            'fecha_inicio': forms.DateInput(attrs={'type': 'date'}),
            'fecha_limite': forms.DateInput(attrs={'type': 'date'}),
        }


class TaskInstanceForm(forms.ModelForm):
    class Meta:
        model = TaskInstance
        fields = ['nombre', 'descripcion', 'responsable', 'estado', 'fecha_limite']
        widgets = {'fecha_limite': forms.DateInput(attrs={'type': 'date'})}


class TaskCommentForm(forms.ModelForm):
    class Meta:
        model = TaskComment
        fields = ['comentario']
        widgets = {
            'comentario': forms.Textarea(attrs={'rows': 2, 'placeholder': 'Agregar comentario...'})
        }


class TaskAttachmentForm(forms.ModelForm):
    class Meta:
        model = TaskAttachment
        fields = ['archivo']
