import json
import urllib.error
import urllib.request

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.models import Problem
from judge.models.ai_gen_code import AIGenCode
from judge.models.ai_prompt import AIPromptTemplate
from judge.models.api_key import (
    AIAPIKey,
    AI_PROVIDER_CONFIGS, AI_PROVIDER_MODELS,
)
from judge.views.ai_problem_creator import _extract_text_from_response
from judge.views.api_key import _parse_http_error

_AI_GEN_CODE_TIMEOUT = 100

_DEFAULT_GEN_CODE_PROMPT = """You are an expert competitive programming testcase generator writer.

You will be given a problem description. Write a C++ program that generates ONE test input.

=== HOW THE GENERATOR IS USED ===
The system will run your program N times (once per testcase).
Each time, the program receives a SINGLE INTEGER T via stdin (1-based testcase index: 1, 2, 3, ..., N).
Your program must output EXACTLY ONE valid test input to stdout, then exit.

=== SUBTASK-BASED GENERATION ===
The total number of testcases is N (you do NOT know N, but assume N ~ {num_cases}).
If the problem defines subtasks with score percentages, you MUST distribute testcases proportionally by score.

Example: 4 subtasks with scores 10%, 30%, 30%, 30% and N=20:
  - Subtask 1 (10%) -> 2 testcases: T=1..2
  - Subtask 2 (30%) -> 6 testcases: T=3..8
  - Subtask 3 (30%) -> 6 testcases: T=9..14
  - Subtask 4 (30%) -> 6 testcases: T=15..20

Implementation pattern — compute subtask boundaries from percentages:
  const int N_TOTAL = {num_cases};
  // scores[] = percentage of each subtask from problem description
  // Compute prefix-sum boundaries, then: if (T <= boundary[0]) subtask 1; else if (T <= boundary[1]) subtask 2; ...

Rules:
- For each subtask, generate inputs at or near the MAXIMUM allowed constraints of that subtask.
- If a subtask has special properties (e.g., "all elements are equal", "tree is a chain"), the generated input MUST satisfy those properties.
- Each testcase within a subtask should be different (use T as random seed).
- The last (hardest) subtask range extends to cover any remaining T values beyond N_TOTAL.

If the problem has NO subtasks, generate all testcases at the maximum overall constraints with random variation.

=== CRITICAL RULES ===
1. Read exactly one integer T from stdin. This is the testcase index, NOT the number of testcases.
2. Use T as the random seed so each testcase is different but reproducible.
3. Output EXACTLY ONE test input following the problem's Input format. Do NOT output multiple testcases.
4. Do NOT output any extra text, labels, comments, or blank lines beyond what the Input format requires.
5. ALL generated values MUST satisfy EVERY constraint for the target subtask (value ranges, array sizes, graph properties, etc.).
6. Push constraints to the MAXIMUM allowed values for each subtask. These are for judging, not samples.

=== VALIDATION ===
Before generating code, verify the problem description is a COMPLETE competitive programming problem with:
- A clear problem statement
- Input format specification
- Output format specification
- Constraints (value ranges, sizes, etc.)
If ANY of these are missing or the description is not a valid problem, respond with ONLY:
  ERROR: <reason why the description is invalid>
Do NOT generate code for invalid or incomplete problems. Do NOT invent constraints that are not stated.

=== CODE REQUIREMENTS ===
- C++17. Use #include <bits/stdc++.h>.
- Use mt19937 or mt19937_64, seeded with T.
- Helper: to generate random int in [lo, hi], use uniform_int_distribution<long long>(lo, hi)(rng).
- Return ONLY the raw C++ source code. No markdown, no code fences, no explanation.

=== PROBLEM DESCRIPTION ===
{problem_description}"""


def _get_gen_code_prompt(problem_description, num_cases=20):
    template = AIPromptTemplate.get_prompt('ai_gen_code', _DEFAULT_GEN_CODE_PROMPT)
    return template.format(problem_description=problem_description, num_cases=num_cases)


def _build_gen_code_payload(provider, model, system_prompt, user_message):
    """Build provider-specific payload for text-only code generation."""
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
            'max_tokens': 16384,
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


def _call_gen_code_provider(provider, model, api_key, system_prompt, user_message):
    """Call AI provider for code generation. Returns (success, text_or_error)."""
    payload = _build_gen_code_payload(provider, model, system_prompt, user_message)
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
        with urllib.request.urlopen(req, timeout=_AI_GEN_CODE_TIMEOUT) as resp:
            response_data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return False, _parse_http_error(e)
    except Exception as e:
        return False, _('Connection error: %s') % str(e)

    text = _extract_text_from_response(provider, response_data)
    if not text:
        return False, _('Empty response from AI provider')

    # Check if AI refused due to invalid problem description
    text = text.strip()
    if text.upper().startswith('ERROR:'):
        return False, text[6:].strip()

    # Strip markdown code fences if AI wraps output
    if text.startswith('```'):
        lines = text.split('\n')
        if lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines)

    return True, text.strip()


@login_required
def ai_gen_code_view(request, problem):
    """POST: Call AI to generate C++ generator code based on problem description."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    problem_obj = get_object_or_404(Problem, code=problem)
    if not (problem_obj.is_editable_by(request.user)
            and request.user.has_perm('judge.generate_testcase_ai')):
        return JsonResponse({'error': _('Permission denied')}, status=403)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': _('Invalid request data')}, status=400)

    provider = body.get('provider', '').strip()
    model = body.get('model', '').strip()
    num_cases = body.get('num_cases', 20)
    if not isinstance(num_cases, int) or num_cases < 1 or num_cases > 50:
        num_cases = 20

    # Validate provider
    if provider not in AI_PROVIDER_MODELS:
        return JsonResponse({'error': _('Invalid provider')}, status=400)

    # Validate model
    if model not in AI_PROVIDER_MODELS.get(provider, []):
        return JsonResponse({'error': _('Invalid model for this provider')}, status=400)

    # Check problem has a meaningful description
    description = problem_obj.description
    if not description or not description.strip():
        return JsonResponse({'error': _('Problem has no description')}, status=400)
    desc_lower = description.lower()
    if len(description.strip()) < 100 or not any(kw in desc_lower for kw in ['input', 'output', 'đầu vào', 'đầu ra', 'nhập', 'xuất']):
        return JsonResponse({
            'error': _('Problem description is too short or missing Input/Output format. '
                       'Please write a complete problem statement first.'),
        }, status=400)

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

    # Build prompt
    system_prompt = _get_gen_code_prompt(description, num_cases)
    user_message = 'Generate the C++ generator program for this problem. Return ONLY the source code.'

    try:
        success, result = _call_gen_code_provider(provider, model, plaintext_key, system_prompt, user_message)
    finally:
        plaintext_key = None  # noqa: F841

    if not success:
        return JsonResponse({'error': result}, status=400)

    # Update last_used_at
    api_key_obj.last_used_at = timezone.now()
    api_key_obj.save(update_fields=['last_used_at'])

    # Save to database
    AIGenCode.objects.create(
        problem=problem_obj,
        user=request.profile,
        provider=provider,
        model=model,
        num_cases=num_cases,
        generated_code=result,
    )

    return JsonResponse({
        'success': True,
        'code': result,
    })
