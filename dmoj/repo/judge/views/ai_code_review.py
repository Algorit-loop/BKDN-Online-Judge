import json
import re
import urllib.error
import urllib.request

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.models.ai_code_review import AICodeReview
from judge.models.ai_prompt import AIPromptTemplate
from judge.models.api_key import (
    AIAPIKey,
    AI_PROVIDER_CONFIGS, AI_PROVIDER_MODELS,
)
from judge.models.problem import ProblemType
from judge.models.submission import Submission
from judge.models.user_problem_tag import UserProblemTag
from judge.views.ai_problem_creator import _extract_text_from_response
from judge.views.api_key import _parse_http_error

_AI_REVIEW_TIMEOUT = 100

_DEFAULT_REVIEW_PROMPT = """You are an expert competitive programming analyst.
Analyze the following code submission.

Problem: "{problem_name}"
Language: {language_name}
Submission result: {result} ({points}/{total_points} points)

Provide analysis covering:
1. Algorithm & Data Structures - identify the main algorithm used
2. Execution Flow - step-by-step explanation of how the code works
3. Time Complexity - with justification
4. Space Complexity - with justification
5. Code Quality - readability, edge cases, potential improvements

Output in plain text format. Do NOT use markdown formatting, HTML, or code blocks.
Use simple text formatting: headers with "===", lists with "- ", indentation for structure.

Output language: {output_language}"""


def _get_review_prompt(submission, output_language='Vietnamese'):
    result_display = submission.get_result_display() or submission.get_status_display()
    template = AIPromptTemplate.get_prompt('ai_code_review', _DEFAULT_REVIEW_PROMPT)
    prompt = template.format(
        problem_name=submission.problem.name,
        language_name=submission.language.name,
        result=result_display,
        points=submission.points or 0,
        total_points=submission.problem.points or 0,
        output_language=output_language,
    )

    # Always append tags instruction AFTER template formatting
    # so it works regardless of DB template content
    all_tags = list(ProblemType.objects.values_list('name', flat=True).order_by('name'))
    if all_tags:
        prompt += '\n\n'
        prompt += 'MANDATORY - You MUST end your response with this exact format on the LAST LINE:\n'
        prompt += 'TAGS: tag1, tag2, tag3\n'
        prompt += 'Choose 2-8 tag IDs from this list: ' + ', '.join(all_tags) + '\n'
        prompt += 'This TAGS line is REQUIRED. Do NOT omit it.'

    return prompt


def _parse_tags_from_review(text):
    """Extract TAGS line from the end of AI review text.

    Returns (review_text_without_tags, list_of_tag_names).
    """
    lines = text.strip().split('\n')

    # Search the last 3 lines for a TAGS: line
    for i in range(len(lines) - 1, max(len(lines) - 4, -1), -1):
        line = lines[i].strip()
        match = re.match(r'^TAGS:\s*(.+)$', line, re.IGNORECASE)
        if match:
            tag_str = match.group(1)
            tag_names = [t.strip() for t in tag_str.split(',') if t.strip()]
            review_text = '\n'.join(lines[:i]).rstrip()
            return review_text, tag_names

    return text, []


def _save_user_problem_tags(user, problem, submission, tag_names):
    """Save validated tags to UserProblemTag.

    Replaces all tags for this user+submission, then inserts new ones.
    Returns list of saved tag names.
    """
    # Delete old tags from previous reviews of this submission
    UserProblemTag.objects.filter(user=user, submission=submission).delete()

    if not tag_names:
        return []

    valid_tags = ProblemType.objects.filter(name__in=tag_names)
    saved = []
    for tag in valid_tags:
        UserProblemTag.objects.create(
            user=user,
            problem=problem,
            tag=tag,
            submission=submission,
        )
        saved.append(tag.name)
    return saved


def _build_review_payload(provider, model, system_prompt, source_code):
    """Build provider-specific payload for text-only code review."""
    if provider == 'openai':
        return {'model': model, 'instructions': system_prompt, 'input': source_code}
    elif provider == 'gemini':
        return {
            'system_instruction': {'parts': [{'text': system_prompt}]},
            'contents': [{'parts': [{'text': source_code}]}],
        }
    elif provider == 'claude':
        return {
            'model': model,
            'max_tokens': 16384,
            'system': system_prompt,
            'messages': [{'role': 'user', 'content': source_code}],
        }
    elif provider == 'deepseek':
        return {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': source_code},
            ],
        }
    return None


