"""The web controller's settings API.

Everything here is reachable from a phone, so bad values must be refused rather
than passed on to the detector, which would fail to start or misbehave.
"""
import json
import os
import unittest
from unittest import mock

import fakes


class ClampingTests(fakes.WebControllerTestCase):
    def post(self, body):
        return self.client.post('/api/config', json=body).get_json()

    def test_zero_and_negative_values_are_clamped_to_something_usable(self):
        # alert=0 would fire an alert on every audio chunk
        self.assertEqual(self.post({'alert': 0})['alert'], 1)
        self.assertEqual(self.post({'reset': 0})['reset'], 1)
        self.assertEqual(self.post({'min_cry': 0})['min_cry'], 1)
        self.assertEqual(self.post({'silence_gap': 0})['silence_gap'], 1)
        self.assertEqual(self.post({'volume': -50})['volume'], 50)
        self.assertEqual(self.post({'heartbeat': 0})['heartbeat'], 1)

    def test_absurdly_high_values_are_clamped(self):
        self.assertEqual(self.post({'alert': 9999})['alert'], 240)
        self.assertEqual(self.post({'cry_freq_min': 9000})['cry_freq_min'], 1900)
        self.assertEqual(self.post({'status_port': 80})['status_port'], 1024)

    def test_sensible_values_pass_through_untouched(self):
        body = {'alert': 12, 'reset': 5, 'min_cry': 4, 'silence_gap': 2,
                'volume': 900, 'cry_freq_min': 400, 'heartbeat': 5}
        self.assertEqual({k: self.post(body)[k] for k in body}, body)

    def test_uncoercible_values_are_rejected_and_change_nothing(self):
        before = dict(self.wc.current_config)
        self.post({'alert': 'abc', 'record': 'yes', 'volume': None, 'nonsense': 1})
        self.assertEqual(dict(self.wc.current_config), before)

    def test_booleans_must_be_booleans(self):
        self.assertTrue(self.post({'record': True})['record'])
        self.assertFalse(self.post({'record': False})['record'])
        self.assertFalse(self.post({'record': 1})['record'],
                         "an int must not be accepted as a bool")

    def test_cry_freq_min_stays_below_the_detector_ceiling(self):
        from fakes import cd
        self.assertLess(self.post({'cry_freq_min': 5000})['cry_freq_min'],
                        cd.CRY_FREQ_MAX)


class StopAtTests(fakes.WebControllerTestCase):
    def post(self, value):
        return self.client.post('/api/config', json={'stop_at': value}).get_json()['stop_at']

    def test_valid_times_are_accepted_and_normalised(self):
        self.assertEqual(self.post('23:59'), '23:59')
        self.assertEqual(self.post('7:5'), '07:05')

    def test_malformed_times_are_refused(self):
        # a bad value here makes the detector exit(1) at startup
        for bad in ['7am', '99:99', '25:00', '', '07:00:00', 'seven', None]:
            self.client.post('/api/config', json={'stop_at': '06:00'})
            self.assertEqual(self.post(bad), '06:00', f"{bad!r} should be refused")


class DeviceTests(fakes.WebControllerTestCase):
    def test_every_known_device_is_accepted(self):
        for device in self.wc.DEVICE_LABELS:
            response = self.client.post('/api/device', json={'pushover_device': device})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.wc.current_config['pushover_device'], device)

    def test_unknown_devices_are_refused_and_nothing_changes(self):
        self.client.post('/api/device', json={'pushover_device': 'a_phone'})
        for bad in ["'; alert(1); //", 'grandma', '', 'h_phone ', '__MUTED__', 42, None]:
            response = self.client.post('/api/device', json={'pushover_device': bad})
            self.assertEqual(response.status_code, 400, f"{bad!r} should be refused")
            self.assertEqual(self.wc.current_config['pushover_device'], 'a_phone')

    def test_unhashable_values_do_not_crash_the_endpoint(self):
        for bad in [[], {}, {'a': 1}, [1, 2]]:
            response = self.client.post('/api/device', json={'pushover_device': bad})
            self.assertEqual(response.status_code, 400)

    def test_unhashable_devices_via_the_config_endpoint_are_refused(self):
        # dict membership raises TypeError on a list, which used to be a 500
        self.client.post('/api/device', json={'pushover_device': 'a_phone'})
        for bad in [[], {}, {'a': 1}]:
            response = self.client.post('/api/config', json={'pushover_device': bad})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.wc.current_config['pushover_device'], 'a_phone')

    def test_the_config_endpoint_enforces_the_same_whitelist(self):
        self.client.post('/api/device', json={'pushover_device': 'a_phone'})
        body = self.client.post('/api/config', json={'pushover_device': 'nope'}).get_json()
        self.assertEqual(body['pushover_device'], 'a_phone')

    def test_switching_works_while_stopped(self):
        # the detector picks it up on next start; this used to be refused outright
        self.assertFalse(self.wc.is_running())
        response = self.client.post('/api/device', json={'pushover_device': 'grandma_phone'})
        self.assertTrue(response.get_json()['success'])
        self.assertEqual(self.wc.current_config['pushover_device'], 'grandma_phone')

    def test_mute_is_recorded_as_an_event(self):
        self.client.post('/api/device', json={'pushover_device': '__muted__'})
        self.assertIn('Config: Pushover alerts MUTED', self.events())


