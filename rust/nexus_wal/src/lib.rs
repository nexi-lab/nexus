#[cfg(feature = "python")]
pub mod pyo3_bindings;
pub mod recovery;
pub mod segment;
pub mod wal;

#[cfg(feature = "python")]
use pyo3::prelude::*;

/// Python module: _nexus_wal
#[cfg(feature = "python")]
#[pymodule]
fn _nexus_wal(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<pyo3_bindings::PyWAL>()?;
    Ok(())
}
