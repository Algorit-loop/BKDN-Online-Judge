from django.db import models
from django.utils.translation import gettext_lazy as _

from judge.models.profile import Profile
from judge.models.submission import Submission

__all__ = ['AICodeReview']


class AICodeReview(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name='ai_reviews',
                                   verbose_name=_('submission'))
    user = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name='ai_code_reviews',
                             verbose_name=_('user'))
    provider = models.CharField(max_length=20, verbose_name=_('provider'))
    model = models.CharField(max_length=100, verbose_name=_('model'))
    review_text = models.TextField(verbose_name=_('review text'))
    output_language = models.CharField(max_length=20, default='vi', verbose_name=_('output language'))
    # Every valid tag the AI suggested in this review, even when it is not added to the skills progress
    # (UserProblemTag only keeps tags of AC submissions, and only from the latest review of each one)
    tags = models.JSONField(default=list, blank=True, verbose_name=_('suggested tags'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('created at'))

    class Meta:
        verbose_name = _('AI code review')
        verbose_name_plural = _('AI code reviews')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['submission', 'user']),
        ]

    def __str__(self):
        return f'Review #{self.id} for submission #{self.submission_id}'
