import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import validate


class ValidationRunnerTests(unittest.TestCase):
    def test_missing_tools_fail_before_running_tests(self):
        with patch.object(validate.shutil, 'which', return_value=None), \
                patch.object(validate.subprocess, 'run') as run:
            with self.assertRaisesRegex(SystemExit, 'Missing test tools: age'):
                validate.main()
            run.assert_not_called()

    def test_components_run_in_separate_processes_and_git_is_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('one/test_a.py', 'one/test_b.py', 'two/test_c.py', '.git/test_ignore.py'):
                path = root / name
                path.parent.mkdir(exist_ok=True)
                path.touch()
            with patch.object(validate, 'ROOT', root), \
                    patch.object(validate.shutil, 'which', return_value='/bin/tool'), \
                    patch.object(validate.subprocess, 'run') as run:
                validate.main()
            self.assertEqual(run.call_count, 3)
            for call, name in zip(run.call_args_list[:2], ('one', 'two')):
                self.assertIn(str(root / name), call.args[0])
                self.assertTrue(call.kwargs['check'])
                self.assertEqual(call.kwargs['env']['PYTHONDONTWRITEBYTECODE'], '1')
            self.assertEqual(run.call_args_list[-1].args[0][:2], ['node', '--test'])
