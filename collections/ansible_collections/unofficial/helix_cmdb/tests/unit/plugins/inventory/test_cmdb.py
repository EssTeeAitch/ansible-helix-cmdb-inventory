from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import io
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from ansible.errors import AnsibleError
from ansible.inventory.data import InventoryData
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.unofficial.helix_cmdb.plugins.inventory import cmdb

BASE_URL = 'https://helix.example.com'
INSTANCES_PATH = '/api/cmdb/v1.0/instances/BMC.ASSET/BMC.CORE/BMC_ComputerSystem'
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), *(['..'] * 8)))


def default_options():
    """Option defaults straight from DOCUMENTATION, plus the doc fragment options the plugin reads."""
    options = dict(
        (name, spec.get('default'))
        for name, spec in yaml.safe_load(cmdb.DOCUMENTATION)['options'].items()
    )
    options.update({
        'plugin': 'unofficial.helix_cmdb.cmdb',
        'compose': {},
        'groups': {},
        'keyed_groups': [],
        'strict': False,
        'use_extra_vars': False,
        'leading_separator': True,
        'cache': False,
        'cache_plugin': 'memory',
        'cache_timeout': 3600,
        'cache_connection': None,
        'cache_prefix': 'ansible_inventory_',
    })
    return options


def make_record(number, **attributes):
    return {
        'instance_id': 'OI-%04d' % number,
        'class_name_key': {'name': 'BMC_ComputerSystem', 'namespace': 'BMC.CORE'},
        'dataset_id': 'BMC.ASSET',
        'attributes': attributes,
        '_links': {'self': [{'href': '%s%s/OI-%04d' % (BASE_URL, INSTANCES_PATH, number)}]},
    }


def make_page(records, num_matches=None):
    page = {'instances': records}
    if num_matches is not None:
        page['num_matches'] = num_matches
    return page


class FakeResponse(object):
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body


class FakeHelix(object):
    """Stands in for open_url.  Serves a token on login and the given pages in order on GET."""

    def __init__(self, pages=None, token='token-abc', get_error=None):
        self.pages = list(pages or [])
        self.token = token
        self.get_error = get_error
        self.calls = []

    def __call__(self, url, data=None, method=None, headers=None, **kwargs):
        self.calls.append({'url': url, 'data': data, 'method': method, 'headers': headers or {}})
        path = urlsplit(url).path
        if path == '/api/jwt/login':
            return FakeResponse(('%s\n' % self.token).encode('utf-8'))
        if path == '/api/jwt/logout':
            return FakeResponse(b'')
        if self.get_error is not None:
            raise self.get_error
        if not self.pages:
            raise AssertionError('Unexpected extra GET %s' % url)
        return FakeResponse(json.dumps(self.pages.pop(0)).encode('utf-8'))

    def gets(self):
        return [call for call in self.calls if call['method'] == 'GET']

    def paths(self):
        return [urlsplit(call['url']).path for call in self.calls]


def query_of(call):
    return parse_qs(urlsplit(call['url']).query)


@pytest.fixture
def plugin():
    module = cmdb.InventoryModule()
    module._options = default_options()
    module.inventory = InventoryData()
    module.templar = Templar(loader=DataLoader())
    return module


@pytest.fixture
def live(plugin, monkeypatch):
    """A plugin pointed at a fake live server.  Returns a function that installs the server."""
    plugin._options.update({'base_url': BASE_URL, 'username': 'svc_ansible', 'password': 'secret'})

    def install(server):
        monkeypatch.setattr(cmdb, 'open_url', server)
        return server
    return install


def run_parse(plugin, monkeypatch, path='/tmp/inventory/helix.yml'):
    """Run parse() with the options already on the plugin, skipping the config file read."""
    monkeypatch.setattr(plugin, '_read_config_data', lambda p: None)
    inventory = InventoryData()
    plugin.parse(inventory, DataLoader(), path, cache=False)
    return inventory


def host_names(inventory):
    return sorted(inventory.hosts)


