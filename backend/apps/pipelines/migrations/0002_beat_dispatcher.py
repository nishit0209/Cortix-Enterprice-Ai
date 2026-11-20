"""Register the pipelines beat dispatcher (django_celery_beat PeriodicTask, every 30 s).

A data migration (not AppConfig.ready) so no DB writes happen at import/startup, the entry exists in every
environment that ran migrations, and operators can pause/edit it in the beat schedule without a deploy.
Idempotent: re-running updates the existing entry.
"""

from django.db import migrations

TASK_NAME = "pipelines.dispatch_due"
ENTRY_NAME = "pipelines: dispatch due schedules"


def forwards(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    db = schema_editor.connection.alias
    interval, _ = IntervalSchedule.objects.using(db).get_or_create(every=30, period="seconds")
    PeriodicTask.objects.using(db).update_or_create(
        name=ENTRY_NAME,
        defaults={"task": TASK_NAME, "interval": interval, "enabled": True, "queue": "default",
                  "description": "Enqueues due pipeline schedules per org and reaps runs whose worker died."},
    )


def backwards(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.using(schema_editor.connection.alias).filter(name=ENTRY_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("pipelines", "0001_initial"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
