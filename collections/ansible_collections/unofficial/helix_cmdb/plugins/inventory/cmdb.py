from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = r'''
    name: cmdb
    short_description: BMC Helix CMDB dynamic inventory source
    description:
      - Builds an Ansible inventory from Configuration Items held in a BMC Helix CMDB class.
      - Authenticates against the AR System JWT endpoint and pages through the CMDB REST API
        at C(/api/cmdb/v1.0/instances/{dataset}/{namespace}/{class}).
      - Supports an offline mock mode so the plugin can be exercised without a live CMDB.
      - There is no Red Hat supported dynamic inventory plugin for BMC Helix CMDB.
        This plugin is an unofficial starter skeleton, not affiliated with or endorsed
        by BMC or Red Hat, and carries no support commitment.
    options:
      plugin:
        description: Token that identifies this plugin.  Must be unofficial.helix_cmdb.cmdb.
        required: true
        choices: ['unofficial.helix_cmdb.cmdb']
      base_url:
        description:
          - Base URL of the Helix / AR System REST host, for example https://helix.example.com:8443.
          - Ignored when O(mock_file) is set.
        type: str
        env:
          - name: HELIX_BASE_URL
      username:
        description: Helix user name.  Ignored when O(mock_file) is set.
        type: str
        env:
          - name: HELIX_USERNAME
      password:
        description: Helix password.  Ignored when O(mock_file) is set.
        type: str
        env:
          - name: HELIX_PASSWORD
      auth_scheme:
        description:
          - Scheme placed in front of the token in the Authorization header.
          - The AR System REST API documents C(AR-JWT).  Some CMDB examples show C(Bearer).
        type: str
        default: AR-JWT
        env:
          - name: HELIX_AUTH_SCHEME
      dataset_id:
        description: CMDB dataset to read from.
        type: str
        default: BMC.ASSET
      namespace:
        description: Namespace of the CMDB class.  Every out of the box class lives in BMC.CORE.
        type: str
        default: BMC.CORE
      class_name_id:
        description: CMDB class to read, for example BMC_ComputerSystem.
        type: str
        default: BMC_ComputerSystem
      attributes:
        description:
          - CMDB attributes to request and expose as host variables.
          - Sent to the API as the attributes parameter.
          - Requesting an attribute the class does not have is likely to fail the query.
        type: list
        elements: str
        default: ['Name', 'ShortDescription', 'Category', 'Type', 'Item']
      qualification:
        description:
          - Optional CMDB qualification, sent as the qualification parameter.
          - For example 'Category' = "Hardware" AND 'Type' = "Server".
          - Do not URL encode it yourself, the plugin does that.
        type: str
        default: ''
      page_size:
        description:
          - Number of instances requested per call, sent as the limit parameter.
          - The plugin keeps requesting pages, advancing offset, until every match has been read.
        type: int
        default: 500
      hostname_attributes:
        description:
          - Attributes tried in order for the Ansible inventory hostname.  The first non empty value wins.
          - A record with none of them set is skipped.
          - Every attribute listed here must also be in O(attributes).
        type: list
        elements: str
        default: ['Name']
      records_path:
        description:
          - Dotted path to the list of records inside the API response.
          - Change this if your Helix version nests the payload differently.
        type: str
        default: instances
      attributes_path:
        description:
          - Dotted path to the attribute dictionary inside a single record.
          - Set to an empty string if attributes sit at the top level of the record.
        type: str
        default: attributes
      group_by_attributes:
        description: Attributes whose values become inventory groups.
        type: list
        elements: str
        default: []
      mock_file:
        description:
          - Path to a JSON file holding a recorded API response.
          - When set, no network call is made.  Use this to develop and test without a live CMDB.
        type: path
      validate_certs:
        description: Whether to verify the TLS certificate of the Helix host.
        type: bool
        default: true
      timeout:
        description: HTTP timeout in seconds.
        type: int
        default: 30
    extends_documentation_fragment:
      - constructed
      - inventory_cache
'''