def group_hosts(inventory, group):
    return sorted(host.name for host in inventory.groups[group].get_hosts())


# verify_file

@pytest.mark.parametrize('name, accepted', [
    ('helix.yml', True),
    ('prod_helix.yaml', True),
    ('site_helix_cmdb.yml', True),
    ('helix.json', False),
    ('inventory.yml', False),
])
def test_verify_file(tmp_path, name, accepted):
    path = tmp_path / name
    path.write_text('plugin: unofficial.helix_cmdb.cmdb\n')
    assert cmdb.InventoryModule().verify_file(str(path)) is accepted


# Request shape, as documented by BMC

def test_query_uses_namespace_and_documented_parameters(plugin, live):
    server = live(FakeHelix([make_page([make_record(1, Name='a')], num_matches=1)]))

    records = plugin._fetch(BASE_URL, 'token-abc', True, 30)

    assert len(records) == 1
    get = server.gets()[0]
    assert urlsplit(get['url']).path == INSTANCES_PATH
    query = query_of(get)
    assert query['offset'] == ['0']
    assert query['limit'] == ['500']
    assert query['num_matches'] == ['true']
    assert query['attributes'] == ['Name,ShortDescription,Category,Type,Item']
    # The names the first draft guessed at, which the API does not use.
    assert 'propertyName' not in query
    assert 'queryExpression' not in query
    assert get['headers']['Authorization'] == 'AR-JWT token-abc'
    assert get['headers']['Accept'] == 'application/json'


def test_qualification_is_encoded_with_percent_20(plugin, live):
    qualification = '\'Category\' = "Hardware" AND \'Name\' LIKE "app%"'
    plugin._options['qualification'] = qualification
    server = live(FakeHelix([make_page([], num_matches=0)]))

    plugin._fetch(BASE_URL, 'token-abc', True, 30)

    raw_query = urlsplit(server.gets()[0]['url']).query
    assert '+' not in raw_query
    assert '%20' in raw_query
    assert query_of(server.gets()[0])['qualification'] == [qualification]


def test_class_namespace_and_dataset_are_options(plugin, live):
    plugin._options.update({'dataset_id': 'BMC.SAMPLE', 'namespace': 'ACME', 'class_name_id': 'ACME_Server'})
    server = live(FakeHelix([make_page([], num_matches=0)]))

    plugin._fetch(BASE_URL, 'token-abc', True, 30)

    assert urlsplit(server.gets()[0]['url']).path == '/api/cmdb/v1.0/instances/BMC.SAMPLE/ACME/ACME_Server'


def test_auth_scheme_option(plugin, live):
    plugin._options['auth_scheme'] = 'Bearer'
    server = live(FakeHelix([make_page([], num_matches=0)]))

    plugin._fetch(BASE_URL, 'token-abc', True, 30)

    assert server.gets()[0]['headers']['Authorization'] == 'Bearer token-abc'


def test_page_size_must_be_positive(plugin, live):
    plugin._options['page_size'] = 0
    live(FakeHelix())
    with pytest.raises(AnsibleError, match='page_size'):
        plugin._fetch(BASE_URL, 'token-abc', True, 30)


# Pagination

def test_pages_until_num_matches_is_reached(plugin, live):
    plugin._options['page_size'] = 2
    records = [make_record(n, Name='host-%d' % n) for n in range(5)]
    server = live(FakeHelix([
        make_page(records[0:2], num_matches=5),
        make_page(records[2:4], num_matches=5),
        make_page(records[4:5], num_matches=5),
    ]))

    fetched = plugin._fetch(BASE_URL, 'token-abc', True, 30)

    assert [r['instance_id'] for r in fetched] == [r['instance_id'] for r in records]
    assert [query_of(call)['offset'] for call in server.gets()] == [['0'], ['2'], ['4']]
    assert all(query_of(call)['limit'] == ['2'] for call in server.gets())


