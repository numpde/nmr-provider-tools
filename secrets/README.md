# Private operator inputs

Keep provider-owned signing credentials and trust files here, or use equivalent
operator-owned locations. Everything under this
directory except this README is ignored by Git. The public settings template is
[config/delivery.example.toml](../config/delivery.example.toml).

Keep the shared settings under `config/deployments/<name>/settings.toml`; they
are ignored by Git and do not contain provider bindings or signing credentials.
Each provider supplies its own credential and optional CA with each outcome;
the outbox retains those exact publication inputs with the accepted work. The
proxy and outbox reload their supported settings while running. Keep the settings
directory and file readable by the shared group so the distinct proxy UID can
read atomic replacements.

These files are operator inputs. Docker volumes own the queue and socket state;
the independent proxy and outbox borrow their own required grants. Do not place
private credentials or trust material in public repository files.