EXAMPLES = r'''
# inventory/mock_helix.yml - offline, no Helix needed
plugin: unofficial.helix_cmdb.cmdb
mock_file: mock/ci_response.json
hostname_attributes:
  - Name
  - HostName
group_by_attributes:
  - Item

# inventory/helix.yml - against a live Helix host
plugin: unofficial.helix_cmdb.cmdb
base_url: https://helix.example.com
username: svc_ansible
password: "{{ lookup('env', 'HELIX_PASSWORD') }}"
dataset_id: BMC.ASSET
namespace: BMC.CORE
class_name_id: BMC_ComputerSystem
qualification: >-
  'Category' = "Hardware" AND 'Type' = "Server"
attributes:
  - Name
  - HostName
  - Domain
  - Category
  - Type
  - Item
hostname_attributes:
  - HostName
  - Name
group_by_attributes:
  - Item
keyed_groups:
  - key: helix_Type
    prefix: type
compose:
  ansible_host: helix_HostName ~ '.' ~ helix_Domain
'''

import hashlib
import json
import os

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote

from ansible.errors import AnsibleError
from ansible.module_utils.common.text.converters import to_text, to_native
from ansible.module_utils.urls import open_url
from ansible.plugins.inventory import BaseInventoryPlugin, Constructable, Cacheable
from ansible.utils.display import Display

display = Display()

# Inventory files must end in one of these so AAP and ansible-inventory pick the plugin up.
VALID_SUFFIXES = ('helix.yml', 'helix.yaml', 'helix_cmdb.yml', 'helix_cmdb.yaml')