def test_exact_multiple_of_page_size_does_not_request_an_extra_page(plugin, live):
    plugin._options['page_size'] = 2
    records = [make_record(n, Name='host-%d' % n) for n in range(4)]
    server = live(FakeHelix([
        make_page(records[0:2], num_matches=4),
        make_page(records[2:4], num_matches=4),
    ]))

    assert len(plugin._fetch(BASE_URL, 'token-abc', True, 30)) == 4
    assert len(server.gets()) == 2


def test_short_page_ends_paging_when_num_matches_is_absent(plugin, live):
    plugin._options['page_size'] = 2
    server = live(FakeHelix([
        make_page([make_record(1, Name='a'), make_record(2, Name='b')]),
        make_page([make_record(3, Name='c')]),
    ]))

    assert len(plugin._fetch(BASE_URL, 'token-abc', True, 30)) == 3
    assert len(server.gets()) == 2


def test_empty_page_ends_paging_when_num_matches_is_absent(plugin, live):
    plugin._options['page_size'] = 2
    server = live(FakeHelix([
        make_page([make_record(1, Name='a'), make_record(2, Name='b')]),
        make_page([]),
    ]))

    assert len(plugin._fetch(BASE_URL, 'token-abc', True, 30)) == 2
    assert len(server.gets()) == 2


# Login, logout and errors

def test_parse_logs_in_queries_and_logs_out(plugin, live, monkeypatch):
    server = live(FakeHelix([make_page([make_record(1, Name='app-01')], num_matches=1)]))

    inventory = run_parse(plugin, monkeypatch)

    assert host_names(inventory) == ['app-01']
    assert server.paths() == ['/api/jwt/login', INSTANCES_PATH, '/api/jwt/logout']
    login = server.calls[0]
    assert login['method'] == 'POST'
    assert parse_qs(login['data']) == {'username': ['svc_ansible'], 'password': ['secret']}
    assert server.calls[2]['headers']['Authorization'] == 'AR-JWT token-abc'


def test_logout_still_runs_when_the_query_fails(plugin, live, monkeypatch):
    error = HTTPError(BASE_URL + INSTANCES_PATH, 404, 'Not Found', {}, io.BytesIO(b'no such class'))
    server = live(FakeHelix(get_error=error))

    with pytest.raises(AnsibleError, match='HTTP 404'):
        run_parse(plugin, monkeypatch)

    assert server.paths()[-1] == '/api/jwt/logout'


def test_login_failure_reports_status(plugin, monkeypatch):
    plugin._options.update({'base_url': BASE_URL, 'username': 'svc_ansible', 'password': 'wrong'})

    def refuse(url, **kwargs):
        raise HTTPError(url, 401, 'Unauthorized', {}, io.BytesIO(b'ERROR (623): Authentication failed'))
    monkeypatch.setattr(cmdb, 'open_url', refuse)

    with pytest.raises(AnsibleError, match='login failed with HTTP 401'):
        run_parse(plugin, monkeypatch)


def test_unreachable_host_is_reported(plugin, monkeypatch):
    plugin._options.update({'base_url': BASE_URL, 'username': 'svc_ansible', 'password': 'secret'})

    def unreachable(url, **kwargs):
        raise URLError('Name or service not known')
    monkeypatch.setattr(cmdb, 'open_url', unreachable)

    with pytest.raises(AnsibleError, match='Could not reach'):
        run_parse(plugin, monkeypatch)


def test_missing_credentials_are_reported(plugin, monkeypatch):
    plugin._options['base_url'] = BASE_URL
    with pytest.raises(AnsibleError, match='required unless mock_file'):
        run_parse(plugin, monkeypatch)


def test_wrong_records_path_lists_the_keys_that_are_there(plugin, live):
    plugin._options['records_path'] = 'entries'
    live(FakeHelix([make_page([], num_matches=0)]))

    with pytest.raises(AnsibleError, match="Could not find 'entries'.*instances, num_matches"):
        plugin._fetch(BASE_URL, 'token-abc', True, 30)


# Turning records into hosts, variables and groups

