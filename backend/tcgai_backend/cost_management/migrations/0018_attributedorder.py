from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("cost_management", "0017_flag_shadowtest_chats"),
    ]

    operations = [
        migrations.CreateModel(
            name="AttributedOrder",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "chat_id_raw",
                    models.CharField(db_index=True, max_length=255),
                ),
                ("shop", models.CharField(db_index=True, default="", max_length=255)),
                ("order_id", models.CharField(max_length=255)),
                (
                    "attribution_type",
                    models.CharField(default="influenced", max_length=20),
                ),
                (
                    "influenced_revenue",
                    models.DecimalField(decimal_places=2, default=0, max_digits=12),
                ),
                (
                    "order_total",
                    models.DecimalField(decimal_places=2, default=0, max_digits=12),
                ),
                ("currency", models.CharField(default="USD", max_length=3)),
                (
                    "order_created_at",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                ("surfaces", models.JSONField(blank=True, null=True)),
                ("influence_score", models.FloatField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "chat",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="attributed_orders",
                        to="cost_management.chat",
                        to_field="chat_id",
                    ),
                ),
            ],
            options={
                "ordering": ["-order_created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="attributedorder",
            constraint=models.UniqueConstraint(
                fields=("shop", "order_id"), name="unique_shop_order_attribution"
            ),
        ),
    ]
