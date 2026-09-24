import json
import re
import urllib.error
import urllib.request

from django.utils.translation import gettext as _

from judge.models import ProblemType, Submission
from judge.models.ai_prompt import AIPromptTemplate
from judge.models.api_key import (
    AI_PROVIDER_CONFIGS, AI_PROVIDER_MODELS,
)
from judge.views.ai_problem_creator import _extract_text_from_response
from judge.views.api_key import _parse_http_error

_AI_TAG_SUGGEST_TIMEOUT = 100

_DEFAULT_TAG_SUGGEST_PROMPT = """You are an expert competitive programming problem classifier.

Given a problem statement and accepted solutions, determine which tags best describe the algorithms, data structures, and techniques required to solve this problem.

=== RULES ===
1. Select tags that are DIRECTLY relevant to solving the problem
2. Include both broad category tags (e.g., "dp", "graphs") and specific technique tags (e.g., "dijkstra", "bitmask_dp") when applicable
3. Typically choose 2-8 tags
4. ONLY choose from the provided tag list below
5. Return ONLY a valid JSON array of tag IDs (the "id" values), nothing else. No markdown fences, no explanation.

Example output: ["dp", "greedy", "binary_search"]

=== AVAILABLE TAGS ===
{available_tags}

=== PROBLEM STATEMENT ===
{problem_description}

{submissions_section}"""


def _get_tag_suggest_prompt(problem_description, submissions, available_tags):
    template = AIPromptTemplate.get_prompt('ai_tag_suggest', _DEFAULT_TAG_SUGGEST_PROMPT)

    if submissions:
        parts = ['=== ACCEPTED SOLUTIONS ===']
        for i, sub in enumerate(submissions, 1):
            parts.append(f"\n--- Solution {i} ({sub['language']}) ---\n{sub['source']}")
        submissions_section = '\n'.join(parts)
    else:
        submissions_section = ''

    tags_text = '\n'.join(f"- {t['name']}: {t['full_name']}" for t in available_tags)

    return template.format(
        problem_description=problem_description,
        submissions_section=submissions_section,
        available_tags=tags_text,
    )


def _build_tag_suggest_payload(provider, model, system_prompt, user_message):
    if provider == 'openai':
        return {'model': model, 'instructions': system_prompt, 'input': user_message}
    elif provider == 'gemini':
        return {
            'system_instruction': {'parts': [{'text': system_prompt}]},
            'contents': [{'parts': [{'text': user_message}]}],
        }
    elif provider == 'claude':
        return {
            'model': model,
            'max_tokens': 4096,
            'system': system_prompt,
            'messages': [{'role': 'user', 'content': user_message}],
        }
    elif provider == 'deepseek':
        return {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_message},
            ],
        }
    return None


def _call_tag_suggest_provider(provider, model, api_key, system_prompt, user_message):
    payload = _build_tag_suggest_payload(provider, model, system_prompt, user_message)
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
        with urllib.request.urlopen(req, timeout=_AI_TAG_SUGGEST_TIMEOUT) as resp:
            response_data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return False, _parse_http_error(e)
    except Exception as e:
        return False, _('Connection error: %s') % str(e)

    text = _extract_text_from_response(provider, response_data)
    if not text:
        return False, _('Empty response from AI provider')

    return True, text.strip()


def call_ai_suggest_tags(problem, provider, model, api_key):
    """Get AI tag suggestions for a problem. Returns (success, result_or_error)."""

    # Get top 3 AC submissions by shortest execution time
    ac_submissions = list(
        Submission.objects
        .filter(problem=problem, result='AC')
        .select_related('language')
        .order_by('time')[:3]
    )

    if len(ac_submissions) < 3:
        return False, _(
            'Not enough data to predict. Need at least 3 accepted submissions, '
            'but only found %(count)d.'
        ) % {'count': len(ac_submissions)}

    # Get submission source code
    submissions_data = []
    for sub in ac_submissions:
        try:
            source = sub.source.source
        except Exception:
            continue
        submissions_data.append({
            'language': sub.language.name if sub.language else 'Unknown',
            'source': source[:10000],
        })

    if len(submissions_data) < 3:
        return False, _('Not enough data to predict. Could not retrieve source code for accepted submissions.')

    # Get all available tags
    all_tags = list(ProblemType.objects.values('name', 'full_name').order_by('full_name'))

    # Build prompt
    description = problem.description or ''
    if not description.strip():
        return False, _('Problem has no description.')

    system_prompt = _get_tag_suggest_prompt(description, submissions_data, all_tags)
    user_message = 'Analyze this problem and its solutions. Return ONLY a JSON array of the most appropriate tag IDs.'

    success, result = _call_tag_suggest_provider(provider, model, api_key, system_prompt, user_message)
    if not success:
        return False, result

    # Parse JSON response - strip markdown fences if present
    text = result
    if text.startswith('```'):
        lines = text.split('\n')
        if lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines)

    try:
        tag_ids = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            try:
                tag_ids = json.loads(match.group())
            except json.JSONDecodeError:
                return False, _('AI returned invalid JSON: %s') % text[:200]
        else:
            return False, _('AI returned invalid response: %s') % text[:200]

    if not isinstance(tag_ids, list):
        return False, _('AI returned invalid format (expected array): %s') % text[:200]

    # Filter to only string tag IDs
    tag_ids = [t for t in tag_ids if isinstance(t, str)]
    if not tag_ids:
        return False, _('AI returned no valid tag IDs.')

    # Validate tag IDs against database
    valid_tags = ProblemType.objects.filter(name__in=tag_ids)
    valid_tag_map = {t.name: t.pk for t in valid_tags}

    result_tags = []
    for tag_id in tag_ids:
        if tag_id in valid_tag_map:
            result_tags.append({
                'pk': valid_tag_map[tag_id],
                'name': tag_id,
            })

    if not result_tags:
        return False, _('AI could not find any matching tags.')

    return True, result_tags
