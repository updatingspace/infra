# Existing resources only. These blocks document their identities and become
# no-ops once imported. Never apply a plan proposing creation/replacement here.
import {
  to = yandex_compute_disk.boot
  id = "fv4m82qqrtj4rq7ppj00"
}

import {
  to = yandex_compute_instance.server
  id = "fv4g468rskpfcem690fs"
}
