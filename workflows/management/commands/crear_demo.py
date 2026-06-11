from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from workflows.models import WorkflowTemplate, TaskTemplate, TaskDependency, WorkflowInstance


class Command(BaseCommand):
    help = 'Crea usuarios y un flujo de demostración con tareas paralelas.'

    def handle(self, *args, **options):
        User = get_user_model()
        admin, _ = User.objects.get_or_create(username='admin', defaults={'is_staff': True, 'is_superuser': True, 'email': 'admin@example.com'})
        admin.set_password('admin123')
        admin.save()
        legal, _ = User.objects.get_or_create(username='legal')
        finanzas, _ = User.objects.get_or_create(username='finanzas')
        gerencia, _ = User.objects.get_or_create(username='gerencia')

        flujo, _ = WorkflowTemplate.objects.get_or_create(nombre='Flujo genérico de aprobación')
        t1, _ = TaskTemplate.objects.get_or_create(workflow=flujo, orden=1, defaults={'nombre': 'Inicio y antecedentes', 'responsable_predeterminado': admin, 'duracion_dias': 2})
        t2, _ = TaskTemplate.objects.get_or_create(workflow=flujo, orden=2, defaults={'nombre': 'Revisión legal', 'responsable_predeterminado': legal, 'duracion_dias': 3})
        t3, _ = TaskTemplate.objects.get_or_create(workflow=flujo, orden=3, defaults={'nombre': 'Revisión financiera', 'responsable_predeterminado': finanzas, 'duracion_dias': 3})
        t4, _ = TaskTemplate.objects.get_or_create(workflow=flujo, orden=4, defaults={'nombre': 'Aprobación gerencia', 'responsable_predeterminado': gerencia, 'duracion_dias': 2, 'requiere_aprobacion': True})
        t5, _ = TaskTemplate.objects.get_or_create(workflow=flujo, orden=5, defaults={'nombre': 'Cierre y archivo', 'responsable_predeterminado': admin, 'duracion_dias': 1})

        for tarea, depende_de in [(t2, t1), (t3, t1), (t4, t2), (t4, t3), (t5, t4)]:
            TaskDependency.objects.get_or_create(tarea=tarea, depende_de=depende_de)

        instancia, _ = WorkflowInstance.objects.get_or_create(
            plantilla=flujo,
            nombre='Caso demo',
            defaults={'propietario': admin, 'descripcion': 'Caso de prueba con tareas paralelas.'}
        )
        self.stdout.write(self.style.SUCCESS('Demo creado. Usuario admin / clave admin123'))
