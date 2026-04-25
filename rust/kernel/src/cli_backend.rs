//! Generic CLI Backend — pure Rust ObjectStore via subprocess execution.
//!
//! Wraps CLI tools (e.g., `gh`, `gws`) as ObjectStore implementations.
//! Auth is always via environment variables (never CLI args).
//!
//! Key schema:
//!   `read_content(_, "issues/42.json", _)`  → `{cli} {service} get issues/42.json`
//!   `write_content(yaml, _, _)`             → `{cli} {service} create` with yaml on stdin
//!   `delete_file("issues/42")`              → `{cli} {service} delete issues/42`
//!   `list_dir("issues/")`                   → parse from read_content("") or CLI-specific list
//!
//! `add_mount(backend_type="cli")`

use crate::backend::{ObjectStore, StorageError, WriteResult};
use crate::kernel::OperationContext;
use std::collections::HashMap;
use std::io;
use std::process::Command;

/// Generic CLI backend — subprocess executor for CLI-based connectors.
pub(crate) struct CLIBackend {
    backend_name: String,
    /// CLI binary name (e.g., "gh", "gws").
    cli_command: String,
    /// Optional service sub-command (e.g., "sheets", "gmail", "docs").
    cli_service: String,
    /// Auth environment variables injected into subprocess.
    auth_env: HashMap<String, String>,
}

impl CLIBackend {
    pub(crate) fn new(
        name: &str,
        cli_command: &str,
        cli_service: &str,
        auth_env_json: &str,
    ) -> Result<Self, io::Error> {
        let auth_env: HashMap<String, String> = if auth_env_json.is_empty() {
            HashMap::new()
        } else {
            serde_json::from_str(auth_env_json)
                .map_err(|e| io::Error::other(format!("Invalid auth_env JSON: {e}")))?
        };
        Ok(Self {
            backend_name: name.to_string(),
            cli_command: cli_command.to_string(),
            cli_service: cli_service.to_string(),
            auth_env,
        })
    }

    /// Execute a CLI command and return (stdout, stderr).
    fn exec(&self, args: &[&str], stdin_data: Option<&[u8]>) -> Result<Vec<u8>, StorageError> {
        let mut cmd = Command::new(&self.cli_command);

        // Add service sub-command if present
        if !self.cli_service.is_empty() {
            cmd.arg(&self.cli_service);
        }

        // Add remaining args
        cmd.args(args);

        // Inject auth env vars
        for (k, v) in &self.auth_env {
            cmd.env(k, v);
        }

        // Set up pipes
        cmd.stdout(std::process::Stdio::piped());
        cmd.stderr(std::process::Stdio::piped());

        if stdin_data.is_some() {
            cmd.stdin(std::process::Stdio::piped());
        } else {
            cmd.stdin(std::process::Stdio::null());
        }

        let mut child = cmd.spawn().map_err(|e| {
            StorageError::IOError(io::Error::other(format!(
                "Failed to spawn {}: {e}",
                self.cli_command
            )))
        })?;

        // Write stdin if provided
        if let Some(data) = stdin_data {
            if let Some(ref mut stdin) = child.stdin {
                use std::io::Write;
                let _ = stdin.write_all(data);
                // Drop stdin to signal EOF
            }
            // Must drop stdin before wait to avoid deadlock
            drop(child.stdin.take());
        }

        let output = child.wait_with_output().map_err(|e| {
            StorageError::IOError(io::Error::other(format!(
                "{} wait failed: {e}",
                self.cli_command
            )))
        })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            let code = output.status.code().unwrap_or(-1);
            return Err(StorageError::IOError(io::Error::other(format!(
                "{} exited with code {code}: {stderr}",
                self.cli_command
            ))));
        }

        Ok(output.stdout)
    }
}

impl ObjectStore for CLIBackend {
    fn name(&self) -> &str {
        &self.backend_name
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        if offset != 0 {
            return Err(StorageError::NotSupported(
                "CLI backend does not support offset writes",
            ));
        }

        // Use content_id as the write path
        let args = if content_id.is_empty() {
            vec!["create"]
        } else {
            vec!["create", content_id]
        };

        let stdout = self.exec(&args, Some(content))?;

        // Try to parse content_id from stdout (CLI may return JSON with id field)
        let content_id = if let Ok(val) = serde_json::from_slice::<serde_json::Value>(&stdout) {
            val.get("id")
                .or_else(|| val.get("content_id"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string()
        } else {
            String::from_utf8_lossy(&stdout).trim().to_string()
        };

        Ok(WriteResult {
            content_id,
            version: String::new(),
            size: content.len() as u64,
        })
    }

    fn read_content(
        &self,
        _content_id: &str,
        backend_path: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        let path = backend_path.trim_matches('/');
        let args = if path.is_empty() {
            vec!["list"]
        } else {
            vec!["get", path]
        };
        self.exec(&args, None)
    }

    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        if content_id.is_empty() {
            return Err(StorageError::NotFound("empty content_id".into()));
        }
        self.exec(&["delete", content_id], None)?;
        Ok(())
    }

    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        self.delete_content(path)
    }

    fn mkdir(&self, _path: &str, _parents: bool, _exist_ok: bool) -> Result<(), StorageError> {
        // Virtual directories — no-op
        Ok(())
    }

    fn rmdir(&self, _path: &str, _recursive: bool) -> Result<(), StorageError> {
        // Virtual directories — no-op
        Ok(())
    }

    fn list_dir(&self, path: &str) -> Result<Vec<String>, StorageError> {
        let path = path.trim_matches('/');
        let args = if path.is_empty() {
            vec!["list"]
        } else {
            vec!["list", path]
        };
        let stdout = self.exec(&args, None)?;
        // Parse JSON array of strings, or fall back to line-separated output
        if let Ok(entries) = serde_json::from_slice::<Vec<String>>(&stdout) {
            return Ok(entries);
        }
        Ok(String::from_utf8_lossy(&stdout)
            .lines()
            .filter(|l| !l.is_empty())
            .map(|l| l.to_string())
            .collect())
    }
}
