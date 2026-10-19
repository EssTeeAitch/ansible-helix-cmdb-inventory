# Ansible dynamic inventory for BMC Helix CMDB

A starting point for sourcing Ansible inventory from BMC Helix CMDB, aimed at
Ansible Automation Platform 2.6.

Clone it, run it offline against the included sample response, run the
connection test against a real CMDB, then work through the checklist in
section 4.

## Quick start

```
# Offline, no Helix needed.  Expect four hosts and one skipped record.
ansible-inventory -i inventory/mock_helix.yml --graph

# Against a real Helix, once the three variables are exported (section 1).
ansible-playbook -i localhost, test_connection.yml

# Unit tests.
python -m pytest collections/ansible_collections/unofficial/helix_cmdb/tests/unit
```

## Scope and support

There is no Red Hat supported dynamic inventory plugin for BMC Helix CMDB. This
is an unofficial, community starter skeleton. It is not affiliated with or
endorsed by BMC or Red Hat, and it carries no support commitment. It is
intended to be forked and owned by whoever deploys it, not consumed as is.

It is loosely based on the BMC Helix CMDB REST API documentation and on the
design of the Red Hat certified ServiceNow inventory plugin,
`servicenow.itsm.now`. Issues and pull requests are welcome, but there is no
commitment that they will be answered. The plugin is covered by unit tests and
has been run end to end against the recorded response in `mock/` and against a
local fake of the documented endpoints. It has **not** been run against a live
Helix instance. Section 4 lists what to confirm on first contact.

## Requirements

`ansible-core` 2.16 or later (what the AAP 2.6 execution environments ship). No
additional collections are needed. `pytest` and `PyYAML` to run the unit tests.

### What it has been tested with

| | |
|---|---|
| ansible-core | 2.16 (unit tests, sanity tests, end to end), 2.14 (offline inventory) |
| Python | 3.11 |
| Helix | the recorded sample in `mock/` and a local fake of the documented endpoints only |
| Not yet tested | a live BMC Helix CMDB, and an inventory sync inside AAP 2.6 |

## Layout

```
ansible.cfg                       collections path and enabled inventory plugins
test_connection.yml               stage 1, connection test using only uri
inventory/mock_helix.yml          stage 2 and 3, offline
inventory/helix.yml               stage 2 and 3, live
mock/ci_response.json             recorded response, lets everything run with no CMDB
test_inventory.yml                stage 3, prints hosts, groups and host variables
collections/ansible_collections/unofficial/helix_cmdb/
  plugins/inventory/cmdb.py       the plugin
  tests/unit/                     unit tests, see section 6
```

The plugin is `unofficial.helix_cmdb.cmdb`.

## The Helix API calls it makes

| Step | Request |
|---|---|
| Log in | `POST /api/jwt/login`, form encoded `username` and `password`. The body of the response is the token. |
| Query | `GET /api/cmdb/v1.0/instances/{dataset}/{namespace}/{class}?offset=N&limit=N&num_matches=true&attributes=A,B&qualification=...` with `Authorization: AR-JWT <token>` |
| Log out | `POST /api/jwt/logout` with the same header |

The query is repeated, advancing `offset` by the number of records returned,
until `num_matches` records have been read or a short page comes back. The
response is expected to look like this, which is what `mock/ci_response.json`
mirrors:

```json
{
  "instances": [
    {
      "instance_id": "OI-...",
      "class_name_key": {"name": "BMC_ComputerSystem", "namespace": "BMC.CORE"},
      "attributes": {"Name": "app-prod-01", "HostName": "app-prod-01"},
      "_links": {"self": [{"href": "..."}]}
    }
  ],
  "num_matches": 292
}
```

The interactive Swagger UI on your own Helix server is the authority for your
version: `https://<host>:<port>/cmdb/api/help.html`.

## Credentials

Never put a password in `inventory/*.yml` or anywhere else in the repository.

