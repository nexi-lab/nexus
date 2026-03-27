# Backends

nexus-fs supports multiple storage backends. Each backend is accessed
via a URI scheme and mounts at an auto-derived path in the virtual
filesystem.

| Backend | URI scheme | Extra | Mount path |
|---------|-----------|-------|------------|
| [Local Filesystem](local.md) | `local://` | _(included)_ | `/local/<path>/` |
| [Amazon S3](s3.md) | `s3://` | `nexus-fs[s3]` | `/s3/<bucket>/` |
| [Google Cloud Storage](gcs.md) | `gcs://` | `nexus-fs[gcs]` | `/gcs/<bucket>/` |
| [Google Drive](gdrive.md) | `gdrive://` | `nexus-fs[gdrive]` | `/gdrive/<id>/` |

All backends expose the same API — `read`, `write`, `ls`, `stat`,
`delete`, `rename`, `copy`, `mkdir`. Code that works against local
storage works against S3 or GCS without changes.
