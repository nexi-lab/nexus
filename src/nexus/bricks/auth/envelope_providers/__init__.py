"""Concrete EncryptionProvider implementations (issue #3803).

- in_memory.InMemoryEncryptionProvider: test fake + default for development.
- vault_transit.VaultTransitProvider: Vault Transit (``derived=true``).
- aws_kms.AwsKmsProvider: AWS KMS per-tenant CMK.
"""
