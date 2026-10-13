# Generated for Plan 10 probe marking (the AOP marks verification-probe
# chats at runtime via /api/cost/mark_probe/)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cost_management', '0023_chat_surface'),
    ]

    operations = [
        migrations.AddField(
            model_name='chat',
            name='is_probe',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='chat',
            name='probe_marked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
