from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import ExpressionWrapper, FloatField, Sum
from django.utils import timezone

from judge.models import Organization, Submission


class Command(BaseCommand):
    help = 'backfill current credit usage for all organizations'

    def backfill_current_credit(self, org: Organization, month_start):
        credit_problem = (
            # Same rule as Submission.get_credit_organization() used by the bridge:
            # an org-private problem is charged to its organization, even inside a contest...
            Submission.objects.filter(
                problem__is_organization_private=True,
                problem__organization=org,
                date__gte=month_start,
            )
            .annotate(
                credit=ExpressionWrapper(
                    Sum('test_cases__time'), output_field=FloatField(),
                ),
            )
            .aggregate(Sum('credit'))['credit__sum'] or 0
        )

        credit_contest = (
            # ...otherwise an org-private contest is charged to its organization.
            Submission.objects.filter(
                problem__is_organization_private=False,
                contest_object__is_organization_private=True,
                contest_object__organization=org,
                date__gte=month_start,
            )
            .annotate(
                credit=ExpressionWrapper(
                    Sum('test_cases__time'), output_field=FloatField(),
                ),
            )
            .aggregate(Sum('credit'))['credit__sum'] or 0
        )

        # Recompute from scratch instead of calling consume_credit(), which would add on top of the
        # usage already tracked by the bridge (double counting) and charge paid_credit a second time.
        # paid_credit is left untouched: it was already charged when the submissions were judged.
        consumed = credit_problem + credit_contest
        org.current_consumed_credit = consumed
        org.free_credit = max(0, settings.BKDNOJ_MONTHLY_FREE_CREDIT - consumed)
        org.save(update_fields=['free_credit', 'current_consumed_credit'])

    def handle(self, *args, **options):
        # get current month
        start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        print('Processing', start, 'at time', timezone.now())

        for org in Organization.objects.all():
            self.backfill_current_credit(org, start)
