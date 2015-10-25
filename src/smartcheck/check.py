# -*- coding: utf-8 -*-
import yaml
import re

INFORMATION_SECTION_START = '=== START OF INFORMATION SECTION ==='
DATA_SECTION_START = '=== START OF READ SMART DATA SECTION ==='
TESTS_SECTION_START = 'SMART Self-test log structure revision number'

INFORMATION_RE = [
	("model_family", re.compile('Model Family: (.*)', re.UNICODE)),
	("device_model", re.compile("(?:Device Model|Product): (.*)", re.UNICODE)),
	("serial", re.compile("Serial Number: (.*)", re.UNICODE | re.IGNORECASE)),
	("firmware_version", re.compile("Firmware version: (.*)", re.UNICODE)),
	("ata_version", re.compile("ATA Version is: (.*)", re.UNICODE)),
	("sata_version", re.compile("SATA Version is: (.*)", re.UNICODE)),
]

DATA_RE = [
	('overall_health_status', re.compile('SMART overall-health self-assessment test result: (.*)', re.UNICODE)),
]
DATA_ATTRIBUTES_RE = re.compile(r"\s*(\d+)\s+([\w\d_\-]+)\s+([0-9a-fx]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\w\d_\-]+)\s+([\w\d]+)\s+([\w\d_\-]+)\s+(.*)", re.UNICODE)

TEST_RESULT_RE = re.compile(r"#\s*(\d+)\s+(.*?)\s{2,}(.*?)\s{2,}\s+([\d%]+)\s+(\d+)\s+(\d+|-)", re.UNICODE)

class SMARTCheck(object):

	def __init__(self, file_or_stream, db_path=None):
		self.raw = file_or_stream.read()
		self.parsed_sections = None
		self.db_path = db_path
		self._database = None

	@property
	def information(self):
		return self.parsed.get('information', {})

	@property
	def smart_data(self):
		return self.parsed.get('data', {})

	@property
	def self_tests(self):
		return self.parsed.get('self_tests', {})

	@property
	def parsed(self):
		if not self.parsed_sections:
			self.parsed_sections = self.parse()
		return self.parsed_sections

	@property
	def database(self):
		if self._database is None:
			if self.db_path:
				with open(self.db_path) as f:
					self._database = yaml.load(f)
			else:
				self._database = []
		return self._database

	def exists_in_database(self):
		for dev in self.database:
			device_regexprs = dev['model'] if isinstance(dev['model'], list) else [dev['model']]
			if any(re.match(r, self.information['device_model'], re.IGNORECASE) for r in device_regexprs):
				return True
		return False

	def get_attributes_from_database(self, device_model):
		for dev in self.database:
			device_regexprs = dev['model'] if isinstance(dev['model'], list) else [dev['model']]
			if any(re.match(r, device_model, re.IGNORECASE) for r in device_regexprs):
				return dev['attributes']
		return None

	def parse(self):
		return {
			'information': self.parse_information_section(self.raw),
			'data': self.parse_data_section(self.raw),
			'self_tests': self.parse_tests_section(self.raw),
		}

	def parse_information_section(self, s):
		if INFORMATION_SECTION_START not in s:
			return {}

		start = s.index(INFORMATION_SECTION_START)

		if DATA_SECTION_START not in s:
			end = len(s)
		else:
			end = s.index(DATA_SECTION_START)

		information_text = s[start:end]

		d = {}
		for k, regex in INFORMATION_RE:
			m = regex.search(information_text)
			if m:
				d[k] = m.group(1).strip() if m.group(1) else ''
		return d

	def parse_data_section(self, s):
		if DATA_SECTION_START not in s:
			return {}

		start = s.index(DATA_SECTION_START)
		data_text = s[start:]

		d = {}
		for k, regex in DATA_RE:
			m = regex.search(data_text)
			if m:
				d[k] = m.group(1).strip() if m.group(1) else ''

		d['attributes'] = DATA_ATTRIBUTES_RE.findall(s)

		return d

	def parse_tests_section(self, s):
		if TESTS_SECTION_START not in s:
			return {
				'test_results': []
			}

		start = s.index(TESTS_SECTION_START)
		end = re.search(r'(\r\n\r\n|\n\n|\r\r)', s[start+1:], re.MULTILINE)
		end = start + end.end(0) if end else len(s)

		tests_text = s[start:end]

		return {
			'test_results': TEST_RESULT_RE.findall(tests_text)
		}

	def check(self):
		return len(self.check_attributes()) == 0 and self.check_tests()

	def check_tests(self):
		ok_test_results = [
			'Completed without error',
			'Interrupted (host reset)' # reboot during self test
		]
		return not any([x[2] not in ok_test_results for x in self.self_tests['test_results']])

	def check_attributes(self):
		device_model = self.information.get('device_model', '')
		device_db_attributes = self.get_attributes_from_database(device_model)

		threshold_from = re.compile('^(\d+):$')
		threshold_to = re.compile('^:(\d+)$')
		threshold_from_to = re.compile('^(\d+):(\d+)$')

		if not device_db_attributes:
			return {}

		failed_attributes = {}

		for attrid, name, flag, value, worst, tresh, type, updated, when_failed, raw_value in self.smart_data['attributes']:
			attrid = int(attrid)
			if attrid in device_db_attributes:
				db_attrs = device_db_attributes[attrid]

				if isinstance(db_attrs, list):
					value_field, min_value, max_value = tuple(device_db_attributes[int(attrid)])

					check_value = int(value if value_field == "VALUE" else raw_value)

					if not (int(min_value) <= check_value <= int(max_value)):
						failed_attributes[(attrid, name)] = ('CRITICAL', value_field, check_value)
				elif isinstance(db_attrs, dict):
					value_field = db_attrs.get('field', 'RAW_VALUE')
					check_value = int(value if value_field == "VALUE" else raw_value)

					min_value = db_attrs.get('min', None)
					max_value = db_attrs.get('max', None)

					if min_value is None and max_value is None:
						for failure_type, threshold_key in [('WARNING', 'warn_threshold'), ('CRITICAL', 'crit_threshold')]:
							if threshold_key not in db_attrs:
								continue
							v = db_attrs.get(threshold_key)

							from_m = threshold_from.match(v)
							to_m = threshold_to.match(v)
							from_to_m = threshold_from_to.match(v)

							if (from_m and check_value >= int(from_m.group(1))) or \
								(to_m and check_value <= int(to_m.group(1))) or \
								(from_to_m and (int(from_to_m.group(1)) <= check_value <= int(from_to_m.group(2)))):

								failed_attributes[(attrid, name)] = (failure_type, value_field, check_value)
					else:
						if (min_value is not None and check_value >= int(min_value)) or \
							(max_value is not None and check_value <= int(max_value)):
							failed_attributes[(attrid, name)] = ('CRITICAL', value_field, check_value)

				else:
					raise ValueError("Unknown attribute specification: %s" % db_attrs)

		return failed_attributes