- **Locally**, export `HELIX_BASE_URL`, `HELIX_USERNAME` and `HELIX_PASSWORD`
  in your shell. The plugin and the connection test both read them from the
  environment.
- **In AAP**, use the custom credential type in section 5, which injects the
  same three environment variables.
- **For local experiments** that need their own inventory file, name it
  `inventory/local_<anything>_helix.yml`, for example
  `inventory/local_dev_helix.yml`. `.gitignore` keeps `inventory/local_*` out of
  git, and the name still ends in `helix.yml`, which the plugin requires.
- Use a dedicated, read only service account in Helix for this, not a named
  person's login.

## 1. Connection test

Proves the URL, the credentials, TLS and the network before the plugin is
involved at all. It uses nothing but `ansible.builtin.uri`, so a failure here is
never the plugin's fault.

```
export HELIX_BASE_URL=https://helix.example.com
export HELIX_USERNAME=svc_ansible
export HELIX_PASSWORD=...
ansible-playbook -i localhost, test_connection.yml
```

It authenticates, requests a single record of one class, prints the total
number of matching CIs, the shape of the response and the attribute names on the
first record, and then invalidates the token.

Failure codes are interpreted in the playbook output. In short, status -1 is
DNS, routing or TLS, 401 at login is credentials, 401 at the query is the
authorization scheme (try `-e helix_auth_scheme=Bearer`), 403 is CMDB read
permission, 404 on the query is a wrong dataset, namespace or class name, and
400 is an attribute or qualification the class does not accept.

To try a qualification, pass it as JSON so the spaces survive:

```
ansible-playbook -i localhost, test_connection.yml \
  -e '{"helix_qualification": "'"'"'Type'"'"' = \"Server\""}'
```

## 2. Inventory connection, offline

No Helix host required. This is the one to try first.

```
ansible-inventory -i inventory/mock_helix.yml --graph
ansible-inventory -i inventory/mock_helix.yml --list
```

Expected result is four hosts. The sample response deliberately contains a
record with no `Name`, which falls back to its `HostName` through
`hostname_attributes`, and a fifth record with neither, which is skipped with a
warning rather than failing the whole sync.

## 3. Query test

```
ansible-playbook -i inventory/mock_helix.yml test_inventory.yml
```

Prints one line per Configuration Item, the groups built from CMDB attributes,
and every `helix_` prefixed variable available on the first host. Swap in
`inventory/helix.yml` once stage 1 passes.

Every CMDB attribute is exposed as a host variable prefixed with `helix_`, so
`Name` becomes `helix_Name`. The prefix exists so a CMDB attribute can never
collide with an Ansible special variable. Two more variables come from the
record itself: `helix_instance_id` and `helix_class_name`, which are what any
later write back to the CMDB will need.

## 4. Things to confirm against a real CMDB

All of these are options in `inventory/helix.yml`, so adjusting them is
configuration and not a code change.

1. **Attribute names.** The defaults are standard Common Data Model attributes
   of `BMC_ComputerSystem`, but sites commonly extend classes. Requesting an
   attribute the class does not have is likely to fail the whole query with a
   400, so check the attribute list printed by the connection test, or the
   class definition in Swagger, before adding to `attributes`.
2. **Which attribute holds a usable host name.** `hostname_attributes` is
   tried in order, `HostName` then `Name` in `inventory/helix.yml`. `Name` is
   frequently not a resolvable name in practice.
3. **How Ansible will reach the host.** Unlike ServiceNow's `cmdb_ci_server`,
   `BMC_ComputerSystem` carries no IP address. IP addresses live in
   `BMC_IPEndpoint`, related to the computer system through a relationship
   class. The skeleton builds `ansible_host` from `HostName` and `Domain` with
   `compose`. If those do not resolve from the execution environment, reading
   the related `BMC_IPEndpoint` is the next feature to add, and is the
   equivalent of the ServiceNow plugin's `enhanced` relationship mode.
