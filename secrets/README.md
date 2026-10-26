# Private operator inputs

Keep private delivery definitions, signing credentials and trust files here, or
use equivalent operator-owned locations. Everything under this directory except
this README is ignored by Git. The public binding template is
[config/delivery.example.toml](../config/delivery.example.toml).

A delivery definition names the exact canonical absolute paths of its credential,
optional CA and resolver files. Deployment mounts those selected files read-only;
it does not copy an entire secrets directory. Keep the original files available
for container restarts. Stop delivery before changing its definition, then start
it with the intended inputs.

These files are operator inputs. Docker volumes own the queue and socket state;
the generated daemon configuration lives in container tmpfs. No managed host
deployment directory, generated secret snapshot or saved Compose recipe is
required.