def test_hosts_variables_and_groups(plugin):
    plugin._options.update({
        'hostname_attributes': ['Name', 'HostName'],
        'group_by_attributes': ['Item'],
        'keyed_groups': [{'key': 'helix_Type', 'prefix': 'type'}],
        'compose': {'ansible_host': "helix_HostName ~ '.' ~ helix_Domain"},
    })

    plugin._populate([
        make_record(1, Name='app-01', HostName='app-01', Domain='example.com', Type='Server', Item='Rack Mount'),
        make_record(2, HostName='app-02', Domain='example.com', Type='Server', Item='Virtual Machine'),
        make_record(3, ShortDescription='no name at all', Type='Server'),
    ])

    inventory = plugin.inventory
    assert host_names(inventory) == ['app-01', 'app-02']

    variables = inventory.get_host('app-01').vars
    assert variables['helix_Name'] == 'app-01'
    assert variables['helix_Item'] == 'Rack Mount'
    assert variables['helix_instance_id'] == 'OI-0001'
    assert variables['helix_class_name'] == 'BMC_ComputerSystem'
    assert variables['ansible_host'] == 'app-01.example.com'

    assert group_hosts(inventory, 'rack_mount') == ['app-01']
    assert group_hosts(inventory, 'virtual_machine') == ['app-02']
    assert group_hosts(inventory, 'type_Server') == ['app-01', 'app-02']


def test_group_names_are_sanitized(plugin):
    plugin._options['group_by_attributes'] = ['Item']
    plugin._populate([make_record(1, Name='a', Item='Blade / Chassis-2')])
    assert 'blade___chassis_2' in plugin.inventory.groups


def test_attributes_at_top_level_of_record(plugin):
    plugin._options['attributes_path'] = ''
    plugin._populate([{'Name': 'flat-01', 'Type': 'Server'}])
    assert plugin.inventory.get_host('flat-01').vars['helix_Type'] == 'Server'


def test_hostname_is_stripped(plugin):
    plugin._populate([make_record(1, Name='  padded-01 ')])
    assert host_names(plugin.inventory) == ['padded-01']


# Caching

def test_cache_key_changes_with_the_query(plugin):
    plugin._options['base_url'] = BASE_URL
    first = plugin._query_cache_key('/tmp/inventory/helix.yml')
    plugin._options['qualification'] = '\'Type\' = "Server"'
    second = plugin._query_cache_key('/tmp/inventory/helix.yml')
    plugin._options['class_name_id'] = 'BMC_Printer'
    third = plugin._query_cache_key('/tmp/inventory/helix.yml')
    assert len({first, second, third}) == 3


# The sample response shipped in the repository

def test_repository_mock_response(plugin, monkeypatch):
    """Keeps mock/ci_response.json and inventory/mock_helix.yml in step with the plugin."""
    with open(os.path.join(REPO_ROOT, 'inventory', 'mock_helix.yml')) as handle:
        config = yaml.safe_load(handle)
    config.pop('plugin')
    config['mock_file'] = os.path.join(REPO_ROOT, config['mock_file'])
    plugin._options.update(config)

    def no_network(*args, **kwargs):
        raise AssertionError('mock mode must not touch the network')
    monkeypatch.setattr(cmdb, 'open_url', no_network)

    inventory = run_parse(plugin, monkeypatch, path=os.path.join(REPO_ROOT, 'inventory', 'mock_helix.yml'))

    assert host_names(inventory) == ['app-prod-01', 'app-prod-02', 'app-test-01', 'db-prod-01']
    assert group_hosts(inventory, 'virtual_machine') == ['app-test-01']
    assert group_hosts(inventory, 'in_repair') == ['db-prod-01']
    assert group_hosts(inventory, 'type_Server') == host_names(inventory)
    assert inventory.get_host('app-test-01').vars['ansible_host'] == 'app-test-01.test.example.com'
    assert inventory.get_host('app-test-01').vars['cmdb_is_virtual'] is True
