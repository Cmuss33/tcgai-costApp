# Evaluator receipts: justification, samples, rubric version, graded-at,
# and superseded-score history on Chat (2026-10-15).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cost_management', '0025_cartevent'),
    ]

    operations = [
        migrations.AddField(
            model_name='chat',
            name='evaluation_justification',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='chat',
            name='evaluation_samples',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='chat',
            name='evaluation_rubric_version',
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
        migrations.AddField(
            model_name='chat',
            name='evaluation_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='chat',
            name='evaluation_history',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
