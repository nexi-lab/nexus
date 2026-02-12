pub mod pyo3_bindings;
pub mod recovery;
pub mod segment;
pub mod wal;

use pyo3::prelude::*;

use crate::pyo3_bindings::PyWAL;

/// Python module: _nexus_wal
#[pymodule]
fn _nexus_wal(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyWAL>()?;
    Ok(())
}
