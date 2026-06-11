# Workflow Django + PostgreSQL

Base genérica para administrar flujos de trabajo con tareas secuenciales y paralelas.

## Incluye

- Plantillas de flujo editables.
- Tareas de plantilla con orden, duración y responsable predeterminado.
- Dependencias entre tareas para permitir tareas paralelas.
- Instancias reales de flujo.
- Activación automática de tareas cuando sus dependencias están terminadas.
- Dashboard con avance, tareas abiertas, atrasos y vista temporal.
- Administración Django para crear flujos, tareas, responsables y dependencias.
- PostgreSQL como base de datos.

## Instalación local

```bash
cd workflow_django
python -m venv venv
source venv/bin/activate   # Linux / WSL
# En Windows PowerShell: venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
```

Crear base PostgreSQL:

```sql
CREATE DATABASE workflow_db;
CREATE USER workflow_user WITH PASSWORD 'workflow_password';
GRANT ALL PRIVILEGES ON DATABASE workflow_db TO workflow_user;
```

Migrar:

```bash
python manage.py makemigrations
python manage.py migrate
python manage.py crear_demo
python manage.py runserver
```

Ingreso:

```text
http://127.0.0.1:8000/
usuario: admin
clave: admin123
```

## Uso inicial

1. Entrar a `/admin/`.
2. Crear usuarios responsables.
3. Crear una plantilla de flujo.
4. Crear tareas de plantilla.
5. Crear dependencias entre tareas.
6. Crear una instancia real del flujo.
7. Iniciar el flujo.
8. Marcar tareas como terminadas.
9. El sistema activa automáticamente las tareas siguientes disponibles.

## Tareas paralelas

Ejemplo:

```text
Tarea 1: Inicio
Tarea 2: Revisión legal       depende de Tarea 1
Tarea 3: Revisión financiera  depende de Tarea 1
Tarea 4: Aprobación gerencia  depende de Tarea 2 y Tarea 3
```

Cuando termina la Tarea 1, se activan al mismo tiempo la Tarea 2 y la Tarea 3.
La Tarea 4 solo se activa cuando ambas están terminadas.

## Próximas mejoras sugeridas

- Correos automáticos.
- Adjuntos por tarea.
- Roles y permisos por área.
- Gantt más avanzado.
- Exportación Excel.
- API REST.
- Notificaciones por Telegram o Gmail.
