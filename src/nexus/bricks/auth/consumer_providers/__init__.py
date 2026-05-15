"""Provider adapter registry. Updated by each adapter task."""

from __future__ import annotations

from nexus.bricks.auth.consumer_providers.base import ProviderAdapter


def default_adapters() -> dict[str, ProviderAdapter]:
    """Return the adapter registry used by CredentialConsumer.

    Adapters are imported lazily so missing optional deps (e.g. AWS payload
    parsing only needs stdlib ``json``, but future providers may need boto3)
    don't cascade-break unrelated code paths.
    """
    from nexus.bricks.auth.consumer_providers.aws import AwsProviderAdapter
    from nexus.bricks.auth.consumer_providers.github import GithubProviderAdapter

    return {
        AwsProviderAdapter.name: AwsProviderAdapter(),
        GithubProviderAdapter.name: GithubProviderAdapter(),
    }
