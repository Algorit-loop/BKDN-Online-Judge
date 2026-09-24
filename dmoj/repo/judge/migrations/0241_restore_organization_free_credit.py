# Re-add the free credit column dropped in 0221 (same db_column as VNOI-Admin/OJ).
# Unlike VNOJ there is no per-organization monthly_free_credit_limit: the amount comes from
# settings.BKDNOJ_MONTHLY_FREE_CREDIT, both on creation and on the monthly reset.

from django.db import migrations, models

import judge.models.profile


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0240_alter_problem_options_alter_problem_enable_new_ide'),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='free_credit',
            field=models.FloatField(db_column='monthly_credit', default=judge.models.profile.default_monthly_free_credit, help_text='Remaining free credits for the current month'),
        ),
    ]
