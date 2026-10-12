import unittest
import yaml
from scripts.aic_healthcheck_cadence import change_service


class HealthCadenceTests(unittest.TestCase):
    def test_preserves_failure_budget_other_services_and_is_idempotent(self):
        for interval, retries in [('10s', 2), ('15s', 3)]:
            source = '''services:
  app:
    image: ${APP_IMAGE:?Required}
    healthcheck:
      test: [CMD, php, artisan, octane:status]
      interval: INTERVAL
      timeout: 5s
      retries: 6
    command: [php, artisan, octane:start]
  db:
    image: postgres:16
'''.replace('INTERVAL', interval)
            result = change_service(source, 'app')
            config = yaml.safe_load(result)
            self.assertEqual(config['services']['app']['healthcheck']['retries'], retries)
            self.assertEqual(config['services']['app']['healthcheck']['start_interval'], '5s')
            self.assertEqual(config['services']['db'], {'image': 'postgres:16'})
            self.assertEqual(config['services']['app']['command'], ['php', 'artisan', 'octane:start'])
            self.assertEqual(result, change_service(result, 'app'))
