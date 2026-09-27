import json

from django.conf import settings
from django.http import JsonResponse
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin

from judge.judgeapi import judge_run_submission
from judge.models import Language, Problem, Submission
from judge.models.problem_data import ProblemData, ProblemTestCase, problem_data_storage
from judge.models.run_submission import RunSubmission


class RunSubmitView(LoginRequiredMixin, View):
    """Run submission: validate like submit, create RunSubmission, dispatch to judge with sample-testcase-only."""

    def post(self, request, problem):
        try:
            prob = Problem.objects.get(code=problem)
        except Problem.DoesNotExist:
            return JsonResponse({'error': 'Problem not found'}, status=404)

        if not prob.enable_new_ide:
            return JsonResponse({'error': 'Feature not enabled for this problem'}, status=403)

        if not prob.is_accessible_by(request.user):
            return JsonResponse({'error': 'Access denied'}, status=403)

        # Check user banned from problem (same as submit)
        if not request.user.is_superuser and prob.banned_users.filter(id=request.profile.id).exists():
            return JsonResponse({'error': 'You are banned from submitting to this problem.'}, status=403)

        # Rate limit: count both real submissions and run submissions (same as submit)
        if not request.user.has_perm('judge.spam_submission'):
            pending_real = Submission.objects.filter(
                user=request.profile, rejudged_date__isnull=True,
            ).exclude(status__in=['D', 'IE', 'CE', 'AB']).count()
            pending_run = RunSubmission.objects.filter(
                user=request.profile,
            ).exclude(status__in=['D', 'IE', 'CE', 'AB']).count()
            if pending_real + pending_run >= settings.DMOJ_SUBMISSION_LIMIT:
                return JsonResponse({'error': 'Too many submissions'}, status=429)

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        source = data.get('source', '')
        language_id = data.get('language')

        if not source or not language_id:
            return JsonResponse({'error': 'source and language are required'}, status=400)

        # Same limit as a normal submission, so code that runs can also be submitted
        max_source_length = settings.ALOJ_MAX_SOURCE_LENGTH
        if len(source) > max_source_length:
            return JsonResponse({'error': 'Source code too long (max %d characters)' % max_source_length}, status=400)

        try:
            language = Language.objects.get(id=language_id)
        except Language.DoesNotExist:
            return JsonResponse({'error': 'Invalid language'}, status=400)

        # Check language allowed (same as submit)
        if not prob.allowed_languages.filter(id=language.id).exists():
            return JsonResponse({'error': 'Language not allowed for this problem'}, status=400)

        # Check sample testcases exist
        sample_cases = list(ProblemTestCase.objects.filter(
            dataset=prob, is_sample=True, type='C',
        ).order_by('order').values_list('input_file', flat=True))

        if not sample_cases:
            return JsonResponse({'error': 'No sample testcases configured for this problem.'}, status=400)

        # Custom inputs (optional)
        custom_inputs = data.get('custom_inputs', [])
        if not isinstance(custom_inputs, list):
            return JsonResponse({'error': 'custom_inputs must be a list'}, status=400)
        max_custom_testcases = getattr(settings, 'DMOJ_IDE_MAX_CUSTOM_TESTCASES', 5)
        if len(custom_inputs) > max_custom_testcases:
            return JsonResponse({'error': 'Maximum %d custom inputs allowed' % max_custom_testcases}, status=400)
        # Validate each custom input
        max_input_length = getattr(settings, 'DMOJ_IDE_MAX_CUSTOM_INPUT_LENGTH', 65536)
        validated_inputs = []
        for ci in custom_inputs:
            if not isinstance(ci, str):
                return JsonResponse({'error': 'Each custom input must be a string'}, status=400)
            if len(ci) > max_input_length:
                return JsonResponse({'error': 'Custom input too long (max %d characters)' % max_input_length}, status=400)
            validated_inputs.append(ci)

        # Create RunSubmission
        run_sub = RunSubmission(
            user=request.profile,
            problem=prob,
            language=language,
            source=source,
            status='QU',
        )
        run_sub.save()

        # Dispatch to judge via bridge
        success = judge_run_submission(run_sub, sample_input_files=sample_cases, custom_inputs=validated_inputs)
        if not success:
            run_sub.delete()
            return JsonResponse({'error': 'Failed to dispatch to judge'}, status=503)

        return JsonResponse({'run_id': run_sub.id, 'id_secret': run_sub.id_secret})


class RunPollView(LoginRequiredMixin, View):
    """Poll for run results by RunSubmission ID."""

    def get(self, request, run_id):
        try:
            run_sub = RunSubmission.objects.get(id=run_id, user=request.profile)
        except RunSubmission.DoesNotExist:
            return JsonResponse({'status': 'NOT_FOUND'}, status=404)

        # Still grading
        if run_sub.status in RunSubmission.IN_PROGRESS_GRADING_STATUS:
            return JsonResponse({
                'status': 'PENDING',
                'grading_status': run_sub.status,
            })

        # Done - build result
        result = {
            'status': 'done' if run_sub.status == 'D' else run_sub.status,
        }

        if run_sub.status == 'D':
            result['result'] = run_sub.result
            result['time'] = run_sub.time
            result['memory'] = run_sub.memory

            testcases = []
            for tc in run_sub.case_results:
                testcases.append({
                    'case': tc.get('case'),
                    'status': tc.get('status'),
                    'time': tc.get('time'),
                    'memory': tc.get('memory'),
                    'output': tc.get('output', ''),
                })
            result['passed'] = int(run_sub.case_points)
            result['total_cases'] = int(run_sub.case_total)
            result['testcases'] = testcases
        elif run_sub.status in ('CE', 'IE'):
            result['error'] = run_sub.error or ''

        return JsonResponse(result)


class SampleTestCaseView(View):
    """Return sample test cases for a problem."""

    def get(self, request, problem):
        try:
            prob = Problem.objects.get(code=problem)
        except Problem.DoesNotExist:
            return JsonResponse({'error': 'Problem not found'}, status=404)

        if not prob.is_accessible_by(request.user):
            return JsonResponse({'error': 'Access denied'}, status=403)

        samples = []
        sample_cases = ProblemTestCase.objects.filter(
            dataset=prob, is_sample=True, type='C',
        ).order_by('order')

        archive = None
        try:
            pd = ProblemData.objects.get(problem=prob)
            if pd.zipfile:
                from zipfile import BadZipFile, ZipFile
                try:
                    archive = ZipFile(pd.zipfile.path)
                except (BadZipFile, FileNotFoundError):
                    archive = None
        except ProblemData.DoesNotExist:
            pass

        for case in sample_cases:
            input_data = ''
            output_data = ''
            try:
                if case.input_file:
                    if archive and case.input_file in archive.namelist():
                        input_data = archive.read(case.input_file).decode('utf-8', errors='replace')
                    else:
                        try:
                            with problem_data_storage.open('%s/%s' % (prob.code, case.input_file)) as f:
                                input_data = f.read().decode('utf-8', errors='replace')
                        except Exception:
                            pass
                if case.output_file:
                    if archive and case.output_file in archive.namelist():
                        output_data = archive.read(case.output_file).decode('utf-8', errors='replace')
                    else:
                        try:
                            with problem_data_storage.open('%s/%s' % (prob.code, case.output_file)) as f:
                                output_data = f.read().decode('utf-8', errors='replace')
                        except Exception:
                            pass
            except Exception:
                continue

            samples.append({
                'input': input_data,
                'output': output_data,
            })

        if archive:
            archive.close()

        return JsonResponse({'samples': samples})
