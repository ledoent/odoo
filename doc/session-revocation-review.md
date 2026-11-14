# Core session revocation review

Fork review against Odoo 20.0 `b02bfedf6f879e4d3969e050c871d5fb90d05f24`.
This changes the session backend extension contract and is not a deployment PR.

## Reproduced defect

An authenticated native `/my/counters` request loads a session and updates its ordinary
counter cache. Pause immediately before its normal persistence, complete the native
CSRF-protected logout, then let the counter response finish. Stock file storage recreates
the authenticated session; its response cookie opens `/my/home` again. The same controlled
HTTP interleaving reproduces against upstream Redis 20.0 `3eaa845`.

This proves an overlapping-request defect, not its frequency in production. The timing
instrument does not inject authentication data or invoke storage operations in the driver.

## Implementation

- A loaded session can only update/rotate while its source still exists under the same
  family lock used by revocation. Logout/device revocation removes all soft-rotation SIDs.
- File writes retain atomic JSON replacement. Locks use a bounded set of 4096 hidden shard
  files, separate from session records; kernel locks are released if a worker dies.
- `is_new` now tracks an unsaved SID: false after persistence, true when retaining a cookie
  whose default session was never saved. First login must work in this latter case.
- Cleanup preserves the active SID. A delayed predecessor write follows the stored successor
  instead of erasing rotation metadata; its stale session changes are discarded.
- A rejected write clears authentication and emits a fresh anonymous cookie. Storage
  exceptions disable further persistence of the request's mutated session.

## Backend migration and review decisions

Custom backends implement `_lock`, `_exists`, `_save`, `_delete_from_identifier`, reads and
expiry operations. Lifecycle overrides fail explicitly at construction, preventing a legacy
Redis implementation from silently bypassing the new policy or writing to filesystem hooks.
The test memory store demonstrates migration and runs the same regression matrix as files.

**The current Redis provider is not compatible with this core patch yet.** It needs an adapter
before this could be deployed with Redis. This PR does not claim to have fixed Redis merely
by changing core. Its atomicity must include automatic Redis expiry and rotation publication,
not just an existence check or an expiring lock lease. Error paths must not retry a rejected
transition as a fresh authenticated session. These are required backend properties.

Coordinate that adapter with open camptocamp/odoo-cloud-platform PRs
[534](https://github.com/camptocamp/odoo-cloud-platform/pull/534) and
[540](https://github.com/camptocamp/odoo-cloud-platform/pull/540). They change expiry and
new-session flags. The review on 540 also demonstrates that a rotated predecessor can outlive
the absolute expiry of its successor; the adapter must preserve that deadline for both.

The constructor guard/API migration, discarding stale predecessor changes, shard contention,
Windows locking, shared-filesystem locking and gevent behavior need review before upstream
submission. Linux/Python tests do not establish Windows, network-filesystem or production
performance properties. The fork-only workflow is a reproducible review aid, not a proposed
replacement for Odoo's upstream CI.

## Validation

The committed regression suite covers initial/unsaved-cookie login, updates, late writes,
rotation reuse, successor metadata, predecessor cleanup, family/device revocation, unrelated
sessions, response cookies, concurrent threads, separate processes and injected storage errors.
It can run without a database:

```python
import odoo.addons
import unittest
odoo.addons.__path__.append('/opt/odoo/addons')
unittest.main(module='odoo.addons.test_http.tests.test_session_store', verbosity=2)
```

The review workflow pins the Python 3.12 Odoo image used locally. Existing HTTP/device tests
also ran in a disposable local database; the PR description records their final result.
