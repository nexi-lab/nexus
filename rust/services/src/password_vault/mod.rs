//! `PasswordVaultService` — gRPC service for the password vault
//! (namespace="passwords"). Domain wrapper above `SecretsService` that
//! provides server-side TOTP, audit-tagged access, and the canonical
//! VaultEntry schema (title/username/password/url/notes/tags/...).
//!
//! Per #3923 integration doc, this is the Phase 1 Rust impl. Per the
//! `services` ⊥ `backends` ⊥ `transport` ⊥ `raft` invariant, this
//! module depends ONLY on `kernel` (via `kernel.sys_*` syscalls);
//! storage lives in redb tables reached through kernel syscalls, never
//! via direct `backends` imports.
//!
//! Status: skeleton. All RPCs return `Status::unimplemented` — real
//! impls land per-method in T34. Server-side TOTP is the security
//! invariant the rewrite preserves: the totp_secret never leaves the
//! server.
//!
//! Hosted by the `vault` profile (`rust/profiles/vault/`), NOT bundled
//! into `cluster` — keeps cluster pure-federation per its slim-binary
//! design goal.

pub mod proto {
    //! Generated tonic stubs from
    //! `proto/nexus/password_vault/v1/password_vault.proto`.
    tonic::include_proto!("nexus.password_vault.v1");
}

// Internal types: storage rows, error enum, plaintext entry shape.
// `pub(crate)` — not part of the service's public surface (which is
// the gRPC trait alone).
#[allow(dead_code)] // wired into RPCs in follow-up commits
mod types;

use tonic::{Request, Response, Status};

use proto::password_vault_service_server::PasswordVaultService;
use proto::{
    DeleteEntryRequest, DeleteEntryResponse, GenerateTotpRequest, GenerateTotpResponse,
    GetEntryRequest, GetEntryResponse, ListEntriesRequest, ListEntriesResponse,
    ListVersionsRequest, ListVersionsResponse, PutEntryRequest, PutEntryResponse,
    RestoreEntryRequest, RestoreEntryResponse,
};

/// Skeleton impl of `PasswordVaultService`. All RPCs return
/// `Status::unimplemented`; concrete impls land per-method in T34.
#[derive(Default, Clone)]
pub struct PasswordVaultServiceImpl;

#[tonic::async_trait]
impl PasswordVaultService for PasswordVaultServiceImpl {
    async fn put_entry(
        &self,
        _req: Request<PutEntryRequest>,
    ) -> Result<Response<PutEntryResponse>, Status> {
        Err(Status::unimplemented("PutEntry — lands in T34"))
    }

    async fn get_entry(
        &self,
        _req: Request<GetEntryRequest>,
    ) -> Result<Response<GetEntryResponse>, Status> {
        Err(Status::unimplemented("GetEntry — lands in T34"))
    }

    async fn list_entries(
        &self,
        _req: Request<ListEntriesRequest>,
    ) -> Result<Response<ListEntriesResponse>, Status> {
        Err(Status::unimplemented("ListEntries — lands in T34"))
    }

    async fn delete_entry(
        &self,
        _req: Request<DeleteEntryRequest>,
    ) -> Result<Response<DeleteEntryResponse>, Status> {
        Err(Status::unimplemented("DeleteEntry — lands in T34"))
    }

    async fn restore_entry(
        &self,
        _req: Request<RestoreEntryRequest>,
    ) -> Result<Response<RestoreEntryResponse>, Status> {
        Err(Status::unimplemented("RestoreEntry — lands in T34"))
    }

    async fn list_versions(
        &self,
        _req: Request<ListVersionsRequest>,
    ) -> Result<Response<ListVersionsResponse>, Status> {
        Err(Status::unimplemented("ListVersions — lands in T34"))
    }

    async fn generate_totp(
        &self,
        _req: Request<GenerateTotpRequest>,
    ) -> Result<Response<GenerateTotpResponse>, Status> {
        Err(Status::unimplemented("GenerateTotp — lands in T34"))
    }
}