class InventoryModule(BaseInventoryPlugin, Constructable, Cacheable):

    NAME = 'unofficial.helix_cmdb.cmdb'

    def verify_file(self, path):
        if not super(InventoryModule, self).verify_file(path):
            return False
        if path.endswith(VALID_SUFFIXES):
            return True
        display.vvv(
            'unofficial.helix_cmdb.cmdb skipped %s, the file name must end in one of %s'
            % (path, ', '.join(VALID_SUFFIXES))
        )
        return False

    @staticmethod
    def _dig(payload, dotted_path):
        """Walk a dotted path into nested dictionaries.  An empty path returns the payload."""
        if not dotted_path:
            return payload
        current = payload
        for part in dotted_path.split('.'):
            if not isinstance(current, dict) or part not in current:
                raise AnsibleError(
                    "Could not find '%s' in the response while walking the path '%s'.  "
                    "Top level keys present were: %s.  "
                    "Adjust records_path or attributes_path to match your Helix version."
                    % (part, dotted_path,
                       ', '.join(sorted(current.keys())) if isinstance(current, dict) else current.__class__.__name__)
                )
            current = current[part]
        return current

    def _records(self, payload):
        """Pull the list of records out of one response page."""
        records_path = self.get_option('records_path')
        records = self._dig(payload, records_path)
        if not isinstance(records, list):
            raise AnsibleError(
                "Expected a list at records_path '%s' but found %s."
                % (records_path, records.__class__.__name__)
            )
        return records

    def _auth_header(self, token):
        return '%s %s' % (self.get_option('auth_scheme'), token)

    def _login(self, base_url, username, password, validate_certs, timeout):
        """Exchange credentials for an AR System JWT."""
        url = '%s/api/jwt/login' % base_url.rstrip('/')
        body = urlencode({'username': username, 'password': password})
        try:
            response = open_url(
                url,
                data=body,
                method='POST',
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                validate_certs=validate_certs,
                timeout=timeout,
            )
        except HTTPError as err:
            raise AnsibleError(
                'Helix login failed with HTTP %s at %s.  Response: %s'
                % (err.code, url, to_text(err.read(), errors='surrogate_or_strict'))
            )
        except URLError as err:
            raise AnsibleError('Could not reach the Helix host at %s. %s' % (url, to_native(err)))

        token = to_text(response.read(), errors='surrogate_or_strict').strip()
        if not token:
            raise AnsibleError('Helix login returned an empty token from %s.' % url)
        return token

    def _logout(self, base_url, token, validate_certs, timeout):
        """Best effort token invalidation.  A failure here is not fatal."""
        url = '%s/api/jwt/logout' % base_url.rstrip('/')
        try:
            open_url(
                url,
                data='',
                method='POST',
                headers={'Authorization': self._auth_header(token)},
                validate_certs=validate_certs,
                timeout=timeout,
            )
        except Exception as err:
            display.vvv('Helix logout did not complete cleanly: %s' % to_native(err))

    def _get_page(self, url, token, validate_certs, timeout):
        """GET one page and return the decoded JSON payload."""
        display.vvv('unofficial.helix_cmdb.cmdb requesting %s' % url)
        try:
            response = open_url(
                url,
                method='GET',
                headers={
                    'Authorization': self._auth_header(token),
                    'Accept': 'application/json',
                },
                validate_certs=validate_certs,
                timeout=timeout,
            )
        except HTTPError as err:
            raise AnsibleError(
                'Helix CMDB query failed with HTTP %s at %s.  Response: %s'
                % (err.code, url, to_text(err.read(), errors='surrogate_or_strict'))
            )
        except URLError as err:
            raise AnsibleError('Could not reach the Helix CMDB API at %s. %s' % (url, to_native(err)))

        raw = to_text(response.read(), errors='surrogate_or_strict')
        try:
            return json.loads(raw)
        except ValueError as err:
            raise AnsibleError('Helix returned a response that is not valid JSON. %s' % to_native(err))

    def _fetch(self, base_url, token, validate_certs, timeout):
        """Read every matching instance of one CMDB class, following offset and limit paging."""
        url = '%s/api/cmdb/v1.0/instances/%s/%s/%s' % (
            base_url.rstrip('/'),
            quote(self.get_option('dataset_id'), safe=''),
            quote(self.get_option('namespace'), safe=''),
            quote(self.get_option('class_name_id'), safe=''),
        )

        attributes = self.get_option('attributes')
        qualification = self.get_option('qualification')
        page_size = self.get_option('page_size')
        if page_size < 1:
            raise AnsibleError('page_size must be at least 1, got %s.' % page_size)

        records = []
        offset = 0
        while True:
            params = [('offset', offset), ('limit', page_size), ('num_matches', 'true')]
            if attributes:
                params.append(('attributes', ','.join(attributes)))
            if qualification:
                params.append(('qualification', qualification))
            # quote rather than the default quote_plus, so spaces in a qualification
            # go over the wire as %20 and not as +.
            payload = self._get_page(
                '%s?%s' % (url, urlencode(params, safe=',', quote_via=quote)),
                token, validate_certs, timeout)

            page = self._records(payload)
            records.extend(page)
            offset += len(page)

            total = payload.get('num_matches') if isinstance(payload, dict) else None
            if not page or len(page) < page_size:
                break
            if isinstance(total, int) and offset >= total:
                break

        display.vvv('unofficial.helix_cmdb.cmdb read %d records' % len(records))
        return records

    def _load_mock(self, mock_file):
        if not os.path.exists(mock_file):
            raise AnsibleError('mock_file does not exist: %s' % mock_file)
        with open(mock_file, 'rb') as handle:
            try:
                payload = json.loads(to_text(handle.read(), errors='surrogate_or_strict'))
            except ValueError as err:
                raise AnsibleError('mock_file is not valid JSON: %s. %s' % (mock_file, to_native(err)))
        return self._records(payload)

    def _hostname(self, attributes):
        for attribute in self.get_option('hostname_attributes'):
            value = attributes.get(attribute)
            if value not in (None, ''):
                return to_text(value).strip()
        return None

    def _populate(self, records):
        attributes_path = self.get_option('attributes_path')
        group_by_attributes = self.get_option('group_by_attributes')
        strict = self.get_option('strict')
        seen = 0
        skipped = 0

        for record in records:
            attributes = self._dig(record, attributes_path)
            if not isinstance(attributes, dict):
                raise AnsibleError(
                    "Expected a dictionary at attributes_path '%s' but found %s."
                    % (attributes_path, attributes.__class__.__name__)
                )

            hostname = self._hostname(attributes)
            if not hostname:
                skipped += 1
                display.warning(
                    'unofficial.helix_cmdb.cmdb skipped a record with none of %s set, instance_id %s'
                    % (', '.join(self.get_option('hostname_attributes')),
                       record.get('instance_id', 'unknown') if isinstance(record, dict) else 'unknown'))
                continue

            self.inventory.add_host(hostname)
            seen += 1

            # Every CMDB attribute is exposed with a helix_ prefix so it cannot
            # collide with an Ansible special variable.
            host_vars = {}
            for key, value in attributes.items():
                host_vars['helix_%s' % key] = value
            # The instance id and class are what a later write back to the CMDB needs.
            if isinstance(record, dict):
                if 'instance_id' in record:
                    host_vars['helix_instance_id'] = record['instance_id']
                class_name_key = record.get('class_name_key')
                if isinstance(class_name_key, dict) and 'name' in class_name_key:
                    host_vars['helix_class_name'] = class_name_key['name']

            for key, value in host_vars.items():
                self.inventory.set_variable(hostname, key, value)

            for attribute in group_by_attributes:
                value = attributes.get(attribute)
                if value in (None, ''):
                    continue
                group_name = self.inventory.add_group(
                    self._sanitize_group_name(to_text(value).strip().lower()))
                self.inventory.add_child(group_name, hostname)

            # constructed: compose, groups and keyed_groups
            self._set_composite_vars(self.get_option('compose'), host_vars, hostname, strict)
            self._add_host_to_composed_groups(self.get_option('groups'), host_vars, hostname, strict)
            self._add_host_to_keyed_groups(self.get_option('keyed_groups'), host_vars, hostname, strict)

        display.vvv('unofficial.helix_cmdb.cmdb added %d hosts, skipped %d records' % (seen, skipped))

    def _query_cache_key(self, path):
        """Cache per query, so changing the class or qualification never serves stale results."""
        query = json.dumps([
            self.get_option('mock_file') or self.get_option('base_url'),
            self.get_option('dataset_id'),
            self.get_option('namespace'),
            self.get_option('class_name_id'),
            self.get_option('attributes'),
            self.get_option('qualification'),
        ])
        return '%s_%s' % (self.get_cache_key(path), hashlib.sha1(query.encode('utf-8')).hexdigest()[:12])

    def parse(self, inventory, loader, path, cache=True):
        super(InventoryModule, self).parse(inventory, loader, path)
        self._read_config_data(path)

        mock_file = self.get_option('mock_file')

        cache_key = self._query_cache_key(path)
        user_cache_setting = self.get_option('cache')
        attempt_read = user_cache_setting and cache
        update_cache = user_cache_setting and not cache

        records = None
        if attempt_read:
            try:
                records = self._cache[cache_key]
            except KeyError:
                update_cache = True

        if records is None:
            if mock_file:
                display.vvv('unofficial.helix_cmdb.cmdb reading mock payload from %s' % mock_file)
                records = self._load_mock(mock_file)
            else:
                base_url = self.get_option('base_url')
                username = self.get_option('username')
                password = self.get_option('password')
                if not base_url or not username or not password:
                    raise AnsibleError(
                        'base_url, username and password are all required unless mock_file is set.')

                validate_certs = self.get_option('validate_certs')
                timeout = self.get_option('timeout')

                token = self._login(base_url, username, password, validate_certs, timeout)
                try:
                    records = self._fetch(base_url, token, validate_certs, timeout)
                finally:
                    self._logout(base_url, token, validate_certs, timeout)

        if update_cache:
            self._cache[cache_key] = records

        self._populate(records)
