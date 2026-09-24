from datetime import datetime, timedelta

import pytz
from celery import shared_task
from django.conf import settings
from django.db import transaction

from judge.models import Organization, OrganizationMonthlyUsage


@shared_task
def organization_monthly_reset():
    # Get first day of last month
    current_time = datetime.now(pytz.utc)
    month_start = (current_time.replace(day=1, hour=0, minute=0, second=0, microsecond=0) -
                   timedelta(days=1)).replace(day=1)

    org_ids = Organization.objects.filter(current_consumed_credit__gt=0).values_list('id', flat=True)

    for org_id in org_ids:
        # Lock the row so a charge from the bridge can't land between reading and resetting the usage
        with transaction.atomic():
            org = Organization.objects.select_for_update().get(id=org_id)
            usage = OrganizationMonthlyUsage(
                organization=org,
                time=month_start,
                consumed_credit=org.current_consumed_credit,
            )
            usage.save()
            org.free_credit = settings.BKDNOJ_MONTHLY_FREE_CREDIT
            org.current_consumed_credit = 0
            org.save(update_fields=['free_credit', 'current_consumed_credit'])

    print('Reset monthly credit for all organizations')
