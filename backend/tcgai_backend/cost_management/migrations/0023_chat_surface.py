# Generated for surface attribution (chatbot PR #553 sends surface tag)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cost_management', '0022_chat_shop'),
    ]

    operations = [
        migrations.AddField(
            model_name='chat',
            name='surface',
            field=models.CharField(db_index=True, default='chat', max_length=32),
        ),
    ]
