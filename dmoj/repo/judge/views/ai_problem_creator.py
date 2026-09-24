import base64
import json
import urllib.error
import urllib.request

from django.conf import settings
from django.utils.translation import gettext as _

from judge.models.ai_prompt import AIPromptTemplate
from judge.models.api_key import AI_PROVIDER_CONFIGS, VISION_PROVIDERS

_AI_CREATE_TIMEOUT = 100

_DEFAULT_SYSTEM_PROMPT = """You are an expert at reading competitive programming problem statements from images.

Extract the problem content and return it as clean Markdown suitable for an Online Judge website.

The output MUST follow this exact structure:

<problem statement here — describe the problem clearly>

## **Input**

<describe input format, constraints inline>

## **Output**

<describe output format>

## **Scoring**

| Subtask | Score | Additional Constraints |
| ------- | ----- | ---------------------- |
| ~1~     | ~...~ | ~constraints~          |

(If there is no subtask/scoring info, omit this section entirely.)

## **Example**

### **Sample input 1**
```
<sample input>
```

### **Sample output 1**
```
<sample output>
```

(Include ALL sample test cases from the problem.)

### **Explanation**

<explanation of sample, if provided in the original problem>

Formatting rules:
- Use ~variable~ for inline math (NOT $variable$). Example: ~N~, ~1 \\leq N \\leq 10^6~, ~O(N \\log N)~.
- Use standard Markdown: **bold**, tables with |, code blocks with ```.
- Keep the original problem's meaning exactly. Do not add or remove information.
- Output language: {output_language}.
- Return ONLY the markdown content. No extra commentary, no wrapping in code fences."""


def get_system_prompt(output_language='English'):
    template = AIPromptTemplate.get_prompt('ai_problem_creator', _DEFAULT_SYSTEM_PROMPT)
    return template.replace('{output_language}', output_language)


def validate_file(uploaded_file):
    """Validate uploaded file. Returns (is_valid, error_message)."""
    if not uploaded_file:
        return False, _('No file uploaded')

    ext = uploaded_file.name.rsplit('.', 1)[-1].lower() if '.' in uploaded_file.name else ''
    allowed_exts = getattr(settings, 'AI_PROBLEM_CREATOR_ALLOWED_EXTS', {'png', 'jpg', 'jpeg', 'webp'})
    if ext not in allowed_exts:
        return False, _('Invalid file type. Allowed: %s') % ', '.join(sorted(allowed_exts))

    max_size = getattr(settings, 'AI_PROBLEM_CREATOR_MAX_FILE_SIZE', 10 * 1024 * 1024)
    if uploaded_file.size > max_size:
        return False, _('File too large. Maximum size: %d MB') % (max_size // (1024 * 1024))

    return True, ''


def _get_mime_type(filename):
    """Get MIME type from filename."""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    mime_map = {
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'webp': 'image/webp',
    }
    return mime_map.get(ext, 'application/octet-stream')


def _build_openai_payload(file_data_b64, mime_type, model, system_prompt):
    content = [
        {'type': 'input_text', 'text': system_prompt},
        {'type': 'input_image', 'image_url': f'data:{mime_type};base64,{file_data_b64}'},
    ]
    return {
        'model': model,
        'input': [{'role': 'user', 'content': content}],
    }


def _build_gemini_payload(file_data_b64, mime_type, model, system_prompt):
    return {
        'contents': [{
            'parts': [
                {'text': system_prompt},
                {'inline_data': {'mime_type': mime_type, 'data': file_data_b64}},
            ],
        }],
    }


def _build_claude_payload(file_data_b64, mime_type, model, system_prompt):
    file_block = {
        'type': 'image',
        'source': {'type': 'base64', 'media_type': mime_type, 'data': file_data_b64},
    }
    return {
        'model': model,
        'max_tokens': 16384,
        'messages': [{'role': 'user', 'content': [file_block, {'type': 'text', 'text': system_prompt}]}],
    }


_PAYLOAD_BUILDERS = {
    'openai': _build_openai_payload,
    'gemini': _build_gemini_payload,
    'claude': _build_claude_payload,
}


def _extract_text_from_response(provider, data):
    """Extract text content from provider-specific response format."""
    if provider == 'openai':
        for item in data.get('output', []):
            for content in item.get('content', []):
                if content.get('type') == 'output_text':
                    return content.get('text', '')
        return ''
    elif provider == 'gemini':
        candidates = data.get('candidates', [])
        if candidates:
            parts = candidates[0].get('content', {}).get('parts', [])
            if parts:
                return parts[0].get('text', '')
        return ''
    elif provider == 'claude':
        content = data.get('content', [])
        if content:
            return content[0].get('text', '')
        return ''
    elif provider == 'deepseek':
        choices = data.get('choices', [])
        if choices:
            return choices[0].get('message', {}).get('content', '')
        return ''
    return ''


def call_ai_provider(provider, model, api_key, uploaded_file, output_language='English'):
    """
    Call AI provider with uploaded file and return markdown description.

    Returns (success, markdown_or_error):
        - (True, str) markdown on success
        - (False, str) error message on failure
    """
    if provider not in VISION_PROVIDERS:
        return False, _('Provider "%s" does not support image input') % provider

    builder = _PAYLOAD_BUILDERS.get(provider)
    if not builder:
        return False, _('Unsupported provider')

    file_content = uploaded_file.read()
    file_data_b64 = base64.b64encode(file_content).decode('ascii')
    mime_type = _get_mime_type(uploaded_file.name)

    system_prompt = get_system_prompt(output_language)
    payload = builder(file_data_b64, mime_type, model, system_prompt)

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
        with urllib.request.urlopen(req, timeout=_AI_CREATE_TIMEOUT) as resp:
            response_data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            error_body = json.loads(e.read())
            error_msg = error_body.get('error', {}).get('message', str(e))
        except Exception:
            error_msg = str(e)
        return False, _('API error (%d): %s') % (e.code, error_msg)
    except Exception as e:
        return False, _('Connection error: %s') % str(e)

    text = _extract_text_from_response(provider, response_data)
    if not text:
        return False, _('Empty response from AI provider')

    # Strip markdown code fences if AI wraps output
    text = text.strip()
    if text.startswith('```'):
        lines = text.split('\n')
        if lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines)

    return True, text
