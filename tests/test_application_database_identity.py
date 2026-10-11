import io
import json
import types
import unittest
from unittest.mock import Mock, patch

import application_database_identity as identity
import database_inspector_entry as entry
from database_mysql import DatabaseError


class ApplicationIdentityTests(unittest.TestCase):
    def request(self, **changes):
        return dict(action='application-identity', engine='mysql', database='business',
                    user='application', password='private/secret', port=3306, **changes)

    def connection(self, rows):
        cursor = Mock()
        cursor.fetchmany.return_value = rows
        connection = Mock()
        connection.cursor.return_value = cursor
        return connection, cursor

    def test_writable_application_account_uses_only_fixed_identity_select(self):
        for engine, version in (('mysql', '8.4.11'), ('mariadb', '11.4.13-MariaDB')):
            value = self.request() | {'engine': engine}
            connection, cursor = self.connection([('business', 'application@%', version)])
            result = identity.inspect(value, lambda: connection)
            self.assertEqual(result, dict(engine=engine, database='business', server_version=version, cluster_identity_verified=False))
            cursor.execute.assert_called_once_with('SELECT DATABASE(),CURRENT_USER(),VERSION()')
            cursor.fetchmany.assert_called_once_with(2)
            cursor.close.assert_called_once()
            connection.close.assert_called_once()
            self.assertNotIn('application', json.dumps(result))

    def test_wrong_identity_version_and_shape_fail_closed(self):
        for rows in ([], [('other', 'application@%', '8.4.11')], [('business', 'other@%', '8.4.11')],
                     [('business', 'application@%', '11.4.13-MariaDB')], [('business', 'application@%', '5.7.1')],
                     [('business', 'application@%', '8.4.11\nsecret')], [('business',)],
                     [('business', 'application@%', '8.4.11')] * 2):
            connection, cursor = self.connection(rows)
            with self.subTest(rows=rows), self.assertRaises(DatabaseError):
                identity.inspect(self.request(), lambda: connection)
            connection.close.assert_called_once()

    def test_invalid_contract_never_connects_and_driver_errors_are_sanitized(self):
        for changed in ({'sql': 'DROP TABLE x'}, {'engine': 'postgresql'}, {'port': True},
                        {'database': 'mysql'}, {'user': ''}, {'password': 'x\x00y'}):
            connect = Mock()
            with self.subTest(changed=changed), self.assertRaises(DatabaseError):
                identity.inspect(self.request() | changed, connect)
            connect.assert_not_called()
        with self.assertRaisesRegex(DatabaseError, 'connection_failed') as error:
            identity.inspect(self.request(), Mock(side_effect=RuntimeError('private/secret')))
        self.assertNotIn('private', str(error.exception))

    def test_entry_routes_private_action_and_keeps_socket_bounds(self):
        connection, cursor = self.connection([('business', 'application@%', '8.4.11')])
        driver = types.SimpleNamespace(connect=Mock(return_value=connection))
        output = io.BytesIO()
        with patch.dict('sys.modules', {'pymysql': driver}), patch.object(entry.sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO(json.dumps(self.request()).encode()))), patch.object(entry.sys, 'stdout', types.SimpleNamespace(buffer=output)):
            entry.main()
        self.assertEqual(json.loads(output.getvalue())['result']['engine'], 'mysql')
        kwargs = driver.connect.call_args.kwargs
        self.assertEqual(kwargs['host'], '127.0.0.1')
        self.assertEqual((kwargs['connect_timeout'], kwargs['read_timeout'], kwargs['write_timeout']), (3, 4, 3))
        self.assertFalse(kwargs['local_infile'])
        self.assertEqual(kwargs['client_flag'], 0)


if __name__ == '__main__':
    unittest.main()
