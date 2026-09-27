import re

from django.db import models
from django.utils.translation import gettext_lazy as _


class AIPromptTemplate(models.Model):
    key = models.CharField(max_length=50, unique=True, verbose_name=_('key'),
                           help_text=_('Unique identifier: ai_problem_creator, ai_code_review, api_key_test'))
    name = models.CharField(max_length=100, verbose_name=_('name'),
                            help_text=_('Human-readable name for this prompt'))
    prompt_text = models.TextField(verbose_name=_('prompt text'),
                                   help_text=_('The prompt template. Use {variable} for substitution.'))
    description = models.TextField(blank=True, verbose_name=_('description'),
                                    help_text=_('Describe what this prompt does and what variables are available.'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('updated at'))

    class Meta:
        verbose_name = _('AI prompt template')
        verbose_name_plural = _('AI prompt templates')
        ordering = ['key']

    def __str__(self):
        return f'{self.name} ({self.key})'

    @classmethod
    def get_prompt(cls, key, default=''):
        """Get prompt text by key. Returns default if not found in DB."""
        try:
            return cls.objects.get(key=key).prompt_text
        except cls.DoesNotExist:
            return default

    @staticmethod
    def fill(template, **values):
        """Substitute {name} placeholders in an admin-editable prompt. Unlike str.format(), other braces (JSON or
        code examples in the prompt) are left as they are, and substituted values are never scanned again."""
        return re.sub(r'\{(\w+)\}', lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
                      template)