def _call_review_provider(provider, model, api_key, system_prompt, source_code):
    """Call AI provider for code review. Returns (success, text_or_error)."""
    payload = _build_review_payload(provider, model, system_prompt, source_code)
    if payload is None:
        return False, _('Unsupported provider')

    config = AI_PROVIDER_CONFIGS[provider]
    url = config['base_url'] + config['endpoint']
    if '{model}' in url:
        url = url.replace('{model}', model)

    headers = {
        'Content-Type': 'application/json',
        config['auth_header']: config['auth_format'].format(key=api_key),
    }
    headers.update(config.get('extra_headers', {}))

    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=_AI_REVIEW_TIMEOUT) as resp:
            response_data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return False, _parse_http_error(e)
    except Exception as e:
        return False, _('Connection error: %s') % str(e)

    text = _extract_text_from_response(provider, response_data)
    if not text:
        return False, _('Empty response from AI provider')

    return True, text.strip()


def _get_submission_for_review(submission_id, user):
    """Fetch submission and verify ownership. Returns (submission, error_response)."""
    try:
        submission = Submission.objects.select_related(
            'source', 'user', 'problem', 'language',
        ).get(id=submission_id)
    except Submission.DoesNotExist:
        return None, JsonResponse({'error': _('Submission not found')}, status=404)

    if submission.user_id != user.id:
        return None, JsonResponse({'error': _('You can only review your own submissions')}, status=403)

    return submission, None


@login_required
def ai_code_review_dispatch(request, submission):
    """Dispatch GET/POST for AI code review endpoint."""
    if request.method == 'POST':
        return _ai_code_review_post(request, submission)
    elif request.method == 'GET':
        return _ai_code_review_get(request, submission)
    return JsonResponse({'error': 'Method not allowed'}, status=405)


def _ai_code_review_post(request, submission):
    """POST: Call AI provider to review code, save result to DB."""
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': _('Invalid request data')}, status=400)

    provider = body.get('provider', '').strip()
    model = body.get('model', '').strip()
    output_language = body.get('output_language', 'Vietnamese').strip()

    # Validate provider
    valid_providers = {p for p, _ in AI_PROVIDER_MODELS.items()}
    if provider not in valid_providers:
        return JsonResponse({'error': _('Invalid provider')}, status=400)

    # Validate model
    valid_models = AI_PROVIDER_MODELS.get(provider, [])
    if model not in valid_models:
        return JsonResponse({'error': _('Invalid model for this provider')}, status=400)

    # Fetch submission + ownership check
    sub, err = _get_submission_for_review(submission, request.profile)
    if err:
        return err

    # Check file-only language
    if sub.language.file_only:
        return JsonResponse({'error': _('Cannot review file-only submissions')}, status=400)

    # Check source code exists
    try:
        source = sub.source.source
    except Exception:
        source = ''
    if not source or not source.strip():
        return JsonResponse({'error': _('Source code is empty')}, status=400)

    # Get API key
    try:
        api_key_obj = AIAPIKey.objects.get(
            user=request.profile, provider=provider, status='verified',
        )
    except AIAPIKey.DoesNotExist:
        return JsonResponse({
            'error': _('No verified API key for %s. Add one in Settings > AI API Keys.') % provider,
        }, status=400)

    plaintext_key = api_key_obj.decrypt_key()
    if not plaintext_key:
        return JsonResponse({'error': _('Failed to decrypt API key')}, status=500)

    # Build prompt and call AI
    system_prompt = _get_review_prompt(sub, output_language)

    try:
        success, result = _call_review_provider(provider, model, plaintext_key, system_prompt, source)
    finally:
        plaintext_key = None  # noqa: F841

    if not success:
        return JsonResponse({'error': result}, status=400)

    # Update last_used_at
    api_key_obj.last_used_at = timezone.now()
    api_key_obj.save(update_fields=['last_used_at'])

    # Parse tags from AI response
    review_text, tag_names = _parse_tags_from_review(result)

    # Save tags to UserProblemTag (per-problem deduplication)
    saved_tags = _save_user_problem_tags(request.profile, sub.problem, sub, tag_names)

    # Save review to DB (without TAGS line)
    review = AICodeReview.objects.create(
        submission=sub,
        user=request.profile,
        provider=provider,
        model=model,
        review_text=review_text,
        output_language=output_language,
    )

    return JsonResponse({
        'success': True,
        'review_text': review.review_text,
        'provider': review.provider,
        'model': review.model,
        'output_language': review.output_language,
        'created_at': review.created_at.isoformat(),
        'tags': saved_tags,
    })


def _ai_code_review_get(request, submission):
    """GET: Return cached AI review from DB if exists."""
    sub, err = _get_submission_for_review(submission, request.profile)
    if err:
        return err

    review = (AICodeReview.objects
              .filter(submission=sub, user=request.profile)
              .order_by('-created_at')
              .first())

    if review:
        # Also return tags for this problem
        tags = list(
            UserProblemTag.objects
            .filter(user=request.profile, problem=sub.problem)
            .values_list('tag__name', flat=True)
        )
        return JsonResponse({
            'exists': True,
            'review_text': review.review_text,
            'provider': review.provider,
            'model': review.model,
            'output_language': review.output_language,
            'created_at': review.created_at.isoformat(),
            'tags': tags,
        })

    return JsonResponse({'exists': False})