class SanitizerUnitTests(fakes.WebControllerTestCase):
    """The sanitizer is also called with data straight off disk, so it defends
    itself rather than relying on the endpoints' checks."""

    def test_a_non_dict_patch_is_ignored_rather_than_raising(self):
        for patch in [[1, 2, 3], 'hello', 42, None]:
            clean, rejected = self.wc.sanitize_config(patch)
            self.assertEqual(clean, {})
            self.assertEqual(rejected, [])

    def test_unknown_keys_are_reported_as_rejected(self):
        clean, rejected = self.wc.sanitize_config({'nonsense': 1, 'alert': 12})
        self.assertEqual(clean, {'alert': 12})
        self.assertEqual(rejected, ['nonsense'])


class MalformedBodyTests(fakes.WebControllerTestCase):
    def test_non_object_bodies_are_refused_not_crashed(self):
        for raw in ['[1,2,3]', '"hello"', '42']:
            for path in ['/api/config', '/api/start']:
                response = self.client.post(path, data=raw,
                                            content_type='application/json')
                self.assertEqual(response.status_code, 400, f"{path} {raw}")


class PersistenceTests(fakes.WebControllerTestCase):
    def test_changes_are_written_to_the_state_file(self):
        self.client.post('/api/config', json={'alert': 20, 'record': True})
        self.client.post('/api/device', json={'pushover_device': 'grandma_phone'})
        with open(self.wc.CONFIG_STATE_FILE) as handle:
            saved = json.load(handle)
        self.assertEqual(saved['alert'], 20)
        self.assertTrue(saved['record'])
        self.assertEqual(saved['pushover_device'], 'grandma_phone')

    def test_settings_are_restored_on_load(self):
        with open(self.wc.CONFIG_STATE_FILE, 'w') as handle:
            json.dump({'alert': 18, 'pushover_device': 'h_phone'}, handle)
        self.wc.current_config.update(self.wc.DEFAULT_CONFIG)
        self.wc.load_config()
        self.assertEqual(self.wc.current_config['alert'], 18)
        self.assertEqual(self.wc.current_config['pushover_device'], 'h_phone')

    def test_a_corrupt_state_file_falls_back_to_defaults(self):
        with open(self.wc.CONFIG_STATE_FILE, 'w') as handle:
            handle.write('not json at all{{{')
        with fakes.captured_output():
            self.wc.load_config()
        self.assertEqual(self.wc.current_config['alert'], self.wc.DEFAULT_CONFIG['alert'])

    def test_a_state_file_holding_a_list_is_ignored(self):
        with open(self.wc.CONFIG_STATE_FILE, 'w') as handle:
            json.dump([1, 2, 3], handle)
        with fakes.captured_output():
            self.wc.load_config()
        self.assertEqual(self.wc.current_config['alert'], self.wc.DEFAULT_CONFIG['alert'])

    def test_bad_saved_values_are_cleaned_up_on_load(self):
        with open(self.wc.CONFIG_STATE_FILE, 'w') as handle:
            json.dump({'alert': 0, 'min_cry': -3, 'volume': 'abc',
                       'pushover_device': 'evil'}, handle)
        with fakes.captured_output():
            self.wc.load_config()
        self.assertEqual(self.wc.current_config['alert'], 1, "clamped")
        self.assertEqual(self.wc.current_config['min_cry'], 1, "clamped")
        self.assertEqual(self.wc.current_config['volume'],
                         self.wc.DEFAULT_CONFIG['volume'], "rejected")
        self.assertEqual(self.wc.current_config['pushover_device'],
                         self.wc.DEFAULT_CONFIG['pushover_device'], "rejected")

    def test_a_no_op_change_does_not_rewrite_the_file(self):
        # counting the atomic replace beats comparing mtimes, which can tie
        with mock.patch('os.replace', wraps=os.replace) as replace:
            self.client.post('/api/config', json={'alert': 15})
            self.assertEqual(replace.call_count, 1)
            self.client.post('/api/config', json={'alert': 15})
            self.assertEqual(replace.call_count, 1,
                             "an unchanged config should not hit the SD card")
            self.client.post('/api/config', json={'alert': 16})
            self.assertEqual(replace.call_count, 2)


class EventLogTests(fakes.WebControllerTestCase):
    def test_config_changes_are_described_in_the_event_log(self):
        self.client.post('/api/config', json={'alert': 20, 'record': True})
        events = self.events()
        self.assertIn('Config: Alert window changed 12 min -> 20 min', events)
        self.assertIn('Config: Recording ENABLED', events)

    def test_changes_while_stopped_reach_the_persistent_log(self):
        # the per-run detector log is moved to USB on stop, so these used to vanish
        self.client.post('/api/config', json={'alert': 20})
        self.assertTrue(any('Alert window' in line for line in self.controller_log()))


if __name__ == '__main__':
    unittest.main()
