"""Cross-app lifecycle signals (lets lower layers notify higher ones without imports).

org_created(sender=None, org_id=UUID)  -> sent inside the new org's tenant context, after commit-safe
                                          setup; receivers (e.g. warehouse provisioning) must be idempotent.
"""

from django.dispatch import Signal

org_created = Signal()