4. **Soft deleted CIs.** Helix marks a deleted CI with `MarkAsDeleted` rather
   than removing it, so an unfiltered query returns retired servers too and
   *Overwrite* in AAP will not remove them. Confirm the stored value with the
   Helix administrator, then enable the `qualification` shown commented out in
   `inventory/helix.yml`.
5. **Authorization scheme.** `AR-JWT` is what the AR System REST API
   documents. Some CMDB documentation examples show `Bearer`. Change
   `auth_scheme` if the connection test reports a 401 on the query after a
   successful login.
6. **Server side page limit.** `page_size` defaults to 500, which is the
   documented default `limit`. If the server caps it lower, paging still
   works, it just takes more calls.
7. **`records_path` and `attributes_path`.** Defaults `instances` and
   `attributes` match the documentation. The connection test says whether they
   match the real response.

Start with no `qualification`. Add it only once an unfiltered query returns
rows, otherwise an empty result is ambiguous between "the filter excluded
everything" and "the query is wrong".

## 5. Running it in Ansible Automation Platform 2.6

A custom inventory plugin has to reach the controller somehow, and the route
used here is to ship it inside a project.

1. **Project.** Add this repository as a Project. The collection stays at
   `collections/ansible_collections/unofficial/helix_cmdb/` and the project's
   `ansible.cfg` points `collections_path` at it, so no Galaxy install is
   needed. Confirm on the first inventory sync that the plugin is found. If it
   is not, list the collection in `collections/requirements.yml` as a git
   source pointing at this repository, or publish it to Private Automation Hub.
   Either way the default execution environment is enough, since the plugin
   needs nothing beyond ansible-core.
2. **Custom credential type.** *Administration, Credential Types, Add*.

   Input configuration:

   ```yaml
   fields:
     - id: base_url
       type: string
       label: Helix base URL
     - id: username
       type: string
       label: Username
     - id: password
       type: string
       label: Password
       secret: true
   required:
     - base_url
     - username
     - password
   ```

   Injector configuration:

   ```yaml
   env:
     HELIX_BASE_URL: '{{ base_url }}'
     HELIX_USERNAME: '{{ username }}'
     HELIX_PASSWORD: '{{ password }}'
   ```

   Then create a credential of that type for the Helix service account. The
   plugin reads all three values from the environment, which is why no secret
   appears in `inventory/helix.yml`.
3. **Connection test from AAP.** Optional but worthwhile. A job template that
   runs `test_connection.yml` against an inventory containing only
   `localhost`, with the same credential attached, proves the execution
   environment can reach Helix before an inventory sync is attempted.
4. **Inventory source.** Create an Inventory, add a source of type *Sourced from
   a Project*, point it at this project, and set the inventory file to
   `inventory/helix.yml`. Attach the credential from step 2.
5. **Overwrite.** Decide deliberately whether to enable *Overwrite* and
   *Overwrite variables*. With them off, a Configuration Item retired in the
   CMDB will linger in the inventory indefinitely.

The source file name must end in `helix.yml`, `helix.yaml`, `helix_cmdb.yml` or
`helix_cmdb.yaml`, otherwise the plugin declines the file and the sync reports
that nothing could be parsed.

## 6. Unit tests

The tests replace the HTTP layer with a fake, so they run anywhere. They cover
the request URL and parameters, encoding of the qualification, paging, login
and logout including logout after a failed query, error reporting, hostname
fallback, variables, groups, `compose` and `keyed_groups`, the cache key, and
that `mock/ci_response.json` and `inventory/mock_helix.yml` still produce the
expected inventory.

From the repository root:

```
python -m pip install pytest pyyaml 'ansible-core>=2.16'
python -m pytest collections/ansible_collections/unofficial/helix_cmdb/tests/unit
```

Or with ansible-test, from inside the collection:

```
cd collections/ansible_collections/unofficial/helix_cmdb
ansible-test units --venv --python 3.11
```

When the plugin is first run against the real CMDB, save a real response
(with names scrubbed) next to `mock/ci_response.json` and add a test for it.
That keeps the plugin honest about the real shape from then on.

## 7. Notes for whoever develops this further

