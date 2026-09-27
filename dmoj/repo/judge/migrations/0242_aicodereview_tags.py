# Keep the tags suggested by each AI code review on the review itself (see AICodeReview.tags).

from django.db import migrations, models


def backfill_tags(apps, schema_editor):
    """Only the latest review of each submission can be recovered: its tags are the UserProblemTag rows of that
    submission, since every new review replaced them. Older reviews keep an empty list."""
    AICodeReview = apps.get_model('judge', 'AICodeReview')
    UserProblemTag = apps.get_model('judge', 'UserProblemTag')
    latest = {}
    for review in AICodeReview.objects.order_by('created_at', 'id'):
        latest[(review.submission_id, review.user_id)] = review
    for (submission_id, user_id), review in latest.items():
        review.tags = list(UserProblemTag.objects.filter(submission_id=submission_id, user_id=user_id)
                           .order_by('id').values_list('tag__name', flat=True))
        if review.tags:
            review.save(update_fields=['tags'])


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0241_restore_organization_free_credit'),
    ]

    operations = [
        migrations.AddField(
            model_name='aicodereview',
            name='tags',
            field=models.JSONField(blank=True, default=list, verbose_name='suggested tags'),
        ),
        migrations.RunPython(backfill_tags, migrations.RunPython.noop),
    ]
