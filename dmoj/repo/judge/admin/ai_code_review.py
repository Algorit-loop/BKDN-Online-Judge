from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from judge.models.ai_code_review import AICodeReview
from judge.models.user_problem_tag import UserProblemTag


class AICodeReviewAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'submission_link', 'provider', 'model', 'output_language', 'created_at')
    list_filter = ('provider', 'output_language', 'created_at')
    search_fields = ('user__user__username', 'provider', 'model')
    fields = ('user', 'submission', 'provider', 'model', 'output_language', 'created_at', 'suggested_tags',
              'progress_tags', 'review_text')
    readonly_fields = fields
    date_hierarchy = 'created_at'

    def submission_link(self, obj):
        return f'#{obj.submission_id}'
    submission_link.short_description = _('Submission')

    def suggested_tags(self, obj):
        return ', '.join(obj.tags) or '—'
    suggested_tags.short_description = _('Suggested tags')

    def progress_tags(self, obj):
        # What this submission currently adds to the user's skills progress (AC submissions only)
        names = UserProblemTag.objects.filter(user=obj.user_id, submission=obj.submission_id) \
            .values_list('tag__name', flat=True)
        return ', '.join(names) or '—'
    progress_tags.short_description = _('Tags in skills progress')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
