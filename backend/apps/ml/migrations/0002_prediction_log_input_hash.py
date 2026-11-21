from django.db import migrations


class Migration(migrations.Migration):
    """DATABASE_SCHEMA names the request digest `input_hash` (sha256 hex of the canonical rows; never raw rows)."""

    dependencies = [("ml", "0001_initial")]

    operations = [
        migrations.RenameField(model_name="predictionlog", old_name="input_sha256", new_name="input_hash"),
    ]
