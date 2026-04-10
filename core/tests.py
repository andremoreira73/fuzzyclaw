import hashlib
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from .models import AgentImage, AgentRun, Briefing, Run
from .registry import (
    AgentNotFound,
    SkillNotFound,
    clear_cache,
    get_agent,
    get_available_agents,
    get_available_skills,
    get_skill,
    parse_agent_md,
    parse_skill_md,
    validate_agent,
    validate_volumes,
)


class RegistryTests(TestCase):
    """Tests for the filesystem-based agent & skill registry."""

    def setUp(self):
        clear_cache()

    def _write_agent(self, tmpdir, filename, content):
        filepath = Path(tmpdir) / filename
        filepath.write_text(content)
        return filepath

    def _write_skill(self, tmpdir, skill_name, content):
        skill_dir = Path(tmpdir) / skill_name
        skill_dir.mkdir(exist_ok=True)
        filepath = skill_dir / 'SKILL.md'
        filepath.write_text(content)
        return filepath

    def test_parse_agent_md(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._write_agent(tmpdir, 'test-agent.md', (
                '---\n'
                'name: test-agent\n'
                'description: A test agent\n'
                'model: gpt-5-mini\n'
                'tools: ["bash"]\n'
                '---\n\n'
                'You are a test agent.\n'
            ))
            data = parse_agent_md(filepath)
            self.assertEqual(data['name'], 'test-agent')
            self.assertEqual(data['description'], 'A test agent')
            self.assertEqual(data['model_choice'], 'gpt-5-mini')
            self.assertEqual(data['tools'], ['bash'])
            self.assertIn('test agent', data['prompt'].lower())

    def test_parse_agent_md_no_frontmatter_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._write_agent(tmpdir, 'bad.md', 'No frontmatter here')
            with self.assertRaises(ValueError):
                parse_agent_md(filepath)

    def test_parse_agent_md_no_name_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._write_agent(tmpdir, 'noname.md', (
                '---\n'
                'description: Missing name\n'
                '---\n\n'
                'Body\n'
            ))
            with self.assertRaises(ValueError):
                parse_agent_md(filepath)

    def test_parse_agent_md_multiline_volumes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._write_agent(tmpdir, 'vol-agent.md', (
                '---\n'
                'name: vol-agent\n'
                'model: gpt-5-mini\n'
                'tools: ["bash"]\n'
                'memory: true\n'
                'volumes:\n'
                '  - host: "./data/output"\n'
                '    mount: "/data/output"\n'
                '    mode: "rw"\n'
                '  - host: "./data/input"\n'
                '    mount: "/data/input"\n'
                '    mode: "ro"\n'
                '---\n\n'
                'Agent with volumes.\n'
            ))
            data = parse_agent_md(filepath)
            self.assertEqual(data['name'], 'vol-agent')
            self.assertEqual(data['tools'], ['bash'])
            self.assertTrue(data['memory'])
            self.assertEqual(len(data['volumes']), 2)
            self.assertEqual(data['volumes'][0], {
                'host': './data/output', 'mount': '/data/output', 'mode': 'rw',
            })
            self.assertEqual(data['volumes'][1], {
                'host': './data/input', 'mount': '/data/input', 'mode': 'ro',
            })

    def test_parse_agent_md_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._write_agent(tmpdir, 'minimal.md', (
                '---\n'
                'name: minimal\n'
                '---\n\n'
                'Minimal agent.\n'
            ))
            data = parse_agent_md(filepath)
            self.assertEqual(data['model_choice'], 'gpt-5-mini')
            self.assertEqual(data['tools'], [])
            self.assertNotIn('skills', data)

    def test_validate_agent_valid(self):
        data = {
            'name': 'test',
            'description': 'test',
            'model_choice': 'gpt-5-mini',
            'tools': ['bash'],
            'prompt': 'You are a test agent.',
        }
        errors = validate_agent(data)
        self.assertEqual(errors, [])

    def test_validate_agent_bad_model(self):
        data = {
            'name': 'test',
            'description': 'test',
            'model_choice': 'nonexistent-model',
            'tools': [],
            'prompt': 'test',
        }
        errors = validate_agent(data)
        self.assertEqual(len(errors), 1)
        self.assertIn('Unknown model', errors[0])

    def test_validate_agent_bad_tools(self):
        data = {
            'name': 'test',
            'description': 'test',
            'model_choice': 'gpt-5-mini',
            'tools': ['nonexistent_tool'],
            'prompt': 'test',
        }
        errors = validate_agent(data)
        self.assertEqual(len(errors), 1)
        self.assertIn('Unknown tool', errors[0])

    def test_validate_agent_no_prompt(self):
        data = {
            'name': 'test',
            'description': 'test',
            'model_choice': 'gpt-5-mini',
            'tools': [],
            'prompt': '',
        }
        errors = validate_agent(data)
        self.assertEqual(len(errors), 1)
        self.assertIn('no prompt', errors[0])

    def test_get_available_agents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_agent(tmpdir, 'agent-a.md', (
                '---\nname: agent-a\ndescription: Agent A\nmodel: gpt-5-mini\n---\n\nPrompt A.\n'
            ))
            self._write_agent(tmpdir, 'agent-b.md', (
                '---\nname: agent-b\ndescription: Agent B\nmodel: gpt-5-mini\n---\n\nPrompt B.\n'
            ))
            agents = get_available_agents(Path(tmpdir))
            self.assertEqual(len(agents), 2)
            names = [a['name'] for a in agents]
            self.assertIn('agent-a', names)
            self.assertIn('agent-b', names)

    def test_get_available_agents_skips_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_agent(tmpdir, 'good.md', (
                '---\nname: good\ndescription: Good\nmodel: gpt-5-mini\n---\n\nPrompt.\n'
            ))
            self._write_agent(tmpdir, 'bad.md', (
                '---\nname: bad\nmodel: nonexistent\n---\n\nPrompt.\n'
            ))
            agents = get_available_agents(Path(tmpdir))
            self.assertEqual(len(agents), 1)
            self.assertEqual(agents[0]['name'], 'good')

    def test_get_available_agents_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agents = get_available_agents(Path(tmpdir))
            self.assertEqual(agents, [])

    def test_get_available_agents_nonexistent_dir(self):
        agents = get_available_agents(Path('/tmp/nonexistent-fuzzyclaw-dir'))
        self.assertEqual(agents, [])

    def test_get_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_agent(tmpdir, 'found.md', (
                '---\nname: found\ndescription: Found\nmodel: gpt-5-mini\n---\n\nPrompt.\n'
            ))
            agent = get_agent('found', Path(tmpdir))
            self.assertEqual(agent['name'], 'found')

    def test_get_agent_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(AgentNotFound):
                get_agent('missing', Path(tmpdir))

    def test_parse_skill_md(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._write_skill(tmpdir, 'web-scraping', (
                '---\n'
                'name: web-scraping\n'
                'description: Scrape web pages\n'
                '---\n\n'
                'Skill docs here.\n'
            ))
            data = parse_skill_md(filepath)
            self.assertEqual(data['name'], 'web-scraping')
            self.assertEqual(data['description'], 'Scrape web pages')

    def test_parse_skill_md_fallback_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._write_skill(tmpdir, 'my-skill', (
                '---\n'
                'description: No explicit name\n'
                '---\n\n'
                'Body.\n'
            ))
            data = parse_skill_md(filepath)
            self.assertEqual(data['name'], 'my-skill')

    def test_get_available_skills(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_skill(tmpdir, 'skill-a', (
                '---\nname: skill-a\ndescription: Skill A\n---\n\nDocs.\n'
            ))
            self._write_skill(tmpdir, 'skill-b', (
                '---\nname: skill-b\ndescription: Skill B\n---\n\nDocs.\n'
            ))
            skills = get_available_skills(Path(tmpdir))
            self.assertEqual(len(skills), 2)

    def test_get_skill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_skill(tmpdir, 'found-skill', (
                '---\nname: found-skill\ndescription: Found\n---\n\nDocs.\n'
            ))
            skill = get_skill('found-skill', Path(tmpdir))
            self.assertEqual(skill['name'], 'found-skill')

    def test_get_skill_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SkillNotFound):
                get_skill('missing', Path(tmpdir))


class ModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass123')
        self.briefing = Briefing.objects.create(
            owner=self.user,
            title='Monitor Tech Jobs',
            content='# Steps\n1. Scrape career pages\n2. Extract senior roles',
            coordinator_model='gemini-2.5-pro',
            schedule_text='every weekday at 9am',
        )
        self.run = Run.objects.create(
            briefing=self.briefing,
            status='completed',
            coordinator_report='Found 5 new senior roles across 3 companies.',
        )

    def test_briefing_str(self):
        self.assertEqual(str(self.briefing), 'Monitor Tech Jobs')

    def test_run_str(self):
        self.assertIn('Monitor Tech Jobs', str(self.run))
        self.assertIn('completed', str(self.run))

    def test_run_cleanup(self):
        old_run = Run.objects.create(briefing=self.briefing, status='completed')
        Run.objects.filter(pk=old_run.pk).update(
            created_at=timezone.now() - timezone.timedelta(weeks=7)
        )
        deleted = Run.cleanup_old_runs(weeks=6)
        self.assertEqual(deleted, 1)
        self.assertTrue(Run.objects.filter(pk=self.run.pk).exists())

    def test_agent_run_str(self):
        agent_run = AgentRun.objects.create(
            run=self.run,
            agent_name='careers-scraper',
            status='completed',
            report='Found 3 senior roles on example.com',
        )
        self.assertIn('careers-scraper', str(agent_run))
        self.assertIn('completed', str(agent_run))

    def test_agent_run_raw_data(self):
        agent_run = AgentRun.objects.create(
            run=self.run,
            agent_name='careers-scraper',
            status='completed',
            raw_data={'jobs': [{'title': 'Senior Engineer', 'company': 'Acme'}]},
        )
        self.assertEqual(agent_run.raw_data['jobs'][0]['title'], 'Senior Engineer')


class ValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser2', password='testpass123')

    def test_invalid_coordinator_model_rejected(self):
        briefing = Briefing(
            owner=self.user,
            title='Bad Model',
            content='test',
            coordinator_model='claude-nonexistent',
        )
        with self.assertRaises(ValidationError):
            briefing.full_clean()

    def test_valid_coordinator_model_accepted(self):
        briefing = Briefing(
            owner=self.user,
            title='Good Model',
            content='test',
            coordinator_model='gemini-2.5-pro',
        )
        briefing.full_clean()  # Should not raise


class CheckCommandTests(TestCase):
    def test_check_agents_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, 'test-agent.md').write_text(
                '---\n'
                'name: test-agent\n'
                'description: A test agent\n'
                'model: gpt-5-mini\n'
                'tools: ["bash"]\n'
                '---\n\n'
                'You are a test agent.\n'
            )
            call_command('check_agents', tmpdir)

    def test_check_agents_invalid_model_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, 'bad-agent.md').write_text(
                '---\n'
                'name: bad-agent\n'
                'description: Bad model\n'
                'model: nonexistent-model\n'
                '---\n\n'
                'You are bad.\n'
            )
            with self.assertRaises(CommandError):
                call_command('check_agents', tmpdir)

    def test_check_skills_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / 'test-skill'
            skill_dir.mkdir()
            (skill_dir / 'SKILL.md').write_text(
                '---\n'
                'name: test-skill\n'
                'description: A test skill\n'
                '---\n\n'
                'Skill docs.\n'
            )
            call_command('check_skills', tmpdir)


class APITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='apiuser', password='testpass123')
        self.token = Token.objects.create(user=self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

        self.briefing = Briefing.objects.create(
            owner=self.user,
            title='API Test Briefing',
            content='# Test',
        )

        # Set up temp agent/skill dirs for filesystem API tests
        self._tmpdir = tempfile.mkdtemp()
        self._agents_dir = Path(self._tmpdir) / 'agents'
        self._agents_dir.mkdir()
        self._skills_dir = Path(self._tmpdir) / 'skills'
        self._skills_dir.mkdir()

        # Write a test agent
        (self._agents_dir / 'test-agent.md').write_text(
            '---\n'
            'name: test-agent\n'
            'description: A test agent\n'
            'model: gpt-5-mini\n'
            'tools: ["bash"]\n'
            '---\n\n'
            'You are a test agent.\n'
        )

        # Write a test skill
        skill_dir = self._skills_dir / 'test-skill'
        skill_dir.mkdir()
        (skill_dir / 'SKILL.md').write_text(
            '---\n'
            'name: test-skill\n'
            'description: A test skill\n'
            '---\n\n'
            'Skill docs.\n'
        )

        # Point settings to temp dirs
        from django.conf import settings
        self._orig_agents_dir = settings.FUZZYCLAW_AGENTS_DIR
        self._orig_skills_dir = settings.FUZZYCLAW_SKILLS_DIR
        settings.FUZZYCLAW_AGENTS_DIR = self._agents_dir
        settings.FUZZYCLAW_SKILLS_DIR = self._skills_dir
        clear_cache()

    def tearDown(self):
        from django.conf import settings
        settings.FUZZYCLAW_AGENTS_DIR = self._orig_agents_dir
        settings.FUZZYCLAW_SKILLS_DIR = self._orig_skills_dir
        clear_cache()

        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_list_agents(self):
        response = self.client.get('/api/agents/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['name'], 'test-agent')

    def test_get_agent_detail(self):
        response = self.client.get('/api/agents/test-agent/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['name'], 'test-agent')

    def test_get_agent_not_found(self):
        response = self.client.get('/api/agents/nonexistent/')
        self.assertEqual(response.status_code, 404)

    def test_list_skills(self):
        response = self.client.get('/api/skills/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['name'], 'test-skill')

    def test_get_skill_detail(self):
        response = self.client.get('/api/skills/test-skill/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['name'], 'test-skill')

    def test_get_skill_not_found(self):
        response = self.client.get('/api/skills/nonexistent/')
        self.assertEqual(response.status_code, 404)

    def test_list_briefings(self):
        response = self.client.get('/api/briefings/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 1)

    def test_create_briefing_sets_owner(self):
        response = self.client.post('/api/briefings/', {
            'title': 'New Briefing',
            'content': '# Do stuff',
        })
        self.assertEqual(response.status_code, 201)
        briefing = Briefing.objects.get(title='New Briefing')
        self.assertEqual(briefing.owner, self.user)

    def test_runs_api_is_read_only(self):
        """POST /api/runs/ is not allowed — runs are launched via the
        briefing launch action, not direct creation."""
        response = self.client.post('/api/runs/', {
            'briefing': self.briefing.pk,
            'status': 'pending',
            'triggered_by': 'manual',
        })
        self.assertEqual(response.status_code, 405)
        self.assertEqual(Run.objects.count(), 0)

    def test_run_patch_cannot_mutate_execution_fields(self):
        """PATCH /api/runs/{id}/ is not allowed — the run log is immutable
        from the API surface."""
        run = Run.objects.create(briefing=self.briefing, status='running')
        response = self.client.patch(f'/api/runs/{run.pk}/', {
            'status': 'completed',
            'coordinator_report': 'fake',
        }, format='json')
        self.assertEqual(response.status_code, 405)
        run.refresh_from_db()
        self.assertEqual(run.status, 'running')
        self.assertEqual(run.coordinator_report, '')

    def test_run_notes_action_updates_user_notes(self):
        run = Run.objects.create(briefing=self.briefing, status='completed')
        response = self.client.patch(f'/api/runs/{run.pk}/notes/', {
            'user_notes': 'Interesting — revisit next week.',
        }, format='json')
        self.assertEqual(response.status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.user_notes, 'Interesting — revisit next week.')
        self.assertEqual(run.status, 'completed')  # unchanged

    @patch('core.api_views.cleanup_run')
    @patch('core.api_views.AsyncResult')
    def test_run_cancel_action_marks_stuck_run_failed(self, mock_async_result, mock_cleanup):
        run = Run.objects.create(
            briefing=self.briefing, status='running', celery_task_id='task-xyz',
        )
        response = self.client.post(f'/api/runs/{run.pk}/cancel/')
        self.assertEqual(response.status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIn('Cancelled', run.error_message)
        self.assertIsNotNone(run.completed_at)
        mock_async_result.assert_called_once_with('task-xyz')
        mock_async_result.return_value.revoke.assert_called_once_with(terminate=True)
        mock_cleanup.assert_called_once_with(run.id)

    @patch('core.api_views._release_container_slot')
    @patch('core.api_views.cleanup_run')
    @patch('core.api_views.AsyncResult')
    def test_run_cancel_finalizes_running_agent_runs(
        self, mock_async_result, mock_cleanup, mock_release,
    ):
        run = Run.objects.create(briefing=self.briefing, status='running')
        ar_running = AgentRun.objects.create(
            run=run, agent_name='r-agent', status='running',
        )
        ar_pending = AgentRun.objects.create(
            run=run, agent_name='p-agent', status='pending',
        )
        ar_already_done = AgentRun.objects.create(
            run=run, agent_name='done-agent', status='completed',
            report='Already done.',
        )

        response = self.client.post(f'/api/runs/{run.pk}/cancel/')
        self.assertEqual(response.status_code, 200)

        ar_running.refresh_from_db()
        ar_pending.refresh_from_db()
        ar_already_done.refresh_from_db()

        self.assertEqual(ar_running.status, 'failed')
        self.assertIn('cancelled', ar_running.error_message.lower())
        self.assertEqual(ar_pending.status, 'failed')
        self.assertEqual(ar_already_done.status, 'completed')  # untouched
        self.assertEqual(ar_already_done.report, 'Already done.')

        # Slots MUST be released as part of the finalization loop. cleanup_run
        # only releases slots for rows still in 'running' — since cancel flips
        # them to 'failed', we'd leak semaphore slots if we relied on cleanup_run.
        released_ids = {call.args[0] for call in mock_release.call_args_list}
        self.assertIn(ar_running.id, released_ids)
        self.assertIn(ar_pending.id, released_ids)
        self.assertNotIn(ar_already_done.id, released_ids)

    def test_run_cancel_rejects_terminal_run(self):
        run = Run.objects.create(briefing=self.briefing, status='completed')
        response = self.client.post(f'/api/runs/{run.pk}/cancel/')
        self.assertEqual(response.status_code, 400)

    @patch('core.tasks.launch_coordinator.delay')
    def test_briefing_launch_creates_run_and_dispatches(self, mock_delay):
        response = self.client.post(f'/api/briefings/{self.briefing.pk}/launch/')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Run.objects.filter(briefing=self.briefing).count(), 1)
        run = Run.objects.get(briefing=self.briefing)
        self.assertEqual(run.status, 'pending')
        self.assertEqual(run.triggered_by, 'manual')
        mock_delay.assert_called_once_with(run.id)

    def test_pending_runs_action(self):
        Run.objects.create(briefing=self.briefing, status='pending')
        Run.objects.create(briefing=self.briefing, status='completed')
        response = self.client.get('/api/runs/pending/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_agent_runs_api_is_read_only(self):
        """POST /api/agent-runs/ is not allowed — agent runs are created
        by the coordinator via the ORM."""
        run = Run.objects.create(briefing=self.briefing, status='running')
        response = self.client.post('/api/agent-runs/', {
            'run': run.pk,
            'agent_name': 'test-agent',
            'status': 'pending',
        })
        self.assertEqual(response.status_code, 405)
        self.assertEqual(AgentRun.objects.count(), 0)

    def test_agent_run_patch_cannot_mutate_execution_fields(self):
        """PATCH /api/agent-runs/{id}/ is not allowed — reports, status,
        and raw_data are execution-managed."""
        run = Run.objects.create(briefing=self.briefing, status='running')
        agent_run = AgentRun.objects.create(run=run, agent_name='test-agent', status='running')
        response = self.client.patch(f'/api/agent-runs/{agent_run.pk}/', {
            'status': 'completed',
            'report': 'Fake report.',
            'raw_data': {'items': [1, 2, 3]},
        }, format='json')
        self.assertEqual(response.status_code, 405)
        agent_run.refresh_from_db()
        self.assertEqual(agent_run.status, 'running')
        self.assertEqual(agent_run.report, '')

    def test_agent_run_notes_action_updates_user_notes(self):
        run = Run.objects.create(briefing=self.briefing, status='running')
        agent_run = AgentRun.objects.create(run=run, agent_name='test-agent', status='completed')
        response = self.client.patch(f'/api/agent-runs/{agent_run.pk}/notes/', {
            'user_notes': 'Missed the key source.',
        }, format='json')
        self.assertEqual(response.status_code, 200)
        agent_run.refresh_from_db()
        self.assertEqual(agent_run.user_notes, 'Missed the key source.')

    def test_run_includes_agent_runs(self):
        run = Run.objects.create(briefing=self.briefing, status='running')
        AgentRun.objects.create(run=run, agent_name='test-agent', status='completed', report='Done.')
        response = self.client.get(f'/api/runs/{run.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['agent_runs']), 1)
        self.assertEqual(response.data['agent_runs'][0]['agent_name'], 'test-agent')

    def test_unauthenticated_access_denied(self):
        client = APIClient()
        response = client.get('/api/briefings/')
        self.assertEqual(response.status_code, 401)


class DashboardViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='webuser', password='testpass123')

    def test_dashboard_requires_login(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_dashboard_authenticated(self):
        self.client.login(username='webuser', password='testpass123')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Dashboard')


class AgentImageModelTests(TestCase):
    def test_create_agent_image(self):
        img = AgentImage.objects.create(
            agent_name='summarizer',
            file_hash='a' * 64,
            image_tag='fuzzyclaw-agent-summarizer:latest',
        )
        self.assertEqual(str(img), 'summarizer (fuzzyclaw-agent-summarizer:latest)')
        self.assertFalse(img.has_error)

    def test_agent_image_with_error(self):
        img = AgentImage.objects.create(
            agent_name='broken',
            file_hash='b' * 64,
            image_tag='fuzzyclaw-agent-broken:latest',
            build_error='Build failed: missing dependency',
        )
        self.assertTrue(img.has_error)

    def test_agent_image_unique_name(self):
        AgentImage.objects.create(
            agent_name='unique-agent',
            file_hash='c' * 64,
            image_tag='fuzzyclaw-agent-unique:latest',
        )
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            AgentImage.objects.create(
                agent_name='unique-agent',
                file_hash='d' * 64,
                image_tag='fuzzyclaw-agent-unique2:latest',
            )


class ContainerHelpersTests(TestCase):
    def test_compute_file_hash(self):
        from .containers import compute_file_hash
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = Path(tmpdir) / 'file1.txt'
            f1.write_text('hello')
            f2 = Path(tmpdir) / 'file2.txt'
            f2.write_text('world')

            h1 = compute_file_hash(f1)
            h2 = compute_file_hash(f1, f2)

            self.assertEqual(len(h1), 64)
            self.assertNotEqual(h1, h2)

            # Deterministic
            self.assertEqual(h1, compute_file_hash(f1))

    def test_compute_file_hash_missing_file(self):
        from .containers import compute_file_hash
        h = compute_file_hash(Path('/nonexistent/file'))
        self.assertEqual(len(h), 64)  # empty hash

    def test_image_tag_for_agent(self):
        from .containers import image_tag_for_agent
        tag = image_tag_for_agent('summarizer')
        self.assertEqual(tag, 'fuzzyclaw-agent-summarizer:latest')

    def test_image_tag_for_agent_uses_setting(self):
        from .containers import image_tag_for_agent
        with self.settings(FUZZYCLAW_AGENT_IMAGE_PREFIX='custom-prefix'):
            tag = image_tag_for_agent('my-agent')
            self.assertEqual(tag, 'custom-prefix-my-agent:latest')


class SyncAgentImagesTests(TestCase):
    """Tests for sync_agent_images with mocked Docker client."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.agents_dir = Path(self.tmpdir)
        # Write a valid agent
        (self.agents_dir / 'test-agent.md').write_text(
            '---\nname: test-agent\ndescription: Test\nmodel: gpt-5-mini\n---\n\nPrompt.\n'
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch('core.containers.get_docker_client')
    def test_sync_builds_new_image(self, mock_get_client):
        from .containers import sync_agent_images

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        # Base image doesn't exist yet
        mock_client.images.get.side_effect = docker_image_not_found()
        mock_client.images.build.return_value = (MagicMock(), [])

        result = sync_agent_images(self.agents_dir)

        self.assertIn('test-agent', result['built'])
        self.assertEqual(result['errors'], [])
        # AgentImage record created
        self.assertTrue(AgentImage.objects.filter(agent_name='test-agent').exists())

    @patch('core.containers.get_docker_client')
    def test_sync_unchanged_when_hash_matches(self, mock_get_client):
        from .containers import compute_file_hash, sync_agent_images

        agent_path = self.agents_dir / 'test-agent.md'
        file_hash = compute_file_hash(agent_path)

        # Pre-create the AgentImage record
        AgentImage.objects.create(
            agent_name='test-agent',
            file_hash=file_hash,
            image_tag='fuzzyclaw-agent-test-agent:latest',
        )

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        # Base image exists with matching hash label so it won't rebuild
        from .containers import _hash_base_image_inputs
        base_hash = _hash_base_image_inputs()
        mock_base_image = MagicMock()
        mock_base_image.labels = {'fuzzyclaw.base_hash': base_hash}
        mock_client.images.get.return_value = mock_base_image

        result = sync_agent_images(self.agents_dir)

        self.assertIn('test-agent', result['unchanged'])
        self.assertEqual(result['built'], [])

    @patch('core.containers.get_docker_client')
    def test_sync_removes_stale_images(self, mock_get_client):
        from .containers import sync_agent_images

        # Create a record for an agent that no longer exists on disk
        AgentImage.objects.create(
            agent_name='deleted-agent',
            file_hash='x' * 64,
            image_tag='fuzzyclaw-agent-deleted-agent:latest',
        )

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        from .containers import _hash_base_image_inputs
        base_hash = _hash_base_image_inputs()
        mock_base_image = MagicMock()
        mock_base_image.labels = {'fuzzyclaw.base_hash': base_hash}
        mock_client.images.get.return_value = mock_base_image
        mock_client.images.build.return_value = (MagicMock(), [])

        result = sync_agent_images(self.agents_dir)

        self.assertIn('deleted-agent', result['removed'])
        self.assertFalse(AgentImage.objects.filter(agent_name='deleted-agent').exists())


class StartAgentContainerTests(TestCase):
    """Tests for start_agent_container (non-blocking) with mocked Docker."""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass123')
        self.briefing = Briefing.objects.create(
            owner=self.user, title='Test', content='test',
        )
        self.run = Run.objects.create(briefing=self.briefing, status='running')
        self.agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='running',
        )

        # Create AgentImage
        AgentImage.objects.create(
            agent_name='test-agent',
            file_hash='a' * 64,
            image_tag='fuzzyclaw-agent-test-agent:latest',
        )

        # Set up temp agent file
        self._tmpdir = tempfile.mkdtemp()
        self._agents_dir = Path(self._tmpdir) / 'agents'
        self._agents_dir.mkdir()
        (self._agents_dir / 'test-agent.md').write_text(
            '---\nname: test-agent\ndescription: Test\nmodel: gpt-5-mini\ntools: []\n---\n\nPrompt.\n'
        )
        self._orig_agents_dir = settings.FUZZYCLAW_AGENTS_DIR
        settings.FUZZYCLAW_AGENTS_DIR = self._agents_dir
        from .registry import clear_cache
        clear_cache()

        # Stub the Redis-backed concurrency gate so tests don't need real Redis
        self._acquire_patcher = patch('core.containers._acquire_container_slot')
        self._release_patcher = patch('core.containers._release_container_slot')
        self.mock_acquire = self._acquire_patcher.start()
        self.mock_release = self._release_patcher.start()

    def tearDown(self):
        settings.FUZZYCLAW_AGENTS_DIR = self._orig_agents_dir
        from .registry import clear_cache
        clear_cache()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

        self._acquire_patcher.stop()
        self._release_patcher.stop()

    def test_no_image_raises(self):
        from .containers import start_agent_container
        AgentImage.objects.all().delete()
        with self.assertRaises(RuntimeError) as ctx:
            start_agent_container('test-agent', 'do stuff', self.agent_run.id, self.run.id)
        self.assertIn('No image built', str(ctx.exception))

    def test_build_error_raises(self):
        from .containers import start_agent_container
        AgentImage.objects.filter(agent_name='test-agent').update(
            build_error='broken'
        )
        with self.assertRaises(RuntimeError) as ctx:
            start_agent_container('test-agent', 'do stuff', self.agent_run.id, self.run.id)
        self.assertIn('build error', str(ctx.exception))

    def test_concurrency_limit(self):
        from .containers import start_agent_container

        self.mock_acquire.side_effect = RuntimeError(
            "Container concurrency limit reached (10/10). Try again later."
        )

        with self.assertRaises(RuntimeError) as ctx:
            start_agent_container('test-agent', 'do stuff', self.agent_run.id, self.run.id)
        self.assertIn('concurrency limit', str(ctx.exception))

    @patch('core.containers.get_docker_client')
    def test_successful_start_returns_container_id(self, mock_get_client):
        from .containers import start_agent_container

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.containers.list.return_value = []  # no running containers

        mock_container = MagicMock()
        mock_container.id = 'abc123'
        mock_client.containers.run.return_value = mock_container

        container_id = start_agent_container(
            'test-agent', 'do stuff', self.agent_run.id, self.run.id,
        )

        self.assertEqual(container_id, 'abc123')
        # Should NOT wait or remove — non-blocking
        mock_container.wait.assert_not_called()
        mock_container.remove.assert_not_called()

    @patch('core.containers.get_docker_client')
    def test_start_acquires_container_slot(self, mock_get_client):
        from .containers import start_agent_container

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.containers.list.return_value = []

        mock_container = MagicMock()
        mock_container.id = 'slot-test'
        mock_client.containers.run.return_value = mock_container

        start_agent_container(
            'test-agent', 'test slot', self.agent_run.id, self.run.id,
        )

        self.mock_acquire.assert_called_once_with(self.agent_run.id)
        self.mock_release.assert_not_called()

    @patch('core.containers.get_docker_client')
    def test_start_passes_redis_env_vars(self, mock_get_client):
        from .containers import start_agent_container

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.containers.list.return_value = []

        mock_container = MagicMock()
        mock_container.id = 'redis-test-123'
        mock_client.containers.run.return_value = mock_container

        start_agent_container(
            'test-agent', 'test redis', self.agent_run.id, self.run.id,
        )

        call_kwargs = mock_client.containers.run.call_args
        env_arg = call_kwargs.kwargs.get('environment') or call_kwargs[1].get('environment')
        self.assertIn('REDIS_URL', env_arg)
        self.assertEqual(env_arg['RUN_ID'], str(self.run.id))
        self.assertEqual(env_arg['AGENT_RUN_ID'], str(self.agent_run.id))


    @patch('core.containers.get_docker_client')
    def test_container_slot_released_on_docker_failure(self, mock_get_client):
        """Container slot must be released if containers.run fails."""
        from .containers import start_agent_container

        mock_get_client.side_effect = Exception("Docker daemon unreachable")

        with self.assertRaises(Exception):
            start_agent_container('test-agent', 'fail test', self.agent_run.id, self.run.id)

        self.mock_acquire.assert_called_once_with(self.agent_run.id)
        self.mock_release.assert_called_once_with(self.agent_run.id)

    @patch('core.containers.get_docker_client')
    def test_container_slot_released_on_volume_validation_failure(self, mock_get_client):
        """Container slot must be released if volume validation fails."""
        from .containers import start_agent_container

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        with self.settings(FUZZYCLAW_VOLUME_BLOCKLIST=['/etc']):
            with patch('core.containers.get_agent') as mock_agent:
                mock_agent.return_value = {
                    'name': 'test-agent',
                    'model_choice': 'gpt-5-mini',
                    'tools': [],
                    'volumes': [{'host': '/etc/shadow', 'mount': '/data', 'mode': 'ro'}],
                }
                with self.assertRaises(RuntimeError):
                    start_agent_container('test-agent', 'vol fail', self.agent_run.id, self.run.id)

        self.mock_acquire.assert_called_once_with(self.agent_run.id)
        self.mock_release.assert_called_once_with(self.agent_run.id)


class DispatchSpecialistTests(TestCase):
    """Tests for dispatch_specialist tool (non-blocking) with mocked container start."""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass123')
        self.briefing = Briefing.objects.create(
            owner=self.user, title='Test', content='test',
        )
        self.run = Run.objects.create(briefing=self.briefing, status='running')

        self._tmpdir = tempfile.mkdtemp()
        self._agents_dir = Path(self._tmpdir) / 'agents'
        self._agents_dir.mkdir()
        (self._agents_dir / 'test-agent.md').write_text(
            '---\nname: test-agent\ndescription: Test\nmodel: gpt-5-mini\n---\n\nPrompt.\n'
        )
        self._orig_agents_dir = settings.FUZZYCLAW_AGENTS_DIR
        settings.FUZZYCLAW_AGENTS_DIR = self._agents_dir
        from .registry import clear_cache
        clear_cache()

    def tearDown(self):
        settings.FUZZYCLAW_AGENTS_DIR = self._orig_agents_dir
        from .registry import clear_cache
        clear_cache()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_dispatch_agent_not_found(self):
        from .agent_tools import make_dispatch_specialist
        dispatch_specialist = make_dispatch_specialist(self.run)
        result = dispatch_specialist.invoke({
            'agent_name': 'nonexistent',
            'task_description': 'do stuff',
        })
        self.assertIn('not found', result)

    @patch('core.containers.start_agent_container')
    def test_dispatch_returns_agent_run_id(self, mock_start):
        from .agent_tools import make_dispatch_specialist

        mock_start.return_value = 'container123'
        dispatch_specialist = make_dispatch_specialist(self.run)

        result = dispatch_specialist.invoke({
            'agent_name': 'test-agent',
            'task_description': 'summarize this',
        })

        self.assertIn('agent_run_id=', result)
        self.assertIn("Dispatched 'test-agent'", result)

        agent_run = AgentRun.objects.get(agent_name='test-agent', run=self.run)
        self.assertEqual(agent_run.status, 'running')
        self.assertEqual(agent_run.container_id, 'container123')
        self.assertIsNotNone(agent_run.started_at)
        # Should NOT be completed yet — non-blocking
        self.assertIsNone(agent_run.completed_at)

    @patch('core.containers.start_agent_container')
    def test_dispatch_exception_deletes_agent_run(self, mock_start):
        from .agent_tools import make_dispatch_specialist

        mock_start.side_effect = RuntimeError('No image built')
        dispatch_specialist = make_dispatch_specialist(self.run)

        result = dispatch_specialist.invoke({
            'agent_name': 'test-agent',
            'task_description': 'do something',
        })

        self.assertIn('dispatch failed', result)

        # AgentRun should be deleted — no phantom record left behind
        self.assertFalse(
            AgentRun.objects.filter(agent_name='test-agent', run=self.run).exists()
        )


class VolumeParsingTests(TestCase):
    """Tests for volume spec parsing and structural validation."""

    def setUp(self):
        clear_cache()

    def _write_agent(self, tmpdir, filename, content):
        filepath = Path(tmpdir) / filename
        filepath.write_text(content)
        return filepath

    def test_parse_agent_with_volumes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._write_agent(tmpdir, 'vol-agent.md', (
                '---\n'
                'name: vol-agent\n'
                'model: gpt-5-mini\n'
                'tools: ["bash"]\n'
                'volumes: [{"host": "/home/user/proj", "mount": "/workspace", "mode": "ro"}]\n'
                '---\n\n'
                'Agent with volumes.\n'
            ))
            data = parse_agent_md(filepath)
            self.assertEqual(len(data['volumes']), 1)
            self.assertEqual(data['volumes'][0]['host'], '/home/user/proj')
            self.assertEqual(data['volumes'][0]['mount'], '/workspace')
            self.assertEqual(data['volumes'][0]['mode'], 'ro')

    def test_parse_agent_no_volumes_defaults_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._write_agent(tmpdir, 'no-vol.md', (
                '---\n'
                'name: no-vol\n'
                'model: gpt-5-mini\n'
                '---\n\n'
                'No volumes.\n'
            ))
            data = parse_agent_md(filepath)
            self.assertEqual(data['volumes'], [])

    def test_parse_agent_multiple_volumes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._write_agent(tmpdir, 'multi-vol.md', (
                '---\n'
                'name: multi-vol\n'
                'model: gpt-5-mini\n'
                'tools: ["bash"]\n'
                'volumes: [{"host": "/data", "mount": "/workspace", "mode": "ro"}, '
                '{"host": "/output", "mount": "/output", "mode": "rw"}]\n'
                '---\n\n'
                'Multiple volumes.\n'
            ))
            data = parse_agent_md(filepath)
            self.assertEqual(len(data['volumes']), 2)


class VolumeValidationTests(TestCase):
    """Tests for validate_volumes structural checks."""

    def test_valid_volume(self):
        errors = validate_volumes([
            {'host': '/home/user/proj', 'mount': '/workspace', 'mode': 'ro'},
        ])
        self.assertEqual(errors, [])

    def test_valid_rw_volume(self):
        errors = validate_volumes([
            {'host': '/data/output', 'mount': '/output', 'mode': 'rw'},
        ])
        self.assertEqual(errors, [])

    def test_invalid_mode(self):
        errors = validate_volumes([
            {'host': '/data', 'mount': '/workspace', 'mode': 'rwx'},
        ])
        self.assertEqual(len(errors), 1)
        self.assertIn("'mode' must be 'ro' or 'rw'", errors[0])

    def test_missing_host(self):
        errors = validate_volumes([
            {'mount': '/workspace', 'mode': 'ro'},
        ])
        self.assertTrue(any("'host'" in e for e in errors))

    def test_missing_mount(self):
        errors = validate_volumes([
            {'host': '/data', 'mode': 'ro'},
        ])
        self.assertTrue(any("'mount'" in e for e in errors))

    def test_relative_mount_rejected(self):
        errors = validate_volumes([
            {'host': '/data', 'mount': 'workspace', 'mode': 'ro'},
        ])
        self.assertTrue(any("absolute path" in e for e in errors))

    def test_reserved_mount_app(self):
        errors = validate_volumes([
            {'host': '/data', 'mount': '/app', 'mode': 'ro'},
        ])
        self.assertTrue(any("conflicts with reserved" in e for e in errors))

    def test_reserved_mount_skills(self):
        errors = validate_volumes([
            {'host': '/data', 'mount': '/app/skills', 'mode': 'ro'},
        ])
        self.assertTrue(any("conflicts with reserved" in e for e in errors))

    def test_reserved_mount_comms(self):
        errors = validate_volumes([
            {'host': '/data', 'mount': '/app/comms', 'mode': 'ro'},
        ])
        self.assertTrue(any("conflicts with reserved" in e for e in errors))

    def test_reserved_mount_subpath(self):
        errors = validate_volumes([
            {'host': '/data', 'mount': '/app/skills/mything', 'mode': 'ro'},
        ])
        self.assertTrue(any("conflicts with reserved" in e for e in errors))

    def test_not_a_dict(self):
        errors = validate_volumes(["not-a-dict"])
        self.assertTrue(any("must be an object" in e for e in errors))

    def test_not_a_list(self):
        errors = validate_volumes("bad")
        self.assertTrue(any("must be a JSON list" in e for e in errors))

    def test_empty_volumes_valid(self):
        errors = validate_volumes([])
        self.assertEqual(errors, [])

    def test_multiple_errors_reported(self):
        errors = validate_volumes([
            {'host': '/data', 'mount': 'relative', 'mode': 'bad'},
        ])
        self.assertGreaterEqual(len(errors), 2)


class VolumeSecurityTests(TestCase):
    """Tests for volume mount security validation at launch time."""

    def test_blocklist_rejects_root(self):
        from .containers import _validate_volume_mount
        with self.assertRaises(RuntimeError) as ctx:
            _validate_volume_mount({'host': '/', 'mount': '/workspace', 'mode': 'ro'})
        self.assertIn('blocklist', str(ctx.exception))

    def test_blocklist_rejects_etc(self):
        from .containers import _validate_volume_mount
        with self.assertRaises(RuntimeError) as ctx:
            _validate_volume_mount({'host': '/etc', 'mount': '/workspace', 'mode': 'ro'})
        self.assertIn('blocklist', str(ctx.exception))

    def test_blocklist_rejects_etc_subpath(self):
        from .containers import _validate_volume_mount
        with self.assertRaises(RuntimeError) as ctx:
            _validate_volume_mount({'host': '/etc/passwd', 'mount': '/workspace', 'mode': 'ro'})
        self.assertIn('blocklist', str(ctx.exception))

    def test_blocklist_rejects_docker_socket(self):
        from .containers import _validate_volume_mount
        with self.assertRaises(RuntimeError) as ctx:
            _validate_volume_mount({'host': '/var/run/docker.sock', 'mount': '/workspace', 'mode': 'ro'})
        self.assertIn('blocklist', str(ctx.exception))

    def test_empty_allowlist_allows_all(self):
        from .containers import _validate_volume_mount
        with self.settings(
            FUZZYCLAW_VOLUME_ALLOWLIST=[],
            FUZZYCLAW_VOLUME_BLOCKLIST=[],
        ):
            # Empty allowlist = everything allowed (minus blocklist)
            _validate_volume_mount({'host': '/home/user/safe', 'mount': '/workspace', 'mode': 'ro'})

    def test_allowlist_permits_allowed_path(self):
        from .containers import _validate_volume_mount
        with self.settings(
            FUZZYCLAW_VOLUME_ALLOWLIST=['/home/user/projects'],
            FUZZYCLAW_VOLUME_BLOCKLIST=[],
        ):
            # Should not raise
            _validate_volume_mount({'host': '/home/user/projects/myapp', 'mount': '/workspace', 'mode': 'ro'})

    def test_allowlist_permits_exact_match(self):
        from .containers import _validate_volume_mount
        with self.settings(
            FUZZYCLAW_VOLUME_ALLOWLIST=['/home/user/projects'],
            FUZZYCLAW_VOLUME_BLOCKLIST=[],
        ):
            _validate_volume_mount({'host': '/home/user/projects', 'mount': '/workspace', 'mode': 'ro'})

    def test_allowlist_rejects_outside_path(self):
        from .containers import _validate_volume_mount
        with self.settings(
            FUZZYCLAW_VOLUME_ALLOWLIST=['/home/user/projects'],
            FUZZYCLAW_VOLUME_BLOCKLIST=[],
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _validate_volume_mount({'host': '/home/user/secrets', 'mount': '/workspace', 'mode': 'ro'})
            self.assertIn('not under any allowlisted', str(ctx.exception))

    def test_blocklist_takes_precedence(self):
        from .containers import _validate_volume_mount
        with self.settings(
            FUZZYCLAW_VOLUME_ALLOWLIST=['/'],
            FUZZYCLAW_VOLUME_BLOCKLIST=['/', '/etc'],
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _validate_volume_mount({'host': '/etc/shadow', 'mount': '/workspace', 'mode': 'ro'})
            self.assertIn('blocklist', str(ctx.exception))

    def test_resolve_relative_path(self):
        from .containers import _resolve_volume_host_path
        with self.settings(FUZZYCLAW_HOST_PROJECT_DIR='/home/user/fuzzyclaw'):
            resolved = _resolve_volume_host_path('./')
            self.assertEqual(resolved, '/home/user/fuzzyclaw')

    def test_resolve_relative_subpath(self):
        from .containers import _resolve_volume_host_path
        with self.settings(FUZZYCLAW_HOST_PROJECT_DIR='/home/user/fuzzyclaw'):
            resolved = _resolve_volume_host_path('./data/output')
            self.assertEqual(resolved, '/home/user/fuzzyclaw/data/output')

    def test_resolve_absolute_path_unchanged(self):
        from .containers import _resolve_volume_host_path
        resolved = _resolve_volume_host_path('/home/user/other')
        self.assertEqual(resolved, '/home/user/other')

    def test_symlink_to_blocked_path_rejected(self):
        """Symlink that resolves to a blocked path must be caught."""
        from .containers import _resolve_volume_host_path, _validate_volume_mount

        with tempfile.TemporaryDirectory() as tmpdir:
            link_path = Path(tmpdir) / 'sneaky_link'
            link_path.symlink_to('/etc')

            with self.settings(
                FUZZYCLAW_HOST_PROJECT_DIR=tmpdir,
                FUZZYCLAW_VOLUME_ALLOWLIST=[tmpdir],
                FUZZYCLAW_VOLUME_BLOCKLIST=['/etc'],
            ):
                resolved = _resolve_volume_host_path(str(link_path))
                self.assertEqual(resolved, '/etc')

                with self.assertRaises(RuntimeError) as ctx:
                    _validate_volume_mount({'host': str(link_path), 'mount': '/data', 'mode': 'ro'})
                self.assertIn('blocklist', str(ctx.exception))

    def test_relative_symlink_to_blocked_path_rejected(self):
        """Relative symlink traversal into a blocked path must be caught."""
        from .containers import _validate_volume_mount

        with tempfile.TemporaryDirectory() as tmpdir:
            link_path = Path(tmpdir) / 'etc_link'
            link_path.symlink_to('/etc')

            with self.settings(
                FUZZYCLAW_HOST_PROJECT_DIR=tmpdir,
                FUZZYCLAW_VOLUME_ALLOWLIST=[tmpdir],
                FUZZYCLAW_VOLUME_BLOCKLIST=['/etc'],
            ):
                with self.assertRaises(RuntimeError):
                    _validate_volume_mount({'host': f'{tmpdir}/etc_link', 'mount': '/data', 'mode': 'ro'})


class VolumeLaunchTests(TestCase):
    """Tests for volume mounts in start_agent_container."""

    def setUp(self):
        self.user = User.objects.create_user(username='voluser', password='testpass123')
        self.briefing = Briefing.objects.create(
            owner=self.user, title='Vol Test', content='test',
        )
        self.run = Run.objects.create(briefing=self.briefing, status='running')
        self.agent_run = AgentRun.objects.create(
            run=self.run, agent_name='vol-agent', status='running',
        )

        AgentImage.objects.create(
            agent_name='vol-agent',
            file_hash='v' * 64,
            image_tag='fuzzyclaw-agent-vol-agent:latest',
        )

        self._tmpdir = tempfile.mkdtemp()
        self._agents_dir = Path(self._tmpdir) / 'agents'
        self._agents_dir.mkdir()
        (self._agents_dir / 'vol-agent.md').write_text(
            '---\n'
            'name: vol-agent\n'
            'description: Agent with volumes\n'
            'model: gpt-5-mini\n'
            'tools: ["bash"]\n'
            'volumes: [{"host": "/home/user/projects/myapp", "mount": "/workspace", "mode": "ro"}]\n'
            '---\n\n'
            'Agent with volumes.\n'
        )
        self._orig_agents_dir = settings.FUZZYCLAW_AGENTS_DIR
        settings.FUZZYCLAW_AGENTS_DIR = self._agents_dir
        clear_cache()

        self._acquire_patcher = patch('core.containers._acquire_container_slot')
        self._release_patcher = patch('core.containers._release_container_slot')
        self.mock_acquire = self._acquire_patcher.start()
        self.mock_release = self._release_patcher.start()

    def tearDown(self):
        settings.FUZZYCLAW_AGENTS_DIR = self._orig_agents_dir
        clear_cache()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        self._acquire_patcher.stop()
        self._release_patcher.stop()

    @patch('core.containers.get_docker_client')
    def test_start_with_volumes_passes_mounts(self, mock_get_client):
        import json as json_mod
        from .containers import start_agent_container

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.containers.list.return_value = []

        mock_container = MagicMock()
        mock_container.id = 'vol123'
        mock_client.containers.run.return_value = mock_container

        with self.settings(
            FUZZYCLAW_VOLUME_ALLOWLIST=['/home/user/projects'],
            FUZZYCLAW_VOLUME_BLOCKLIST=[],
        ):
            container_id = start_agent_container(
                'vol-agent', 'test volumes', self.agent_run.id, self.run.id,
            )

        self.assertEqual(container_id, 'vol123')

        # Verify custom volume was passed to Docker
        call_kwargs = mock_client.containers.run.call_args
        volumes_arg = call_kwargs.kwargs.get('volumes') or call_kwargs[1].get('volumes')
        self.assertIn('/home/user/projects/myapp', volumes_arg)
        self.assertEqual(volumes_arg['/home/user/projects/myapp'], {'bind': '/workspace', 'mode': 'ro'})

        # Verify AGENT_VOLUMES env var was set
        env_arg = call_kwargs.kwargs.get('environment') or call_kwargs[1].get('environment')
        self.assertIn('AGENT_VOLUMES', env_arg)
        vol_info = json_mod.loads(env_arg['AGENT_VOLUMES'])
        self.assertEqual(vol_info[0]['mount'], '/workspace')
        self.assertEqual(vol_info[0]['mode'], 'ro')

    @patch('core.containers.get_docker_client')
    def test_start_with_blocked_volume_raises(self, mock_get_client):
        from .containers import start_agent_container

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.containers.list.return_value = []

        # Rewrite agent to mount /etc
        (self._agents_dir / 'vol-agent.md').write_text(
            '---\n'
            'name: vol-agent\n'
            'description: Agent with blocked volume\n'
            'model: gpt-5-mini\n'
            'tools: ["bash"]\n'
            'volumes: [{"host": "/etc", "mount": "/workspace", "mode": "ro"}]\n'
            '---\n\n'
            'Agent with blocked volumes.\n'
        )
        clear_cache()

        with self.assertRaises(RuntimeError) as ctx:
            start_agent_container(
                'vol-agent', 'test blocked', self.agent_run.id, self.run.id,
            )
        self.assertIn('blocklist', str(ctx.exception))


class ReadAgentReportTests(TestCase):
    """Tests for read_agent_report filesystem reading."""

    def setUp(self):
        self.user = User.objects.create_user(username='reportuser', password='testpass123')
        self.briefing = Briefing.objects.create(
            owner=self.user, title='Report Test', content='test',
        )
        self.run = Run.objects.create(briefing=self.briefing, status='running')
        self.comms_base = settings.BASE_DIR / 'comms'

    def tearDown(self):
        import shutil
        if self.comms_base.is_dir():
            shutil.rmtree(self.comms_base, ignore_errors=True)

    def test_read_report_json(self):
        import json
        from .containers import read_agent_report

        agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='running',
        )
        comms_dir = self.comms_base / str(agent_run.id)
        comms_dir.mkdir(parents=True, exist_ok=True)
        (comms_dir / 'report.json').write_text(json.dumps({
            'agent_name': 'test-agent',
            'status': 'completed',
            'report': 'Test report',
        }))

        report, exit_code = read_agent_report(agent_run.id)
        self.assertEqual(exit_code, 0)
        self.assertEqual(report['report'], 'Test report')

    def test_read_error_json(self):
        import json
        from .containers import read_agent_report

        agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='running',
        )
        comms_dir = self.comms_base / str(agent_run.id)
        comms_dir.mkdir(parents=True, exist_ok=True)
        (comms_dir / 'error.json').write_text(json.dumps({
            'status': 'failed',
            'error': 'Agent crashed',
        }))

        report, exit_code = read_agent_report(agent_run.id)
        self.assertEqual(exit_code, 1)
        self.assertEqual(report['error'], 'Agent crashed')

    def test_read_no_report_no_container(self):
        from .containers import read_agent_report

        agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='running',
        )
        comms_dir = self.comms_base / str(agent_run.id)
        comms_dir.mkdir(parents=True, exist_ok=True)
        # No files written

        report, exit_code = read_agent_report(agent_run.id)
        self.assertEqual(exit_code, -1)
        self.assertIn('No report file', report['error'])


class GetContainerStatusTests(TestCase):
    """Tests for get_container_status."""

    @patch('core.containers.get_docker_client')
    def test_running_container(self, mock_get_client):
        from .containers import get_container_status

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_container = MagicMock()
        mock_container.status = 'running'
        mock_client.containers.get.return_value = mock_container

        status = get_container_status(42, 'test-agent')
        self.assertEqual(status, 'running')

    @patch('core.containers.get_docker_client')
    def test_exited_container(self, mock_get_client):
        from .containers import get_container_status

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_container = MagicMock()
        mock_container.status = 'exited'
        mock_client.containers.get.return_value = mock_container

        status = get_container_status(42, 'test-agent')
        self.assertEqual(status, 'exited')

    @patch('core.containers.get_docker_client')
    def test_removed_container(self, mock_get_client):
        import docker.errors
        from .containers import get_container_status

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.containers.get.side_effect = docker.errors.NotFound('gone')

        status = get_container_status(42, 'test-agent')
        self.assertEqual(status, 'removed')


class CleanupRunTests(TestCase):
    """Tests for cleanup_run."""

    def setUp(self):
        self.user = User.objects.create_user(username='cleanupuser', password='testpass123')
        self.briefing = Briefing.objects.create(
            owner=self.user, title='Cleanup Test', content='test',
        )
        self.run = Run.objects.create(briefing=self.briefing, status='completed')
        self.comms_base = settings.BASE_DIR / 'comms'

    def tearDown(self):
        import shutil
        if self.comms_base.is_dir():
            shutil.rmtree(self.comms_base, ignore_errors=True)

    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_docker_client')
    def test_cleanup_removes_comms_dirs(self, mock_get_client, mock_redis):
        from .containers import cleanup_run

        mock_redis.return_value = None  # No Redis

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        import docker.errors
        mock_client.containers.get.side_effect = docker.errors.NotFound('gone')

        agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='completed',
        )

        # Create comms dir
        comms_dir = self.comms_base / str(agent_run.id)
        comms_dir.mkdir(parents=True, exist_ok=True)
        (comms_dir / 'report.json').write_text('{}')

        result = cleanup_run(self.run.id)

        self.assertEqual(result['comms_removed'], 1)
        self.assertFalse(comms_dir.is_dir())

    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_docker_client')
    def test_cleanup_removes_containers(self, mock_get_client, mock_redis):
        from .containers import cleanup_run

        mock_redis.return_value = None

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container

        agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='completed',
            container_id='abc123',
        )

        result = cleanup_run(self.run.id)

        # Should have tried to remove by ID and by name
        self.assertGreaterEqual(result['containers_removed'], 1)

    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_docker_client')
    def test_cleanup_deletes_redis_stream(self, mock_get_client, mock_redis):
        from .containers import cleanup_run

        mock_r = MagicMock()
        mock_redis.return_value = mock_r

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        import docker.errors
        mock_client.containers.get.side_effect = docker.errors.NotFound('gone')

        AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='completed',
        )

        result = cleanup_run(self.run.id)

        self.assertTrue(result['stream_deleted'])
        mock_r.delete.assert_called_once_with(
            f'fuzzyclaw:run:{self.run.id}:done',
            f'fuzzyclaw:board:{self.run.id}',
            f'fuzzyclaw:board:{self.run.id}:participants',
        )


class CheckReportsToolTests(TestCase):
    """Tests for the check_reports coordinator tool."""

    def setUp(self):
        from .agent_tools import make_check_reports

        self.user = User.objects.create_user(username='checkuser', password='testpass123')
        self.briefing = Briefing.objects.create(
            owner=self.user, title='Check Test', content='test',
        )
        self.run = Run.objects.create(briefing=self.briefing, status='running')
        self.comms_base = settings.BASE_DIR / 'comms'
        self.check_reports = make_check_reports(self.run)

    def tearDown(self):
        import shutil
        if self.comms_base.is_dir():
            shutil.rmtree(self.comms_base, ignore_errors=True)

    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_container_status')
    def test_check_reports_completed(self, mock_status, mock_redis):
        import json

        mock_redis.return_value = None  # No Redis

        agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='running',
        )

        # Write report file
        comms_dir = self.comms_base / str(agent_run.id)
        comms_dir.mkdir(parents=True, exist_ok=True)
        (comms_dir / 'report.json').write_text(json.dumps({'status': 'completed'}))

        result = self.check_reports.invoke({'wait_seconds': 1})
        data = json.loads(result)

        self.assertIn('progress', data)
        self.assertEqual(len(data['agents']), 1)
        self.assertEqual(data['agents'][0]['status'], 'completed')
        self.assertEqual(data['agents'][0]['agent_run_id'], agent_run.id)

    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_container_status')
    def test_check_reports_failed(self, mock_status, mock_redis):
        import json

        mock_redis.return_value = None

        agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='running',
        )

        comms_dir = self.comms_base / str(agent_run.id)
        comms_dir.mkdir(parents=True, exist_ok=True)
        (comms_dir / 'error.json').write_text(json.dumps({'status': 'failed'}))

        result = self.check_reports.invoke({'wait_seconds': 1})
        data = json.loads(result)

        self.assertEqual(data['agents'][0]['status'], 'failed')

    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_container_status')
    def test_check_reports_still_running(self, mock_status, mock_redis):
        import json

        mock_redis.return_value = None
        mock_status.return_value = 'running'

        AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='running',
        )

        result = self.check_reports.invoke({'wait_seconds': 1})
        data = json.loads(result)

        self.assertEqual(data['agents'][0]['status'], 'running')

    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_container_status')
    def test_check_reports_crashed(self, mock_status, mock_redis):
        import json

        mock_redis.return_value = None
        mock_status.return_value = 'exited'

        agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='running',
        )

        result = self.check_reports.invoke({'wait_seconds': 1})
        data = json.loads(result)

        self.assertEqual(data['agents'][0]['status'], 'crashed')

        agent_run.refresh_from_db()
        self.assertEqual(agent_run.status, 'failed')
        self.assertIn('without writing a report', agent_run.error_message)

    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_container_status')
    def test_check_reports_timed_out(self, mock_status, mock_redis):
        import json

        mock_redis.return_value = None
        mock_status.return_value = 'running'

        agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='running',
            started_at=timezone.now() - timezone.timedelta(seconds=700),
        )

        with self.settings(FUZZYCLAW_AGENT_TIMEOUT=600):
            result = self.check_reports.invoke({'wait_seconds': 1})
        data = json.loads(result)

        self.assertEqual(data['agents'][0]['status'], 'timed_out')

        agent_run.refresh_from_db()
        self.assertEqual(agent_run.status, 'failed')
        self.assertIn('timed out', agent_run.error_message)

    @patch('core.agent_tools.get_agent')
    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_container_status')
    def test_check_reports_hitl_agent_gets_longer_timeout(self, mock_status, mock_redis, mock_get_agent):
        """Agents with message_board tool use HITL timeout, not agent timeout."""
        import json

        mock_redis.return_value = None
        mock_status.return_value = 'running'
        mock_get_agent.return_value = {'name': 'test-agent', 'tools': ['message_board']}

        AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='running',
            started_at=timezone.now() - timezone.timedelta(seconds=700),
        )

        with self.settings(FUZZYCLAW_AGENT_TIMEOUT=600, FUZZYCLAW_HITL_TIMEOUT=1800):
            result = self.check_reports.invoke({'wait_seconds': 1})
        data = json.loads(result)

        self.assertEqual(data['agents'][0]['status'], 'running')

    @patch('core.agent_tools.get_agent')
    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_container_status')
    def test_check_reports_non_hitl_agent_times_out_normally(self, mock_status, mock_redis, mock_get_agent):
        """Agents without message_board use the standard agent timeout."""
        import json

        mock_redis.return_value = None
        mock_status.return_value = 'running'
        mock_get_agent.return_value = {'name': 'test-agent', 'tools': ['web_search']}

        AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='running',
            started_at=timezone.now() - timezone.timedelta(seconds=700),
        )

        with self.settings(FUZZYCLAW_AGENT_TIMEOUT=600, FUZZYCLAW_HITL_TIMEOUT=1800):
            result = self.check_reports.invoke({'wait_seconds': 1})
        data = json.loads(result)

        self.assertEqual(data['agents'][0]['status'], 'timed_out')

    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_container_status')
    def test_check_reports_excludes_already_finalized(self, mock_status, mock_redis):
        import json

        mock_redis.return_value = None

        AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='completed',
            report='Done.',
        )

        result = self.check_reports.invoke({'wait_seconds': 1})
        data = json.loads(result)

        self.assertEqual(data['agents'], [])
        self.assertIn('1/1', data['progress'])

    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_container_status')
    def test_check_reports_mixed_only_shows_running(self, mock_status, mock_redis):
        import json

        mock_redis.return_value = None
        mock_status.return_value = 'running'

        AgentRun.objects.create(
            run=self.run, agent_name='agent-a', status='completed',
        )
        AgentRun.objects.create(
            run=self.run, agent_name='agent-b', status='running',
        )

        result = self.check_reports.invoke({'wait_seconds': 1})
        data = json.loads(result)

        self.assertEqual(len(data['agents']), 1)
        self.assertEqual(data['agents'][0]['agent_name'], 'agent-b')
        self.assertEqual(data['agents'][0]['status'], 'running')
        self.assertIn('1/2', data['progress'])

    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_container_status')
    def test_check_reports_caps_wait_seconds(self, mock_status, mock_redis):
        import json

        mock_redis.return_value = None

        result = self.check_reports.invoke({'wait_seconds': 999})
        data = json.loads(result)
        self.assertEqual(data['agents'], [])
        self.assertIn('0/0', data['progress'])

    @patch('core.containers._get_redis_client')
    @patch('core.containers.get_container_status')
    def test_check_reports_excludes_dispatch_failures(self, mock_status, mock_redis):
        import json

        mock_redis.return_value = None

        AgentRun.objects.create(
            run=self.run, agent_name='failed-dispatch', status='pending',
        )
        AgentRun.objects.create(
            run=self.run, agent_name='running-agent', status='running',
        )
        mock_status.return_value = 'running'

        result = self.check_reports.invoke({'wait_seconds': 1})
        data = json.loads(result)

        self.assertEqual(len(data['agents']), 1)
        self.assertEqual(data['agents'][0]['agent_name'], 'running-agent')
        self.assertIn('0/1', data['progress'])

    def test_check_reports_cannot_inspect_other_runs(self):
        """Closure-bound: the tool only sees its own run's agents."""
        import json

        other_briefing = Briefing.objects.create(
            owner=self.user, title='Other', content='x',
        )
        other_run = Run.objects.create(briefing=other_briefing, status='running')
        AgentRun.objects.create(
            run=other_run, agent_name='other-agent', status='running',
        )

        result = self.check_reports.invoke({'wait_seconds': 1})
        data = json.loads(result)
        self.assertEqual(data['agents'], [])


class ReadReportToolTests(TestCase):
    """Tests for the read_report coordinator tool."""

    def setUp(self):
        from .agent_tools import make_read_report

        self.user = User.objects.create_user(username='readuser', password='testpass123')
        self.briefing = Briefing.objects.create(
            owner=self.user, title='Read Test', content='test',
        )
        self.run = Run.objects.create(briefing=self.briefing, status='running')
        self.comms_base = settings.BASE_DIR / 'comms'
        self.read_report = make_read_report(self.run)

    def tearDown(self):
        import shutil
        if self.comms_base.is_dir():
            shutil.rmtree(self.comms_base, ignore_errors=True)

    def test_read_report_not_found(self):
        result = self.read_report.invoke({'agent_run_id': 99999})
        self.assertIn('not found', result)

    def test_read_report_already_completed(self):
        agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='completed',
            report='Already stored report.',
        )
        result = self.read_report.invoke({'agent_run_id': agent_run.id})
        self.assertEqual(result, 'Already stored report.')

    def test_read_report_already_failed(self):
        agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='failed',
            error_message='Already stored error.',
        )
        result = self.read_report.invoke({'agent_run_id': agent_run.id})
        self.assertIn('Already stored error', result)

    def test_read_report_from_filesystem(self):
        import json

        agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='running',
        )

        comms_dir = self.comms_base / str(agent_run.id)
        comms_dir.mkdir(parents=True, exist_ok=True)
        (comms_dir / 'report.json').write_text(json.dumps({
            'agent_name': 'test-agent',
            'status': 'completed',
            'report': 'Filesystem report content.',
        }))

        result = self.read_report.invoke({'agent_run_id': agent_run.id})
        self.assertEqual(result, 'Filesystem report content.')

        agent_run.refresh_from_db()
        self.assertEqual(agent_run.status, 'completed')
        self.assertEqual(agent_run.report, 'Filesystem report content.')
        self.assertIsNotNone(agent_run.completed_at)

    def test_read_report_error_from_filesystem(self):
        import json

        agent_run = AgentRun.objects.create(
            run=self.run, agent_name='test-agent', status='running',
        )

        comms_dir = self.comms_base / str(agent_run.id)
        comms_dir.mkdir(parents=True, exist_ok=True)
        (comms_dir / 'error.json').write_text(json.dumps({
            'status': 'failed',
            'error': 'Agent timeout.',
        }))

        result = self.read_report.invoke({'agent_run_id': agent_run.id})
        self.assertIn('failed', result)
        self.assertIn('Agent timeout', result)

        agent_run.refresh_from_db()
        self.assertEqual(agent_run.status, 'failed')

    def test_read_report_rejects_agent_run_from_other_run(self):
        """Closure-bound: a coordinator cannot read reports from other runs,
        even with a valid (but foreign) agent_run_id."""
        other_briefing = Briefing.objects.create(
            owner=self.user, title='Other', content='x',
        )
        other_run = Run.objects.create(briefing=other_briefing, status='running')
        foreign_ar = AgentRun.objects.create(
            run=other_run, agent_name='other-agent', status='completed',
            report='Should not be readable.',
        )

        result = self.read_report.invoke({'agent_run_id': foreign_ar.id})
        self.assertIn('not found in the current run', result)


def docker_image_not_found():
    """Helper to create a docker ImageNotFound side effect."""
    import docker.errors
    return docker.errors.ImageNotFound('not found')


class SchedulingTests(TestCase):
    """Tests for briefing scheduling (NL→cron, PeriodicTask sync)."""

    def setUp(self):
        self.user = User.objects.create_user('scheduser', password='pass')
        self.briefing = Briefing.objects.create(
            owner=self.user,
            title='Scheduled Briefing',
            content='Do things on schedule.',
            schedule_text='every weekday at 9am',
            is_active=True,
        )
        self.mock_cron = MagicMock()
        self.mock_cron.minute = '0'
        self.mock_cron.hour = '9'
        self.mock_cron.day_of_week = '1-5'
        self.mock_cron.day_of_month = '*'
        self.mock_cron.month_of_year = '*'
        self.mock_cron.human_readable = 'Weekdays at 9:00 AM'

    def tearDown(self):
        from django_celery_beat.models import PeriodicTask
        PeriodicTask.objects.filter(name__startswith='briefing-').delete()

    @patch('core.scheduling.parse_schedule_text')
    def test_sync_schedule_creates_periodic_task(self, mock_parse):
        from django_celery_beat.models import PeriodicTask
        from core.scheduling import sync_schedule

        mock_parse.return_value = self.mock_cron
        result = sync_schedule(self.briefing)

        self.assertEqual(result['action'], 'created')
        pt = PeriodicTask.objects.get(name=f'briefing-{self.briefing.id}')
        self.assertEqual(pt.task, 'core.tasks.launch_briefing_scheduled')
        self.assertTrue(pt.enabled)
        self.assertEqual(pt.crontab.minute, '0')
        self.assertEqual(pt.crontab.hour, '9')
        self.assertEqual(pt.crontab.day_of_week, '1-5')

    @patch('core.scheduling.parse_schedule_text')
    def test_sync_schedule_updates_on_change(self, mock_parse):
        from django_celery_beat.models import PeriodicTask
        from core.scheduling import sync_schedule

        mock_parse.return_value = self.mock_cron
        sync_schedule(self.briefing)

        # Change schedule text
        self.briefing.schedule_text = 'daily at midnight'
        self.briefing.save()
        self.mock_cron.minute = '0'
        self.mock_cron.hour = '0'
        self.mock_cron.day_of_week = '*'
        self.mock_cron.human_readable = 'Daily at midnight'
        result = sync_schedule(self.briefing)

        self.assertEqual(result['action'], 'updated')
        self.assertEqual(PeriodicTask.objects.filter(name__startswith='briefing-').count(), 1)

    @patch('core.scheduling.parse_schedule_text')
    def test_sync_schedule_deletes_when_cleared(self, mock_parse):
        from django_celery_beat.models import PeriodicTask
        from core.scheduling import sync_schedule

        mock_parse.return_value = self.mock_cron
        sync_schedule(self.briefing)

        self.briefing.schedule_text = ''
        self.briefing.save()
        result = sync_schedule(self.briefing)

        self.assertEqual(result['action'], 'removed')
        self.assertFalse(PeriodicTask.objects.filter(name=f'briefing-{self.briefing.id}').exists())

    @patch('core.scheduling.parse_schedule_text')
    def test_sync_schedule_pauses_when_inactive(self, mock_parse):
        from django_celery_beat.models import PeriodicTask
        from core.scheduling import sync_schedule

        mock_parse.return_value = self.mock_cron
        sync_schedule(self.briefing)

        self.briefing.is_active = False
        self.briefing.save()
        result = sync_schedule(self.briefing)

        self.assertEqual(result['action'], 'paused')
        pt = PeriodicTask.objects.get(name=f'briefing-{self.briefing.id}')
        self.assertFalse(pt.enabled)

    @patch('core.scheduling.parse_schedule_text')
    def test_sync_schedule_resumes_without_llm_call(self, mock_parse):
        from django_celery_beat.models import PeriodicTask
        from core.scheduling import sync_schedule

        mock_parse.return_value = self.mock_cron
        sync_schedule(self.briefing)

        # Pause
        self.briefing.is_active = False
        self.briefing.save()
        sync_schedule(self.briefing)

        # Resume
        self.briefing.is_active = True
        self.briefing.save()
        mock_parse.reset_mock()
        result = sync_schedule(self.briefing)

        self.assertEqual(result['action'], 'resumed')
        mock_parse.assert_not_called()  # No LLM call needed
        pt = PeriodicTask.objects.get(name=f'briefing-{self.briefing.id}')
        self.assertTrue(pt.enabled)

    @patch('core.tasks.launch_coordinator')
    def test_launch_briefing_scheduled_creates_run(self, mock_launch):
        from core.tasks import launch_briefing_scheduled

        mock_launch.delay = MagicMock()
        launch_briefing_scheduled(self.briefing.id)

        run = Run.objects.filter(briefing=self.briefing).latest('created_at')
        self.assertEqual(run.triggered_by, 'scheduled')
        self.assertEqual(run.status, 'pending')
        mock_launch.delay.assert_called_once_with(run.id)

    @patch('core.tasks.launch_coordinator')
    def test_launch_briefing_scheduled_skips_inactive(self, mock_launch):
        from core.tasks import launch_briefing_scheduled

        self.briefing.is_active = False
        self.briefing.save()
        mock_launch.delay = MagicMock()
        launch_briefing_scheduled(self.briefing.id)

        self.assertFalse(Run.objects.filter(briefing=self.briefing).exists())
        mock_launch.delay.assert_not_called()

    @patch('core.tasks.launch_coordinator')
    def test_launch_briefing_scheduled_skips_if_running(self, mock_launch):
        from core.tasks import launch_briefing_scheduled

        Run.objects.create(briefing=self.briefing, status='running', triggered_by='manual')
        mock_launch.delay = MagicMock()
        launch_briefing_scheduled(self.briefing.id)

        # Should not create a second run
        self.assertEqual(Run.objects.filter(briefing=self.briefing).count(), 1)
        mock_launch.delay.assert_not_called()

    @patch('core.scheduling.parse_schedule_text')
    def test_schedule_status_detects_stale(self, mock_parse):
        from core.scheduling import get_schedule_status, sync_schedule

        mock_parse.return_value = self.mock_cron
        sync_schedule(self.briefing)

        # Change schedule_text without re-scheduling
        self.briefing.schedule_text = 'daily at noon'
        self.briefing.save()
        status = get_schedule_status(self.briefing)

        self.assertIsNotNone(status)
        self.assertTrue(status['stale'])

    @patch('core.scheduling.parse_schedule_text')
    def test_briefing_delete_cleans_up_schedule(self, mock_parse):
        from django_celery_beat.models import PeriodicTask
        from core.scheduling import sync_schedule

        mock_parse.return_value = self.mock_cron
        sync_schedule(self.briefing)

        briefing_id = self.briefing.id
        self.briefing.delete()

        self.assertFalse(PeriodicTask.objects.filter(name=f'briefing-{briefing_id}').exists())


class LaunchRunHelperTests(TestCase):
    """Tests for the launch_run helper that captures Celery task IDs."""

    def setUp(self):
        self.user = User.objects.create_user(username='launchuser', password='testpass123')
        self.briefing = Briefing.objects.create(
            owner=self.user, title='Launch Test', content='test',
        )

    @patch('core.tasks.launch_coordinator.delay')
    def test_launch_run_stores_task_id(self, mock_delay):
        from core.tasks import launch_run

        mock_async = MagicMock()
        mock_async.id = 'task-abc-123'
        mock_delay.return_value = mock_async

        run = Run.objects.create(briefing=self.briefing, status='pending')
        launch_run(run)

        run.refresh_from_db()
        self.assertEqual(run.celery_task_id, 'task-abc-123')
        mock_delay.assert_called_once_with(run.id)


class LaunchCoordinatorCancellationTests(TestCase):
    """Tests that launch_coordinator respects a mid-flight cancellation."""

    def setUp(self):
        self.user = User.objects.create_user(username='canceluser', password='testpass123')
        self.briefing = Briefing.objects.create(
            owner=self.user, title='Cancel Test', content='test',
        )

    @patch('core.containers.cleanup_run')
    @patch('core.tasks.run_coordinator')
    def test_coordinator_success_does_not_overwrite_cancelled_run(
        self, mock_run_coordinator, mock_cleanup,
    ):
        """If the run is cancelled mid-flight, a successful coordinator return
        must not flip status back to 'completed'."""
        from core.tasks import launch_coordinator

        run = Run.objects.create(
            briefing=self.briefing, status='pending', triggered_by='manual',
        )

        def cancel_mid_flight(*args, **kwargs):
            Run.objects.filter(pk=run.id).update(
                status='failed',
                error_message='Cancelled by user.',
                completed_at=timezone.now(),
            )
            return 'coordinator finished successfully'

        mock_run_coordinator.side_effect = cancel_mid_flight

        launch_coordinator(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.error_message, 'Cancelled by user.')

    @patch('core.containers.cleanup_run')
    @patch('core.tasks.run_coordinator')
    def test_coordinator_exception_does_not_overwrite_cancellation_reason(
        self, mock_run_coordinator, mock_cleanup,
    ):
        """If the run was already cancelled, a coordinator exception must not
        overwrite the cancellation error_message with the crash message."""
        from core.tasks import launch_coordinator

        run = Run.objects.create(
            briefing=self.briefing, status='pending', triggered_by='manual',
        )

        def cancel_then_crash(*args, **kwargs):
            Run.objects.filter(pk=run.id).update(
                status='failed',
                error_message='Cancelled by user.',
                completed_at=timezone.now(),
            )
            raise RuntimeError('coordinator crashed after cancellation')

        mock_run_coordinator.side_effect = cancel_then_crash

        # Should not raise — the task swallows the exception when the run
        # is already in a terminal state due to cancellation.
        launch_coordinator(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.error_message, 'Cancelled by user.')


class BoardViewTests(TestCase):
    """Tests for the Redis-only message board views."""

    def setUp(self):
        self.user = User.objects.create_user('boarduser', password='testpass123')
        self.client.login(username='boarduser', password='testpass123')
        self.briefing = Briefing.objects.create(
            owner=self.user, title='Board Test', content='test',
        )
        self.run = Run.objects.create(
            briefing=self.briefing, status='running',
        )

    def _mock_redis(self):
        """Return a MagicMock configured as a Redis client."""
        return MagicMock()

    @patch('core.views._get_board_redis')
    def test_board_messages_empty(self, mock_get_redis):
        mock_r = self._mock_redis()
        mock_r.xrevrange.return_value = []
        mock_get_redis.return_value = mock_r

        resp = self.client.get(f'/runs/{self.run.pk}/board/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'No messages yet.')

    @patch('core.views._get_board_redis')
    def test_board_messages_shows_messages(self, mock_get_redis):
        mock_r = self._mock_redis()
        mock_r.xrevrange.return_value = [
            ('2-0', {'from': 'human', 'to': 'agent_1', 'content': 'Hi!', 'ts': '2026-03-25T10:01:00+00:00'}),
            ('1-0', {'from': 'agent_1', 'to': 'human', 'content': 'Hello?', 'ts': '2026-03-25T10:00:00+00:00'}),
        ]
        mock_get_redis.return_value = mock_r

        resp = self.client.get(f'/runs/{self.run.pk}/board/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Hello?')
        self.assertContains(resp, 'Hi!')

    @patch('core.views._get_board_redis')
    def test_board_messages_filter_human(self, mock_get_redis):
        mock_r = self._mock_redis()
        mock_r.xrevrange.return_value = [
            ('2-0', {'from': 'agent_1', 'to': 'human', 'content': 'question', 'ts': '2026-03-25T10:01:00+00:00'}),
            ('1-0', {'from': 'agent_1', 'to': 'all', 'content': 'broadcast', 'ts': '2026-03-25T10:00:00+00:00'}),
        ]
        mock_get_redis.return_value = mock_r

        resp = self.client.get(f'/runs/{self.run.pk}/board/?filter=human')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'question')
        self.assertNotContains(resp, 'broadcast')

    @patch('core.views._get_board_redis')
    def test_board_messages_404_wrong_user(self, mock_get_redis):
        other_user = User.objects.create_user('other', password='pass')
        other_briefing = Briefing.objects.create(owner=other_user, title='X', content='x')
        other_run = Run.objects.create(briefing=other_briefing, status='running')

        resp = self.client.get(f'/runs/{other_run.pk}/board/')
        self.assertEqual(resp.status_code, 404)

    @patch('core.views._get_board_redis')
    def test_board_reply_posts_to_redis(self, mock_get_redis):
        mock_r = self._mock_redis()
        # Return empty for the subsequent board_messages call
        mock_r.xrevrange.return_value = []
        mock_get_redis.return_value = mock_r

        resp = self.client.post(
            f'/runs/{self.run.pk}/board/reply/',
            {'message': '@agent_1 the answer is 42'},
        )
        self.assertEqual(resp.status_code, 200)

        # Verify XADD was called with correct data
        mock_r.xadd.assert_called_once()
        call_args = mock_r.xadd.call_args
        self.assertEqual(call_args[0][0], f'fuzzyclaw:board:{self.run.id}')
        data = call_args[0][1]
        self.assertEqual(data['from'], 'human')
        self.assertEqual(data['to'], 'agent_1')
        self.assertEqual(data['content'], 'the answer is 42')

    @patch('core.views._get_board_redis')
    def test_board_reply_defaults_to_coordinator(self, mock_get_redis):
        mock_r = self._mock_redis()
        mock_r.xrevrange.return_value = []
        mock_get_redis.return_value = mock_r

        resp = self.client.post(
            f'/runs/{self.run.pk}/board/reply/',
            {'message': 'hello everyone'},
        )
        self.assertEqual(resp.status_code, 200)
        mock_r.xadd.assert_called_once()
        data = mock_r.xadd.call_args[0][1]
        self.assertEqual(data['to'], f'coordinator_{self.run.id}')

    @patch('core.views._get_board_redis')
    def test_board_reply_multiple_mentions(self, mock_get_redis):
        mock_r = self._mock_redis()
        mock_r.xrevrange.return_value = []
        mock_get_redis.return_value = mock_r

        resp = self.client.post(
            f'/runs/{self.run.pk}/board/reply/',
            {'message': '@agent_1 @agent_2 hello both'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_r.xadd.call_count, 2)
        recipients = [call[0][1]['to'] for call in mock_r.xadd.call_args_list]
        self.assertEqual(sorted(recipients), ['agent_1', 'agent_2'])
        for call in mock_r.xadd.call_args_list:
            self.assertEqual(call[0][1]['content'], 'hello both')

    @patch('core.views._get_board_redis')
    def test_board_reply_empty_returns_400(self, mock_get_redis):
        resp = self.client.post(
            f'/runs/{self.run.pk}/board/reply/',
            {'message': ''},
        )
        self.assertEqual(resp.status_code, 400)

    @patch('core.views._get_board_redis')
    def test_board_reply_mention_only_returns_400(self, mock_get_redis):
        """@agent_1 with no message body should be rejected."""
        resp = self.client.post(
            f'/runs/{self.run.pk}/board/reply/',
            {'message': '@agent_1'},
        )
        self.assertEqual(resp.status_code, 400)

    @patch('core.views._get_board_redis')
    def test_board_reply_redis_failure_returns_502(self, mock_get_redis):
        mock_r = self._mock_redis()
        mock_r.xadd.side_effect = Exception("Redis connection lost")
        mock_get_redis.return_value = mock_r

        resp = self.client.post(
            f'/runs/{self.run.pk}/board/reply/',
            {'message': '@agent_1 hello'},
        )
        self.assertEqual(resp.status_code, 502)
        self.assertContains(resp, 'Failed to send message', status_code=502)

    @patch('core.views._get_board_redis')
    def test_board_badge_counts_runs_with_messages(self, mock_get_redis):
        mock_r = self._mock_redis()
        mock_r.xlen.return_value = 3  # stream has messages
        mock_get_redis.return_value = mock_r

        resp = self.client.get('/board/badge/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '1')  # 1 run with board messages

    @patch('core.views._get_board_redis')
    def test_board_badge_empty_when_no_messages(self, mock_get_redis):
        mock_r = self._mock_redis()
        mock_r.xlen.return_value = 0
        mock_get_redis.return_value = mock_r

        resp = self.client.get('/board/badge/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'bg-red-500')

    @patch('core.views._get_board_redis')
    def test_board_active_runs_json(self, mock_get_redis):
        import json

        mock_r = self._mock_redis()
        mock_r.xlen.return_value = 5  # stream has messages
        mock_get_redis.return_value = mock_r

        resp = self.client.get('/board/active-runs/')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['id'], self.run.id)
        self.assertEqual(data[0]['title'], 'Board Test')

    @patch('core.views._get_board_redis')
    def test_board_active_runs_excludes_empty_streams(self, mock_get_redis):
        import json

        mock_r = self._mock_redis()
        mock_r.xlen.return_value = 0
        mock_get_redis.return_value = mock_r

        resp = self.client.get('/board/active-runs/')
        data = json.loads(resp.content)
        self.assertEqual(data, [])

    @patch('core.views._get_board_redis')
    def test_board_participants(self, mock_get_redis):
        mock_r = self._mock_redis()
        mock_r.smembers.return_value = {'agent_1', 'agent_2'}
        mock_get_redis.return_value = mock_r

        AgentRun.objects.create(
            run=self.run, agent_name='agent', status='running',
        )

        resp = self.client.get(f'/runs/{self.run.pk}/board/participants/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '@agent_1')
        self.assertContains(resp, '@agent_2')

    def test_board_views_require_login(self):
        self.client.logout()
        urls = [
            f'/runs/{self.run.pk}/board/',
            f'/runs/{self.run.pk}/board/reply/',
            f'/runs/{self.run.pk}/board/participants/',
            '/board/badge/',
            '/board/active-runs/',
        ]
        for url in urls:
            resp = self.client.get(url)
            self.assertIn(resp.status_code, [302, 405], msg=f"{url} should redirect or deny")


class MarkdownSanitizationTests(TestCase):
    """Regression tests: markdown rendering must strip dangerous HTML."""

    def test_script_tag_stripped(self):
        from .templatetags.markdown_extras import render_markdown
        result = render_markdown("<script>alert('xss')</script>Hello")
        self.assertNotIn('<script>', result)
        self.assertIn('Hello', result)

    def test_event_handler_stripped(self):
        from .templatetags.markdown_extras import render_markdown
        result = render_markdown('<img src=x onerror="alert(1)">')
        self.assertNotIn('onerror', result)

    def test_normal_markdown_renders(self):
        from .templatetags.markdown_extras import render_markdown
        result = render_markdown("# Hello\n\n**bold** text")
        self.assertIn('<h1>', result)
        self.assertIn('<strong>', result)

    def test_link_preserved(self):
        from .templatetags.markdown_extras import render_markdown
        result = render_markdown("[link](https://example.com)")
        self.assertIn('href="https://example.com"', result)

    def test_javascript_href_stripped(self):
        from .templatetags.markdown_extras import render_markdown
        result = render_markdown('<a href="javascript:alert(1)">click</a>')
        self.assertNotIn('javascript:', result)

    def test_empty_input(self):
        from .templatetags.markdown_extras import render_markdown
        self.assertEqual(render_markdown(''), '')
        self.assertEqual(render_markdown(None), '')


class APIIsolationTests(TestCase):
    """Regression tests: users must not access other users' data via the API."""

    def setUp(self):
        self.user_a = User.objects.create_user('alice', password='pass')
        self.user_b = User.objects.create_user('bob', password='pass')
        self.token_a = Token.objects.create(user=self.user_a)
        self.token_b = Token.objects.create(user=self.user_b)

        self.client_a = APIClient()
        self.client_a.credentials(HTTP_AUTHORIZATION=f'Token {self.token_a.key}')
        self.client_b = APIClient()
        self.client_b.credentials(HTTP_AUTHORIZATION=f'Token {self.token_b.key}')

        self.briefing_a = Briefing.objects.create(owner=self.user_a, title='A brief', content='a')
        self.briefing_b = Briefing.objects.create(owner=self.user_b, title='B brief', content='b')
        self.run_a = Run.objects.create(briefing=self.briefing_a, status='completed')
        self.run_b = Run.objects.create(briefing=self.briefing_b, status='completed')
        self.ar_a = AgentRun.objects.create(run=self.run_a, agent_name='agent-a', status='completed')
        self.ar_b = AgentRun.objects.create(run=self.run_b, agent_name='agent-b', status='completed')

    def test_user_cannot_list_other_users_briefings(self):
        resp = self.client_a.get('/api/briefings/')
        self.assertEqual(resp.status_code, 200)
        ids = [b['id'] for b in resp.data['results']]
        self.assertIn(self.briefing_a.id, ids)
        self.assertNotIn(self.briefing_b.id, ids)

    def test_user_cannot_retrieve_other_users_briefing(self):
        resp = self.client_a.get(f'/api/briefings/{self.briefing_b.id}/')
        self.assertEqual(resp.status_code, 404)

    def test_user_cannot_update_other_users_briefing(self):
        resp = self.client_a.patch(f'/api/briefings/{self.briefing_b.id}/', {'title': 'hacked'})
        self.assertEqual(resp.status_code, 404)

    def test_user_cannot_delete_other_users_briefing(self):
        resp = self.client_a.delete(f'/api/briefings/{self.briefing_b.id}/')
        self.assertEqual(resp.status_code, 404)

    def test_user_cannot_list_other_users_runs(self):
        resp = self.client_a.get('/api/runs/')
        ids = [r['id'] for r in resp.data['results']]
        self.assertIn(self.run_a.id, ids)
        self.assertNotIn(self.run_b.id, ids)

    def test_user_cannot_retrieve_other_users_run(self):
        resp = self.client_a.get(f'/api/runs/{self.run_b.id}/')
        self.assertEqual(resp.status_code, 404)

    def test_user_cannot_list_other_users_agent_runs(self):
        resp = self.client_a.get('/api/agent-runs/')
        ids = [ar['id'] for ar in resp.data['results']]
        self.assertIn(self.ar_a.id, ids)
        self.assertNotIn(self.ar_b.id, ids)

    def test_user_cannot_retrieve_other_users_agent_run(self):
        resp = self.client_a.get(f'/api/agent-runs/{self.ar_b.id}/')
        self.assertEqual(resp.status_code, 404)

    def test_pending_runs_only_returns_own(self):
        self.run_a.status = 'pending'
        self.run_a.save()
        self.run_b.status = 'pending'
        self.run_b.save()
        resp = self.client_a.get('/api/runs/pending/')
        ids = [r['id'] for r in resp.data]
        self.assertIn(self.run_a.id, ids)
        self.assertNotIn(self.run_b.id, ids)

    def test_user_cannot_launch_other_users_briefing(self):
        """User A cannot launch user B's briefing — cross-user isolation
        applies to the launch action too."""
        resp = self.client_a.post(f'/api/briefings/{self.briefing_b.id}/launch/')
        self.assertEqual(resp.status_code, 404)

    @patch('core.tasks.launch_coordinator.delay')
    def test_user_can_launch_own_briefing(self, mock_delay):
        resp = self.client_a.post(f'/api/briefings/{self.briefing_a.id}/launch/')
        self.assertEqual(resp.status_code, 201)
        mock_delay.assert_called_once()

    def test_user_cannot_cancel_other_users_run(self):
        self.run_b.status = 'running'
        self.run_b.save()
        resp = self.client_a.post(f'/api/runs/{self.run_b.id}/cancel/')
        self.assertEqual(resp.status_code, 404)

    def test_user_cannot_annotate_other_users_run(self):
        resp = self.client_a.patch(f'/api/runs/{self.run_b.id}/notes/', {
            'user_notes': 'hostile',
        }, format='json')
        self.assertEqual(resp.status_code, 404)