- `unparsed_is_failed = True` is set in `ansible.cfg` on purpose. Without it,
  any plugin error is reduced to "Unable to parse ... as an inventory source"
  and the real reason is hidden. `host_list` is enabled alongside it so the
  connection test can run with `-i localhost,`.
- Errors are raised as `AnsibleError` rather than `AnsibleParserError`, for the
  same reason. `AnsibleParserError` causes the inventory manager to quietly move
  on to the next plugin, which buries the diagnostic.
- Caching is supported through the standard `inventory_cache` options and is
  turned off in `inventory/helix.yml`. Leave it off while the query is still
  being shaped. The cache key includes the class and the qualification, so a
  changed query is never answered from an old cache entry.
- Group names built from `group_by_attributes` are lower cased and passed
  through Ansible's own group name sanitizer, as the certified ServiceNow plugin
  does.
- `compose`, `groups` and `keyed_groups` all work, because the plugin implements
  the standard `constructed` interface. Prefer those over adding more code.

## 8. Next steps

Known gaps, roughly in the order they are likely to matter:

1. **Read IP addresses from related records.** `BMC_ComputerSystem` has no IP
   address (section 4, item 3). Following the relationship to `BMC_IPEndpoint`
   would give `ansible_host` a real address. This is the equivalent of the
   ServiceNow plugin's `enhanced` mode.
2. **Capture a real response as a test case.** After the first live run, save
   a scrubbed response next to `mock/ci_response.json` and add a unit test that
   loads it.
3. **Log in again when the token expires.** The token is good for about an hour
   by default. A sync that pages through a very large class for longer than that
   will start failing with 401 part way through.
4. **Continuous integration.** A GitHub Actions workflow that runs
   `ansible-test sanity` and `ansible-test units` on every pull request.
5. **More than one class per source.** Today one inventory file reads one
   class. Reading several (for example `BMC_ComputerSystem` and
   `BMC_VirtualSystemEnabler`) currently means one inventory source each.

## References

- BMC Helix CMDB, using the REST API:
  https://docs.helixops.ai/bin/Service-Management/IT-Service-Management/BMC-Helix-CMDB/ac233/Developing/Using-BMC-Helix-CMDB-functions-in-an-external-application-with-the-REST-API/
- AR System REST API access and authentication (JWT):
  https://docs.helixops.ai/bin/Service-Management/Innovation-Suite/BMC-Helix-Innovation-Suite/is262/Developing-an-API-program/Integrating-AR-System-forms-with-a-third-party-application-by-using-the-REST-API/Access-and-authentication-for-the-REST-API/
- Red Hat certified ServiceNow inventory plugin, used as the design reference:
  https://github.com/ansible-collections/servicenow.itsm/blob/main/plugins/inventory/now.py

## Making it your own

The collection is `unofficial.helix_cmdb`, and the plugin's fully qualified
name is `unofficial.helix_cmdb.cmdb`. If you adopt it, move it into your own
namespace so it is clear who owns your copy:

1. Rename `collections/ansible_collections/unofficial/helix_cmdb/` to
   `collections/ansible_collections/<your_namespace>/<your_name>/`.
2. Update `namespace` and `name` in `galaxy.yml`.
3. Replace `unofficial.helix_cmdb` everywhere else: `ansible.cfg`, the
   `plugin:` line in both `inventory/*.yml` files, `NAME` and the `plugin`
   choices in `cmdb.py`, and the unit tests. `grep -rn unofficial .` finds them
   all.
4. Run the unit tests and `ansible-inventory -i inventory/mock_helix.yml --graph`
   to confirm nothing was missed.

## License

MIT No Attribution (MIT-0). See `LICENSE`. Use, modify and redistribute it
without having to keep the copyright notice, and relicense your fork however
you need, for example GPLv3 to match the wider Ansible ecosystem.

BMC, BMC Helix and ServiceNow are trademarks of their respective owners. Their
use here only describes what the plugin talks to and what it was modelled on.
