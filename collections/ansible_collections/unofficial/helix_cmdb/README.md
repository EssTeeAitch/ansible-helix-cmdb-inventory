# unofficial.helix_cmdb

Contains a single inventory plugin, `unofficial.helix_cmdb.cmdb`, which builds an
Ansible inventory from Configuration Items held in a BMC Helix CMDB class, read
through `GET /api/cmdb/v1.0/instances/{dataset}/{namespace}/{class}` with
offset and limit paging.

Unit tests are under `tests/unit` and run with `ansible-test units` or plain
`pytest`.

This is an unofficial starter skeleton, not affiliated with or endorsed by BMC
or Red Hat, and carries no support commitment.  It is licensed MIT-0, so fork
it, rename it into your own namespace and relicense it as you see fit.

See the repository README for the staged walkthrough, the AAP setup, and the
checklist of things to confirm against a live CMDB.
