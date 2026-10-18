//! bcrypt work stays off async workers; admission belongs to the blocking work.

use std::sync::Arc;
use tokio::sync::Semaphore;
use tonic::Status;

#[derive(Clone)]
pub(crate) struct Passwords(Arc<Semaphore>);

impl Passwords {
    pub fn new(concurrency: usize) -> Result<Self, Status> {
        if concurrency == 0 || concurrency > 32 {
            return Err(Status::invalid_argument(
                "Password worker limit must be 1..32",
            ));
        }
        Ok(Self(Arc::new(Semaphore::new(concurrency))))
    }

    pub async fn verify(&self, password: String, stored: String) -> Result<(), Status> {
        self.run(move || {
            let bytes = password.as_bytes();
            let valid = bcrypt::verify(&bytes[..bytes.len().min(72)], &stored)
                .map_err(|_| Status::unavailable("Account password data is unavailable"))?;
            if !valid {
                return Err(Status::unauthenticated(
                    "Invalid email/username or password",
                ));
            }
            Ok(())
        })
        .await
    }

    pub async fn hash(&self, password: String) -> Result<String, Status> {
        self.run(move || {
            let bytes = password.as_bytes();
            bcrypt::hash(&bytes[..bytes.len().min(72)], 12)
                .map_err(|_| Status::internal("Could not hash password"))
        })
        .await
    }

    async fn run<T: Send + 'static>(
        &self,
        work: impl FnOnce() -> Result<T, Status> + Send + 'static,
    ) -> Result<T, Status> {
        let permit = Arc::clone(&self.0)
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("Password workers are busy; retry later"))?;
        tokio::task::spawn_blocking(move || {
            let _permit = permit;
            work()
        })
        .await
        .map_err(|_| Status::internal("Password worker failed"))?
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn cancellation_keeps_capacity_with_the_blocking_work() {
        let passwords = Passwords::new(1).unwrap();
        let runner = passwords.clone();
        let (started, entered) = tokio::sync::oneshot::channel();
        let (release, waiting) = std::sync::mpsc::channel();
        let task = tokio::spawn(async move {
            runner
                .run(move || {
                    let _ = started.send(());
                    let _ = waiting.recv();
                    Ok(())
                })
                .await
        });
        entered.await.unwrap();
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        assert_eq!(
            passwords.run(|| Ok(())).await.unwrap_err().code(),
            tonic::Code::ResourceExhausted
        );
        release.send(()).unwrap();
        let permit = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            Arc::clone(&passwords.0).acquire_owned(),
        )
        .await
        .unwrap()
        .unwrap();
        drop(permit);
        passwords.run(|| Ok(())).await.unwrap();
    }
}
