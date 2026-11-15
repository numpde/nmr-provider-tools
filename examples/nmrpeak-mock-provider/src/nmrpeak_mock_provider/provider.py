"""Set up the provider's scientific objects and SDK execution lifetime.

The application calls open_provider explicitly and supplies its model constructor.
Request discovery, language-model services and report delivery belong to that
application. This module creates no requests or scripted service responses.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import tomllib

from provider_core.embedded_host import EmbeddedHost
from provider_core.hosting import OperationPolicy, observe_cleanup
from provider_core.scientific import HostedContribution, ResourceRequirement

from .composition import adopt_offerings
from .science import Model, Settings


# Read the selected deployment configuration from the application's working directory.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = Path("config/deployments/localhost/provider.toml")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Provider:
    """The execution manager, per-request policy and available scientific objects.

    These fields have different roles. The offerings field is the unordered
    inventory; it does not assign fixed slots to particular scientific variants.
    """

    host: EmbeddedHost
    operation_policy: OperationPolicy
    offerings: frozenset[HostedContribution]


@asynccontextmanager
async def open_provider(*, create_model: Callable[[], Model]) -> AsyncIterator[Provider]:
    """Read deployment settings, assemble science and retire the SDK on exit.

    This is provider-written setup, not an SDK discovery hook. The application's
    request-handling code uses the yielded components and finishes its report
    receivers before leaving this context. Model loading starts only when science
    is requested. Setup failure, body failure and independent cleanup failure
    retain their original causes.
    """
    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        provider_config = tomllib.load(source)
    host = EmbeddedHost(**provider_config["execution"]["embedded_host"])
    try:
        operation_policy = OperationPolicy(**provider_config["execution"]["operation_policy"])
        requested_requirements = frozenset(
            ResourceRequirement(**resource_requirement)
            for resource_requirement in provider_config["execution"]["resource_requirement"]
        )
        offerings = adopt_offerings(
            host=host,
            requested_requirements=requested_requirements,
            hf_settings=Settings(**provider_config["science"]["hf"]["settings"]),
            chf_settings=Settings(**provider_config["science"]["chf"]["settings"]),
            create_model=create_model,
        )
        yield Provider(
            host=host,
            operation_policy=operation_policy,
            offerings=offerings,
        )
    except BaseException as original:
        try:
            await observe_cleanup(control=host.retire())
        except BaseException as cleanup:
            raise BaseExceptionGroup(
                "Provider work and retirement failed independently",
                [original, cleanup],
            ) from None
        raise
    else:
        await observe_cleanup(control=host.retire())
